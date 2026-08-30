from __future__ import annotations

import hashlib
import sqlite3

import pytest

from intraday_scanner.alpha.commit_bridge import (
    _mint_authenticated_fill_truth,
    _mint_authenticated_no_trade,
)
from intraday_scanner.decisioning.contracts import canonical_json
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
    fill = {
        "receipt_id": f"fill-{trade_id}",
        "account_id": "paper-total",
        "strategy_id": "aggregate",
        "strategy_version": "aggregate.v1",
        "market_date": day,
        "session_id": f"session-{day}",
        "symbol": "SPY",
        "run_id": f"run-{trade_id}",
        "fill_id": f"fill-{trade_id}",
        "execution_status": "CLOSED",
        "committed": True,
        "side": "long",
        "quantity": 10,
        "entry_price": 100,
        "exit_price": 101,
        "spread_cost_cents": 2,
        "slippage_cost_cents": 3,
        "fees_cents": 1,
        "regulatory_cost_cents": 0,
        "borrow_cost_cents": 0,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    fill.update({key: value for key, value in extra.items() if key in fill})
    fill["receipt_hash_sha256"] = hashlib.sha256(canonical_json(fill).encode()).hexdigest()
    return {
        "trade_id": trade_id,
        "market_date": day,
        "fill_truth": _mint_authenticated_fill_truth(fill),
        **{key: value for key, value in extra.items() if key not in fill},
    }


def _no_trade(day: str) -> object:
    payload = {
        "receipt_id": f"no-trade-{day}",
        "account_id": "paper-total",
        "strategy_id": "aggregate",
        "strategy_version": "aggregate.v1",
        "market_date": day,
        "session_id": f"session-{day}",
        "run_id": f"run-{day}",
        "status": "FINALIZED",
        "decision": "NO_TRADE",
        "no_entry": True,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["receipt_hash_sha256"] = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    return _mint_authenticated_no_trade(payload)


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
        no_trade_receipts=[_no_trade("2026-08-27")],
    )
    rows = list(result.rows)
    assert len(rows) == 5
    assert rows[0]["status"] == "TRADE"
    assert rows[0]["net_return_pct"] == 0.994
    assert rows[0]["target_status"] == "TARGET_NOT_MET"
    assert rows[0]["target_shortfall_pct"] == 0.006
    assert rows[1]["status"] == "PARTIAL"
    assert rows[1]["net_return_pct"] is None
    assert rows[2]["status"] == "MISSING"
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


def test_authenticated_no_trade_requires_known_equity_and_is_exactly_zero(tmp_path):
    service = CanonicalAccountLedger(tmp_path / "ledger.sqlite", account_id="paper-total")
    result = service.build(
        account=_account(),
        expected_sessions=[_session("2026-08-25")],
        no_trade_receipts=[_no_trade("2026-08-25")],
    )
    row = result.rows[0]
    assert row["status"] == "AUTHENTICATED_NO_TRADE"
    assert row["net_return_pct"] == 0.0
    assert row["target_status"] == "NO_TRADE"


def test_aggregates_multiple_strategies_and_idempotent_persistence(tmp_path):
    path = tmp_path / "ledger.sqlite"
    service = CanonicalAccountLedger(path, account_id="paper-total")
    kwargs = {
        "account": _account(),
        "expected_sessions": [_session("2026-08-25")],
        "trades": [
            _trade(
                "t2",
                "2026-08-25",
                entry_price=100,
                exit_price=100.45,
            ),
            _trade(
                "t1",
                "2026-08-25",
                entry_price=100,
                exit_price=100.95,
            ),
        ],
    }
    first = service.build(**kwargs)
    second = service.build(**{**kwargs, "trades": list(reversed(kwargs["trades"]))})
    assert first.input_hash_sha256 == second.input_hash_sha256
    assert first.rows[0]["realized_net_pnl_cents"] == 1_388
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


def test_partial_date_rebuild_preserves_out_of_scope_history(tmp_path):
    path = tmp_path / "ledger.sqlite"
    service = CanonicalAccountLedger(path, account_id="paper-total")
    full = service.build(
        account=_account(),
        expected_sessions=[_session("2026-08-25"), _session("2026-08-26")],
        trades=[_trade("t1", "2026-08-25")],
        no_trade_receipts=[_no_trade("2026-08-26")],
    )
    assert service.persist(full, account=_account()) == 2

    day_one_only = service.build(
        account=_account(),
        expected_sessions=[_session("2026-08-25")],
        trades=[_trade("t1", "2026-08-25")],
    )
    assert service.persist(day_one_only, account=_account()) == 1
    assert [row["market_date"] for row in service.load()] == [
        "2026-08-25",
        "2026-08-26",
    ]


def test_fill_cost_identity_mismatch_is_partial(tmp_path):
    service = CanonicalAccountLedger(tmp_path / "ledger.sqlite", account_id="paper-total")
    result = service.build(
        account=_account(),
        expected_sessions=[_session("2026-08-25")],
        trades=[_trade("bad-costs", "2026-08-25", regulatory_cost_cents=None)],
    )
    assert result.rows[0]["status"] == "PARTIAL"
    assert result.rows[0]["quarantine_reason"] == "fill_truth_financials_incomplete"


@pytest.mark.parametrize(
    ("side", "entry", "exit", "expected_gross"),
    [("long", 100.00, 100.01, 2), ("short", 100.01, 100.00, 2)],
)
def test_derives_signed_gross_from_authenticated_side_and_fractional_quantity(
    tmp_path, side, entry, exit, expected_gross
):
    service = CanonicalAccountLedger(tmp_path / "ledger.sqlite", account_id="paper-total")
    trade = _trade(
        "fractional",
        "2026-08-25",
        side=side,
        entry_price=entry,
        exit_price=exit,
        quantity=1.5,
    )
    result = service.build(
        account=_account(), expected_sessions=[_session("2026-08-25")], trades=[trade]
    )
    row = result.rows[0]
    assert row["realized_gross_pnl_cents"] == expected_gross
    assert row["realized_net_pnl_cents"] == expected_gross - 6


def test_arbitrary_fill_flags_and_wrong_session_identity_never_realize(tmp_path):
    service = CanonicalAccountLedger(tmp_path / "ledger.sqlite", account_id="paper-total")
    forged = _trade("forged", "2026-08-25")
    forged["fill_truth"] = {"status": "COMMITTED"}
    result = service.build(
        account=_account(), expected_sessions=[_session("2026-08-25")], trades=[forged]
    )
    assert result.rows[0]["status"] == "PARTIAL"

    wrong_session = _trade("wrong-session", "2026-08-25")
    payload = wrong_session["fill_truth"].to_dict()
    payload["session_id"] = "other-session"
    payload["receipt_hash_sha256"] = hashlib.sha256(
        canonical_json(
            {key: value for key, value in payload.items() if key != "receipt_hash_sha256"}
        ).encode()
    ).hexdigest()
    wrong_session["fill_truth"] = _mint_authenticated_fill_truth(payload)
    result = service.build(
        account=_account(), expected_sessions=[_session("2026-08-25")], trades=[wrong_session]
    )
    assert result.rows[0]["status"] == "PARTIAL"
