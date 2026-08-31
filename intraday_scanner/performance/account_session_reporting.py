"""Read-only reporting for the canonical total-account/session ledger.

This module is deliberately a reporting boundary.  It never creates a ledger
row, converts a signal into a return, or treats an absent fact as zero.  The
daily finalizer and public snapshot can therefore expose the state of the
1%-target evidence without changing the existing publication gate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from intraday_scanner.errors import StorageError
from intraday_scanner.performance.account_contract import (
    ACCOUNT_SESSION_CONTRACT_VERSION,
)
from intraday_scanner.storage.read_only import connect_read_only

REPORT_SCHEMA_VERSION = "dawnstrike.account_session_report.v1"
RESEARCH_ONLY = True
BROKER_EXECUTION_ENABLED = False


def build_account_session_report(
    db_path: str | Path,
    *,
    market_date: str | None = None,
    account_id: str | None = None,
    window_days: int = 30,
    code_sha: str | None = None,
    experiment_id: str | None = None,
    arm_id: str | None = None,
) -> dict[str, Any]:
    """Return canonical account/session status and metrics without mutation.

    ``expected_market_sessions`` is the denominator.  Compound and geometric
    metrics are emitted only for a calendar-complete set of valid ledger rows.
    V5/V6 (and any other cohort) remain separate series; they are never
    combined into a synthetic account return.
    """

    if window_days <= 0:
        raise ValueError("window_days must be positive")
    path = Path(db_path)
    base = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "contract_version": ACCOUNT_SESSION_CONTRACT_VERSION,
        "market_date": market_date,
        "window_days": window_days,
        "account_id": account_id,
        "experiment_id": experiment_id,
        "arm_id": arm_id,
        "code_sha": str(code_sha or "unknown"),
        "research_only": RESEARCH_ONLY,
        "broker_execution_enabled": BROKER_EXECUTION_ENABLED,
    }
    if not path.exists():
        return {**base, **_empty_report("WAITING_FOR_CANONICAL_ACCOUNT_LEDGER")}
    try:
        with connect_read_only(path, row_factory=sqlite3.Row) as connection:
            expected = _expected_sessions(connection, market_date, window_days)
            ledger, unsafe_ledger_count = _ledger_rows(
                connection, market_date, window_days, account_id
            )
    except (OSError, StorageError, sqlite3.Error):
        return {**base, **_empty_report("WAITING_FOR_CANONICAL_ACCOUNT_LEDGER")}

    if not expected:
        return {
            **base,
            **_empty_report("INCOMPLETE_EXPECTED_SESSIONS"),
            "expected_session_count": 0,
            "expected_calendar_hash_sha256": _hash([]),
        }
    if not ledger:
        status = (
            "WAITING_FOR_AUTHENTICATED_FILL_TRUTH"
            if unsafe_ledger_count
            else "WAITING_FOR_CANONICAL_ACCOUNT_LEDGER"
        )
        return {
            **base,
            **_empty_report(status),
            "expected_session_count": len(expected),
            "expected_calendar_hash_sha256": _hash(expected),
            "unsafe_ledger_count": unsafe_ledger_count,
        }

    grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in ledger:
        if experiment_id is not None and str(row.get("experiment_id") or "") != experiment_id:
            continue
        if arm_id is not None and str(row.get("arm_id") or "") != arm_id:
            continue
        key = (
            _version_bucket(row),
            str(row.get("cohort") or ""),
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or ""),
            f"{row.get('experiment_id') or ''}:{row.get('arm_id') or ''}",
        )
        grouped[key].append(row)

    series: list[dict[str, Any]] = []
    for key, rows in sorted(grouped.items()):
        series.append(
            _series_report(
                rows,
                expected,
                account_id=account_id or str(rows[0].get("account_id") or ""),
                code_sha=code_sha,
                experiment_id=experiment_id,
                arm_id=arm_id,
                market_date=market_date,
                window_days=window_days,
                version_bucket=key[0],
                cohort=key[1],
                strategy_id=key[2],
                strategy_version=key[3],
            )
        )

    if not series:
        status = "WAITING_FOR_CANONICAL_ACCOUNT_LEDGER"
        report = _empty_report(status)
    elif len(series) == 1:
        report = dict(series[0])
    else:
        # A mixed V5/V6 load is intentionally not a combined performance
        # series.  Consumers must choose one immutable series.
        report = _empty_report("INCOMPLETE_EXPECTED_SESSIONS")
        report.update({"series_count": len(series)})

    by_version: dict[str, dict[str, Any]] = {}
    for item in series:
        bucket = str(item.get("version_bucket") or "other")
        if bucket not in by_version:
            by_version[bucket] = item
        else:
            # Multiple arms in one bucket are not safely aggregable.
            by_version[bucket] = {
                **_empty_report("INCOMPLETE_EXPECTED_SESSIONS"),
                "version_bucket": bucket,
                "series_count": 2,
            }
    report.update(
        {
            "schema_version": REPORT_SCHEMA_VERSION,
            "contract_version": ACCOUNT_SESSION_CONTRACT_VERSION,
            "market_date": market_date,
            "window_days": window_days,
            "account_id": account_id or (series[0].get("account_id") if len(series) == 1 else None),
            "experiment_id": experiment_id,
            "arm_id": arm_id,
            "code_sha": str(code_sha or "unknown"),
            "expected_session_count": len(expected),
            "expected_calendar_hash_sha256": _hash(expected),
            "series": series,
            "by_version": by_version,
            "research_only": RESEARCH_ONLY,
            "broker_execution_enabled": BROKER_EXECUTION_ENABLED,
            "unsafe_ledger_count": unsafe_ledger_count,
        }
    )
    if unsafe_ledger_count:
        # An unsafe account contract must never be hidden by a complete-looking
        # safe subset.  Keep safe series available for diagnosis, but block the
        # aggregate report until every contributing account is research-only
        # and broker-disabled in persisted truth.
        report["status"] = "WAITING_FOR_AUTHENTICATED_FILL_TRUTH"
    return report


def public_account_session_report(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project the report to a bounded, raw-data-free public payload."""

    if not isinstance(report, dict):
        return None
    allowed = {
        "schema_version",
        "contract_version",
        "status",
        "market_date",
        "window_days",
        "account_id",
        "experiment_id",
        "arm_id",
        "code_sha",
        "expected_session_count",
        "ledger_row_count",
        "complete_count",
        "no_trade_count",
        "missing_count",
        "partial_count",
        "quarantined_count",
        "target_met_count",
        "target_not_met_count",
        "compound_return_pct",
        "geometric_mean_daily_return_pct",
        "expected_calendar_hash_sha256",
        "source_hashes_sha256",
        "input_hash_sha256",
        "series_count",
        "version_bucket",
        "by_version",
        "research_only",
        "broker_execution_enabled",
        "unsafe_ledger_count",
    }
    output = {key: report.get(key) for key in allowed if key in report}
    output["series"] = [
        public_account_session_report(item)
        for item in report.get("series", [])
        if isinstance(item, dict)
    ]
    raw_by_version = report.get("by_version")
    output["by_version"] = (
        {
            str(key): public_account_session_report(value)
            for key, value in raw_by_version.items()
            if isinstance(value, dict)
        }
        if isinstance(raw_by_version, dict)
        else {}
    )
    return output


def _expected_sessions(
    connection: sqlite3.Connection, market_date: str | None, window_days: int
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT session_id, market_date, exchange, session_open_utc,
                  session_close_utc, status, calendar_source,
                  calendar_source_hash_sha256
             FROM expected_market_sessions
            WHERE (? IS NULL OR market_date <= ?)
              AND status <> 'CANCELLED'
            ORDER BY market_date DESC
            LIMIT ?""",
        (market_date, market_date, window_days),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def _ledger_rows(
    connection: sqlite3.Connection,
    market_date: str | None,
    window_days: int,
    account_id: str | None,
) -> tuple[list[dict[str, Any]], int]:
    rows = connection.execute(
        """SELECT ledger.*, account.research_only AS account_research_only,
                  account.broker_execution_enabled AS account_broker_execution_enabled
             FROM paper_account_daily_ledger AS ledger
             LEFT JOIN paper_accounts AS account
               ON account.account_id = ledger.account_id
            WHERE (? IS NULL OR ledger.market_date <= ?)
              AND (? IS NULL OR ledger.account_id = ?)
            ORDER BY ledger.market_date ASC, ledger.account_id ASC, ledger.ledger_id ASC""",
        (market_date, market_date, account_id, account_id),
    ).fetchall()
    values = [dict(row) for row in rows]
    safe = [
        row
        for row in values
        if row.get("account_research_only") == 1
        and row.get("account_broker_execution_enabled") == 0
    ]
    return safe, len(values) - len(safe)


def _series_report(
    rows: list[dict[str, Any]],
    expected: list[dict[str, Any]],
    *,
    account_id: str,
    code_sha: str | None,
    experiment_id: str | None,
    arm_id: str | None,
    market_date: str | None,
    window_days: int,
    version_bucket: str,
    cohort: str,
    strategy_id: str,
    strategy_version: str,
) -> dict[str, Any]:
    expected_ids = {str(item["session_id"]): item for item in expected}
    relevant = [row for row in rows if str(row.get("expected_session_id") or "") in expected_ids]
    counts = Counter(str(row.get("status") or "").upper() for row in relevant)
    complete_statuses = {"TRADE", "AUTHENTICATED_NO_TRADE", "NO_TRADE"}
    valid = [row for row in relevant if str(row.get("status") or "").upper() in complete_statuses]
    missing_expected = expected_ids.keys() - {
        str(row.get("expected_session_id") or "") for row in relevant
    }
    incomplete = bool(missing_expected) or len(valid) != len(expected_ids)
    status = "COMPLETE"
    if not relevant:
        status = "WAITING_FOR_CANONICAL_ACCOUNT_LEDGER"
    elif missing_expected:
        status = "INCOMPLETE_EXPECTED_SESSIONS"
    elif any(
        str(row.get("status") or "").upper() in {"PARTIAL", "MISSING", "PENDING", "DEGRADED"}
        for row in relevant
    ):
        status = "WAITING_FOR_AUTHENTICATED_FILL_TRUTH"
    elif counts.get("QUARANTINED", 0) or counts.get("HALTED", 0):
        status = "WAITING_FOR_AUTHENTICATED_FILL_TRUTH"
    return_values: list[Decimal] = []
    for row in valid:
        value = _number(row.get("net_return_pct"))
        if value is not None:
            return_values.append(value)
    if len(return_values) != len(expected_ids):
        incomplete = True
        if status == "COMPLETE":
            status = "WAITING_FOR_AUTHENTICATED_FILL_TRUTH"
    compound = None
    geometric = None
    if not incomplete and return_values:
        wealth = Decimal("1")
        for value in return_values:
            wealth *= Decimal("1") + value / Decimal("100")
        compound = _float((wealth - Decimal("1")) * Decimal("100"))
        geometric = _float(
            (wealth ** (Decimal("1") / Decimal(len(return_values))) - Decimal("1")) * Decimal("100")
        )
    source_hashes = sorted(
        {
            str(row.get("source_hash_sha256") or "")
            for row in relevant
            if row.get("source_hash_sha256")
        }
    )
    input_payload = {
        "expected": expected,
        "ledger": [
            {
                key: row.get(key)
                for key in (
                    "ledger_id",
                    "market_date",
                    "status",
                    "input_hash_sha256",
                    "source_hash_sha256",
                )
            }
            for row in relevant
        ],
        "contract_version": ACCOUNT_SESSION_CONTRACT_VERSION,
    }
    return {
        "status": status,
        "version_bucket": version_bucket,
        "cohort": cohort,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "account_id": account_id,
        "market_date": market_date,
        "window_days": window_days,
        "expected_session_count": len(expected),
        "ledger_row_count": len(relevant),
        "complete_count": sum(counts[item] for item in complete_statuses),
        "no_trade_count": counts.get("AUTHENTICATED_NO_TRADE", 0) + counts.get("NO_TRADE", 0),
        "missing_count": counts.get("MISSING", 0) + len(missing_expected),
        "partial_count": counts.get("PARTIAL", 0)
        + counts.get("PENDING", 0)
        + counts.get("DEGRADED", 0),
        "quarantined_count": counts.get("QUARANTINED", 0) + counts.get("HALTED", 0),
        "target_met_count": sum(
            1 for row in valid if str(row.get("target_status") or "") == "TARGET_MET"
        ),
        "target_not_met_count": sum(
            1 for row in valid if str(row.get("target_status") or "") == "TARGET_NOT_MET"
        ),
        "compound_return_pct": compound,
        "geometric_mean_daily_return_pct": geometric,
        "expected_calendar_hash_sha256": _hash(expected),
        "source_hashes_sha256": _hash(source_hashes),
        "input_hash_sha256": _hash(input_payload),
        "code_sha": str(code_sha or "unknown"),
        "experiment_id": experiment_id,
        "arm_id": arm_id,
        "research_only": RESEARCH_ONLY,
        "broker_execution_enabled": BROKER_EXECUTION_ENABLED,
    }


def _version_bucket(row: dict[str, Any]) -> str:
    text = " ".join(
        str(row.get(key) or "").lower() for key in ("cohort", "strategy_id", "strategy_version")
    )
    if "v6" in text or "challenger" in text or "shadow" in text:
        return "v6"
    if "v5" in text or "official" in text:
        return "v5"
    return str(row.get("cohort") or "other")


def _empty_report(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "expected_session_count": 0,
        "ledger_row_count": 0,
        "complete_count": 0,
        "no_trade_count": 0,
        "missing_count": 0,
        "partial_count": 0,
        "quarantined_count": 0,
        "target_met_count": 0,
        "target_not_met_count": 0,
        "compound_return_pct": None,
        "geometric_mean_daily_return_pct": None,
        "expected_calendar_hash_sha256": None,
        "source_hashes_sha256": None,
        "input_hash_sha256": _hash({"status": status}),
        "series": [],
        "by_version": {},
        "unsafe_ledger_count": 0,
        "research_only": RESEARCH_ONLY,
        "broker_execution_enabled": BROKER_EXECUTION_ENABLED,
    }


def _number(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _float(value: Decimal | None) -> float | None:
    return round(float(value), 8) if value is not None else None


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "build_account_session_report",
    "public_account_session_report",
]
