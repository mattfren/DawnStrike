"""Truth-gated exporter for the browser-facing static Dawnstrike dashboard.

The deployed dashboard cannot read the local PaperOps archive or AlphaOps
SQLite database.  This module is the single publication boundary between those
private operational stores and ``assets/dashboard-data.json``.  It exports only
an explicit public allow-list and never converts missing outcomes into returns.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.dashboard.data_loader import load_calendar_day_detail
from intraday_scanner.dashboard.paper_ops_calendar_service import (
    build_paper_ops_calendar_view,
    load_paper_ops_calendar,
)
from intraday_scanner.dashboard.static_dashboard import (
    SOURCE_SCHEMA,
    build_dashboard_payload,
)
from intraday_scanner.market_calendar import next_market_day

OUTPUT_SCHEMA = "dawnstrike.static-dashboard.v3"
MARKET_TIMEZONE = ZoneInfo("America/Chicago")
FRESHNESS_DEADLINE_LOCAL_TIME = time(hour=17)


class StaticDashboardExportError(RuntimeError):
    """Raised when canonical evidence cannot support a truthful publication."""


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _finite_decimal(value: object, *, field: str, optional: bool = False) -> Decimal | None:
    if value is None or value == "":
        if optional:
            return None
        raise StaticDashboardExportError(f"Canonical {field} is missing.")
    if isinstance(value, bool):
        raise StaticDashboardExportError(f"Canonical {field} is not numeric.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StaticDashboardExportError(f"Canonical {field} is not numeric.") from exc
    if not number.is_finite():
        raise StaticDashboardExportError(f"Canonical {field} is not finite.")
    return number


def _integer(value: object, *, field: str) -> int:
    number = _finite_decimal(value, field=field)
    assert number is not None
    if number != number.to_integral_value():
        raise StaticDashboardExportError(f"Canonical {field} is not an integer.")
    return int(number)


def _canonical_json(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise StaticDashboardExportError(
            "Public dashboard payload contains a non-JSON-safe value."
        ) from exc
    return rendered.encode("utf-8")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise StaticDashboardExportError(f"Evidence file is unreadable: {path.name}") from exc
    return digest.hexdigest()


def _public_outcome_note(audit_status: str) -> str:
    """Return controlled public copy without exposing operator-authored notes."""

    normalized = audit_status.strip().lower()
    if normalized == "resolved_no_entry":
        return "Saved trigger did not activate; trade return is not applicable."
    if normalized == "audited":
        return "Audited sourced outcome."
    if normalized == "partial":
        return "Partial outcome; unavailable values remain uncounted."
    return "Outcome is incomplete; unavailable returns remain uncounted."


def _iso_datetime(value: datetime | None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise StaticDashboardExportError("generated_at must include a timezone.")
    return resolved.astimezone(timezone.utc)


def _freshness(run_date: date, generated_at: datetime, *, is_latest: bool) -> dict[str, Any]:
    next_session = next_market_day(run_date + timedelta(days=1))
    deadline_local = datetime.combine(
        next_session,
        FRESHNESS_DEADLINE_LOCAL_TIME,
        tzinfo=MARKET_TIMEZONE,
    )
    deadline = deadline_local.astimezone(timezone.utc)
    return {
        "asOfDate": run_date.isoformat(),
        "expectedNextSessionDate": next_session.isoformat(),
        "deadlineAt": deadline.isoformat().replace("+00:00", "Z"),
        "statusAtGeneration": (
            "fresh" if is_latest and generated_at <= deadline else "stale"
        ),
    }


def _validate_paper_view(
    dataset: Mapping[str, Any], view: Mapping[str, Any], requested_date: str
) -> None:
    if dataset.get("status") != "verified":
        raise StaticDashboardExportError("PaperOps dataset is not verified.")
    if view.get("status") != "verified" or view.get("truth_status") != "verified":
        raise StaticDashboardExportError("PaperOps forward view is not truth-verified.")
    if view.get("mode") != "forward" or view.get("claim_scope") != "official_forward":
        raise StaticDashboardExportError("PaperOps export must be official forward evidence.")
    if (
        dataset.get("research_only") is not True
        or dataset.get("broker_execution_allowed") is not False
    ):
        raise StaticDashboardExportError("PaperOps research-only boundary is not asserted.")
    if view.get("research_only") is not True or view.get("broker_execution_allowed") is not False:
        raise StaticDashboardExportError("PaperOps view exposes an invalid execution boundary.")
    if requested_date not in [str(item) for item in view.get("dates") or []]:
        raise StaticDashboardExportError(
            f"PaperOps has no retained forward session for {requested_date}."
        )
    if _rows(view.get("unknown_rows")) or _rows(view.get("impossible_forward_rows")):
        raise StaticDashboardExportError("PaperOps includes unpublishable strategy identities.")
    if view.get("blotter_verified") is not True:
        raise StaticDashboardExportError("PaperOps trade blotter is not verified.")

    gates = _mapping(dataset.get("gates"))
    for name in ("reconciliation", "calendar_truth", "ledger_rebuild", "trade_blotter"):
        if _mapping(gates.get(name)).get("status") != "passed":
            raise StaticDashboardExportError(f"PaperOps {name} gate is not passed.")
    source_gate = _mapping(gates.get("source_bar_truth_forward"))
    if source_gate.get("status") != "passed" or source_gate.get("mode") != "forward":
        raise StaticDashboardExportError("PaperOps forward source-bar truth gate is not passed.")

    summaries = {
        str(row.get("date")): row for row in _rows(view.get("day_summaries"))
    }
    requested = summaries.get(requested_date)
    if requested is None:
        raise StaticDashboardExportError("PaperOps requested day summary is missing.")
    if requested.get("coverage_complete") is not True:
        raise StaticDashboardExportError("PaperOps requested day coverage is incomplete.")
    if requested.get("coverage_status") != "complete":
        raise StaticDashboardExportError("PaperOps requested day coverage gate is not complete.")
    if _integer(requested.get("missing_strategies"), field="missing_strategies") != 0:
        raise StaticDashboardExportError("PaperOps requested day has missing strategy rows.")


def _public_strategy_registry(dataset: Mapping[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _rows(dataset.get("official_series")):
        strategy_id = str(row.get("strategy_id") or "").strip()
        if not strategy_id or strategy_id in seen:
            raise StaticDashboardExportError("PaperOps strategy registry identities are invalid.")
        seen.add(strategy_id)
        output.append(
            {
                "id": strategy_id,
                "name": str(row.get("strategy_label") or strategy_id),
                "version": str(row.get("strategy_version") or ""),
                "executionPolicyVersion": str(row.get("execution_policy_version") or ""),
                "activationDate": str(row.get("registry_inception_date") or ""),
                "fingerprint": str(row.get("strategy_semantics_fingerprint") or ""),
            }
        )
    if not output:
        raise StaticDashboardExportError("PaperOps official strategy registry is empty.")
    return sorted(output, key=lambda row: (str(row["activationDate"]), str(row["id"])))


def _public_paper_days(
    view: Mapping[str, Any], registry: list[dict[str, Any]], through_date: str
) -> list[dict[str, Any]]:
    official_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _rows(view.get("official_rows")):
        row_date = str(row.get("date") or "")
        if row_date <= through_date:
            official_by_date[row_date].append(row)
    summaries = [
        row
        for row in _rows(view.get("day_summaries"))
        if str(row.get("date") or "") <= through_date
    ]
    output: list[dict[str, Any]] = []
    for summary in summaries:
        day = str(summary.get("date") or "")
        rows = official_by_date.get(day, [])
        if summary.get("coverage_complete") is not True:
            raise StaticDashboardExportError(f"PaperOps {day} coverage is incomplete.")
        expected = _integer(summary.get("coverage_expected"), field=f"{day} coverage_expected")
        if len(rows) != expected:
            raise StaticDashboardExportError(f"PaperOps {day} row coverage disagrees with summary.")
        present_ids = [str(row.get("strategy_id") or "") for row in rows]
        if "" in present_ids or len(present_ids) != len(set(present_ids)):
            raise StaticDashboardExportError(f"PaperOps {day} strategy identities are invalid.")

        strategy_rows: list[dict[str, Any]] = []
        starting_equity = Decimal(0)
        row_pnl = Decimal(0)
        for row in rows:
            strategy_id = str(row["strategy_id"])
            starting = _finite_decimal(
                row.get("session_open_equity"), field=f"{day}/{strategy_id} starting equity"
            )
            total_pnl = _finite_decimal(
                row.get("total_pnl"), field=f"{day}/{strategy_id} total P&L"
            )
            assert starting is not None and total_pnl is not None
            starting_equity += starting
            row_pnl += total_pnl
            closed = _integer(row.get("trades_closed"), field="trades_closed")
            wins = _integer(row.get("wins"), field="wins")
            strategy_rows.append(
                {
                    "id": strategy_id,
                    "dailyReturnFraction": str(
                        _finite_decimal(
                            row.get("daily_return_pct"),
                            field=f"{day}/{strategy_id} daily return",
                        )
                    ),
                    "cumulativeReturnFraction": str(
                        _finite_decimal(
                            row.get("cumulative_return_pct"),
                            field=f"{day}/{strategy_id} cumulative return",
                        )
                    ),
                    "drawdownFraction": str(
                        _finite_decimal(
                            row.get("drawdown_pct"),
                            field=f"{day}/{strategy_id} drawdown",
                        )
                    ),
                    "realizedPnl": str(
                        _finite_decimal(row.get("realized_pnl"), field="realized_pnl")
                    ),
                    "unrealizedPnl": str(
                        _finite_decimal(row.get("unrealized_pnl"), field="unrealized_pnl")
                    ),
                    "totalPnl": str(total_pnl),
                    "tradesOpened": _integer(row.get("trades_opened"), field="trades_opened"),
                    "tradesClosed": closed,
                    "openPositions": _integer(row.get("open_positions"), field="open_positions"),
                    "pendingOrders": _integer(row.get("pending_orders"), field="pending_orders"),
                    "winRate": None if closed == 0 else wins / closed,
                    "runId": str(row.get("run_id") or ""),
                    "dataSnapshotId": str(row.get("data_snapshot_id") or ""),
                }
            )

        fleet_pnl = _finite_decimal(summary.get("fleet_daily_pnl"), field=f"{day} fleet P&L")
        fleet_return = _finite_decimal(
            summary.get("fleet_daily_return"), field=f"{day} fleet daily return"
        )
        assert fleet_pnl is not None and fleet_return is not None
        if fleet_pnl != row_pnl:
            raise StaticDashboardExportError(f"PaperOps {day} fleet P&L disagrees with rows.")
        if starting_equity <= 0 or fleet_return != fleet_pnl / starting_equity:
            raise StaticDashboardExportError(f"PaperOps {day} fleet return math is invalid.")
        output.append(
            {
                "date": day,
                "fleetDailyReturnFraction": str(fleet_return),
                "fleetCumulativeReturnFraction": str(
                    _finite_decimal(
                        summary.get("fleet_cumulative_return"),
                        field=f"{day} cumulative return",
                    )
                ),
                "fleetDailyPnl": str(fleet_pnl),
                "fleetStartingEquity": str(starting_equity),
                "fleetEndingEquity": str(
                    _finite_decimal(
                        summary.get("fleet_ending_equity"), field=f"{day} ending equity"
                    )
                ),
                "coveragePresent": len(rows),
                "coverageExpected": expected,
                "notYetEligible": _integer(
                    summary.get("not_yet_registered_strategies"),
                    field="not_yet_registered_strategies",
                ),
                "tradesOpened": _integer(summary.get("trades_opened"), field="trades_opened"),
                "tradesClosed": _integer(summary.get("trades_closed"), field="trades_closed"),
                "openPositions": _integer(summary.get("open_positions"), field="open_positions"),
                "pendingOrders": _integer(summary.get("pending_orders"), field="pending_orders"),
                "status": str(summary.get("status") or ""),
                "strategies": sorted(strategy_rows, key=lambda row: str(row["id"])),
            }
        )
    if not output or str(output[-1]["date"]) != through_date:
        raise StaticDashboardExportError("PaperOps selected day is absent from public history.")
    eligible = {
        str(row["id"])
        for row in registry
        if str(row.get("activationDate") or "9999-12-31") <= through_date
    }
    latest_ids = {str(row["id"]) for row in output[-1]["strategies"]}
    if eligible != latest_ids:
        raise StaticDashboardExportError("PaperOps latest rows disagree with registry activation.")
    return output


def _public_position_rows(
    root: Path, registry: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    names = {str(row["id"]): str(row["name"]) for row in registry}
    open_rows = _load_json_rows(root / "state" / "open_positions.json", required=True)
    pending_rows = _load_json_rows(root / "state" / "pending_orders.json", required=True)
    public_open = [
        {
            "fillDate": str(row.get("opened_at") or "")[:10] or None,
            "symbol": str(row.get("symbol") or ""),
            "strategyName": names.get(
                str(row.get("strategy_id") or ""), str(row.get("strategy_id") or "")
            ),
            "direction": str(row.get("direction") or ""),
            "fillPrice": row.get("entry_price"),
            "stop": row.get("stop"),
            "target": row.get("target"),
            "unrealizedPnl": row.get("unrealized_pnl"),
        }
        for row in open_rows
        if row.get("mode", "forward") == "forward"
    ]
    public_pending = [
        {
            "signalDate": str(row.get("trade_date") or row.get("signal_time") or "")[:10] or None,
            "symbol": str(row.get("symbol") or ""),
            "strategyName": names.get(
                str(row.get("strategy_id") or ""), str(row.get("strategy_id") or "")
            ),
            "direction": str(row.get("direction") or ""),
            "entryReference": row.get("entry"),
            "stop": row.get("stop"),
            "target": row.get("target"),
        }
        for row in pending_rows
        if row.get("mode", "forward") == "forward"
    ]
    return public_open, public_pending


def _load_json_rows(path: Path, *, required: bool) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise StaticDashboardExportError(
                f"Required PaperOps state is unreadable: {path.name}"
            ) from exc
        return []
    if not isinstance(payload, list) or not all(isinstance(row, Mapping) for row in payload):
        raise StaticDashboardExportError(f"Required PaperOps state is invalid: {path.name}")
    return [dict(row) for row in payload]


def _public_alpha_day(detail: Mapping[str, Any], expected_date: str) -> dict[str, Any]:
    overview = _mapping(detail.get("overview"))
    if (
        str(detail.get("date") or "") != expected_date
        or str(overview.get("date") or "") != expected_date
    ):
        raise StaticDashboardExportError(f"AlphaOps day {expected_date} is not retained.")
    picks = _rows(detail.get("picks"))
    returns = {
        (int(row.get("rank") or 0), str(row.get("ticker") or "")): row
        for row in _rows(detail.get("return_rows"))
    }
    public_picks: list[dict[str, Any]] = []
    audited = 0
    no_entry = 0
    missing = 0
    for pick in picks:
        key = (int(pick.get("rank") or 0), str(pick.get("ticker") or ""))
        outcome = returns.get(key, {})
        audit_status = str(outcome.get("audit_status") or "Outcome needed")
        if audit_status == "audited":
            audited += 1
        elif audit_status == "resolved_no_entry":
            no_entry += 1
        else:
            missing += 1
        public_picks.append(
            {
                "rank": key[0],
                "ticker": key[1],
                "company": pick.get("company"),
                "action": pick.get("label/action"),
                "score": pick.get("total_score"),
                "gapPct": pick.get("gap_pct"),
                "trigger": pick.get("trigger"),
                "invalidation": pick.get("invalidation"),
                "target": pick.get("target"),
                "source": pick.get("source"),
                "outcome": {
                    "status": audit_status,
                    "entryPrice": outcome.get("entry_price"),
                    "entryTime": outcome.get("entry_time") or None,
                    "recommendedExitPolicy": outcome.get("recommended_exit_policy"),
                    "recommendedExitPrice": outcome.get("recommended_exit_price"),
                    "recommendedExitReturnPct": outcome.get("recommended_exit_return"),
                    "closeReturnPct": outcome.get("close_return"),
                    "monitorExitReturnPct": outcome.get("monitor_exit_return"),
                    "highAfterEntryReturnPct": outcome.get("high_after_entry_return"),
                    "lowAfterEntryDrawdownPct": outcome.get("low_after_entry_drawdown"),
                    "source": outcome.get("outcome_source"),
                    "notes": _public_outcome_note(audit_status),
                },
            }
        )
    if len(returns) != len(picks):
        raise StaticDashboardExportError(
            f"AlphaOps {expected_date} pick/outcome identity mismatch."
        )
    return {
        "date": expected_date,
        "status": str(detail.get("status") or "UNAVAILABLE"),
        "decision": overview.get("alphaops_decision"),
        "modelVersion": overview.get("model_version"),
        "sourceStatus": overview.get("source_status"),
        "sourceLabel": overview.get("source_label"),
        "pickCount": len(public_picks),
        "outcomeCoverage": {
            "eligible": len(public_picks),
            "audited": audited,
            "resolvedNoEntry": no_entry,
            "missing": missing,
            "complete": missing == 0,
        },
        "picks": sorted(public_picks, key=lambda row: int(row["rank"])),
        "missingOutcomes": [
            {
                "ticker": row.get("ticker"),
                "rank": row.get("rank"),
                "status": row.get("audit_status"),
            }
            for row in _rows(detail.get("missing_outcomes"))
        ],
    }


def _legacy_alpha_source(alpha_day: Mapping[str, Any], detail: Mapping[str, Any]) -> dict[str, Any]:
    picks = _rows(alpha_day.get("picks"))
    telegram = _rows(detail.get("telegram"))
    watch_count = sum("WATCH" in str(row.get("action") or "").upper() for row in picks)
    blocked_count = sum("BLOCK" in str(row.get("action") or "").upper() for row in picks)
    return {
        # This means the scan itself completed. Outcome completeness is retained
        # separately in alphaOutcomeCalendar and is never inferred here.
        "status": "complete",
        "marketDate": alpha_day.get("date"),
        "observedAt": _mapping(detail.get("overview")).get("date"),
        "scanId": None,
        "modelVersion": alpha_day.get("modelVersion"),
        "candidateCount": len(picks),
        "manualConfirmationCount": watch_count,
        "blockedCount": blocked_count,
        "cleanAcceptedCount": max(0, len(picks) - watch_count - blocked_count),
        "notification": {
            "channel": "telegram",
            "status": "delivery recorded" if telegram else "no delivery recorded",
            "sent": len(telegram),
            "dryRun": None,
        },
        "topReasons": [
            "Watch-only research; Alpha outcomes are reported separately from PaperOps.",
            "Missing outcomes remain ineligible and are never treated as zero.",
        ],
        "candidates": [
            {
                "rank": row.get("rank"),
                "ticker": row.get("ticker"),
                "company": row.get("company"),
                "score": row.get("score"),
                "gate": row.get("action"),
                "gapPct": row.get("gapPct"),
                "entryTrigger": row.get("trigger"),
                "rewardRisk": None,
                "source": row.get("source"),
                "sourceCount": None,
                "sourceConfidence": None,
            }
            for row in picks
        ],
    }


def _legacy_paper_source(
    root: Path,
    dataset: Mapping[str, Any],
    view: Mapping[str, Any],
    registry: list[dict[str, Any]],
    days: list[dict[str, Any]],
) -> dict[str, Any]:
    open_rows, pending_rows = _public_position_rows(root, registry)
    blotter_rows = _rows(view.get("blotter_rows"))
    no_setup = sum(row.get("lifecycle_status") == "no_setup" for row in blotter_rows)
    lifecycle = len(blotter_rows) - no_setup
    future_dates = sorted(
        {
            str(row["activationDate"])
            for row in registry
            if str(row["activationDate"]) > str(days[-1]["date"])
        }
    )
    return {
        "status": "verified",
        "mode": "forward",
        "nextActivationDate": future_dates[0] if future_dates else None,
        "rawBlotterRows": len(blotter_rows),
        "noSetupRows": no_setup,
        "signalLinkedLifecycleRows": lifecycle,
        "strategies": registry,
        "days": days,
        "openPositions": open_rows,
        "pendingOrders": pending_rows,
        "officialStrategyCount": dataset.get("official_strategy_count"),
    }


def build_static_dashboard_payload(
    *,
    paper_ops_root: str | Path,
    database_path: str | Path,
    run_date: str | date | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build one public dashboard payload from canonical local evidence."""

    root = Path(paper_ops_root).resolve()
    database = Path(database_path).resolve()
    if not root.is_dir():
        raise StaticDashboardExportError("PaperOps root does not exist.")
    if not database.is_file():
        raise StaticDashboardExportError("AlphaOps database does not exist.")
    generated = _iso_datetime(generated_at)
    dataset = load_paper_ops_calendar(root)
    view = build_paper_ops_calendar_view(dataset, "forward")
    latest_source_date = str(view.get("latest_date") or "")
    selected = (
        run_date.isoformat() if isinstance(run_date, date) else str(run_date or latest_source_date)
    )
    try:
        selected_day = date.fromisoformat(selected)
    except ValueError as exc:
        raise StaticDashboardExportError("run_date must be an ISO date.") from exc
    _validate_paper_view(dataset, view, selected)

    registry = _public_strategy_registry(dataset)
    paper_days = _public_paper_days(view, registry, selected)
    observed_dates = [str(row["date"]) for row in paper_days]
    alpha_details: list[dict[str, Any]] = []
    alpha_days: list[dict[str, Any]] = []
    for day in observed_dates:
        detail = load_calendar_day_detail(database, day)
        alpha_details.append(detail)
        alpha_days.append(_public_alpha_day(detail, day))
    latest_alpha = alpha_days[-1]
    latest_detail = alpha_details[-1]
    public_alpha_hash = _sha256_json(alpha_days)
    registry_hash = _sha256_json(registry)
    paper_run_ids = sorted(
        {
            str(row["runId"])
            for day in paper_days
            for row in _rows(day.get("strategies"))
            if row.get("runId")
        }
    )
    snapshot_ids = sorted(
        {
            str(row["dataSnapshotId"])
            for day in paper_days
            for row in _rows(day.get("strategies"))
            if row.get("dataSnapshotId")
        }
    )
    latest_run_ids = sorted(
        {
            str(row["runId"])
            for row in _rows(paper_days[-1].get("strategies"))
            if row.get("runId")
        }
    )
    latest_snapshots = sorted(
        {
            str(row["dataSnapshotId"])
            for row in _rows(paper_days[-1].get("strategies"))
            if row.get("dataSnapshotId")
        }
    )
    if len(latest_run_ids) != 1 or len(latest_snapshots) != 1:
        raise StaticDashboardExportError("PaperOps latest day has conflicting run lineage.")
    gates = _mapping(dataset.get("gates"))
    evidence = {
        "calendarTruthStatus": _mapping(gates.get("calendar_truth")).get("status"),
        "sourceBarTruthStatus": _mapping(gates.get("source_bar_truth_forward")).get("status"),
        "reconciliationStatus": _mapping(gates.get("reconciliation")).get("status"),
        "tradeBlotterStatus": _mapping(gates.get("trade_blotter")).get("status"),
        "paperOpsCalendarSha256": str(dataset.get("source_sha256") or ""),
        "paperOpsRegistrySha256": registry_hash,
        "alphaDatabaseSha256": _sha256_file(database),
        "alphaPublicDaysSha256": public_alpha_hash,
        "paperOpsRunIds": paper_run_ids,
        "paperOpsRunId": latest_run_ids[0],
        "dataSnapshotIds": snapshot_ids,
        "dataSnapshotId": latest_snapshots[0],
    }
    source = {
        "schemaVersion": SOURCE_SCHEMA,
        "generatedAt": generated.isoformat().replace("+00:00", "Z"),
        "latestRunDate": selected,
        "sourceCommit": "canonical local evidence export",
        "sourceEvidence": {
            **evidence,
            # Legacy validator names retained until the static builder's v3
            # contract is the sole reader.
            "calendarSha256": evidence["paperOpsCalendarSha256"],
            "registrySha256": evidence["paperOpsRegistrySha256"],
            "fleetReportStatus": "complete",
        },
        "alphaOps": _legacy_alpha_source(latest_alpha, latest_detail),
        "paperOps": _legacy_paper_source(root, dataset, view, registry, paper_days),
        "system": {
            "scheduler": {
                "status": "not assessed by static exporter",
                "runDate": selected,
                "exitCode": None,
                "completedAt": None,
            }
        },
    }
    try:
        payload = build_dashboard_payload(source)
    except (KeyError, TypeError, ValueError) as exc:
        raise StaticDashboardExportError(
            "Static dashboard builder rejected canonical evidence."
        ) from exc
    freshness = _freshness(selected_day, generated, is_latest=selected == latest_source_date)
    payload.update(
        {
            "schemaVersion": OUTPUT_SCHEMA,
            "generatedAt": source["generatedAt"],
            "latestRunDate": selected,
            "freshness": freshness,
            "freshnessDeadline": freshness["deadlineAt"],
            "sourceObservedDates": observed_dates,
            "evidence": evidence,
            "paperOps": {
                "status": "verified",
                "mode": "forward",
                "claimScope": "official_forward",
                "researchOnly": True,
                "brokerExecutionAllowed": False,
                "strategies": registry,
                "days": paper_days,
            },
            "alphaOps": {
                "scope": "watchlist outcome audit",
                "researchOnly": True,
                "days": alpha_days,
            },
        }
    )
    payload["evidence"]["publicPayloadSha256"] = _sha256_json(payload)
    return payload


def export_static_dashboard(
    *,
    paper_ops_root: str | Path,
    database_path: str | Path,
    output_path: str | Path,
    run_date: str | date | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Truth-check, render, and atomically replace the static dashboard asset."""

    payload = build_static_dashboard_payload(
        paper_ops_root=paper_ops_root,
        database_path=database_path,
        run_date=run_date,
        generated_at=generated_at,
    )
    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        rendered = (
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    except (TypeError, ValueError) as exc:
        raise StaticDashboardExportError(
            "Public dashboard payload contains a non-JSON-safe value."
        ) from exc
    temp_path: Path | None = None
    try:
        descriptor, raw_temp = tempfile.mkstemp(
            prefix=f"{destination.stem}-", suffix=".tmp", dir=destination.parent
        )
        temp_path = Path(raw_temp)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-ops-root", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True, dest="database_path")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--date", dest="run_date")
    parser.add_argument(
        "--generated-at",
        help="Optional timezone-aware ISO timestamp for deterministic release builds.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    generated = None
    if args.generated_at:
        try:
            generated = datetime.fromisoformat(args.generated_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SystemExit("--generated-at must be a timezone-aware ISO timestamp") from exc
    export_static_dashboard(
        paper_ops_root=args.paper_ops_root,
        database_path=args.database_path,
        output_path=args.output,
        run_date=args.run_date,
        generated_at=generated,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
