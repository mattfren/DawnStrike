from __future__ import annotations

import sqlite3

import pytest

from intraday_scanner.performance.canonical_account_ledger import (
    CanonicalAccountLedger,
    LedgerConflictError,
)


def _account() -> dict[str, object]:
    return {
        "account_id": "paper-total",
        "opening_equity_cents": 100_000,
        "strategy_id": "aggregate",
        "strategy_version": "aggregate.v1",
        "execution_policy_version": "paper.v1",
        "cost_model_version": "cost.v1",
    }


def _session(day: str, session_id: str | None = None, **extra: object) -> dict[str, object]:
    return {
        "market_date": day,
        "session_id": session_id or f"session-{day}",
        "status": "CLOSED",
        **extra,
    }


def _trade(trade_id: str, day: str, **extra: object) -> dict[str, object]:
    return {
        "trade_id": trade_id,
        "market_date": day,
        "gross_pnl_cents": 1_300,
        "fees_cents": 100,
        "slippage_cents": 100,
        "net_pnl_cents": 1_100,
        "fill_truth_authenticated": True,
        **extra,
    }


def test_builds_one_row_per_expected_session_and_preserves_unknowns(tmp_path):
    service = CanonicalAccountLedger(tmp_path / "ledger.sqlite", account_id="paper-total")
    sessions = [
        _session("2026-08-25"),
        _session("2026-08-26"),
        _session("2026-08-27"),
        _session("2026-08-28", halted=True),
        _session("2026-08-29", status="CANCELLED"),
    ]
    result = service.build(
        account=_account(),
        expected_sessions=sessions,
        trades=[_trade("t1", "2026-08-25")],
        positions=[{"position_id": "p1", "market_date": "2026-08-26", "status": "OPEN"}],
        no_trade_receipts=[
            {
                "receipt_id": "nt-27",
                "account_id": "paper-total",
                "market_date": "2026-08-27",
                "session_id": "session-2026-08-27",
                "authoritative": True,
            }
        ],
    )
    rows = list(result.rows)
    assert len(rows) == 5
    assert rows[0]["status"] == "TRADE"
    assert rows[0]["net_return_pct"] == 1.1
    assert rows[0]["target_status"] == "TARGET_MET"
    assert rows[0]["target_shortfall_pct"] == 0.0
    assert rows[1]["status"] == "PARTIAL"
    assert rows[1]["net_return_pct"] is None
    assert rows[2]["status"] == "AUTHENTICATED_NO_TRADE"
    assert rows[2]["net_return_pct"] is None
    assert rows[3]["status"] == "HALTED"
    assert rows[4]["status"] == "NOT_EXPECTED"


def test_missing_no_trade_receipt_is_not_zero(tmp_path):
    service = CanonicalAccountLedger(tmp_path / "ledger.sqlite", account_id="paper-total")
    result = service.build(account=_account(), expected_sessions=[_session("2026-08-25")])
    row = result.rows[0]
    assert row["status"] == "MISSING"
    assert row["net_return_pct"] is None
    assert row["ending_equity_cents"] is None


def test_aggregates_multiple_strategies_and_idempotent_persistence(tmp_path):
    path = tmp_path / "ledger.sqlite"
    service = CanonicalAccountLedger(path, account_id="paper-total")
    kwargs = {
        "account": _account(),
        "expected_sessions": [_session("2026-08-25")],
        "trades": [
            _trade("t2", "2026-08-25", net_pnl_cents=250, strategy_id="s2"),
            _trade("t1", "2026-08-25", net_pnl_cents=750, strategy_id="s1"),
        ],
    }
    first = service.build(**kwargs)
    second = service.build(**{**kwargs, "trades": list(reversed(kwargs["trades"]))})
    assert first.input_hash_sha256 == second.input_hash_sha256
    assert first.rows[0]["realized_net_pnl_cents"] == 1_000
    assert service.persist(first, account=_account()) == 1
    assert service.persist(second, account=_account()) == 1
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM paper_account_daily_ledger WHERE account_id = ?",
                ("paper-total",),
            ).fetchone()[0]
            == 1
        )


def test_conflicting_session_or_trade_identity_fails_closed(tmp_path):
    service = CanonicalAccountLedger(tmp_path / "ledger.sqlite", account_id="paper-total")
    with pytest.raises(LedgerConflictError):
        service.build(
            account=_account(),
            expected_sessions=[_session("2026-08-25", "s-a"), _session("2026-08-25", "s-b")],
        )
    with pytest.raises(LedgerConflictError):
        service.build(
            account=_account(),
            expected_sessions=[_session("2026-08-25")],
            trades=[_trade("same", "2026-08-25"), _trade("same", "2026-08-25", net_pnl_cents=999)],
        )


def test_explicit_equity_must_match_accounting_identity(tmp_path):
    service = CanonicalAccountLedger(tmp_path / "ledger.sqlite", account_id="paper-total")
    with pytest.raises(LedgerConflictError):
        service.build(
            account=_account(),
            expected_sessions=[_session("2026-08-25", ending_equity_cents=100_001)],
            trades=[_trade("t1", "2026-08-25")],
        )
