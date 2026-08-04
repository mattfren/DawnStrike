"""Truth-safe public calendar built only from canonical performance rows."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.market_calendar import MarketSessionStatus, market_session
from intraday_scanner.performance.contracts import safe_float, stable_hash
from intraday_scanner.performance.service import CanonicalPerformanceService
from intraday_scanner.sql_safety import quote_sql_identifier
from intraday_scanner.storage.migrations import run_migrations

MAX_CALENDAR_BYTES = 500 * 1024
DISPLAY_STATUSES = frozenset(
    {
        "COMPLETE",
        "NO_TRADE",
        "PARTIAL",
        "PENDING",
        "MISSING",
        "UNAVAILABLE",
        "UNREALIZED",
    }
)


def write_public_calendar(
    db_path: str | Path,
    output_path: str | Path,
    *,
    market_date: str | None = None,
    days: int = 400,
    row_limit: int = 2_000,
    canonical_input_hash_sha256: str | None = None,
    performance_payload_sha256: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Write one bounded calendar and manifest with atomic per-file replacement."""

    performance = CanonicalPerformanceService(db_path).load_public_data(
        days=max(1, days),
        row_limit=max(1, row_limit),
        market_date=market_date,
        generated_at=generated_at,
    )
    selection_context = _load_selection_context(Path(db_path), market_date=market_date)
    canonical_hash = canonical_input_hash_sha256 or stable_hash(
        {
            "daily": performance.get("daily") or [],
            "rows": performance.get("rows") or [],
            "accounts": performance.get("accounts") or [],
            "account_ledger": performance.get("account_ledger") or [],
        }
    )
    payload = build_calendar_payload(
        performance,
        as_of_market_date=market_date,
        selection_context=selection_context,
        canonical_input_hash_sha256=canonical_hash,
        performance_payload_sha256=performance_payload_sha256,
        generated_at=generated_at,
    )
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    compressed_bytes = len(gzip.compress(encoded, compresslevel=9, mtime=0))
    if compressed_bytes > MAX_CALENDAR_BYTES:
        raise ValueError(f"Public calendar exceeds {MAX_CALENDAR_BYTES} compressed bytes")

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, encoded)
    payload_sha = hashlib.sha256(encoded).hexdigest()
    effective_date = str(payload.get("as_of_market_date") or market_date or "unknown")
    generated_at = generated_at or _utc_now()
    manifest = {
        "schema_version": "dawnstrike.public_calendar_manifest.v1",
        "manifest_id": hashlib.sha256(
            f"{effective_date}:{canonical_hash}:{payload_sha}".encode()
        ).hexdigest(),
        "market_date": effective_date,
        "status": _calendar_status(payload, effective_date),
        "generated_at": generated_at,
        "canonical_input_hash_sha256": canonical_hash,
        "performance_payload_sha256": performance_payload_sha256,
        "payload_sha256": payload_sha,
        "artifact_path": path.name,
        "day_count": len(payload.get("days") or []),
        "record_count": sum(len(day.get("records") or []) for day in payload.get("days") or []),
        "byte_count": len(encoded),
        "compressed_byte_count": compressed_bytes,
        "compression": "gzip",
        "research_only": True,
        "live_trading_enabled": False,
    }
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    manifest_encoded = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
    _atomic_write(manifest_path, manifest_encoded)
    with sqlite3.connect(Path(db_path)) as connection:
        run_migrations(connection)
        values = (
            manifest["manifest_id"],
            effective_date,
            manifest["status"],
            generated_at,
            canonical_hash,
            payload_sha,
            manifest["artifact_path"],
            manifest["day_count"],
            manifest["byte_count"],
            json.dumps(manifest, sort_keys=True),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO public_calendar_versions
            (manifest_id, market_date, status, generated_at,
             canonical_input_hash_sha256, payload_sha256, artifact_path,
             day_count, byte_count, failure_reason, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            values,
        )
        connection.execute(
            """
            INSERT INTO public_calendar_manifests
            (manifest_id, market_date, status, generated_at,
             canonical_input_hash_sha256, payload_sha256, artifact_path,
             day_count, byte_count, failure_reason, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(market_date) DO UPDATE SET
                manifest_id = excluded.manifest_id,
                status = excluded.status,
                generated_at = excluded.generated_at,
                canonical_input_hash_sha256 = excluded.canonical_input_hash_sha256,
                payload_sha256 = excluded.payload_sha256,
                artifact_path = excluded.artifact_path,
                day_count = excluded.day_count,
                byte_count = excluded.byte_count,
                failure_reason = NULL,
                payload_json = excluded.payload_json
            """,
            values,
        )
    return {
        "manifest": manifest,
        "calendar_path": str(path),
        "manifest_path": str(manifest_path),
    }


def build_calendar_payload(
    performance: dict[str, Any],
    *,
    as_of_market_date: str | None = None,
    selection_context: dict[str, dict[str, Any]] | None = None,
    canonical_input_hash_sha256: str | None = None,
    performance_payload_sha256: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Map canonical daily rows into filterable day and month records."""

    daily = [dict(row) for row in performance.get("daily") or [] if isinstance(row, dict)]
    detail_rows = [dict(row) for row in performance.get("rows") or [] if isinstance(row, dict)]
    ledger = [dict(row) for row in performance.get("account_ledger") or [] if isinstance(row, dict)]
    contexts = selection_context or {}
    as_of = _parse_date(
        as_of_market_date
        or performance.get("as_of_market_date")
        or max((str(row.get("market_date") or "") for row in daily), default="")
    )
    if as_of is None:
        as_of = datetime.now(timezone.utc).date()
    observed_dates = [
        parsed
        for row in daily
        if (parsed := _parse_date(row.get("market_date"))) is not None and parsed <= as_of
    ]
    earliest = min(observed_dates, default=as_of.replace(day=1))
    start = max(earliest, as_of - timedelta(days=399)).replace(day=1)
    rows_by_identity: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in detail_rows:
        rows_by_identity[_identity(row)].append(row)
    ledger_by_account_day = {
        (str(row.get("account_id") or ""), str(row.get("market_date") or "")): row for row in ledger
    }
    daily_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in daily:
        day = str(row.get("market_date") or "")
        if day:
            daily_by_day[day].append(row)

    days: list[dict[str, Any]] = []
    current = start
    while current <= as_of:
        day = current.isoformat()
        session = market_session(current)
        records = [
            _calendar_record(
                row,
                detail_rows=rows_by_identity.get(_identity(row), []),
                selection_context=contexts,
                ledger_row=ledger_by_account_day.get((str(row.get("account_id") or ""), day)),
            )
            for row in sorted(
                daily_by_day.get(day, []),
                key=lambda item: (
                    _cohort_priority(str(item.get("cohort") or "")),
                    str(item.get("strategy_id") or ""),
                    str(item.get("strategy_version") or ""),
                ),
            )
        ]
        market_open = session.status in {
            MarketSessionStatus.OPEN,
            MarketSessionStatus.EARLY_CLOSE,
        }
        primary = records[0] if records else None
        day_status = (
            str(primary["status"])
            if primary is not None
            else "MISSING"
            if market_open
            else "UNAVAILABLE"
        )
        days.append(
            {
                "date": day,
                "weekday": current.strftime("%A"),
                "month": day[:7],
                "market_session_status": session.status.value,
                "market_session_reason": session.reason,
                "market_open_time_et": session.open_time_et,
                "market_close_time_et": session.close_time_et,
                "calendar_id": session.calendar_id,
                "status": day_status,
                "observed": bool(records),
                "observed_zero": any(bool(record.get("observed_zero")) for record in records),
                "records": records,
            }
        )
        current += timedelta(days=1)

    months = _monthly_aggregates(days, performance.get("accounts") or [], as_of)
    identities = [
        {
            "cohort": record["cohort"],
            "strategy_id": record["strategy_id"],
            "strategy_version": record["strategy_version"],
            "execution_policy_version": record["execution_policy_version"],
            "account_id": record.get("account_id"),
        }
        for day in days
        for record in day["records"]
    ]
    canonical_hash = canonical_input_hash_sha256 or stable_hash(
        {"daily": daily, "rows": detail_rows, "ledger": ledger}
    )
    return {
        "schema_version": "dawnstrike.public_calendar.v1",
        "generated_at": generated_at or _utc_now(),
        "as_of_market_date": as_of.isoformat(),
        "timezone": "America/New_York",
        "canonical_input_hash_sha256": canonical_hash,
        "performance_payload_sha256": performance_payload_sha256,
        "research_only": True,
        "live_trading_enabled": False,
        "missing_is_zero": False,
        "return_contract": (
            "Daily values are copied from canonical performance. Monthly returns "
            "compound only eligible numeric daily values; null days are excluded "
            "and remain in the denominator."
        ),
        "status_definitions": {
            "COMPLETE": "Realized after-cost evidence and account basis are complete.",
            "NO_TRADE": "The account was observed and held no eligible position.",
            "PARTIAL": "Some evidence exists, but the day is not fully reconciled.",
            "PENDING": "An outcome or required account component is still pending.",
            "MISSING": "No eligible canonical observation exists for this market day.",
            "UNAVAILABLE": "The market was closed or evidence failed validation.",
            "UNREALIZED": "An open paper position lacks a realized outcome.",
        },
        "filters": {
            "cohorts": sorted({str(item["cohort"]) for item in identities}),
            "strategy_ids": sorted({str(item["strategy_id"]) for item in identities}),
            "strategy_versions": sorted({str(item["strategy_version"]) for item in identities}),
            "execution_policy_versions": sorted(
                {str(item["execution_policy_version"]) for item in identities}
            ),
            "accounts": sorted(
                {
                    str(item["account_id"])
                    for item in identities
                    if str(item.get("account_id") or "")
                }
            ),
        },
        "days": days,
        "months": months,
    }


def _calendar_record(
    row: dict[str, Any],
    *,
    detail_rows: list[dict[str, Any]],
    selection_context: dict[str, dict[str, Any]],
    ledger_row: dict[str, Any] | None,
) -> dict[str, Any]:
    status = _display_status(row)
    numeric_return = safe_float(row.get("return_pct"))
    numeric_gross = safe_float(row.get("gross_return_pct"))
    benchmark = safe_float(row.get("benchmark_return_pct"))
    excess = safe_float(row.get("excess_return_pct"))
    explicit_observed_zero = bool(
        ledger_row and ledger_row.get("observed_zero") is True and numeric_return == 0.0
    )
    if status == "NO_TRADE" and numeric_return == 0.0:
        explicit_observed_zero = True
    eligible = status in {"COMPLETE", "NO_TRADE"} and numeric_return is not None
    details = [
        _calendar_detail(detail, selection_context.get(str(detail.get("signal_id") or "")))
        for detail in detail_rows[:20]
    ]
    missing_reasons = _missing_reasons(row, status)
    return {
        "performance_id": row.get("performance_id"),
        "date": str(row.get("market_date") or ""),
        "cohort": str(row.get("cohort") or ""),
        "strategy_id": str(row.get("strategy_id") or ""),
        "strategy_version": str(row.get("strategy_version") or ""),
        "execution_policy_version": str(
            row.get("execution_policy_version") or "unregistered-policy"
        ),
        "account_id": row.get("account_id"),
        "status": status,
        "evidence_state": row.get("evidence_state"),
        "eligible_for_return": eligible,
        "observed_zero": explicit_observed_zero,
        "gross_return_pct": numeric_gross if eligible else None,
        "net_return_pct": numeric_return if eligible else None,
        "benchmark_return_pct": benchmark if eligible else None,
        "excess_return_pct": excess if eligible else None,
        "cumulative_return_pct": safe_float(row.get("cumulative_return_pct")),
        "drawdown_pct": safe_float(row.get("drawdown_pct")),
        "gross_pnl_cents": row.get("gross_pnl_cents"),
        "fees_cents": row.get("fees_cents"),
        "slippage_cents": row.get("slippage_cents"),
        "net_pnl_cents": row.get("net_pnl_cents"),
        "opening_equity_cents": row.get("opening_equity_cents"),
        "external_flow_cents": row.get("external_flow_cents"),
        "ending_equity_cents": row.get("ending_equity_cents"),
        "cash_cents": row.get("cash_cents"),
        "position_market_value_cents": row.get("position_market_value_cents"),
        "accounting_delta_cents": row.get("accounting_delta_cents"),
        "entries": int(row.get("realized_trade_count") or 0)
        + int(row.get("unrealized_trade_count") or 0),
        "exits": int(row.get("realized_trade_count") or 0),
        "open_positions": int(row.get("unrealized_trade_count") or 0),
        "no_trade_count": int(row.get("no_trade_count") or 0),
        "coverage": row.get("coverage") if isinstance(row.get("coverage"), dict) else {},
        "source_refs": list(row.get("source_refs") or []),
        "missing_reasons": missing_reasons,
        "details": details,
        "canonical_input_hash_sha256": row.get("input_hash_sha256"),
        "calculation_version": row.get("calculation_version"),
        "return_basis": row.get("return_basis"),
        "cost_status": row.get("cost_status"),
    }


def _calendar_detail(
    row: dict[str, Any],
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    context = context or {}
    return {
        "record_id": row.get("record_id"),
        "signal_id": row.get("signal_id"),
        "ticker": row.get("ticker"),
        "rank": row.get("rank"),
        "record_status": row.get("record_status"),
        "entry_price": safe_float(row.get("entry_price")),
        "exit_price": safe_float(row.get("exit_price")),
        "quantity": safe_float(row.get("quantity")),
        "notional_cents": row.get("notional_cents"),
        "gross_pnl_cents": row.get("gross_pnl_cents"),
        "fees_cents": row.get("fees_cents"),
        "slippage_cents": row.get("slippage_cents"),
        "net_pnl_cents": row.get("net_pnl_cents"),
        "net_return_pct": safe_float(row.get("return_pct")),
        "telegram_selection_tier": context.get("telegram_selection_tier"),
        "selection_decision": context.get("selection_decision"),
        "delivery_status": context.get("delivery_status"),
        "catalyst": context.get("catalyst"),
        "source_lineage": _unique(
            *(row.get("source_refs") or []),
            *(context.get("source_lineage") or []),
        ),
        "block_or_veto_reasons": list(context.get("block_or_veto_reasons") or []),
        "quarantine_reason": row.get("quarantine_reason"),
    }


def _monthly_aggregates(
    days: list[dict[str, Any]],
    accounts: Iterable[dict[str, Any]],
    as_of: date,
) -> list[dict[str, Any]]:
    account_activation = {
        str(row.get("account_id") or ""): _parse_date(row.get("activation_timestamp"))
        for row in accounts
        if isinstance(row, dict)
    }
    grouped: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    session_days: dict[str, list[date]] = defaultdict(list)
    for day in days:
        parsed = _parse_date(day.get("date"))
        if parsed is not None and day.get("market_session_status") in {
            MarketSessionStatus.OPEN.value,
            MarketSessionStatus.EARLY_CLOSE.value,
        }:
            session_days[str(day.get("month") or "")].append(parsed)
        for record in day.get("records") or []:
            key = (
                str(day.get("month") or ""),
                str(record.get("cohort") or ""),
                str(record.get("strategy_id") or ""),
                str(record.get("strategy_version") or ""),
                str(record.get("execution_policy_version") or ""),
                str(record.get("account_id") or ""),
            )
            grouped[key].append(record)
    output: list[dict[str, Any]] = []
    for key, records in sorted(grouped.items()):
        month, cohort, strategy_id, strategy_version, policy, account_id = key
        record_dates = [
            parsed for record in records if (parsed := _parse_date(record.get("date"))) is not None
        ]
        inception = account_activation.get(account_id) or min(record_dates, default=as_of)
        expected_dates = [
            day for day in session_days.get(month, []) if day >= inception and day <= as_of
        ]
        eligible = [
            record
            for record in records
            if record.get("eligible_for_return") is True
            and safe_float(record.get("net_return_pct")) is not None
        ]
        net_return = _compound(safe_float(record.get("net_return_pct")) for record in eligible)
        gross_return = _compound(safe_float(record.get("gross_return_pct")) for record in eligible)
        benchmark_return = _compound_complete(
            safe_float(record.get("benchmark_return_pct")) for record in eligible
        )
        expected_count = len(expected_dates)
        eligible_count = len(eligible)
        output.append(
            {
                "month": month,
                "cohort": cohort,
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "execution_policy_version": policy,
                "account_id": account_id or None,
                "status": (
                    "COMPLETE"
                    if expected_count > 0 and eligible_count == expected_count
                    else "PARTIAL"
                ),
                "eligible_day_count": eligible_count,
                "observed_day_count": len(records),
                "expected_market_day_count": expected_count,
                "missing_or_ineligible_day_count": max(expected_count - eligible_count, 0),
                "coverage_pct": (
                    round(eligible_count / expected_count * 100.0, 4) if expected_count else None
                ),
                "no_trade_day_count": sum(
                    1 for record in eligible if record.get("status") == "NO_TRADE"
                ),
                "net_return_pct": net_return,
                "gross_return_pct": gross_return,
                "benchmark_return_pct": benchmark_return,
                "excess_return_pct": (
                    round(net_return - benchmark_return, 4)
                    if net_return is not None and benchmark_return is not None
                    else None
                ),
                "net_pnl_cents": _sum_complete(record.get("net_pnl_cents") for record in eligible),
                "return_method": "compounded_eligible_daily_account_returns",
            }
        )
    return output


def _load_selection_context(
    db_path: Path,
    *,
    market_date: str | None,
) -> dict[str, dict[str, Any]]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        selections = _select_optional(connection, "signal_selections", market_date)
        deliveries = _select_optional(connection, "notification_delivery_memberships", market_date)
        signals = _select_optional(connection, "historical_signals", market_date)
    deliveries_by_signal = {
        str(row.get("signal_id") or ""): row
        for row in sorted(
            deliveries,
            key=lambda item: str(item.get("delivered_at") or item.get("attempted_at") or ""),
        )
    }
    signals_by_id = {str(row.get("signal_id") or ""): row for row in signals}
    output: dict[str, dict[str, Any]] = {}
    for selection in selections:
        signal_id = str(selection.get("signal_id") or "")
        selection_payload = _json_object(selection.get("payload_json"))
        signal = signals_by_id.get(signal_id, {})
        signal_payload = _json_object(signal.get("raw_payload_json") or signal.get("payload_json"))
        delivery = deliveries_by_signal.get(signal_id, {})
        delivery_payload = _json_object(delivery.get("payload_json"))
        combined = {**signal_payload, **selection_payload, **delivery_payload}
        combined_source_refs = (
            list(combined.get("source_refs") or [])
            if isinstance(combined.get("source_refs"), list)
            else []
        )
        output[signal_id] = {
            "telegram_selection_tier": _first_text(
                combined,
                "telegram_selection_tier",
                "selection_tier",
                "official_paper_eligibility_status",
                default=str(selection.get("cohort") or ""),
            ),
            "selection_decision": selection.get("decision"),
            "delivery_status": delivery.get("delivery_status"),
            "catalyst": _first_text(
                combined,
                "catalyst_summary",
                "catalyst",
                "catalyst_category",
                "catalyst_type",
            ),
            "source_lineage": _unique(
                *combined_source_refs,
                combined.get("source_url"),
                combined.get("source_bar_hash_sha256"),
            ),
            "block_or_veto_reasons": _reason_list(combined),
        }
    return output


def _select_optional(
    connection: sqlite3.Connection,
    table: str,
    market_date: str | None,
) -> list[dict[str, Any]]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    if not exists:
        return []
    table_sql = quote_sql_identifier(table)
    if market_date:
        try:
            # The table name is a validated SQLite identifier.
            rows = connection.execute(
                f"SELECT * FROM {table_sql} WHERE market_date <= ? ORDER BY rowid ASC",  # nosec B608
                (market_date,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = connection.execute(
                f"SELECT * FROM {table_sql} ORDER BY rowid ASC"  # nosec B608
            ).fetchall()
    else:
        rows = connection.execute(
            f"SELECT * FROM {table_sql} ORDER BY rowid ASC"  # nosec B608
        ).fetchall()
    return [dict(row) for row in rows]


def _display_status(row: dict[str, Any]) -> str:
    source = str(row.get("status") or "").upper()
    if source == "COMPLETE":
        status = "COMPLETE"
    elif source == "NO_TRADE":
        status = "NO_TRADE"
    elif source == "DEGRADED":
        status = "UNAVAILABLE"
    elif source == "PARTIAL" and int(row.get("unrealized_trade_count") or 0) > 0:
        status = "UNREALIZED"
    elif source == "PARTIAL":
        status = "PENDING"
    else:
        status = "MISSING"
    if status not in DISPLAY_STATUSES:  # pragma: no cover - defensive contract
        return "UNAVAILABLE"
    return status


def _missing_reasons(row: dict[str, Any], status: str) -> list[str]:
    reasons: list[str] = []
    if int(row.get("missing_outcome_count") or 0) > 0:
        reasons.append(f"{int(row['missing_outcome_count'])} outcome(s) missing")
    if int(row.get("unrealized_trade_count") or 0) > 0:
        reasons.append(f"{int(row['unrealized_trade_count'])} position(s) unrealized")
    if int(row.get("quarantined_count") or 0) > 0:
        reasons.append(f"{int(row['quarantined_count'])} record(s) quarantined")
    if safe_float(row.get("opening_equity_cents")) is None and status != "UNAVAILABLE":
        reasons.append("opening equity missing")
    if status in {"PENDING", "MISSING", "UNREALIZED", "UNAVAILABLE"} and not reasons:
        reasons.append("canonical return is not eligible")
    return reasons


def _identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("market_date") or ""),
        str(row.get("cohort") or ""),
        str(row.get("strategy_id") or ""),
        str(row.get("strategy_version") or ""),
    )


def _cohort_priority(value: str) -> int:
    return {
        "official_forward_paper": 0,
        "alphaops_signal_research": 1,
        "shadow_challenger": 2,
        "historical_backtest": 3,
    }.get(value, 4)


def _calendar_status(payload: dict[str, Any], effective_date: str) -> str:
    day = next(
        (
            item
            for item in payload.get("days") or []
            if str(item.get("date") or "") == effective_date
        ),
        None,
    )
    if not day or not day.get("records"):
        return "no_data"
    official = [
        record
        for record in day["records"]
        if str(record.get("cohort") or "") == "official_forward_paper"
    ]
    readiness_records = official or day["records"]
    statuses = {str(record.get("status") or "") for record in readiness_records}
    if statuses == {"NO_TRADE"}:
        return "no_trade"
    if statuses <= {"COMPLETE", "NO_TRADE"}:
        return "complete"
    return "degraded"


def _compound(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None and math.isfinite(value)]
    if not present:
        return None
    wealth = 1.0
    for value in present:
        wealth *= 1.0 + value / 100.0
    return round((wealth - 1.0) * 100.0, 4)


def _compound_complete(values: Iterable[float | None]) -> float | None:
    materialized = list(values)
    if not materialized or any(value is None for value in materialized):
        return None
    return _compound(materialized)


def _sum_complete(values: Iterable[object]) -> int | None:
    materialized = list(values)
    if not materialized or any(value is None for value in materialized):
        return None
    try:
        return sum(int(str(value)) for value in materialized)
    except (TypeError, ValueError):
        return None


def _reason_list(payload: dict[str, Any]) -> list[str]:
    output: list[str] = []
    for key in (
        "blocked_reasons",
        "block_reasons",
        "vetoes",
        "avoid_reasons",
        "risk_flags",
        "reasons",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            output.extend(str(item) for item in value if str(item).strip())
        elif isinstance(value, str) and value.strip():
            output.append(value.strip())
    no_trade_reason = str(payload.get("no_trade_reason") or "").strip()
    if no_trade_reason:
        output.append(no_trade_reason)
    return list(_unique(*output))


def _first_text(
    payload: dict[str, Any],
    *keys: str,
    default: str | None = None,
) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _unique(*values: object) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values if str(value or "").strip()}))


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
