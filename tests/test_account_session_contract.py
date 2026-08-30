from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from intraday_scanner.performance.account_contract import (
    AccountSessionStatus,
    AccountSessionTarget,
    account_session_return_pct,
    compute_net_total_account_return,
    validate_account_session,
)
from intraday_scanner.storage.migrations import get_schema_version, run_migrations


def test_total_account_return_uses_equity_identity_and_external_flows() -> None:
    assert compute_net_total_account_return(
        beginning_equity_cents=100_000,
        ending_equity_cents=102_000,
        external_flow_cents=1_000,
    ) == Decimal("0.01")
    assert account_session_return_pct(
        beginning_equity_cents=100_000,
        ending_equity_cents=102_000,
        external_flow_cents=1_000,
    ) == Decimal("1.00")


def test_missing_equity_is_not_converted_to_zero() -> None:
    result = validate_account_session(
        expected_session=True,
        beginning_equity_cents=100_000,
        ending_equity_cents=None,
    )
    assert result.status is AccountSessionStatus.MISSING
    assert result.net_return is None


def test_no_trade_requires_authoritative_receipt() -> None:
    missing = validate_account_session(
        expected_session=True,
        beginning_equity_cents=100_000,
        ending_equity_cents=100_000,
        no_trade=True,
    )
    assert missing.status is AccountSessionStatus.MISSING
    assert missing.net_return is None

    observed = validate_account_session(
        expected_session=True,
        beginning_equity_cents=100_000,
        ending_equity_cents=100_000,
        no_trade=True,
        authoritative_receipt={"receipt_id": "session-receipt-1"},
    )
    assert observed.status is AccountSessionStatus.NO_TRADE
    assert observed.net_return == Decimal("0")


def test_complete_statuses_are_bound_to_one_percent_target() -> None:
    met = validate_account_session(
        expected_session=True,
        beginning_equity_cents=100_000,
        ending_equity_cents=101_000,
    )
    not_met = validate_account_session(
        expected_session=True,
        beginning_equity_cents=100_000,
        ending_equity_cents=100_999,
    )
    assert met.status is AccountSessionStatus.COMPLETE_TARGET_MET
    assert not_met.status is AccountSessionStatus.COMPLETE_TARGET_NOT_MET


def test_account_target_rejects_non_research_or_missing_no_trade_receipt() -> None:
    with pytest.raises(ValueError, match="authoritative receipt"):
        AccountSessionTarget(
            account_id="account",
            market_date="2026-08-31",
            expected_session_id="XNYS:2026-08-31",
            status=AccountSessionStatus.NO_TRADE,
            beginning_equity_cents=100_000,
            ending_equity_cents=100_000,
            external_flow_cents=0,
            net_return=Decimal("0"),
        )

    with pytest.raises(ValueError, match="research-only"):
        AccountSessionTarget(
            account_id="account",
            market_date="2026-08-31",
            expected_session_id="XNYS:2026-08-31",
            status=AccountSessionStatus.MISSING,
            beginning_equity_cents=100_000,
            ending_equity_cents=None,
            external_flow_cents=None,
            net_return=None,
            research_only=False,
        )


def test_migration_31_sidecar_is_additive_idempotent_and_append_only() -> None:
    with sqlite3.connect(":memory:") as connection:
        assert run_migrations(connection) == 30
        assert get_schema_version(connection) == 30
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "expected_market_sessions",
            "intraday_capture_runs",
            "committed_fill_truth_receipts",
            "experiment_trial_ledger",
        } <= tables
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(paper_account_daily_ledger)")
        }
        assert {
            "target_return_pct",
            "target_status",
            "expected_session_id",
            "experiment_id",
            "arm_id",
            "evidence_mode",
            "lineage_sha256",
        } <= columns
        assert run_migrations(connection) == 30
        connection.execute(
            "INSERT INTO expected_market_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "XNYS:2026-08-31",
                "2026-08-31",
                "XNYS",
                "2026-08-31T13:30:00+00:00",
                "2026-08-31T20:00:00+00:00",
                "EXPECTED",
                "test-calendar",
                "calendar-hash",
                "2026-08-30T00:00:00+00:00",
                1,
                0,
                "{}",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE expected_market_sessions SET status = 'OPEN' WHERE session_id = ?",
                ("XNYS:2026-08-31",),
            )


def test_account_sidecar_is_not_skipped_for_a_preexisting_marker_31_store() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_version VALUES (31, '2026-08-30T00:00:00+00:00')")
        assert run_migrations(connection) == 31
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'expected_market_sessions'"
        ).fetchone() == (1,)
