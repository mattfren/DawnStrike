"""Durable Telegram delivery for the verified PaperOps strategy-fleet digest.

The digest is a read-only, research/paper notification.  It consumes the
already-built strategy-fleet report plus canonical forward PaperOps artifacts;
it never runs strategies, changes positions, or treats a missing return as
zero.  A filesystem outbox is persisted before notification dispatch and the
existing SQLite notification receipt is the idempotency authority.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from intraday_scanner.config import load_config
from intraday_scanner.errors import NotificationError
from intraday_scanner.models import utc_now_iso
from intraday_scanner.notifiers import NotificationEvent, build_notifiers, dispatch_events
from intraday_scanner.notifiers.console import ConsoleNotifier
from intraday_scanner.paper_ops_root import production_paper_ops_root
from intraday_scanner.services.strategy_fleet_report_service import (
    ALPHAOPS_HORIZON,
    ALPHAOPS_SOURCE,
    CASH_BASELINE_ID,
    PAPEROPS_HORIZON,
    PAPEROPS_SOURCE,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from intraday_scanner.v2.paper_ops.calendar_truth import verify_calendar_truth
from intraday_scanner.v2.paper_ops.engine import (
    PaperOpsPaths,
    _config_from_payload,
    _execution_policy_fingerprint_payload,
    _recover_pending_transaction,
    _strategy_coverage_inception,
    _strategy_semantics_fingerprint,
    _strategy_semantics_payload,
)
from intraday_scanner.v2.paper_ops.models import PAPER_EXECUTION_POLICY_VERSION
from intraday_scanner.v2.paper_ops.source_bar_truth import verify_source_bar_truth
from intraday_scanner.v2.paper_ops.storage import (
    exclusive_file_lock,
    read_json,
    read_jsonl,
    write_json,
)
from intraday_scanner.v2.strategies import build_strategy_catalog

_CHANNELS = frozenset({"console", "telegram"})
_DIGEST_ROOT = ("notifications", "paperops_fleet_digest")
_TELEGRAM_SAFE_CHARS = 3900
_ALPHA_EMPTY_WARNING_PREFIX = "AlphaOps scorecard table yielded no rows"
_NON_FLEET_STRATEGY_STATUSES = frozenset(
    {"baseline", "benchmark", "parked", "quarantined", "rejected"}
)
_STRATEGY_LABELS = {
    "bullish_fvg_continuation": "Bullish FVG",
    "cross_sectional_relative_strength": "Relative strength",
    "donchian_breakout_20_10": "Donchian 20/10",
    "failed_breakout_reversal_short": "Failed breakout short",
    "gap_up_continuation": "Gap-up continuation",
    "gap_up_continuation_atr": "ATR gap-up continuation",
    "pullback_reclaim_uptrend": "Pullback reclaim",
    "ts_momentum_sma_atr": "SMA/ATR momentum",
    "volatility_contraction_breakout": "Volatility contraction",
}


def _eligible_catalog_strategy_ids() -> tuple[str, ...]:
    """Return the official PaperOps fleet identity from the combined catalog."""

    return tuple(
        sorted(
            str(strategy.strategy_id).strip()
            for strategy in build_strategy_catalog()
            if str(strategy.status).strip().lower() not in _NON_FLEET_STRATEGY_STATUSES
        )
    )


def build_paperops_fleet_digest(
    *,
    market_date: str,
    db_path: str | Path = "data/shadow_real.sqlite",
    paper_ops_root: str | Path | None = None,
    fleet_report_path: str | Path = "outputs/strategy_fleet/strategy_fleet_report.json",
    alpha_run_contract_path: str | Path = "outputs/alpha_cycle/alpha_run_contract.json",
) -> dict[str, Any]:
    """Build a fail-closed, forward-only digest payload without sending it."""

    day = _market_date(market_date)
    root = production_paper_ops_root(override=paper_ops_root)
    report_path = Path(fleet_report_path)
    alpha_contract_path = Path(alpha_run_contract_path)
    blockers: list[str] = []
    paths = PaperOpsPaths.create(root)
    pending_transaction_path = root / "state" / "paper_transaction_pending.json"
    try:
        _recover_pending_transaction(paths)
        calendar_truth = verify_calendar_truth(output_root=root)
        source_bar_truth = verify_source_bar_truth(
            output_root=root,
            mode="forward",
        )
    except Exception as exc:  # Fail closed at the standalone transaction/truth boundary.
        blockers.append(
            "PaperOps transaction recovery/calendar truth verification failed: "
            f"{_safe_error(exc)}"
        )
        return _blocked_payload(day, root, report_path, blockers)
    calendar_truth_evidence = calendar_truth.to_dict()
    source_bar_truth_evidence = source_bar_truth.to_dict()
    if pending_transaction_path.exists():
        blockers.append("PaperOps transaction journal still exists after recovery")
    if calendar_truth.status != "passed":
        for field in (
            "duplicate_rows",
            "missing_rows",
            "math_mismatches",
            "ledger_mismatches",
        ):
            raw_values = calendar_truth_evidence.get(field)
            values = [
                str(value)
                for value in (raw_values if isinstance(raw_values, list | tuple) else [])
                if str(value)
            ]
            if values:
                blockers.append(
                    f"PaperOps calendar truth {field}: " + " | ".join(values[:5])
                )
    if source_bar_truth.status != "passed":
        blockers.extend(
            f"PaperOps source-bar truth: {warning}"
            for warning in source_bar_truth.warnings[:10]
        )
    if blockers:
        return _blocked_payload(day, root, report_path, blockers)
    report = _load_mapping(report_path, "strategy fleet report", blockers)
    if not report:
        return _blocked_payload(day, root, report_path, blockers)

    sources = _mapping(report.get("sources"))
    paper_source = _mapping(sources.get(PAPEROPS_SOURCE))
    alpha_source = _mapping(sources.get(ALPHAOPS_SOURCE))
    if str(report.get("schema_version") or "") != "dawnstrike.strategy_fleet_report.v3":
        blockers.append("fleet report schema is not dawnstrike.strategy_fleet_report.v3")
    if str(paper_source.get("status") or "") != "complete":
        blockers.append(
            f"PaperOps fleet evidence is {paper_source.get('status') or 'missing'}"
        )

    expected_calendar = root / "calendar" / "strategy_daily_returns.csv"
    source_calendar = str(paper_source.get("path") or "").strip()
    if not source_calendar:
        blockers.append("PaperOps source calendar path is missing")
    elif not _same_path(Path(source_calendar), expected_calendar):
        blockers.append("fleet report does not reference the configured live PaperOps root")

    reported_ids = tuple(
        sorted(str(value) for value in list(paper_source.get("expected_strategy_ids") or []))
    )
    if len(reported_ids) != len(set(reported_ids)):
        blockers.append("fleet report expected strategy identities contain duplicates")
    catalog_ids = _eligible_catalog_strategy_ids()
    if any(not strategy_id for strategy_id in catalog_ids):
        blockers.append("current strategy catalog contains a blank eligible strategy identity")
    catalog_duplicates = sorted(
        strategy_id
        for strategy_id, count in Counter(catalog_ids).items()
        if count > 1
    )
    if catalog_duplicates:
        blockers.append(
            "current strategy catalog duplicates eligible PaperOps strategies: "
            + ", ".join(catalog_duplicates)
        )
    expected_ids = tuple(sorted(strategy_id for strategy_id in set(catalog_ids) if strategy_id))
    if not expected_ids:
        blockers.append("current strategy catalog has no eligible PaperOps strategies")
    if set(reported_ids) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(reported_ids))
        extra = sorted(set(reported_ids) - set(expected_ids))
        if missing:
            blockers.append(
                "fleet report is missing current catalog strategies: " + ", ".join(missing)
            )
        if extra:
            blockers.append(
                "fleet report has strategies outside the current catalog: " + ", ".join(extra)
            )

    daily_rows = [
        _mapping(row) for row in list(report.get("daily_rows") or []) if isinstance(row, dict)
    ]
    paper_rows = [
        row
        for row in daily_rows
        if str(row.get("date") or "")[:10] == day
        and str(row.get("horizon") or "") == PAPEROPS_HORIZON
    ]
    alpha_rows = [
        row
        for row in daily_rows
        if str(row.get("date") or "")[:10] == day
        and str(row.get("horizon") or "") == ALPHAOPS_HORIZON
        and str(row.get("cohort") or "") == "official_telegram"
    ]
    paper_by_id = _unique_by_strategy(paper_rows, blockers, "fleet report")
    config_path = root / "state" / "paper_ops_config.json"
    paper_config = _load_mapping(config_path, "PaperOps configuration", blockers)
    universe = {
        str(value).strip().upper()
        for value in list(paper_config.get("universe_symbols") or [])
        if str(value).strip()
    }
    if not universe:
        blockers.append("PaperOps configured universe evidence is missing")
    live_lineage = _resolve_live_paperops_lineage(
        root=root,
        market_date=day,
        expected_ids=expected_ids,
        paper_by_id=paper_by_id,
        paper_config=paper_config,
        blockers=blockers,
    )
    activation_by_id = {
        strategy_id: _mapping(value)
        for strategy_id, value in _mapping(
            live_lineage.get("strategy_activation_by_id")
        ).items()
    }
    eligible_ids = tuple(
        sorted(
            strategy_id
            for strategy_id, activation in activation_by_id.items()
            if str(activation.get("status") or "") == "eligible"
        )
    )
    pending_activation_by_id = {
        strategy_id: activation
        for strategy_id, activation in activation_by_id.items()
        if str(activation.get("status") or "") == "registered_not_yet_eligible"
    }
    if set(activation_by_id) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(activation_by_id))
        if missing:
            blockers.append(
                "exact strategy activation lineage is missing: " + ", ".join(missing)
            )

    if set(paper_by_id) != set(eligible_ids):
        missing = sorted(set(eligible_ids) - set(paper_by_id))
        extra = sorted(set(paper_by_id) - set(eligible_ids))
        if missing:
            blockers.append("missing forward strategy rows: " + ", ".join(missing))
        if extra:
            pending_extra = sorted(set(extra) & set(pending_activation_by_id))
            foreign_extra = sorted(set(extra) - set(pending_activation_by_id))
            if pending_extra:
                blockers.append(
                    "pre-inception forward strategy rows: " + ", ".join(pending_extra)
                )
            if foreign_extra:
                blockers.append(
                    "unexpected forward strategy rows: " + ", ".join(foreign_extra)
                )
    for strategy_id, row in paper_by_id.items():
        if str(row.get("mode") or "") != "forward":
            blockers.append(f"{strategy_id} is not labeled forward")
        if row.get("normalized_daily_return_pct") is None:
            blockers.append(f"{strategy_id} has a missing account return")
    alpha_truth = _resolve_alpha_truth(
        market_date=day,
        db_path=Path(db_path),
        contract_path=alpha_contract_path,
        alpha_rows=alpha_rows,
        alpha_source=alpha_source,
    )
    blockers.extend(str(value) for value in alpha_truth["blockers"])
    alpha_optional = bool(alpha_truth["optional"])
    report_status = str(report.get("status") or "missing")
    alpha_source_status = str(alpha_source.get("status") or "missing")
    if report_status != "complete" and not (
        report_status == "partial"
        and alpha_optional
        and alpha_source_status == "empty"
        and str(paper_source.get("status") or "") == "complete"
    ):
        blockers.append(f"fleet report status is {report_status}")
    if alpha_source_status != "complete" and not (
        alpha_optional and alpha_source_status == "empty"
    ):
        blockers.append(f"AlphaOps fleet evidence is {alpha_source_status}")

    calendar_rows = _load_calendar_rows(expected_calendar, day, blockers)
    calendar_by_id = _unique_by_strategy(calendar_rows, blockers, "PaperOps calendar")
    if set(calendar_by_id) != set(eligible_ids):
        missing = sorted(set(eligible_ids) - set(calendar_by_id))
        extra = sorted(set(calendar_by_id) - set(eligible_ids))
        if missing:
            blockers.append("missing canonical calendar rows: " + ", ".join(missing))
        pending_extra = sorted(set(extra) & set(pending_activation_by_id))
        foreign_extra = sorted(set(extra) - set(pending_activation_by_id))
        if pending_extra:
            blockers.append(
                "pre-inception canonical calendar rows: " + ", ".join(pending_extra)
            )
        if foreign_extra:
            blockers.append(
                "unexpected canonical calendar rows: " + ", ".join(foreign_extra)
            )
    for strategy_id in sorted(
        set(paper_by_id) & set(calendar_by_id) & set(eligible_ids)
    ):
        report_row = paper_by_id[strategy_id]
        calendar_row = calendar_by_id[strategy_id]
        if str(report_row.get("source_run_id") or "") != str(
            calendar_row.get("run_id") or ""
        ):
            blockers.append(f"{strategy_id} fleet/calendar run identity mismatch")
        if str(report_row.get("strategy_version") or "") != str(
            calendar_row.get("strategy_version") or ""
        ):
            blockers.append(f"{strategy_id} fleet/calendar version mismatch")
        if str(report_row.get("execution_policy_version") or "") != str(
            calendar_row.get("execution_policy_version") or ""
        ):
            blockers.append(f"{strategy_id} fleet/calendar execution policy mismatch")
        calendar_return = _optional_number(calendar_row.get("daily_return_pct"))
        report_return = _optional_number(report_row.get("normalized_daily_return_pct"))
        if calendar_return is None or report_return is None:
            blockers.append(f"{strategy_id} fleet/calendar return evidence is missing")
        elif abs(report_return - (calendar_return * 100.0)) > 1e-8:
            blockers.append(f"{strategy_id} fleet/calendar return mismatch")
        for lifecycle_field in ("trades_opened", "trades_closed"):
            if _integer(report_row.get(lifecycle_field)) != _integer(
                calendar_row.get(lifecycle_field)
            ):
                blockers.append(
                    f"{strategy_id} fleet/calendar {lifecycle_field} mismatch"
                )

    decisions_path = root / "exports" / f"strategy_decisions_forward_{day}.json"
    decisions_raw = _load_list(decisions_path, "forward strategy decisions", blockers)
    exact_decisions: list[dict[str, Any]] = []
    for index, raw_row in enumerate(decisions_raw, start=1):
        if not isinstance(raw_row, Mapping):
            blockers.append(
                f"forward strategy decision row {index} must be a JSON object"
            )
            continue
        row = dict(raw_row)
        date_values = [
            str(row.get(field) or "")[:10]
            for field in ("market_date", "trade_date")
            if str(row.get(field) or "").strip()
        ]
        row_exact = True
        if not date_values or any(value != day for value in date_values):
            blockers.append(
                f"forward strategy decision row {index} date does not match {day}"
            )
            row_exact = False
        if str(row.get("mode") or "") != "forward":
            blockers.append(
                f"forward strategy decision row {index} mode is not forward"
            )
            row_exact = False
        strategy_id = str(row.get("strategy_id") or "")
        if strategy_id not in expected_ids:
            blockers.append(
                f"forward strategy decision row {index} has unknown strategy "
                f"{strategy_id or '<blank>'}"
            )
            row_exact = False
        elif strategy_id not in eligible_ids:
            inception = str(
                pending_activation_by_id.get(strategy_id, {}).get(
                    "coverage_inception_date"
                )
                or "unknown"
            )
            blockers.append(
                f"forward strategy decision row {index} uses {strategy_id}, which is "
                f"registered but not eligible until {inception}"
            )
            row_exact = False
        if row_exact:
            exact_decisions.append(row)
    decisions = sorted(
        exact_decisions,
        key=lambda row: (
            str(row.get("strategy_id") or ""),
            str(row.get("symbol") or ""),
            str(row.get("decision_id") or row.get("pick_id") or ""),
        ),
    )
    decisions_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in decisions:
        strategy_id = str(row.get("strategy_id") or "")
        decisions_by_id[strategy_id].append(row)
    live_semantic_fingerprints = _mapping(
        live_lineage.get("strategy_semantics_fingerprints")
    )
    for strategy_id in eligible_ids:
        expected_semantic_fingerprint = str(
            live_semantic_fingerprints.get(strategy_id) or ""
        )
        calendar_row = calendar_by_id.get(strategy_id, {})
        if (
            str(calendar_row.get("strategy_semantics_fingerprint") or "")
            != expected_semantic_fingerprint
        ):
            blockers.append(f"{strategy_id} calendar strategy semantics mismatch")
        strategy_decisions = decisions_by_id.get(strategy_id, [])
        if not strategy_decisions:
            blockers.append(f"{strategy_id} has no exact forward decision evidence")
            continue
        eligible_report_row = paper_by_id.get(strategy_id)
        if eligible_report_row is None or not calendar_row:
            continue
        expected_version = str(eligible_report_row.get("strategy_version") or "")
        expected_policy = str(
            eligible_report_row.get("execution_policy_version") or ""
        )
        expected_run_id = str(calendar_by_id[strategy_id].get("run_id") or "")
        if any(
            str(row.get("strategy_version") or "") != expected_version
            for row in strategy_decisions
        ):
            blockers.append(f"{strategy_id} decision strategy version mismatch")
        if any(
            str(row.get("execution_policy_version") or "") != expected_policy
            for row in strategy_decisions
        ):
            blockers.append(f"{strategy_id} decision execution policy mismatch")
        if any(
            str(row.get("strategy_semantics_fingerprint") or "")
            != expected_semantic_fingerprint
            for row in strategy_decisions
        ):
            blockers.append(f"{strategy_id} decision strategy semantics mismatch")
        if any(
            str(row.get("run_id") or "") != expected_run_id
            for row in strategy_decisions
        ):
            blockers.append(f"{strategy_id} decision run identity mismatch")
        observed_symbols = [
            str(row.get("symbol") or "").strip().upper() for row in strategy_decisions
        ]
        if len(observed_symbols) != len(set(observed_symbols)):
            blockers.append(f"{strategy_id} has duplicate symbol decision evidence")
        if universe and set(observed_symbols) != universe:
            blockers.append(f"{strategy_id} decision coverage does not match the universe")

    ledger_path = root / "ledger" / "paper_ledger.jsonl"
    ledger_events = _load_ledger_events(ledger_path, blockers)
    lifecycle_details, lifecycle_ledger_events = _resolve_lifecycle_details(
        market_date=day,
        expected_ids=eligible_ids,
        registered_ids=expected_ids,
        paper_by_id=paper_by_id,
        calendar_by_id=calendar_by_id,
        decisions_by_id=decisions_by_id,
        strategy_semantics_fingerprints=live_semantic_fingerprints,
        execution_configuration=_mapping(
            live_lineage.get("current_execution_configuration")
        ),
        ledger_events=ledger_events,
        blockers=blockers,
    )

    warnings = [str(value) for value in list(report.get("warnings") or []) if str(value)]
    unexpected_warnings = [
        warning
        for warning in warnings
        if not (alpha_optional and warning.startswith(_ALPHA_EMPTY_WARNING_PREFIX))
    ]
    if unexpected_warnings:
        blockers.append(
            f"fleet report has {len(unexpected_warnings)} non-Alpha evidence warning(s)"
        )

    if blockers:
        return _blocked_payload(day, root, report_path, blockers)

    strategy_lines: list[str] = []
    classifications: Counter[str] = Counter()
    for strategy_id in eligible_ids:
        line, classification = _strategy_line(
            strategy_id,
            paper_by_id[strategy_id],
            calendar_by_id[strategy_id],
            decisions_by_id[strategy_id],
        )
        strategy_lines.append(line)
        classifications[classification] += 1
    pending_activation_lines = [
        _pending_activation_line(strategy_id, pending_activation_by_id[strategy_id])
        for strategy_id in sorted(pending_activation_by_id)
    ]

    benchmark = _consistent_optional_number(
        paper_rows, "benchmark_return_pct", blockers, "benchmark"
    )
    cash = _consistent_optional_number(paper_rows, "cash_return_pct", blockers, "cash")
    if blockers:
        return _blocked_payload(day, root, report_path, blockers)
    alpha_line = str(alpha_truth["line"])
    replay_count = _integer(paper_source.get("excluded_non_forward_rows"))
    opened = sum(_integer(row.get("trades_opened")) for row in paper_rows)
    closed = sum(_integer(row.get("trades_closed")) for row in paper_rows)

    evidence = {
        "alpha_truth": alpha_truth["evidence"],
        "calendar_truth": calendar_truth_evidence,
        "source_bar_truth": source_bar_truth_evidence,
        "calendar_rows": [calendar_by_id[value] for value in eligible_ids],
        "decision_rows": decisions,
        "ledger_lifecycle_details": lifecycle_details,
        "ledger_lifecycle_events": lifecycle_ledger_events,
        "live_paperops_lineage": live_lineage,
        "market_date": day,
        "paper_rows": [paper_by_id[value] for value in eligible_ids],
        "report_schema_version": report.get("schema_version"),
    }
    fingerprint = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:20]
    event_key = f"paperops_fleet_digest:{day}:{fingerprint}"
    lifecycle_artifact_path = root.joinpath(
        *_DIGEST_ROOT,
        "artifacts",
        f"{day}_{fingerprint}.json",
    )
    forward_header = (
        f"FORWARD — {len(eligible_ids)} eligible daily-swing "
        f"{'strategy' if len(eligible_ids) == 1 else 'strategies'}"
        if pending_activation_lines
        else f"FORWARD — {len(eligible_ids)} daily-swing "
        f"{'strategy' if len(eligible_ids) == 1 else 'strategies'}"
    )
    activation_section = (
        [
            "",
            f"REGISTERED — {len(pending_activation_lines)} pending activation",
            *pending_activation_lines,
        ]
        if pending_activation_lines
        else []
    )
    message_prefix = [
        f"📊 Dawnstrike Paper Fleet — {day}",
        "Research/paper only. No broker orders.",
        "",
        forward_header,
        *strategy_lines,
        *activation_section,
        "",
        f"Lifecycle: {opened} opened | {closed} closed | "
        f"{classifications['no_setup']} no setup | "
        f"{classifications['no_fill']} no fill | "
        f"{classifications['pending']} pending | "
        f"{classifications['held']} held",
        "",
        "EXACT FORWARD LIFECYCLE",
    ]
    message_suffix = [
        "",
        f"Benchmark (equal-weight buy/hold): {_pct_or_na(benchmark)}",
        f"Cash no-trade policy: {_pct_or_na(cash)}",
        f"Replay: excluded from forward results ({replay_count} stored row(s)).",
        "",
        "ALPHAOPS — exact official Telegram cohort",
        alpha_line,
        "",
        (
            "Evidence: PaperOps complete | Alpha: sourced no_signal; scorecard N/A"
            if alpha_optional
            else "Evidence: complete | Missing: none"
        ),
        "N/A means no eligible trade return; it is never converted to 0%.",
        "Returns are observed paper evidence, not a profit guarantee.",
    ]
    lifecycle_lines = [_lifecycle_line(detail) for detail in lifecycle_details]
    message, displayed_lifecycle_count, telegram_truncated = _capped_digest_message(
        prefix=message_prefix,
        lifecycle_lines=lifecycle_lines,
        suffix=message_suffix,
        artifact_reference=(
            "PaperOps/"
            + lifecycle_artifact_path.relative_to(root).as_posix()
        ),
    )
    return {
        "schema_version": "dawnstrike.paperops_fleet_digest.v1",
        "status": "ready",
        "ready": True,
        "market_date": day,
        "research_only": True,
        "paper_ops_root": str(root),
        "fleet_report_path": str(report_path),
        "alpha_run_contract_path": str(alpha_contract_path),
        "decisions_path": str(decisions_path),
        "ledger_path": str(ledger_path),
        "lifecycle_artifact_path": str(lifecycle_artifact_path),
        "lifecycle_details": lifecycle_details,
        "event_key": event_key,
        "evidence_fingerprint": fingerprint,
        "message": message,
        "blockers": [],
        "summary": {
            "forward_strategy_count": len(eligible_ids),
            "registered_strategy_count": len(expected_ids),
            "pending_activation_strategy_count": len(pending_activation_by_id),
            "strategy_activation_by_id": activation_by_id,
            "opened": opened,
            "closed": closed,
            "classifications": dict(sorted(classifications.items())),
            "lifecycle_detail_count": len(lifecycle_details),
            "lifecycle_details_displayed": displayed_lifecycle_count,
            "telegram_truncated": telegram_truncated,
            "message_char_count": len(message),
            "benchmark_return_pct": benchmark,
            "cash_return_pct": cash,
            "replay_rows_excluded": replay_count,
            "alpha_status": alpha_truth["status"],
            "alpha_optional": alpha_optional,
            "active_execution_policy_version": live_lineage.get(
                "active_execution_policy_version"
            ),
            "active_policy_fingerprint": live_lineage.get(
                "active_policy_fingerprint"
            ),
            "current_config_policy_fingerprint": live_lineage.get(
                "current_config_policy_fingerprint"
            ),
            "strategy_semantics_fingerprints": live_lineage.get(
                "strategy_semantics_fingerprints"
            ),
            "strategy_registry_sha256": live_lineage.get(
                "strategy_registry_sha256"
            ),
            "strategy_semantics_manifest_sha256": live_lineage.get(
                "strategy_semantics_manifest_sha256"
            ),
            "execution_policy_manifest_sha256": live_lineage.get(
                "execution_policy_manifest_sha256"
            ),
        },
    }


def send_paperops_fleet_digest(
    *,
    market_date: str,
    db_path: str | Path = "data/shadow_real.sqlite",
    paper_ops_root: str | Path | None = None,
    fleet_report_path: str | Path = "outputs/strategy_fleet/strategy_fleet_report.json",
    alpha_run_contract_path: str | Path = "outputs/alpha_cycle/alpha_run_contract.json",
    notify: str = "telegram",
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
) -> dict[str, Any]:
    """Persist, dispatch, and receipt one verified fleet digest.

    Identical evidence produces the same event key.  Failed sends remain in the
    filesystem outbox and are retried by a repeated invocation.  The existing
    ``notifications_sent`` row prevents a confirmed delivery from being sent
    again.
    """

    channel = notify.strip().lower()
    if channel not in _CHANNELS:
        raise NotificationError("PaperOps fleet digest notify must be telegram or console.")
    if max_attempts < 1 or max_attempts > 5:
        raise NotificationError("PaperOps fleet digest max_attempts must be between 1 and 5.")
    digest = build_paperops_fleet_digest(
        market_date=market_date,
        db_path=db_path,
        paper_ops_root=paper_ops_root,
        fleet_report_path=fleet_report_path,
        alpha_run_contract_path=alpha_run_contract_path,
    )
    root = Path(str(digest["paper_ops_root"]))
    if not digest["ready"]:
        blocked_path = root.joinpath(*_DIGEST_ROOT, "blocked", f"{digest['market_date']}.json")
        write_json(blocked_path, digest)
        reasons = "; ".join(str(value) for value in digest["blockers"])
        raise NotificationError(f"PaperOps fleet digest blocked: {reasons}")

    event_key = str(digest["event_key"])
    fingerprint = str(digest["evidence_fingerprint"])
    lifecycle_artifact_path = Path(str(digest["lifecycle_artifact_path"]))
    write_json(
        lifecycle_artifact_path,
        {
            "schema_version": "dawnstrike.paperops_fleet_lifecycle_artifact.v1",
            "market_date": digest["market_date"],
            "research_only": True,
            "event_key": event_key,
            "evidence_fingerprint": fingerprint,
            "ledger_path": digest["ledger_path"],
            "lifecycle_details": digest["lifecycle_details"],
        },
    )
    outbox_path = root.joinpath(
        *_DIGEST_ROOT,
        "outbox",
        f"{digest['market_date']}_{fingerprint}_{channel}.json",
    )
    lock_path = outbox_path.with_suffix(".lock")
    store = SQLiteScanStore(db_path)
    receipt_key = f"{event_key}:{channel}"
    last_error: Exception | None = None

    with exclusive_file_lock(lock_path):
        record = _outbox_record(outbox_path, digest, channel)
        if store.has_notification(receipt_key):
            record.update(
                {
                    "status": "delivered",
                    "receipt_key": receipt_key,
                    "last_error": None,
                    "delivered_at": utc_now_iso(),
                }
            )
            write_json(outbox_path, record)
            return _delivery_result(digest, outbox_path, record, sent=0, skipped=1)

        for attempt in range(max_attempts):
            record["attempt_count"] = _integer(record.get("attempt_count")) + 1
            record["status"] = "sending"
            record["last_error"] = None
            record["last_attempted_at"] = utc_now_iso()
            write_json(outbox_path, record)
            event = NotificationEvent(
                event_key=event_key,
                title="Dawnstrike Paper Fleet",
                body=str(digest["message"]),
                channel_hint="daily_summary",
                payload={
                    "telegram_compact_message": str(digest["message"]),
                    "market_date": digest["market_date"],
                    "run_id": event_key,
                    "research_only": True,
                    "evidence_fingerprint": fingerprint,
                },
            )
            try:
                config = load_config(
                    database_path=Path(db_path),
                    notifier_channels=channel,
                )
                notifiers = (
                    [ConsoleNotifier()] if channel == "console" else build_notifiers(config)
                )
                stats = dispatch_events([event], notifiers, store, dry_run=False)
            except Exception as exc:  # The durable failure record is part of the contract.
                last_error = exc
                record["status"] = "delivery_failed"
                record["last_error"] = _safe_error(exc)
                write_json(outbox_path, record)
                if attempt + 1 < max_attempts and retry_delay_seconds > 0:
                    time.sleep(retry_delay_seconds * (attempt + 1))
                continue

            record.update(
                {
                    "status": "delivered",
                    "receipt_key": receipt_key,
                    "last_error": None,
                    "delivered_at": utc_now_iso(),
                }
            )
            write_json(outbox_path, record)
            return _delivery_result(
                digest,
                outbox_path,
                record,
                sent=_integer(stats.get("sent")),
                skipped=_integer(stats.get("skipped")),
            )

    raise NotificationError(
        "PaperOps fleet Telegram delivery failed after durable retry attempts: "
        f"{_safe_error(last_error) if last_error else 'unknown error'}"
    ) from last_error


def _strategy_line(
    strategy_id: str,
    report_row: Mapping[str, Any],
    calendar_row: Mapping[str, Any],
    decisions: list[dict[str, Any]],
) -> tuple[str, str]:
    label = _STRATEGY_LABELS.get(strategy_id, strategy_id)
    account_return = _optional_number(report_row.get("normalized_daily_return_pct"))
    opened = _integer(calendar_row.get("trades_opened"))
    closed = _integer(calendar_row.get("trades_closed"))
    pending = _integer(calendar_row.get("pending_orders"))
    held = _integer(calendar_row.get("open_positions"))
    statuses = Counter(str(row.get("decision_status") or "missing") for row in decisions)
    if opened or closed:
        return (
            f"• {label}: acct {_pct_or_na(account_return)} | "
            f"opened {opened}, closed {closed} | forward paper",
            "fill_activity",
        )
    if held:
        return (
            f"• {label}: acct {_pct_or_na(account_return)} | held {held}; no new fill",
            "held",
        )
    if pending:
        return (
            f"• {label}: {pending} paper entry pending | trade return N/A",
            "pending",
        )
    if set(statuses) == {"no_setup"}:
        return f"• {label}: no setup | trade return N/A", "no_setup"
    if statuses.get("accepted"):
        return (
            f"• {label}: accepted signal, no paper fill | trade return N/A",
            "no_fill",
        )
    return f"• {label}: signal did not pass gates | trade return N/A", "no_fill"


def _pending_activation_line(
    strategy_id: str,
    activation: Mapping[str, Any],
) -> str:
    label = _STRATEGY_LABELS.get(strategy_id, strategy_id)
    inception = str(activation.get("coverage_inception_date") or "unknown")
    return (
        f"• {label}: registered / not yet eligible | starts {inception} | "
        "return N/A (pending)"
    )


def _resolve_lifecycle_details(
    *,
    market_date: str,
    expected_ids: tuple[str, ...],
    registered_ids: tuple[str, ...],
    paper_by_id: Mapping[str, Mapping[str, Any]],
    calendar_by_id: Mapping[str, Mapping[str, Any]],
    decisions_by_id: Mapping[str, list[dict[str, Any]]],
    strategy_semantics_fingerprints: Mapping[str, Any],
    execution_configuration: Mapping[str, Any],
    ledger_events: list[dict[str, Any]],
    blockers: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve exact forward order/position lifecycle rows from the ledger."""

    lifecycle_types = {
        "paper_fill",
        "paper_order_blocked",
        "paper_order_created",
        "paper_order_pending_no_fill_data",
        "paper_position_checked_no_action",
        "paper_position_closed",
        "paper_position_marked_to_market",
        "paper_position_opened",
    }
    exact_events: list[dict[str, Any]] = []
    for raw_event in ledger_events:
        event = _mapping(raw_event)
        event_type = str(event.get("event_type") or "")
        strategy_id = str(event.get("strategy_id") or "")
        if (
            event_type in lifecycle_types
            and str(event.get("trade_date") or "")[:10] == market_date
            and strategy_id not in expected_ids
        ):
            if strategy_id in registered_ids:
                blockers.append(
                    "same-day ledger lifecycle references a registered strategy that "
                    f"is not yet eligible: {strategy_id}"
                )
            else:
                blockers.append(
                    "same-day ledger lifecycle references unknown strategy "
                    f"{strategy_id or '<blank>'}"
                )
            continue
        if (
            event_type not in lifecycle_types
            or str(event.get("mode") or "") != "forward"
            or strategy_id not in expected_ids
        ):
            continue
        payload = _mapping(event.get("payload"))
        expected_row = paper_by_id.get(strategy_id, {})
        expected_version = str(expected_row.get("strategy_version") or "")
        expected_policy = str(expected_row.get("execution_policy_version") or "")
        expected_semantics = str(
            strategy_semantics_fingerprints.get(strategy_id) or ""
        )
        event_date = str(event.get("trade_date") or "")[:10]
        if event_date == market_date:
            calendar_run_id = str(
                calendar_by_id.get(strategy_id, {}).get("run_id") or ""
            )
            if str(event.get("run_id") or "") != calendar_run_id:
                blockers.append(
                    f"{strategy_id} same-day {event_type} run identity mismatch"
                )
                continue
            if str(payload.get("strategy_id") or "") != strategy_id:
                blockers.append(
                    f"{strategy_id} same-day {event_type} payload strategy mismatch"
                )
                continue
            if str(payload.get("strategy_version") or "") != expected_version:
                blockers.append(
                    f"{strategy_id} same-day {event_type} strategy version mismatch"
                )
                continue
            if str(payload.get("execution_policy_version") or "") != expected_policy:
                blockers.append(
                    f"{strategy_id} same-day {event_type} execution policy mismatch"
                )
                continue
            if (
                str(payload.get("strategy_semantics_fingerprint") or "")
                != expected_semantics
            ):
                blockers.append(
                    f"{strategy_id} same-day {event_type} strategy semantics mismatch"
                )
                continue
        if (
            str(payload.get("strategy_version") or "") == expected_version
            and str(payload.get("execution_policy_version") or "") == expected_policy
            and str(payload.get("strategy_semantics_fingerprint") or "")
            == expected_semantics
        ):
            exact_events.append(event)

    day_events = [
        event
        for event in exact_events
        if str(event.get("trade_date") or "")[:10] == market_date
    ]
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in day_events:
        by_type[str(event.get("event_type") or "")].append(event)

    fills_by_order = _unique_events_by_payload_id(
        by_type["paper_fill"], "order_id", blockers, "same-day paper fill"
    )
    opens_by_position = _unique_events_by_payload_id(
        by_type["paper_position_opened"],
        "position_id",
        blockers,
        "same-day opened position",
    )
    closes_by_position = _unique_events_by_payload_id(
        by_type["paper_position_closed"],
        "position_id",
        blockers,
        "same-day closed position",
    )
    all_orders_by_id = _unique_events_by_payload_id(
        [
            event
            for event in exact_events
            if str(event.get("event_type") or "") == "paper_order_created"
        ],
        "order_id",
        blockers,
        "exact paper order",
    )
    all_fills_by_order = _unique_events_by_payload_id(
        [
            event
            for event in exact_events
            if str(event.get("event_type") or "") == "paper_fill"
        ],
        "order_id",
        blockers,
        "exact paper fill",
    )
    all_positions_by_id = _unique_events_by_payload_id(
        [
            event
            for event in exact_events
            if str(event.get("event_type") or "") == "paper_position_opened"
        ],
        "position_id",
        blockers,
        "exact opened position",
    )
    same_day_open_counts_by_order: Counter[str] = Counter(
        str(_mapping(event.get("payload")).get("order_id") or "")
        for event in opens_by_position.values()
    )
    for order_id, fill_event in fills_by_order.items():
        strategy_id = str(fill_event.get("strategy_id") or "")
        if order_id not in all_orders_by_id:
            blockers.append(f"{strategy_id} same-day fill has no exact originating order")
        if same_day_open_counts_by_order[order_id] != 1:
            blockers.append(
                f"{strategy_id} same-day fill {order_id} has "
                f"{same_day_open_counts_by_order[order_id]} exact opened positions"
            )

    state_candidates = [
        *by_type["paper_position_opened"],
        *by_type["paper_position_checked_no_action"],
        *by_type["paper_position_marked_to_market"],
    ]
    open_state_by_position: dict[str, dict[str, Any]] = {}
    for event in state_candidates:
        position_id = str(_mapping(event.get("payload")).get("position_id") or "")
        if position_id:
            open_state_by_position[position_id] = event
    for position_id in closes_by_position:
        open_state_by_position.pop(position_id, None)

    pending_by_order: dict[str, dict[str, Any]] = {}
    for event in [
        *by_type["paper_order_created"],
        *by_type["paper_order_pending_no_fill_data"],
    ]:
        order_id = str(_mapping(event.get("payload")).get("order_id") or "")
        if order_id:
            pending_by_order[order_id] = event
    terminal_order_ids = set(fills_by_order)
    terminal_order_ids.update(
        str(_mapping(event.get("payload")).get("order_id") or "")
        for event in by_type["paper_order_blocked"]
    )
    for order_id in terminal_order_ids:
        pending_by_order.pop(order_id, None)

    for strategy_id in expected_ids:
        calendar_row = calendar_by_id.get(strategy_id)
        if not calendar_row:
            continue
        opened_count = sum(
            1
            for event in opens_by_position.values()
            if str(event.get("strategy_id") or "") == strategy_id
        )
        closed_count = sum(
            1
            for event in closes_by_position.values()
            if str(event.get("strategy_id") or "") == strategy_id
        )
        pending_count = sum(
            1
            for event in pending_by_order.values()
            if str(event.get("strategy_id") or "") == strategy_id
        )
        open_count = sum(
            1
            for event in open_state_by_position.values()
            if str(event.get("strategy_id") or "") == strategy_id
        )
        expected_counts = {
            "trades_opened": opened_count,
            "trades_closed": closed_count,
            "pending_orders": pending_count,
            "open_positions": open_count,
        }
        for field, observed in expected_counts.items():
            if _integer(calendar_row.get(field)) != observed:
                blockers.append(
                    f"{strategy_id} calendar {field} does not match exact "
                    f"same-day ledger lifecycle ({_integer(calendar_row.get(field))} != "
                    f"{observed})"
                )

    details: list[dict[str, Any]] = []
    closed_position_ids = set(closes_by_position)
    for position_id, close_event in closes_by_position.items():
        close_payload = _mapping(close_event.get("payload"))
        position_event = all_positions_by_id.get(position_id)
        position_payload = _mapping(position_event.get("payload")) if position_event else {}
        strategy_id = str(close_event.get("strategy_id") or "")
        if not position_payload:
            blockers.append(
                f"{strategy_id} closed position {position_id} has no exact opening lineage"
            )
        _validate_position_payload(position_payload, strategy_id, "closed", blockers)
        lineage_event_ids = _position_lineage_event_ids(
            strategy_id=strategy_id,
            position_event=position_event,
            all_orders_by_id=all_orders_by_id,
            all_fills_by_order=all_fills_by_order,
            execution_configuration=execution_configuration,
            blockers=blockers,
        )
        if (
            _optional_number(close_payload.get("close_price")) is None
            or _optional_number(close_payload.get("net_pnl")) is None
            or not str(close_payload.get("close_reason") or "")
        ):
            blockers.append(f"{strategy_id} closed lifecycle economics are incomplete")
        _validate_close_economics(
            close_event=close_event,
            close_payload=close_payload,
            position_event=position_event,
            position_payload=position_payload,
            expected_run_id=str(
                calendar_by_id.get(strategy_id, {}).get("run_id") or ""
            ),
            execution_configuration=execution_configuration,
            blockers=blockers,
        )
        details.append(
            _position_lifecycle_detail(
                kind="closed",
                event=close_event,
                position_payload=position_payload,
                close_payload=close_payload,
                source_event_ids=[
                    *lineage_event_ids,
                    str(close_event.get("event_id") or ""),
                ],
            )
        )

    for position_id, state_event in open_state_by_position.items():
        if position_id in closed_position_ids:
            continue
        payload = _mapping(state_event.get("payload"))
        strategy_id = str(state_event.get("strategy_id") or "")
        _validate_position_payload(payload, strategy_id, "open", blockers)
        opened_event = opens_by_position.get(position_id)
        lineage_event = all_positions_by_id.get(position_id)
        lineage_event_ids = _position_lineage_event_ids(
            strategy_id=strategy_id,
            position_event=lineage_event,
            all_orders_by_id=all_orders_by_id,
            all_fills_by_order=all_fills_by_order,
            execution_configuration=execution_configuration,
            blockers=blockers,
        )
        if opened_event:
            order_id = str(payload.get("order_id") or "")
            if order_id not in fills_by_order:
                blockers.append(
                    f"{strategy_id} opened position {position_id} has no exact same-day fill"
                )
        details.append(
            _position_lifecycle_detail(
                kind="opened" if opened_event else "held",
                event=state_event,
                position_payload=payload,
                close_payload={},
                source_event_ids=[
                    *lineage_event_ids,
                    str(state_event.get("event_id") or ""),
                ],
            )
        )

    represented_pick_counts: Counter[str] = Counter()
    for event in [*by_type["paper_order_created"], *by_type["paper_order_blocked"]]:
        payload = _mapping(event.get("payload"))
        pick_id = str(payload.get("pick_id") or "")
        if pick_id:
            represented_pick_counts[pick_id] += 1

    for event in pending_by_order.values():
        payload = _mapping(event.get("payload"))
        strategy_id = str(event.get("strategy_id") or "")
        if (
            not str(payload.get("expected_fill_rule") or "")
            or not str(payload.get("earliest_fill_date") or "")
            or not str(payload.get("direction") or "")
            or _integer(payload.get("quantity")) <= 0
            or _optional_number(payload.get("stop")) is None
        ):
            blockers.append(f"{strategy_id} pending lifecycle detail is incomplete")
        details.append(
            {
                "kind": "pending",
                "strategy_id": strategy_id,
                "symbol": str(event.get("symbol") or payload.get("symbol") or ""),
                "direction": str(payload.get("direction") or ""),
                "entry_rule": str(payload.get("expected_fill_rule") or ""),
                "earliest_fill_date": str(payload.get("earliest_fill_date") or ""),
                "planned_quantity": _integer(payload.get("quantity")),
                "entry_reference": _optional_number(payload.get("entry")),
                "stop": _optional_number(payload.get("stop")),
                "target": _optional_number(payload.get("target")),
                "fill_price": None,
                "close_price": None,
                "close_reason": None,
                "net_pnl_after_costs": None,
                "reason": None,
                "source_event_ids": [str(event.get("event_id") or "")],
            }
        )

    for event in by_type["paper_order_blocked"]:
        payload = _mapping(event.get("payload"))
        details.append(
            {
                "kind": "blocked",
                "strategy_id": str(event.get("strategy_id") or ""),
                "symbol": str(event.get("symbol") or payload.get("symbol") or ""),
                "direction": str(payload.get("direction") or ""),
                "entry_rule": str(payload.get("expected_fill_rule") or ""),
                "earliest_fill_date": str(payload.get("earliest_fill_date") or ""),
                "planned_quantity": _integer(payload.get("quantity")),
                "entry_reference": _optional_number(payload.get("entry")),
                "stop": _optional_number(payload.get("stop")),
                "target": _optional_number(payload.get("target")),
                "fill_price": None,
                "close_price": None,
                "close_reason": None,
                "net_pnl_after_costs": None,
                "reason": str(payload.get("reason") or "unspecified_gate"),
                "source_event_ids": [str(event.get("event_id") or "")],
            }
        )

    for strategy_id in expected_ids:
        for decision in decisions_by_id.get(strategy_id, []):
            if str(decision.get("decision_status") or "") != "accepted":
                continue
            pick_id = str(decision.get("pick_id") or "")
            if not pick_id:
                blockers.append(f"{strategy_id} accepted decision is missing pick identity")
            elif represented_pick_counts[pick_id] != 1:
                blockers.append(
                    f"{strategy_id} accepted pick {pick_id} has "
                    f"{represented_pick_counts[pick_id]} exact order resolution events"
                )

    kind_order = {
        "closed": 0,
        "opened": 1,
        "held": 2,
        "pending": 3,
        "blocked": 4,
    }
    sorted_details = sorted(
        details,
        key=lambda detail: (
            str(detail.get("strategy_id") or ""),
            kind_order.get(str(detail.get("kind") or ""), 99),
            str(detail.get("symbol") or ""),
            "|".join(str(value) for value in list(detail.get("source_event_ids") or [])),
        ),
    )
    evidence_event_ids = {
        str(event_id)
        for detail in sorted_details
        for event_id in list(detail.get("source_event_ids") or [])
        if str(event_id)
    }
    evidence_events = sorted(
        [
            event
            for event in exact_events
            if str(event.get("event_id") or "") in evidence_event_ids
        ],
        key=lambda event: str(event.get("event_id") or ""),
    )
    return sorted_details, evidence_events


def _unique_events_by_payload_id(
    events: list[dict[str, Any]],
    id_field: str,
    blockers: list[str],
    label: str,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[str(_mapping(event.get("payload")).get(id_field) or "")].append(event)
    result: dict[str, dict[str, Any]] = {}
    for entity_id, matches in grouped.items():
        if not entity_id:
            blockers.append(f"{label} is missing {id_field}")
        elif len(matches) != 1:
            blockers.append(f"{label} has {len(matches)} events for {entity_id}")
        else:
            result[entity_id] = matches[0]
    return result


def _validate_position_payload(
    payload: Mapping[str, Any],
    strategy_id: str,
    state: str,
    blockers: list[str],
) -> None:
    if (
        not str(payload.get("symbol") or "")
        or not str(payload.get("direction") or "")
        or _integer(payload.get("quantity")) <= 0
        or _optional_number(payload.get("entry_price")) is None
        or _optional_number(payload.get("stop")) is None
    ):
        blockers.append(f"{strategy_id} {state} position lifecycle detail is incomplete")


def _position_lineage_event_ids(
    *,
    strategy_id: str,
    position_event: Mapping[str, Any] | None,
    all_orders_by_id: Mapping[str, Mapping[str, Any]],
    all_fills_by_order: Mapping[str, Mapping[str, Any]],
    execution_configuration: Mapping[str, Any],
    blockers: list[str],
) -> list[str]:
    if not position_event:
        blockers.append(f"{strategy_id} position has no exact opened-position event")
        return []
    position_payload = _mapping(position_event.get("payload"))
    order_id = str(position_payload.get("order_id") or "")
    order_event = all_orders_by_id.get(order_id)
    fill_event = all_fills_by_order.get(order_id)
    if not order_id or not order_event:
        blockers.append(f"{strategy_id} position has no exact originating order")
    if not order_id or not fill_event:
        blockers.append(f"{strategy_id} position has no exact originating fill")

    position_symbol = str(position_payload.get("symbol") or "")
    position_direction = str(position_payload.get("direction") or "")
    position_quantity = _integer(position_payload.get("quantity"))
    position_entry = _optional_number(position_payload.get("entry_price"))
    position_entry_fee = _optional_number(position_payload.get("entry_fee"))
    if str(position_event.get("strategy_id") or "") != strategy_id:
        blockers.append(f"{strategy_id} opened-position event strategy mismatch")
    if str(position_event.get("symbol") or "") != position_symbol:
        blockers.append(f"{strategy_id} opened-position event symbol mismatch")
    for label, ancestor in (("order", order_event), ("fill", fill_event)):
        if not ancestor:
            continue
        payload = _mapping(ancestor.get("payload"))
        if str(ancestor.get("strategy_id") or "") != strategy_id:
            blockers.append(f"{strategy_id} position {label} strategy lineage mismatch")
        if str(payload.get("symbol") or "") != position_symbol:
            blockers.append(f"{strategy_id} position {label} symbol lineage mismatch")
        if _integer(payload.get("quantity")) != position_quantity:
            blockers.append(f"{strategy_id} position {label} quantity lineage mismatch")
    if fill_event:
        fill_payload = _mapping(fill_event.get("payload"))
        fill_price = _optional_number(fill_payload.get("fill_price"))
        if (
            fill_price is None
            or position_entry is None
            or abs(fill_price - position_entry) > 1e-8
        ):
            blockers.append(f"{strategy_id} position fill-price lineage mismatch")
        fill_fee = _optional_number(fill_payload.get("fee"))
        if (
            fill_fee is None
            or position_entry_fee is None
            or not math.isclose(fill_fee, position_entry_fee, rel_tol=1e-9, abs_tol=1e-8)
        ):
            blockers.append(f"{strategy_id} position entry-fee lineage mismatch")
        fee_bps = _optional_number(execution_configuration.get("fee_bps"))
        slippage_bps = _optional_number(
            execution_configuration.get("slippage_bps")
        )
        fill_slippage = _optional_number(fill_payload.get("slippage"))
        if (
            fill_price is None
            or fill_fee is None
            or fill_slippage is None
            or fee_bps is None
            or slippage_bps is None
            or not all(
                math.isfinite(value)
                for value in (
                    fill_price,
                    fill_fee,
                    fill_slippage,
                    fee_bps,
                    slippage_bps,
                )
            )
            or fill_price <= 0
            or fee_bps < 0
            or slippage_bps < 0
        ):
            blockers.append(f"{strategy_id} originating fill economics are invalid")
        else:
            expected_entry_fee = fill_price * position_quantity * fee_bps / 10_000.0
            expected_fill_slippage = _execution_slippage_cost(
                execution_price=fill_price,
                quantity=position_quantity,
                slippage_bps=slippage_bps,
                direction=position_direction,
                is_entry=True,
            )
            if not math.isclose(
                fill_fee,
                expected_entry_fee,
                rel_tol=1e-9,
                abs_tol=1e-8,
            ):
                blockers.append(
                    f"{strategy_id} originating fill fee does not match canonical recomputation"
                )
            if expected_fill_slippage is None or not math.isclose(
                fill_slippage,
                expected_fill_slippage,
                rel_tol=1e-9,
                abs_tol=1e-8,
            ):
                blockers.append(
                    f"{strategy_id} originating fill slippage does not match canonical "
                    "recomputation"
                )
    if order_event:
        order_payload = _mapping(order_event.get("payload"))
        if str(order_payload.get("direction") or "") != position_direction:
            blockers.append(f"{strategy_id} position order-direction lineage mismatch")
        for field in ("stop", "target"):
            order_value = _optional_number(order_payload.get(field))
            position_value = _optional_number(position_payload.get(field))
            if order_value is None and position_value is None:
                continue
            if (
                order_value is None
                or position_value is None
                or not math.isclose(
                    order_value,
                    position_value,
                    rel_tol=1e-9,
                    abs_tol=1e-8,
                )
            ):
                blockers.append(
                    f"{strategy_id} position order-{field} lineage mismatch"
                )

    return [
        str(event.get("event_id") or "")
        for event in (order_event, fill_event, position_event)
        if event
    ]


def _validate_close_economics(
    *,
    close_event: Mapping[str, Any],
    close_payload: Mapping[str, Any],
    position_event: Mapping[str, Any] | None,
    position_payload: Mapping[str, Any],
    expected_run_id: str,
    execution_configuration: Mapping[str, Any],
    blockers: list[str],
) -> None:
    """Recompute a close from its exact opened position and active fee policy."""

    strategy_id = str(close_event.get("strategy_id") or "")
    position_id = str(position_payload.get("position_id") or "")
    symbol = str(position_payload.get("symbol") or "")
    direction = str(position_payload.get("direction") or "").lower()
    close_symbol = str(close_payload.get("symbol") or "")
    event_symbol = str(close_event.get("symbol") or "")
    if close_symbol != symbol or event_symbol != symbol:
        blockers.append(f"{strategy_id} close symbol does not match opened position")
    if position_event and str(position_event.get("symbol") or "") != symbol:
        blockers.append(f"{strategy_id} opened-position event symbol is inconsistent")
    if str(close_payload.get("position_id") or "") != position_id:
        blockers.append(f"{strategy_id} close position identity is inconsistent")
    if str(close_event.get("run_id") or "") != expected_run_id:
        blockers.append(f"{strategy_id} close event run does not match calendar run")
    if str(close_payload.get("run_id") or "") != expected_run_id:
        blockers.append(f"{strategy_id} close payload run does not match calendar run")
    payload_direction = str(close_payload.get("direction") or "").lower()
    if payload_direction and payload_direction != direction:
        blockers.append(f"{strategy_id} close direction contradicts opened position")
    if close_payload.get("mode") not in {None, "", "forward"}:
        blockers.append(f"{strategy_id} close payload mode is not forward")

    required = {
        "entry_price": _optional_number(position_payload.get("entry_price")),
        "stop": _optional_number(position_payload.get("stop")),
        "entry_fee": _optional_number(position_payload.get("entry_fee")),
        "close_price": _optional_number(close_payload.get("close_price")),
        "exit_fee": _optional_number(close_payload.get("fee")),
        "close_slippage": _optional_number(close_payload.get("slippage")),
        "close_entry_fee": _optional_number(close_payload.get("entry_fee")),
        "gross_pnl": _optional_number(close_payload.get("gross_pnl")),
        "net_pnl": _optional_number(close_payload.get("net_pnl")),
        "r_multiple": _optional_number(close_payload.get("r_multiple")),
        "fee_bps": _optional_number(execution_configuration.get("fee_bps")),
        "slippage_bps": _optional_number(
            execution_configuration.get("slippage_bps")
        ),
    }
    quantity = _integer(position_payload.get("quantity"))
    if direction not in {"long", "short"}:
        blockers.append(f"{strategy_id} close direction is unsupported")
        return
    if quantity <= 0:
        blockers.append(f"{strategy_id} close quantity is invalid")
        return
    missing_or_nonfinite = [
        name
        for name, value in required.items()
        if value is None or not math.isfinite(value)
    ]
    if missing_or_nonfinite:
        blockers.append(
            f"{strategy_id} close economics are missing/non-finite: "
            + ", ".join(sorted(missing_or_nonfinite))
        )
        return

    validated = {name: value for name, value in required.items() if value is not None}
    entry_price = validated["entry_price"]
    stop = validated["stop"]
    entry_fee = validated["entry_fee"]
    close_price = validated["close_price"]
    fee_bps = validated["fee_bps"]
    slippage_bps = validated["slippage_bps"]
    if entry_price <= 0 or stop <= 0 or close_price <= 0 or fee_bps < 0 or slippage_bps < 0:
        blockers.append(f"{strategy_id} close economic inputs are outside valid bounds")
        return

    expected_gross = _directional_pnl(
        direction,
        entry_price,
        close_price,
        quantity,
    )
    expected_exit_fee = close_price * quantity * fee_bps / 10_000.0
    expected_close_slippage = _execution_slippage_cost(
        execution_price=close_price,
        quantity=quantity,
        slippage_bps=slippage_bps,
        direction=direction,
        is_entry=False,
    )
    expected_net = expected_gross - entry_fee - expected_exit_fee
    stop_rate = slippage_bps / 10_000.0
    stop_fill = stop * (1 - stop_rate) if direction == "long" else stop * (1 + stop_rate)
    stop_gross_loss = max(
        0.0,
        -_directional_pnl(direction, entry_price, stop_fill, quantity),
    )
    stop_exit_fee = stop_fill * quantity * fee_bps / 10_000.0
    risk_amount = stop_gross_loss + entry_fee + stop_exit_fee
    expected_r = expected_net / risk_amount if risk_amount else 0.0
    comparisons = {
        "entry fee": (validated["close_entry_fee"], entry_fee),
        "exit fee": (validated["exit_fee"], expected_exit_fee),
        "gross P&L": (validated["gross_pnl"], expected_gross),
        "after-cost net P&L": (validated["net_pnl"], expected_net),
        "R-multiple": (validated["r_multiple"], expected_r),
    }
    for label, (observed, expected) in comparisons.items():
        if not math.isclose(observed, expected, rel_tol=1e-9, abs_tol=1e-8):
            blockers.append(
                f"{strategy_id} close {label} does not match canonical recomputation"
            )
    if expected_close_slippage is None or not math.isclose(
        validated["close_slippage"],
        expected_close_slippage,
        rel_tol=1e-9,
        abs_tol=1e-8,
    ):
        blockers.append(
            f"{strategy_id} close slippage does not match canonical recomputation"
        )

    if str(close_payload.get("close_reason") or "") not in {
        "stop",
        "target",
        "timeout",
    }:
        blockers.append(f"{strategy_id} close reason is not a supported engine outcome")


def _directional_pnl(
    direction: str,
    entry_price: float,
    exit_price: float,
    quantity: int,
) -> float:
    if direction == "long":
        return (exit_price - entry_price) * quantity
    return (entry_price - exit_price) * quantity


def _execution_slippage_cost(
    *,
    execution_price: float,
    quantity: int,
    slippage_bps: float,
    direction: str,
    is_entry: bool,
) -> float | None:
    """Invert the engine execution multiplier and recover paid slippage."""

    if (
        direction not in {"long", "short"}
        or quantity <= 0
        or execution_price <= 0
        or slippage_bps < 0
        or not math.isfinite(execution_price)
        or not math.isfinite(slippage_bps)
    ):
        return None
    rate = slippage_bps / 10_000.0
    if is_entry:
        multiplier = 1 + rate if direction == "long" else 1 - rate
    else:
        multiplier = 1 - rate if direction == "long" else 1 + rate
    if multiplier <= 0:
        return None
    raw_price = execution_price / multiplier
    return abs(execution_price - raw_price) * quantity


def _position_lifecycle_detail(
    *,
    kind: str,
    event: Mapping[str, Any],
    position_payload: Mapping[str, Any],
    close_payload: Mapping[str, Any],
    source_event_ids: list[str],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "strategy_id": str(event.get("strategy_id") or ""),
        "symbol": str(event.get("symbol") or position_payload.get("symbol") or ""),
        "direction": str(position_payload.get("direction") or ""),
        "entry_rule": None,
        "earliest_fill_date": None,
        "planned_quantity": None,
        "entry_reference": None,
        "fill_price": _optional_number(position_payload.get("entry_price")),
        "quantity": _integer(position_payload.get("quantity")),
        "stop": _optional_number(position_payload.get("stop")),
        "target": _optional_number(position_payload.get("target")),
        "last_mark_price": _optional_number(position_payload.get("last_mark_price")),
        "close_price": _optional_number(close_payload.get("close_price")),
        "close_reason": str(close_payload.get("close_reason") or "") or None,
        "net_pnl_after_costs": _optional_number(close_payload.get("net_pnl")),
        "reason": None,
        "source_event_ids": [value for value in source_event_ids if value],
    }


def _lifecycle_line(detail: Mapping[str, Any]) -> str:
    strategy_id = str(detail.get("strategy_id") or "")
    label = _STRATEGY_LABELS.get(strategy_id, strategy_id or "Unknown strategy")
    symbol = str(detail.get("symbol") or "N/A").upper()
    direction = str(detail.get("direction") or "N/A").upper()
    kind = str(detail.get("kind") or "")
    stop_target = (
        f"stop {_price_or_na(detail.get('stop'))} / "
        f"target {_price_or_na(detail.get('target'))}"
    )
    if kind == "closed":
        return (
            f"• {label} | {symbol} {direction} | fill "
            f"{_price_or_na(detail.get('fill_price'))} x {_quantity_or_na(detail.get('quantity'))} "
            f"| {stop_target} | closed {detail.get('close_reason') or 'N/A'} @ "
            f"{_price_or_na(detail.get('close_price'))} | after-cost net "
            f"{_money_or_na(detail.get('net_pnl_after_costs'))}"
        )
    if kind in {"opened", "held"}:
        state = "filled/open" if kind == "opened" else "held open"
        return (
            f"• {label} | {symbol} {direction} | {state} "
            f"{_price_or_na(detail.get('fill_price'))} x {_quantity_or_na(detail.get('quantity'))} "
            f"| {stop_target} | realized net P&L N/A"
        )
    if kind == "pending":
        return (
            f"• {label} | {symbol} {direction} | pending next valid bar open >= "
            f"{detail.get('earliest_fill_date') or 'N/A'}; planned qty "
            f"{_quantity_or_na(detail.get('planned_quantity'))} | {stop_target} | net P&L N/A"
        )
    if kind == "blocked":
        return (
            f"• {label} | {symbol} {direction} | order blocked: "
            f"{detail.get('reason') or 'N/A'} | {stop_target} | net P&L N/A"
        )
    return (
        f"• {label} | {symbol} {direction} | accepted signal; no paper order/fill "
        f"| {stop_target} | net P&L N/A"
    )


def _capped_digest_message(
    *,
    prefix: list[str],
    lifecycle_lines: list[str],
    suffix: list[str],
    artifact_reference: str,
) -> tuple[str, int, bool]:
    full = "\n".join([*prefix, *(lifecycle_lines or ["• No active lifecycle events."]), *suffix])
    if len(full) <= _TELEGRAM_SAFE_CHARS:
        return full, len(lifecycle_lines), False

    selected: list[str] = []
    for line in lifecycle_lines:
        next_selected = [*selected, line]
        omitted = len(lifecycle_lines) - len(next_selected)
        truncation = [
            f"… {omitted} lifecycle row(s) omitted from Telegram.",
            f"Full exact lifecycle artifact: {artifact_reference}",
        ]
        candidate = "\n".join([*prefix, *next_selected, *truncation, *suffix])
        if len(candidate) > _TELEGRAM_SAFE_CHARS:
            break
        selected = next_selected
    omitted = len(lifecycle_lines) - len(selected)
    message = "\n".join(
        [
            *prefix,
            *selected,
            f"… {omitted} lifecycle row(s) omitted from Telegram.",
            f"Full exact lifecycle artifact: {artifact_reference}",
            *suffix,
        ]
    )
    if len(message) > _TELEGRAM_SAFE_CHARS:
        raise NotificationError(
            "PaperOps fleet digest fixed content exceeds the safe Telegram limit."
        )
    return message, len(selected), True


def _resolve_live_paperops_lineage(
    *,
    root: Path,
    market_date: str,
    expected_ids: tuple[str, ...],
    paper_by_id: Mapping[str, Mapping[str, Any]],
    paper_config: Mapping[str, Any],
    blockers: list[str],
) -> dict[str, Any]:
    """Bind report rows to the currently active registry and policy manifest."""

    registry_path = root / "state" / "strategy_registry.json"
    registry_raw = _load_list(registry_path, "live strategy registry", blockers)
    registry_rows = [_mapping(row) for row in registry_raw if isinstance(row, dict)]
    registry_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in registry_rows:
        registry_groups[str(row.get("strategy_id") or "")].append(row)
    registry_by_id: dict[str, dict[str, Any]] = {}
    for strategy_id, rows in registry_groups.items():
        if not strategy_id:
            blockers.append("live strategy registry contains a blank strategy identity")
        elif len(rows) != 1:
            blockers.append(
                f"live strategy registry has {len(rows)} rows for {strategy_id}"
            )
        else:
            registry_by_id[strategy_id] = rows[0]
    if set(registry_by_id) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(registry_by_id))
        extra = sorted(set(registry_by_id) - set(expected_ids))
        if missing:
            blockers.append("live strategy registry is missing: " + ", ".join(missing))
        if extra:
            blockers.append("live strategy registry has unexpected rows: " + ", ".join(extra))

    for strategy_id in sorted(set(expected_ids) & set(registry_by_id) & set(paper_by_id)):
        registry_row = registry_by_id[strategy_id]
        report_row = paper_by_id[strategy_id]
        registry_version = str(registry_row.get("strategy_version") or "")
        registry_policy = str(registry_row.get("execution_policy_version") or "")
        if not registry_version:
            blockers.append(f"{strategy_id} live registry version is missing")
        if not registry_policy:
            blockers.append(f"{strategy_id} live registry execution policy is missing")
        if str(report_row.get("strategy_version") or "") != registry_version:
            blockers.append(f"{strategy_id} does not match the live strategy registry version")
        if str(report_row.get("execution_policy_version") or "") != registry_policy:
            blockers.append(
                f"{strategy_id} does not match the live registry execution policy"
            )

    catalog_groups: dict[str, list[Any]] = defaultdict(list)
    for strategy in build_strategy_catalog():
        catalog_groups[str(strategy.strategy_id)].append(strategy)
    catalog_by_id: dict[str, Any] = {}
    for strategy_id, matches in catalog_groups.items():
        if len(matches) != 1:
            blockers.append(
                f"current strategy catalog has {len(matches)} implementations for {strategy_id}"
            )
        else:
            catalog_by_id[strategy_id] = matches[0]
    if not set(expected_ids).issubset(catalog_by_id):
        blockers.append("current strategy catalog does not match the official PaperOps fleet")

    semantics_manifest_path = root / "state" / "strategy_semantics_manifest.json"
    semantics_manifest = _load_mapping(
        semantics_manifest_path,
        "live strategy-semantics manifest",
        blockers,
    )
    if str(semantics_manifest.get("schema_version") or "") != (
        "v2.strategy_semantics_manifest.v1"
    ):
        blockers.append("live strategy-semantics manifest schema is missing or unsupported")
    semantic_entries = semantics_manifest.get("strategies")
    if not isinstance(semantic_entries, Mapping):
        blockers.append("live strategy-semantics manifest strategies must be an object")
        semantic_entries = {}
    strategy_semantics_fingerprints: dict[str, str] = {}
    for strategy_id in expected_ids:
        registry_row = registry_by_id.get(strategy_id, {})
        catalog_strategy = catalog_by_id.get(strategy_id)
        if not registry_row or catalog_strategy is None:
            continue
        registry_version = str(registry_row.get("strategy_version") or "")
        if str(catalog_strategy.version) != registry_version:
            blockers.append(
                f"{strategy_id} current implementation version does not match live registry"
            )
        current_strategy_configuration = _strategy_semantics_payload(catalog_strategy)
        current_semantic_fingerprint = _strategy_semantics_fingerprint(catalog_strategy)
        registry_semantic_fingerprint = str(
            registry_row.get("strategy_semantics_fingerprint") or ""
        )
        entry_key = f"{strategy_id}@{registry_version}"
        entry = _mapping(semantic_entries.get(entry_key))
        manifest_configuration = _mapping(entry.get("configuration"))
        manifest_fingerprint = str(entry.get("fingerprint") or "")
        if not entry:
            blockers.append(f"{strategy_id} strategy-semantics manifest entry is missing")
        if not _is_sha256(registry_semantic_fingerprint):
            blockers.append(
                f"{strategy_id} registry semantics fingerprint is not bounded SHA-256"
            )
        if not _is_sha256(manifest_fingerprint):
            blockers.append(
                f"{strategy_id} manifest semantics fingerprint is not bounded SHA-256"
            )
        elif manifest_fingerprint != _payload_sha256(manifest_configuration):
            blockers.append(
                f"{strategy_id} manifest semantics fingerprint does not match configuration"
            )
        if manifest_configuration != current_strategy_configuration:
            blockers.append(
                f"{strategy_id} current implementation semantics do not match manifest"
            )
        if manifest_fingerprint != current_semantic_fingerprint:
            blockers.append(
                f"{strategy_id} current implementation fingerprint does not match manifest"
            )
        if registry_semantic_fingerprint != current_semantic_fingerprint:
            blockers.append(
                f"{strategy_id} registry semantics fingerprint does not match current code"
            )
        strategy_semantics_fingerprints[strategy_id] = current_semantic_fingerprint

    policy_manifest_path = root / "state" / "execution_policy_manifest.json"
    manifest = _load_mapping(
        policy_manifest_path,
        "live execution-policy manifest",
        blockers,
    )
    active_policy = str(manifest.get("active_execution_policy_version") or "")
    if str(manifest.get("schema_version") or "") != (
        "v2.paper_execution_policy_manifest.v1"
    ):
        blockers.append("live execution-policy manifest schema is missing or unsupported")
    if not active_policy:
        blockers.append("live execution-policy manifest has no active policy")
    config_policy = str(paper_config.get("execution_policy_version") or "")
    if config_policy != active_policy:
        blockers.append("PaperOps config does not match the active execution-policy manifest")
    if active_policy and any(
        str(row.get("execution_policy_version") or "") != active_policy
        for row in registry_by_id.values()
    ):
        blockers.append("live strategy registry does not use the active execution policy")

    policies = manifest.get("policies")
    policy_entry = (
        _mapping(policies.get(active_policy))
        if isinstance(policies, Mapping) and active_policy
        else {}
    )
    configuration = _mapping(policy_entry.get("configuration"))
    active_fingerprint = str(policy_entry.get("fingerprint") or "")
    if not policy_entry:
        blockers.append("active execution-policy manifest entry is missing")
    if not configuration:
        blockers.append("active execution-policy configuration identity is missing")
    expected_fingerprint = _payload_sha256(configuration) if configuration else ""
    if not active_fingerprint:
        blockers.append("active execution-policy fingerprint is missing")
    elif not _is_sha256(active_fingerprint):
        blockers.append("active execution-policy fingerprint is not bounded SHA-256")
    elif active_fingerprint != expected_fingerprint:
        blockers.append("active execution-policy fingerprint does not match its configuration")

    current_configuration: dict[str, object] = {}
    try:
        current_config = _config_from_payload(dict(paper_config))
        current_configuration = _execution_policy_fingerprint_payload(current_config)
    except (TypeError, ValueError) as exc:
        blockers.append(f"current PaperOps config semantics are invalid: {_safe_error(exc)}")
    current_fingerprint = (
        _payload_sha256(current_configuration) if current_configuration else ""
    )
    if current_configuration and configuration != current_configuration:
        blockers.append(
            "current PaperOps config semantics do not match the active "
            "execution-policy manifest"
        )
    if current_fingerprint and active_fingerprint != current_fingerprint:
        blockers.append(
            "active execution-policy fingerprint does not match current "
            "PaperOps config semantics"
        )
    implementation = str(configuration.get("engine_policy_implementation") or "")
    if implementation != PAPER_EXECUTION_POLICY_VERSION:
        blockers.append(
            "active execution-policy implementation identity does not match the engine"
        )

    run_date = date.fromisoformat(market_date)
    paths = PaperOpsPaths.create(root)
    strategy_activation_by_id: dict[str, dict[str, Any]] = {}
    for strategy_id in expected_ids:
        registry_row = registry_by_id.get(strategy_id, {})
        if not registry_row:
            continue
        strategy_version = str(registry_row.get("strategy_version") or "")
        execution_policy_version = str(
            registry_row.get("execution_policy_version") or ""
        )
        semantics_fingerprint = str(
            registry_row.get("strategy_semantics_fingerprint") or ""
        )
        try:
            inception = _strategy_coverage_inception(
                paths,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                execution_policy_version=execution_policy_version,
                strategy_semantics_fingerprint=semantics_fingerprint,
            )
        except (OSError, TypeError, ValueError) as exc:
            blockers.append(
                f"{strategy_id} exact activation lineage is invalid: {_safe_error(exc)}"
            )
            continue
        strategy_activation_by_id[strategy_id] = {
            "coverage_inception_date": inception.isoformat(),
            "status": (
                "eligible"
                if run_date >= inception
                else "registered_not_yet_eligible"
            ),
        }

    strategy_identities = [
        {
            "strategy_id": strategy_id,
            "strategy_version": registry_by_id[strategy_id].get("strategy_version"),
            "execution_policy_version": registry_by_id[strategy_id].get(
                "execution_policy_version"
            ),
            "strategy_semantics_fingerprint": strategy_semantics_fingerprints.get(
                strategy_id
            ),
        }
        for strategy_id in expected_ids
        if strategy_id in registry_by_id
    ]
    return {
        "active_execution_policy_version": active_policy or None,
        "active_policy_fingerprint": active_fingerprint or None,
        "current_config_policy_fingerprint": current_fingerprint or None,
        "current_execution_configuration": current_configuration,
        "current_config_semantic_sha256": (
            _payload_sha256(current_configuration) if current_configuration else None
        ),
        "strategy_semantics_fingerprints": strategy_semantics_fingerprints,
        "strategy_activation_by_id": strategy_activation_by_id,
        "strategy_identities": strategy_identities,
        "strategy_registry_sha256": _payload_sha256(registry_raw),
        "strategy_semantics_manifest_sha256": _payload_sha256(semantics_manifest),
        "execution_policy_manifest_sha256": _payload_sha256(manifest),
    }


def _resolve_alpha_truth(
    *,
    market_date: str,
    db_path: Path,
    contract_path: Path,
    alpha_rows: list[dict[str, Any]],
    alpha_source: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve Alpha independently so a proven no-signal day needs no fake row."""

    source_status = str(alpha_source.get("status") or "missing")
    if len(alpha_rows) > 1:
        return _alpha_truth_result(
            status="conflicting",
            line="Alpha truth is conflicting; return N/A was not inferred.",
            blockers=["AlphaOps has conflicting official scorecard rows for the market date"],
        )
    if alpha_rows:
        row = alpha_rows[0]
        blockers: list[str] = []
        source_path = str(alpha_source.get("path") or "").strip()
        if not source_path or not _same_path(Path(source_path), db_path):
            blockers.append(
                "AlphaOps complete source does not reference the configured SQLite database"
            )
        if source_status != "complete":
            blockers.append(
                "AlphaOps official scorecard exists but its source is not complete"
            )
        if str(row.get("evidence_status") or "") != "complete":
            blockers.append("AlphaOps official scorecard reconciliation is partial")
        observed = bool(row.get("return_observed"))
        value = _optional_number(row.get("normalized_daily_return_pct"))
        if observed and value is None:
            blockers.append("AlphaOps marks a return observed but its value is missing")
        if not observed and value is not None:
            blockers.append("AlphaOps carries a return value without observed-return truth")
        if _integer(row.get("unresolved_count")):
            blockers.append("AlphaOps official scorecard has unresolved outcomes")
        return _alpha_truth_result(
            status="scorecard_complete" if not blockers else "partial",
            line=_alpha_line(row) if not blockers else "Alpha evidence is partial; return N/A.",
            blockers=blockers,
            evidence={"source": "fleet_official_scorecard", "row": row},
        )

    if source_status != "empty":
        return _alpha_truth_result(
            status="missing",
            line="Alpha truth is unavailable; this is not a no-pick result.",
            blockers=[
                "AlphaOps official truth is missing; absence was not interpreted as no-pick"
            ],
        )
    source_path = str(alpha_source.get("path") or "").strip()
    if not source_path or not _same_path(Path(source_path), db_path):
        return _alpha_truth_result(
            status="conflicting",
            line="Alpha source identity is unavailable; this is not a no-pick result.",
            blockers=["AlphaOps empty source does not reference the configured SQLite database"],
        )

    contract_blockers: list[str] = []
    contract = _load_mapping(contract_path, "AlphaOps run contract", contract_blockers)
    if not contract:
        return _alpha_truth_result(
            status="missing",
            line="Alpha truth is unavailable; this is not a no-pick result.",
            blockers=[
                *contract_blockers,
                "AlphaOps no-pick contract is missing; absence was not interpreted as no-pick",
            ],
        )
    expected_contract = {
        "broker_execution": "disabled",
        "market_date": market_date,
        "notification_dry_run": False,
        "producer": "alphaops",
        "research_only": True,
        "schema_version": "alphaops.run_contract.v1",
        "selection_outcome": "valid_no_edge",
    }
    for field, expected in expected_contract.items():
        if contract.get(field) != expected:
            contract_blockers.append(
                f"AlphaOps no-pick contract {field} is not {expected!r}"
            )
    if str(contract.get("source_status") or "") not in {"success", "ok"}:
        contract_blockers.append("AlphaOps no-pick source did not complete successfully")
    if contract.get("alertable_count") in {None, ""}:
        contract_blockers.append("AlphaOps no-pick contract has no alertable-count truth")
    elif _integer(contract.get("alertable_count")) != 0:
        contract_blockers.append("AlphaOps no-pick contract has alertable selections")
    channels = {
        value.strip().lower()
        for value in str(contract.get("notification_channel") or "").split(",")
        if value.strip()
    }
    if "telegram" not in channels:
        contract_blockers.append("AlphaOps no-pick contract is not the Telegram cohort")
    if str(contract.get("notification_status") or "") not in {
        "delivery_recorded",
        "deduplicated",
    }:
        contract_blockers.append("AlphaOps no-pick Telegram delivery is not confirmed")
    scan_id = str(contract.get("producer_run_id") or "").strip()
    if not scan_id:
        contract_blockers.append("AlphaOps no-pick contract has no producer run identity")

    selections, selection_error = _load_alpha_run_selections(db_path, scan_id)
    if selection_error:
        contract_blockers.append(selection_error)
    if not selections:
        contract_blockers.append(
            "AlphaOps no-pick run has no exact official NO_TRADE selection identity"
        )
    non_no_signal = [
        row
        for row in selections
        if str(row.get("decision") or "").lower() != "no_trade"
        or str(row.get("ticker") or "").upper() != "NO_TRADE"
        or str(row.get("cohort") or "") != "official_telegram"
        or str(row.get("strategy_id") or "") != "alphaops_v4"
    ]
    if non_no_signal:
        contract_blockers.append(
            "AlphaOps no-pick contract conflicts with selected-signal identities"
        )
    if len(selections) > 1:
        contract_blockers.append(
            "AlphaOps no-pick run has duplicate official selection identities"
        )
    if any(str(row.get("selected_at") or "")[:10] != market_date for row in selections):
        contract_blockers.append("AlphaOps no-pick selection date conflicts with the run")
    if contract_blockers:
        return _alpha_truth_result(
            status="conflicting",
            line="Alpha evidence is conflicting; this is not a no-pick result.",
            blockers=contract_blockers,
            evidence={"contract": contract, "selection_count": len(selections)},
        )
    return _alpha_truth_result(
        status="sourced_no_signal_scorecard_unavailable",
        line=(
            "Source-complete no_signal run; official scorecard unavailable. "
            "Return N/A (not 0%)."
        ),
        blockers=[],
        optional=True,
        evidence={
            "source": "alphaops_run_contract",
            "producer_run_id": scan_id,
            "contract_sha256": hashlib.sha256(
                json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "model_version": contract.get("model_version"),
            "source_status": contract.get("source_status"),
            "selection_basis": "explicit_no_trade_identity",
            "selection_count": len(selections),
            "selection_identities": [
                {
                    "selection_id": row.get("selection_id"),
                    "signal_id": row.get("signal_id"),
                    "event_key": row.get("event_key"),
                    "body_sha256": row.get("body_sha256"),
                    "ticker": row.get("ticker"),
                    "decision": row.get("decision"),
                }
                for row in selections
            ],
            "selection_outcome": contract.get("selection_outcome"),
            "notification_channel": contract.get("notification_channel"),
            "notification_status": contract.get("notification_status"),
        },
    )


def _load_alpha_run_selections(
    db_path: Path, scan_id: str
) -> tuple[list[dict[str, Any]], str | None]:
    if not db_path.is_file():
        return [], f"AlphaOps SQLite database is absent: {db_path}"
    try:
        connection = sqlite3.connect(
            f"file:{db_path.resolve().as_posix()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("signal_selections",),
        ).fetchone()
        if table is None:
            return [], "AlphaOps exact selection table is missing"
        rows = connection.execute(
            "SELECT * FROM signal_selections WHERE scan_id = ? ORDER BY selected_at, rank",
            (scan_id,),
        ).fetchall()
        return [dict(row) for row in rows], None
    except sqlite3.Error as exc:
        return [], f"AlphaOps exact selections are unreadable: {_safe_error(exc)}"
    finally:
        if "connection" in locals():
            connection.close()


def _alpha_truth_result(
    *,
    status: str,
    line: str,
    blockers: list[str],
    optional: bool = False,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "line": line,
        "blockers": blockers,
        "optional": optional,
        "evidence": dict(evidence or {}),
    }


def _alpha_line(row: Mapping[str, Any]) -> str:
    observed = bool(row.get("return_observed"))
    value = _optional_number(row.get("normalized_daily_return_pct"))
    opened = _integer(row.get("trades_opened"))
    closed = _integer(row.get("trades_closed"))
    unresolved = _integer(row.get("unresolved_count"))
    if observed and value is not None:
        benchmark = _optional_number(row.get("benchmark_return_pct"))
        return (
            f"Observed after-cost paper return {_pct_or_na(value)} | "
            f"opened {opened}, closed {closed} | SPY {_pct_or_na(benchmark)}"
        )
    if unresolved:
        return f"{unresolved} unresolved paper outcome(s); return N/A (not 0%)."
    if not opened and not closed:
        return "No triggered/closed official paper trade; return N/A (not 0%)."
    return f"Opened {opened}, closed {closed}; eligible return remains N/A (not 0%)."


def _load_calendar_rows(path: Path, market_date: str, blockers: list[str]) -> list[dict[str, Any]]:
    if not path.is_file():
        blockers.append(f"canonical PaperOps calendar is absent: {path}")
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error) as exc:
        blockers.append(f"canonical PaperOps calendar is unreadable: {_safe_error(exc)}")
        return []
    return [
        dict(row)
        for row in rows
        if str(row.get("date") or "")[:10] == market_date
        and str(row.get("mode") or "") == "forward"
        and str(row.get("strategy_id") or "")
        not in {CASH_BASELINE_ID, "benchmark_buy_hold_equal_weight"}
    ]


def _unique_by_strategy(
    rows: list[dict[str, Any]], blockers: list[str], source: str
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("strategy_id") or "")].append(row)
    result: dict[str, dict[str, Any]] = {}
    for strategy_id, matches in grouped.items():
        if not strategy_id:
            blockers.append(f"{source} contains a blank strategy id")
        elif len(matches) != 1:
            blockers.append(f"{source} has {len(matches)} rows for {strategy_id}")
        else:
            result[strategy_id] = matches[0]
    return result


def _consistent_optional_number(
    rows: list[dict[str, Any]],
    field: str,
    blockers: list[str],
    label: str,
) -> float | None:
    values = [_optional_number(row.get(field)) for row in rows]
    observed = {round(value, 10) for value in values if value is not None}
    if len(observed) > 1:
        blockers.append(f"conflicting {label} returns in same-day fleet rows")
    if any(value is None for value in values) and observed:
        blockers.append(f"partial {label} comparison evidence in same-day fleet rows")
    return next(iter(observed)) if len(observed) == 1 else None


def _outbox_record(
    path: Path, digest: Mapping[str, Any], channel: str
) -> dict[str, Any]:
    try:
        loaded = read_json(path, {})
    except (OSError, json.JSONDecodeError) as exc:
        raise NotificationError(
            f"PaperOps fleet outbox is unreadable: {_safe_error(exc)}"
        ) from exc
    record = dict(loaded) if isinstance(loaded, dict) else {}
    if record and str(record.get("event_key")) != str(digest["event_key"]):
        raise NotificationError(f"PaperOps fleet outbox identity mismatch: {path}")
    if record:
        return record
    return {
        "schema_version": "dawnstrike.paperops_fleet_digest_outbox.v1",
        "status": "pending",
        "attempt_count": 0,
        "created_at": utc_now_iso(),
        "channel": channel,
        "event_key": digest["event_key"],
        "market_date": digest["market_date"],
        "evidence_fingerprint": digest["evidence_fingerprint"],
        "lifecycle_artifact_path": digest["lifecycle_artifact_path"],
        "message": digest["message"],
        "research_only": True,
        "receipt_key": None,
        "last_error": None,
        "last_attempted_at": None,
        "delivered_at": None,
    }


def _delivery_result(
    digest: Mapping[str, Any],
    outbox_path: Path,
    record: Mapping[str, Any],
    *,
    sent: int,
    skipped: int,
) -> dict[str, Any]:
    return {
        "schema_version": "dawnstrike.paperops_fleet_digest_delivery.v1",
        "status": "complete",
        "market_date": digest["market_date"],
        "event_key": digest["event_key"],
        "evidence_fingerprint": digest["evidence_fingerprint"],
        "research_only": True,
        "notification_stats": {"sent": sent, "skipped": skipped},
        "attempt_count": _integer(record.get("attempt_count")),
        "outbox_path": str(outbox_path),
        "lifecycle_artifact_path": digest["lifecycle_artifact_path"],
        "message": digest["message"],
    }


def _blocked_payload(
    market_date: str,
    root: Path,
    report_path: Path,
    blockers: list[str],
) -> dict[str, Any]:
    reasons = sorted(set(blockers or ["fleet evidence is unavailable"]))
    message = "\n".join(
        [
            f"📊 Dawnstrike Paper Fleet — {market_date}",
            "Evidence INCOMPLETE — notification blocked; no return was inferred.",
            *(f"• {reason}" for reason in reasons),
            "Replay remains excluded from forward results.",
            "Research/paper only. No broker orders.",
        ]
    )
    return {
        "schema_version": "dawnstrike.paperops_fleet_digest.v1",
        "status": "blocked_incomplete_evidence",
        "ready": False,
        "market_date": market_date,
        "research_only": True,
        "paper_ops_root": str(root),
        "fleet_report_path": str(report_path),
        "event_key": None,
        "evidence_fingerprint": None,
        "message": message,
        "blockers": reasons,
        "summary": {},
    }


def _load_mapping(path: Path, label: str, blockers: list[str]) -> dict[str, Any]:
    if not path.is_file():
        blockers.append(f"{label} is absent: {path}")
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        blockers.append(f"{label} is unreadable: {_safe_error(exc)}")
        return {}
    if not isinstance(payload, dict):
        blockers.append(f"{label} must contain a JSON object")
        return {}
    return dict(payload)


def _load_list(path: Path, label: str, blockers: list[str]) -> list[Any]:
    if not path.is_file():
        blockers.append(f"{label} are absent: {path}")
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        blockers.append(f"{label} are unreadable: {_safe_error(exc)}")
        return []
    if not isinstance(payload, list):
        blockers.append(f"{label} must contain a JSON array")
        return []
    return payload


def _load_ledger_events(path: Path, blockers: list[str]) -> list[dict[str, Any]]:
    if not path.is_file():
        blockers.append(f"canonical forward PaperOps ledger is absent: {path}")
        return []
    try:
        rows = read_jsonl(path)
    except (OSError, ValueError) as exc:
        blockers.append(f"canonical forward PaperOps ledger is unreadable: {_safe_error(exc)}")
        return []
    if not rows:
        blockers.append("canonical forward PaperOps ledger contains no events")
        return []
    event_id_counts: Counter[str] = Counter()
    result: list[dict[str, Any]] = []
    for index, raw_row in enumerate(rows, start=1):
        row = dict(raw_row)
        raw_event_id = row.get("event_id")
        event_id = raw_event_id.strip() if isinstance(raw_event_id, str) else ""
        if not isinstance(raw_event_id, str):
            blockers.append(
                f"canonical PaperOps ledger row {index} event_id must be a string"
            )
        elif not event_id:
            blockers.append(f"canonical PaperOps ledger row {index} has blank event_id")
        else:
            event_id_counts[event_id] += 1
            if raw_event_id != event_id:
                blockers.append(
                    f"canonical PaperOps ledger row {index} event_id has surrounding whitespace"
                )
        for field in ("event_type", "run_id", "mode", "trade_date", "strategy_id", "symbol"):
            if not str(row.get(field) or "").strip():
                blockers.append(
                    f"canonical PaperOps ledger row {index} has blank envelope {field}"
                )
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            blockers.append(
                f"canonical PaperOps ledger row {index} payload must be a JSON object"
            )
            result.append(row)
            continue
        for field in ("strategy_id", "symbol"):
            if str(payload.get(field) or "") != str(row.get(field) or ""):
                blockers.append(
                    f"canonical PaperOps ledger row {index} envelope/payload {field} mismatch"
                )
        if "mode" in payload and str(payload.get("mode") or "") != str(
            row.get("mode") or ""
        ):
            blockers.append(
                f"canonical PaperOps ledger row {index} envelope/payload mode mismatch"
            )
        event_type = str(row.get("event_type") or "")
        if event_type in {
            "paper_order_blocked",
            "paper_order_pending_no_fill_data",
        }:
            payload_run_id = str(payload.get("run_id") or "")
            origin_run_id = str(payload.get("origin_run_id") or "")
            lifecycle_run_id = str(payload.get("lifecycle_run_id") or "")
            if not payload_run_id:
                blockers.append(
                    f"canonical PaperOps ledger row {index} has blank payload run_id"
                )
            if not origin_run_id:
                blockers.append(
                    f"canonical PaperOps ledger row {index} has blank payload origin_run_id"
                )
            elif origin_run_id != payload_run_id:
                blockers.append(
                    "canonical PaperOps ledger row "
                    f"{index} payload origin_run_id does not match payload run_id"
                )
            if "lifecycle_run_id" in payload:
                if not lifecycle_run_id:
                    blockers.append(
                        "canonical PaperOps ledger row "
                        f"{index} has blank payload lifecycle_run_id"
                    )
                elif lifecycle_run_id != str(row.get("run_id") or ""):
                    blockers.append(
                        "canonical PaperOps ledger row "
                        f"{index} envelope/payload lifecycle_run_id mismatch"
                    )
        else:
            for field in ("run_id", "trade_date"):
                if field in payload and str(payload.get(field) or "") != str(
                    row.get(field) or ""
                ):
                    blockers.append(
                        "canonical PaperOps ledger row "
                        f"{index} envelope/payload {field} mismatch"
                    )
        result.append(row)
    blockers.extend(
        f"canonical PaperOps ledger has duplicate event_id {event_id} ({count} rows)"
        for event_id, count in sorted(event_id_counts.items())
        if count > 1
    )
    return result


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve(strict=False) == right.resolve(strict=False)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _payload_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _is_sha256(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _market_date(value: str) -> str:
    normalized = str(value).strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        raise NotificationError("PaperOps fleet digest requires market date YYYY-MM-DD.")
    return normalized


def _optional_number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value: Any) -> int:
    if value in {None, ""}:
        return 0
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise NotificationError("PaperOps integer evidence is invalid.") from exc
    if not math.isfinite(parsed) or not parsed.is_integer():
        raise NotificationError("PaperOps integer evidence must be a finite whole number.")
    return int(parsed)


def _pct_or_na(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.2f}%"


def _price_or_na(value: Any) -> str:
    number = _optional_number(value)
    return "N/A" if number is None else f"${number:,.2f}"


def _quantity_or_na(value: Any) -> str:
    number = _integer(value)
    return "N/A" if number <= 0 else str(number)


def _money_or_na(value: Any) -> str:
    number = _optional_number(value)
    return "N/A" if number is None else f"${number:+,.2f}"


def _safe_error(exc: object) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(?i)(bot\d+:)[A-Za-z0-9_-]+", r"\1[redacted]", text)
    text = re.sub(
        r"(?i)((?:telegram_)?(?:bot_)?token\s*[:=]\s*)\S+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)((?:telegram_)?chat_id\s*[:=]\s*)-?\d+",
        r"\1[redacted]",
        text,
    )
    return text[:500]
