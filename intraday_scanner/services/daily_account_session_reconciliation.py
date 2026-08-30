"""Bounded producer for the canonical paper account/session ledger.

This service is intentionally small and append-safe.  It is the write-side
counterpart to ``account_session_reporting``: the checked-in calendar is
materialized first, then persisted receipt IDs are resolved through the typed
CommitBridge/NoTradeBridge boundary, and only an existing research paper
account can receive a canonical ledger row.  Missing account or evidence is
reported as a blocking state; it is never manufactured into a zero return.

No provider or broker client is imported here.  This path consumes retained
SQLite evidence only and always writes ``research_only=1``/
``broker_execution_enabled=0`` records.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.commit_bridge import (
    CommitBridge,
    FillTruthIdentity,
    NoTradeBridge,
    NoTradeIdentity,
)
from intraday_scanner.decisioning.contracts import canonical_json
from intraday_scanner.market_calendar import MARKET_TIMEZONE, MarketSessionDecision, market_session
from intraday_scanner.performance.canonical_account_ledger import (
    CanonicalAccountLedger,
    LedgerConflictError,
    LedgerEvidenceError,
)
from intraday_scanner.storage.migrations import run_migrations

RESEARCH_ONLY = True
BROKER_EXECUTION_ENABLED = False
_FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC = timezone.utc


class AccountReconciliationError(RuntimeError):
    """Base error for the fail-closed daily account producer."""


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc_session_timestamp(day: date, clock: str | None) -> str:
    if not clock:
        return ""
    parsed = time.fromisoformat(clock)
    return datetime.combine(day, parsed, tzinfo=MARKET_TIMEZONE).astimezone(_UTC).isoformat()


def _calendar_row(decision: MarketSessionDecision) -> dict[str, Any]:
    day = date.fromisoformat(decision.market_date)
    calendar_payload = decision.to_dict()
    calendar_hash = _hash(calendar_payload)
    return {
        "session_id": f"XNYS:{decision.market_date}",
        "market_date": decision.market_date,
        "exchange": "NYSE",
        "session_open_utc": _utc_session_timestamp(day, decision.open_time_et),
        "session_close_utc": _utc_session_timestamp(day, decision.close_time_et),
        "status": "CLOSED" if decision.is_trading_day else "CANCELLED",
        "calendar_source": decision.calendar_id,
        "calendar_source_hash_sha256": calendar_hash,
        "research_only": 1,
        "broker_execution_enabled": 0,
        "payload_json": canonical_json(calendar_payload),
    }


def _ensure_expected_session(connection: sqlite3.Connection, row: Mapping[str, Any]) -> bool:
    """Append the exact calendar row, or verify an existing immutable row."""

    existing = connection.execute(
        """SELECT session_id, market_date, exchange, session_open_utc,
                session_close_utc, status, calendar_source,
                calendar_source_hash_sha256, research_only,
                broker_execution_enabled, payload_json
           FROM expected_market_sessions WHERE market_date = ?""",
        (row["market_date"],),
    ).fetchone()
    if existing is None:
        connection.execute(
            """INSERT INTO expected_market_sessions
               (session_id, market_date, exchange, session_open_utc,
                session_close_utc, status, calendar_source,
                calendar_source_hash_sha256, created_at, research_only,
                broker_execution_enabled, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["session_id"],
                row["market_date"],
                row["exchange"],
                row["session_open_utc"],
                row["session_close_utc"],
                row["status"],
                row["calendar_source"],
                row["calendar_source_hash_sha256"],
                datetime.now(_UTC).isoformat(),
                1,
                0,
                row["payload_json"],
            ),
        )
        return True
    expected = (
        str(row["session_id"]),
        str(row["market_date"]),
        str(row["exchange"]),
        str(row["session_open_utc"]),
        str(row["session_close_utc"]),
        str(row["status"]),
        str(row["calendar_source"]),
        str(row["calendar_source_hash_sha256"]),
        1,
        0,
        str(row["payload_json"]),
    )
    actual = tuple(existing)
    if actual != expected:
        raise AccountReconciliationError(
            f"expected session {row['market_date']} conflicts with immutable calendar row"
        )
    return False


def _account_from_row(row: sqlite3.Row) -> dict[str, Any]:
    account = dict(row)
    raw_payload = account.get("payload_json")
    if isinstance(raw_payload, str):
        try:
            decoded = json.loads(raw_payload)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, Mapping):
            account = {**dict(decoded), **account}
    return account


def _receipt_ids(
    connection: sqlite3.Connection, table: str, account_id: str, day: str
) -> list[str]:
    if table == "committed_fill_truth_receipts":
        query = (
            "SELECT receipt_id FROM committed_fill_truth_receipts "
            "WHERE account_id = ? AND market_date = ? ORDER BY receipt_id"
        )
    elif table == "no_trade_session_receipts":
        query = (
            "SELECT receipt_id FROM no_trade_session_receipts "
            "WHERE account_id = ? AND market_date = ? ORDER BY receipt_id"
        )
    else:
        raise ValueError("unsupported receipt table")
    rows = connection.execute(query, (account_id, day)).fetchall()
    return [str(row[0]) for row in rows if str(row[0] or "").strip()]


def _resolve_evidence(
    connection: sqlite3.Connection,
    *,
    account: Mapping[str, Any],
    session: Mapping[str, Any],
    code_sha: str | None,
) -> tuple[list[dict[str, Any]], list[Mapping[str, Any]], dict[str, int]]:
    account_id = str(account["account_id"])
    day = str(session["market_date"])
    if not _FULL_GIT_SHA.fullmatch(str(code_sha or "").lower()):
        return [], [], {"fill_receipts_seen": 0, "fill_receipts_authenticated": 0,
                         "no_trade_receipts_seen": 0, "no_trade_receipts_authenticated": 0}
    fill_ids = _receipt_ids(connection, "committed_fill_truth_receipts", account_id, day)
    no_trade_ids = _receipt_ids(connection, "no_trade_session_receipts", account_id, day)
    return _resolve_evidence_from_store(
        account=account,
        session=session,
        code_sha=str(code_sha),
        fill_ids=fill_ids,
        no_trade_ids=no_trade_ids,
        connection=connection,
    )


class _ConnectionEvidenceStore:
    """Minimal CommitBridge store facade over the reconciliation connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def load_committed_fill_truth_receipt_record(self, receipt_id: str) -> dict[str, Any] | None:
        return self._load("committed_fill_truth_receipts", receipt_id)

    def load_no_trade_session_receipt_record(self, receipt_id: str) -> dict[str, Any] | None:
        return self._load("no_trade_session_receipts", receipt_id)

    def _load(self, table: str, receipt_id: str) -> dict[str, Any] | None:
        if table == "committed_fill_truth_receipts":
            query = "SELECT * FROM committed_fill_truth_receipts WHERE receipt_id = ?"
        elif table == "no_trade_session_receipts":
            query = "SELECT * FROM no_trade_session_receipts WHERE receipt_id = ?"
        else:
            raise ValueError("unsupported receipt table")
        row = self.connection.execute(query, (receipt_id,)).fetchone()
        if row is None:
            return None
        columns = dict(row)
        payload_json = str(columns.get("payload_json") or "")
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, Mapping):
            return None
        return {"columns": columns, "payload": dict(payload), "payload_json": payload_json}


def _resolve_evidence_from_store(
    *,
    account: Mapping[str, Any],
    session: Mapping[str, Any],
    code_sha: str,
    fill_ids: list[str],
    no_trade_ids: list[str],
    connection: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], list[Mapping[str, Any]], dict[str, int]]:
    account_id = str(account["account_id"])
    day = str(session["market_date"])
    session_id = str(session["session_id"])
    strategy_id = str(account.get("strategy_id") or "")
    strategy_version = str(account.get("strategy_version") or "")
    store = _ConnectionEvidenceStore(connection)
    fill_bridge = CommitBridge(store)
    no_trade_bridge = NoTradeBridge(store)
    trades: list[dict[str, Any]] = []
    no_trade: list[Mapping[str, Any]] = []
    fill_auth = 0
    no_trade_auth = 0
    for receipt_id in fill_ids:
        resolved = fill_bridge.resolve(
            receipt_id,
            identity=FillTruthIdentity(
                account_id=account_id,
                market_date=day,
                session_id=session_id,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
            ),
            expected_code_sha=code_sha,
        )
        if resolved is not None:
            fill_auth += 1
            trades.append(
                {"trade_id": receipt_id, "receipt_id": receipt_id, "market_date": day,
                 "fill_truth": resolved}
            )
    for receipt_id in no_trade_ids:
        no_trade_resolved = no_trade_bridge.resolve(
            receipt_id,
            identity=NoTradeIdentity(
                account_id=account_id,
                market_date=day,
                session_id=session_id,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
            ),
            expected_code_sha=code_sha,
            expected_calendar_source_hash=str(session["calendar_source_hash_sha256"]),
        )
        if no_trade_resolved is not None:
            no_trade_auth += 1
            no_trade.append(no_trade_resolved)
    return trades, no_trade, {
        "fill_receipts_seen": len(fill_ids),
        "fill_receipts_authenticated": fill_auth,
        "no_trade_receipts_seen": len(no_trade_ids),
        "no_trade_receipts_authenticated": no_trade_auth,
    }


class DailyAccountSessionReconciliationService:
    """Produce one bounded account/date ledger slice from retained evidence."""

    def __init__(self, db_path: str | Path, *, release_sha: str | None = None) -> None:
        self.db_path = Path(db_path)
        self.release_sha = str(release_sha or "").strip().lower()

    def reconcile(self, *, market_date: str, account_id: str | None = None,
                  now: str | None = None,
                  evidence_mode: str = "forward_observed") -> dict[str, Any]:
        if evidence_mode not in {"forward_observed", "retrospective_research"}:
            raise AccountReconciliationError(
                "evidence_mode must be forward_observed or retrospective_research"
            )
        try:
            parsed = date.fromisoformat(str(market_date).strip())
        except (TypeError, ValueError) as exc:
            raise AccountReconciliationError("market_date must be ISO YYYY-MM-DD") from exc
        decision = market_session(parsed)
        expected = _calendar_row(decision)
        calculated_at = now or datetime.now(_UTC).isoformat()
        result: dict[str, Any] = {
            "schema_version": "dawnstrike.daily_account_reconciliation.v1",
            "market_date": decision.market_date,
            "release_sha": self.release_sha or None,
            "evidence_mode": evidence_mode,
            "expected_session": {**expected, "research_only": True,
                                  "broker_execution_enabled": False},
            "calendar_source_hash_sha256": expected["calendar_source_hash_sha256"],
            "research_only": RESEARCH_ONLY,
            "broker_execution_enabled": BROKER_EXECUTION_ENABLED,
            "accounts": [],
        }
        if not _FULL_GIT_SHA.fullmatch(self.release_sha):
            result.update({"status": "WAITING", "reason": "release_sha_missing"})
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            run_migrations(connection)
            inserted = _ensure_expected_session(connection, expected)
            result["expected_session_inserted"] = inserted
            accounts = connection.execute(
                "SELECT * FROM paper_accounts "
                "WHERE (? IS NULL OR account_id = ?) ORDER BY account_id",
                (account_id, account_id),
            ).fetchall()
            if not accounts:
                result.update({"status": "MISSING", "reason": "paper_account_missing"})
                connection.commit()
                return result
            for row in accounts:
                account = _account_from_row(row)
                aid = str(account.get("account_id") or "")
                item: dict[str, Any] = {"account_id": aid}
                research_only = int(str(account.get("research_only") or "0"))
                broker_enabled = int(
                    str(account.get("broker_execution_enabled"))
                    if account.get("broker_execution_enabled") is not None
                    else "1"
                )
                if research_only != 1 or broker_enabled != 0:
                    item.update({"status": "QUARANTINED", "reason": "account_execution_boundary"})
                    result["accounts"].append(item)
                    continue
                session = dict(expected)
                lineage = _hash({
                    "account_id": aid,
                    "session_id": session["session_id"],
                    "market_date": decision.market_date,
                    "calendar_source_hash_sha256": session["calendar_source_hash_sha256"],
                    "release_sha": self.release_sha,
                    "research_only": True,
                    "broker_execution_enabled": False,
                })
                trades, receipts, counts = _resolve_evidence(
                    connection, account=account, session=session, code_sha=self.release_sha
                )
                try:
                    ledger = CanonicalAccountLedger(self.db_path, account_id=aid,
                                                    code_sha=self.release_sha)
                    built = ledger.build(
                        account=account,
                        expected_sessions=[session],
                        trades=trades,
                        no_trade_receipts=receipts,
                        calculated_at=calculated_at,
                        evidence_mode=evidence_mode,
                        lineage_sha256=lineage,
                    )
                    # Release the connection's read/write transaction before
                    # the ledger's path-backed persistence opens its own
                    # connection.  This avoids a nested SQLite writer while
                    # retaining the calendar/account discovery transaction.
                    connection.commit()
                    ledger.persist(built, account=account)
                    ledger_row = dict(built.rows[0])
                    item.update({"status": str(ledger_row.get("status")),
                                 "ledger_row": ledger_row, **counts})
                except (LedgerConflictError, LedgerEvidenceError) as exc:
                    item.update({"status": "DEGRADED", "reason": str(exc), **counts})
                result["accounts"].append(item)
            connection.commit()
        statuses = {str(item.get("status")) for item in result["accounts"]}
        if statuses and statuses <= {"AUTHENTICATED_NO_TRADE", "TRADE"}:
            result["status"] = "COMPLETE"
        elif "QUARANTINED" in statuses:
            result["status"] = "QUARANTINED"
        elif "DEGRADED" in statuses:
            result["status"] = "DEGRADED"
        else:
            result["status"] = "MISSING"
            result["reason"] = "authenticated_account_evidence_missing"
        return result


def reconcile_daily_account_sessions(
    db_path: str | Path,
    *,
    market_date: str,
    account_id: str | None = None,
    release_sha: str | None = None,
    now: str | None = None,
    evidence_mode: str = "forward_observed",
) -> dict[str, Any]:
    """Convenience wrapper for the CLI and daily finalizer hook."""

    return DailyAccountSessionReconciliationService(
        db_path, release_sha=release_sha
    ).reconcile(
        market_date=market_date,
        account_id=account_id,
        now=now,
        evidence_mode=evidence_mode,
    )


__all__ = [
    "AccountReconciliationError",
    "DailyAccountSessionReconciliationService",
    "reconcile_daily_account_sessions",
]
