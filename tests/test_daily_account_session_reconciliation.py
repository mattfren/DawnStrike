from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_ACCOUNT_ID,
    ALPHAOPS_V5_STRATEGY_ID,
    ALPHAOPS_V5_STRATEGY_VERSION,
)
from intraday_scanner.decisioning.contracts import canonical_json
from intraday_scanner.performance.account_session_reporting import (
    build_account_session_report,
)
from intraday_scanner.services.daily_account_session_reconciliation import (
    reconcile_daily_account_sessions,
)
from intraday_scanner.services.daily_finalize_service import (
    _account_session_report_ready,
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
        "session_id": "XNYS:2026-08-28:regular",
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

    assert first["expected_session"]["session_id"] == "XNYS:2026-08-28:regular"
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


def test_failed_new_release_cannot_relabel_retained_complete_ledger(tmp_path: Path):
    path = tmp_path / "reconciliation.sqlite"
    release_a = "a" * 40
    release_b = "b" * 40
    with sqlite3.connect(path) as connection:
        run_migrations(connection)
        account_payload = {
            "account_id": ALPHAOPS_V5_ACCOUNT_ID,
            "cohort": "official_forward_paper",
            "strategy_id": ALPHAOPS_V5_STRATEGY_ID,
            "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
            "opening_equity_cents": 100_000,
        }
        connection.execute(
            """INSERT INTO paper_accounts
               (account_id, strategy_id, strategy_version, activation_timestamp,
                opening_equity_cents, currency, account_type,
                execution_policy_version, cost_model_version, research_only,
                broker_execution_enabled, created_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)""",
            (
                ALPHAOPS_V5_ACCOUNT_ID,
                ALPHAOPS_V5_STRATEGY_ID,
                ALPHAOPS_V5_STRATEGY_VERSION,
                "2026-08-01T00:00:00+00:00",
                100_000,
                "USD",
                "simulated_paper",
                "alphaops-v5-paper.v1",
                "alphaops-v5-cost.v1",
                "2026-08-01T00:00:00+00:00",
                canonical_json(account_payload),
            ),
        )

    seeded = reconcile_daily_account_sessions(
        path,
        market_date="2026-08-28",
        account_id=ALPHAOPS_V5_ACCOUNT_ID,
        release_sha=release_a,
    )
    receipt: dict[str, object] = {
        "receipt_id": "official-no-trade-2026-08-28",
        "account_id": ALPHAOPS_V5_ACCOUNT_ID,
        "strategy_id": ALPHAOPS_V5_STRATEGY_ID,
        "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
        "market_date": "2026-08-28",
        "session_id": "XNYS:2026-08-28:regular",
        "run_id": "run-2026-08-28",
        "status": "FINALIZED",
        "decision": "NO_TRADE",
        "no_entry": True,
        "source_artifact_hash_sha256": "c" * 64,
        "source_config_hash_sha256": "d" * 64,
        "calendar_source_hash_sha256": seeded["calendar_source_hash_sha256"],
        "code_sha": release_a,
        "created_at": "2026-08-28T20:00:00+00:00",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    receipt["receipt_hash_sha256"] = hashlib.sha256(
        canonical_json(receipt).encode("utf-8")
    ).hexdigest()
    assert SQLiteScanStore(path).persist_no_trade_session_receipt(receipt)

    reconciliation_a = reconcile_daily_account_sessions(
        path,
        market_date="2026-08-28",
        account_id=ALPHAOPS_V5_ACCOUNT_ID,
        release_sha=release_a,
    )
    report_a = build_account_session_report(
        path,
        market_date="2026-08-28",
        account_id=ALPHAOPS_V5_ACCOUNT_ID,
        code_sha=release_a,
    )
    assert reconciliation_a["status"] == "COMPLETE"
    assert report_a["current_session_lineage_match"] is True
    assert _account_session_report_ready(
        report_a,
        market_date="2026-08-28",
        release_sha=release_a,
        reconciliation=reconciliation_a,
    )

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE paper_accounts SET opening_equity_cents = 0 WHERE account_id = ?",
            (ALPHAOPS_V5_ACCOUNT_ID,),
        )
    reconciliation_b = reconcile_daily_account_sessions(
        path,
        market_date="2026-08-28",
        account_id=ALPHAOPS_V5_ACCOUNT_ID,
        release_sha=release_b,
    )
    report_b = build_account_session_report(
        path,
        market_date="2026-08-28",
        account_id=ALPHAOPS_V5_ACCOUNT_ID,
        code_sha=release_b,
    )

    assert reconciliation_b["status"] == "DEGRADED"
    assert report_b["status"] == "WAITING_FOR_AUTHENTICATED_FILL_TRUTH"
    assert report_b["current_session_lineage_match"] is False
    assert not _account_session_report_ready(
        report_b,
        market_date="2026-08-28",
        release_sha=release_b,
        reconciliation=reconciliation_b,
    )
