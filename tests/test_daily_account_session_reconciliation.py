from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from intraday_scanner.decisioning.contracts import canonical_json
from intraday_scanner.services.daily_account_session_reconciliation import (
    reconcile_daily_account_sessions,
)
from intraday_scanner.storage.migrations import run_migrations
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

CODE_SHA = "a" * 40


def _account(path: Path, account_id: str = "paper-total") -> None:
    with sqlite3.connect(path) as connection:
        run_migrations(connection)
        connection.execute(
            """INSERT INTO paper_accounts
               (account_id, strategy_id, strategy_version, activation_timestamp,
                opening_equity_cents, currency, account_type,
                execution_policy_version, cost_model_version, research_only,
                broker_execution_enabled, created_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account_id,
                "aggregate",
                "aggregate.v1",
                "2026-08-01T00:00:00+00:00",
                100_000,
                "USD",
                "simulated_paper",
                "paper.v1",
                "cost.v1",
                1,
                0,
                "2026-08-01T00:00:00+00:00",
                "{}",
            ),
        )


def _no_trade(path: Path, *, calendar_hash: str, account_id: str = "paper-total") -> None:
    payload: dict[str, object] = {
        "receipt_id": "no-trade-2026-08-28",
        "account_id": account_id,
        "strategy_id": "aggregate",
        "strategy_version": "aggregate.v1",
        "market_date": "2026-08-28",
        "session_id": "XNYS:2026-08-28",
        "run_id": "run-2026-08-28",
        "status": "FINALIZED",
        "decision": "NO_TRADE",
        "no_entry": True,
        "source_artifact_hash_sha256": "b" * 64,
        "source_config_hash_sha256": "c" * 64,
        "calendar_source_hash_sha256": calendar_hash,
        "code_sha": CODE_SHA,
        "created_at": "2026-08-28T20:00:00+00:00",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["receipt_hash_sha256"] = hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()
    assert SQLiteScanStore(path).persist_no_trade_session_receipt(payload)


def test_reconciliation_materializes_calendar_once_and_waits_without_account(tmp_path: Path):
    path = tmp_path / "reconciliation.sqlite"
    first = reconcile_daily_account_sessions(path, market_date="2026-08-28", release_sha=CODE_SHA)
    second = reconcile_daily_account_sessions(path, market_date="2026-08-28", release_sha=CODE_SHA)

    assert first["status"] == "MISSING"
    assert first["reason"] == "paper_account_missing"
    assert first["expected_session_inserted"] is True
    assert second["expected_session_inserted"] is False
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM expected_market_sessions WHERE market_date = ?",
            ("2026-08-28",),
        ).fetchone()[0] == 1


def test_reconciliation_does_not_turn_forged_no_trade_mapping_into_zero(tmp_path: Path):
    path = tmp_path / "reconciliation.sqlite"
    _account(path)
    first = reconcile_daily_account_sessions(path, market_date="2026-08-28", release_sha=CODE_SHA)
    _no_trade(path, calendar_hash="d" * 64)
    result = reconcile_daily_account_sessions(path, market_date="2026-08-28", release_sha=CODE_SHA)

    assert first["accounts"][0]["status"] == "MISSING"
    assert result["status"] == "MISSING"
    assert result["accounts"][0]["status"] == "MISSING"
    assert result["accounts"][0]["ledger_row"]["net_return_pct"] is None


def test_reconciliation_accepts_only_bridge_authenticated_no_trade(tmp_path: Path):
    path = tmp_path / "reconciliation.sqlite"
    _account(path)
    seeded = reconcile_daily_account_sessions(path, market_date="2026-08-28", release_sha=CODE_SHA)
    _no_trade(
        path,
        calendar_hash=str(seeded["calendar_source_hash_sha256"]),
    )
    result = reconcile_daily_account_sessions(path, market_date="2026-08-28", release_sha=CODE_SHA)

    account = result["accounts"][0]
    assert result["status"] == "COMPLETE"
    assert account["status"] == "AUTHENTICATED_NO_TRADE"
    assert account["ledger_row"]["net_return_pct"] == 0.0
    assert account["ledger_row"]["research_only"] is True
    assert account["ledger_row"]["broker_execution_enabled"] is False
