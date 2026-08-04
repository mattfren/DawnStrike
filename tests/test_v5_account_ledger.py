from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_ACCOUNT_ID,
    ALPHAOPS_V5_COST_MODEL_VERSION,
    ALPHAOPS_V5_POLICY_VERSION,
    ALPHAOPS_V5_STRATEGY_ID,
    ALPHAOPS_V5_STRATEGY_VERSION,
)
from intraday_scanner.performance.service import CanonicalPerformanceService
from intraday_scanner.storage.migrations import run_migrations
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

DAY = "2026-07-31"


def test_v5_account_ledger_enforces_equity_identity_and_account_return(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v5-ledger.sqlite"
    _initialize(db_path)
    with sqlite3.connect(db_path) as connection:
        _insert_trade(connection)

    result = CanonicalPerformanceService(db_path).reconcile(
        market_date=DAY,
        now=f"{DAY}T21:00:00+00:00",
    )

    ledger = result["account_ledger"][0]
    assert ledger["account_id"] == ALPHAOPS_V5_ACCOUNT_ID
    assert ledger["beginning_equity_cents"] == 10_000_000
    assert ledger["external_flow_cents"] == 0
    assert ledger["realized_net_pnl_cents"] == 20_000
    assert ledger["ending_equity_cents"] == 10_020_000
    assert ledger["accounting_delta_cents"] == 0
    assert ledger["net_return_pct"] == 0.2
    assert ledger["cash_cents"] == 10_020_000
    assert ledger["position_market_value_cents"] == 0

    daily = next(
        row
        for row in result["daily"]
        if row["strategy_id"] == ALPHAOPS_V5_STRATEGY_ID
    )
    assert daily["account_id"] == ALPHAOPS_V5_ACCOUNT_ID
    assert daily["return_pct"] == 0.2
    assert daily["return_basis"] == "account_equity_identity_after_external_flows"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT opening_equity_cents FROM paper_accounts WHERE account_id = ?",
            (ALPHAOPS_V5_ACCOUNT_ID,),
        ).fetchone() == (10_000_000,)


def test_v5_explicit_no_trade_is_observed_zero_not_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "v5-no-trade.sqlite"
    _initialize(db_path)
    with sqlite3.connect(db_path) as connection:
        _insert_scorecard(connection, session_status="explicit_no_trade")

    result = CanonicalPerformanceService(db_path).reconcile(
        market_date=DAY,
        now=f"{DAY}T21:00:00+00:00",
    )

    ledger = result["account_ledger"][0]
    assert ledger["status"] == "NO_TRADE"
    assert ledger["observed_zero"] is True
    assert ledger["net_return_pct"] == 0.0
    daily = next(
        row
        for row in result["daily"]
        if row["strategy_id"] == ALPHAOPS_V5_STRATEGY_ID
    )
    assert daily["status"] == "NO_TRADE"
    assert daily["return_pct"] == 0.0
    public = CanonicalPerformanceService(db_path).load_public_data(market_date=DAY)
    assert {
        item["state"] for item in public["safety_evidence"].values()
    } == {"verified"}


def test_v5_canonical_no_trade_selection_is_observed_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "v5-selection-no-trade.sqlite"
    _initialize(db_path)
    no_trade_day = "2026-08-04"
    signal_id = f"no_trade:scan-v5:{no_trade_day}"
    payload = {
        **_identity_payload(),
        "decision": "no_trade",
        "ticker": "NO_TRADE",
        "rank": 0,
        "signal_id": signal_id,
        "decision_payload": {"no_trade": True, "reason": "No clean edge."},
        "research_only": True,
        "broker_execution_enabled": False,
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO signal_selections
            (selection_id, scan_id, signal_id, ticker, rank, strategy_id,
             strategy_version, cohort, decision, selected_at, event_key,
             body_sha256, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "selection-no-trade-v5",
                "scan-v5",
                signal_id,
                "NO_TRADE",
                0,
                ALPHAOPS_V5_STRATEGY_ID,
                ALPHAOPS_V5_STRATEGY_VERSION,
                "official_telegram",
                "no_trade",
                f"{no_trade_day}T13:35:00+00:00",
                "alphaops:scan-v5:alpha_no_trade",
                "a" * 64,
                json.dumps(payload, sort_keys=True),
            ),
        )

    result = CanonicalPerformanceService(db_path).reconcile(
        market_date=no_trade_day,
        now=f"{no_trade_day}T21:00:00+00:00",
    )

    ledger = next(
        row
        for row in result["account_ledger"]
        if row["market_date"] == no_trade_day
    )
    assert ledger["status"] == "NO_TRADE"
    assert ledger["observed_zero"] is True
    assert ledger["net_return_pct"] == 0.0
    assert ledger["beginning_equity_cents"] is None
    assert ledger["ending_equity_cents"] is None
    daily = next(
        row
        for row in result["daily"]
        if row["strategy_id"] == ALPHAOPS_V5_STRATEGY_ID
        and row["market_date"] == no_trade_day
    )
    assert daily["status"] == "NO_TRADE"
    assert daily["return_pct"] == 0.0
    assert daily["cost_status"] == "complete"
    assert daily["return_basis"] == "explicit_no_trade_observed_zero"


def test_v5_missing_session_never_becomes_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "v5-missing.sqlite"
    _initialize(db_path)

    result = CanonicalPerformanceService(db_path).reconcile(
        market_date=DAY,
        now=f"{DAY}T21:00:00+00:00",
    )

    ledger = result["account_ledger"][0]
    assert ledger["status"] == "MISSING"
    assert ledger["observed_zero"] is False
    assert ledger["realized_net_pnl_cents"] is None
    assert ledger["ending_equity_cents"] is None
    assert ledger["net_return_pct"] is None
    daily = next(
        row
        for row in result["daily"]
        if row["strategy_id"] == ALPHAOPS_V5_STRATEGY_ID
    )
    assert daily["status"] == "PARTIAL"
    assert daily["return_pct"] is None


def test_canonical_eod_trade_supersedes_matching_watcher_position(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v5-dedup.sqlite"
    _initialize(db_path)
    with sqlite3.connect(db_path) as connection:
        _insert_trade(connection)
        payload = _identity_payload()
        connection.execute(
            """
            INSERT INTO paper_positions
            (position_id, signal_id, market_date, ticker, status, quantity,
             entry_intent_id, exit_intent_id, opened_at, closed_at, entry_price,
             exit_price, stop_price, target_price, notional, realized_pnl,
             realized_return_pct, max_favorable_excursion,
             max_adverse_excursion, updated_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "watcher-position-v5",
                "signal-v5",
                DAY,
                "NOVA",
                "CLOSED",
                1_000,
                "entry-v5",
                "exit-v5",
                f"{DAY}T14:00:00+00:00",
                f"{DAY}T20:00:00+00:00",
                10.0,
                10.25,
                9.5,
                11.5,
                10_000.0,
                200.0,
                2.0,
                None,
                None,
                f"{DAY}T20:00:00+00:00",
                json.dumps(payload, sort_keys=True),
            ),
        )

    result = CanonicalPerformanceService(db_path).reconcile(
        market_date=DAY,
        now=f"{DAY}T21:00:00+00:00",
    )

    ids = {row["record_id"] for row in result["rows"]}
    assert "strategy_trade:trade-v5" in ids
    assert "paper_position:watcher-position-v5" not in ids
    daily = [
        row
        for row in result["daily"]
        if row["strategy_id"] == ALPHAOPS_V5_STRATEGY_ID
    ]
    assert len(daily) == 1
    assert daily[0]["net_pnl_cents"] == 20_000


def _initialize(path: Path) -> None:
    SQLiteScanStore(path).initialize()
    with sqlite3.connect(path) as connection:
        run_migrations(connection)


def _identity_payload() -> dict[str, object]:
    return {
        "selection_id": "selection-v5",
        "signal_id": "signal-v5",
        "strategy_id": ALPHAOPS_V5_STRATEGY_ID,
        "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
        "cohort": "official_telegram",
        "account_id": ALPHAOPS_V5_ACCOUNT_ID,
        "execution_policy_version": ALPHAOPS_V5_POLICY_VERSION,
        "cost_model_version": ALPHAOPS_V5_COST_MODEL_VERSION,
        "gross_pnl": 250.0,
        "fees": 10.0,
        "slippage_cost": 40.0,
        "net_pnl": 200.0,
    }


def _insert_trade(connection: sqlite3.Connection) -> None:
    payload = _identity_payload()
    connection.execute(
        """
        INSERT INTO strategy_paper_trades
        (trade_id, selection_id, signal_id, market_date, ticker, strategy_id,
         strategy_version, cohort, direction, decision_time, entry_time,
         entry_fill_price, exit_time, exit_fill_price, exit_reason, quantity,
         notional, net_pnl, net_return_pct, r_multiple, fees, slippage_cost,
         source_bar_hash_sha256, execution_policy_version, created_at,
         payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?)
        """,
        (
            "trade-v5",
            "selection-v5",
            "signal-v5",
            DAY,
            "NOVA",
            ALPHAOPS_V5_STRATEGY_ID,
            ALPHAOPS_V5_STRATEGY_VERSION,
            "official_telegram",
            "long",
            f"{DAY}T13:55:00+00:00",
            f"{DAY}T14:00:00+00:00",
            10.0,
            f"{DAY}T20:00:00+00:00",
            10.25,
            "eod_close",
            1_000.0,
            10_000.0,
            200.0,
            2.0,
            0.8,
            10.0,
            40.0,
            "a" * 64,
            ALPHAOPS_V5_POLICY_VERSION,
            f"{DAY}T21:00:00+00:00",
            json.dumps(payload, sort_keys=True),
        ),
    )


def _insert_scorecard(
    connection: sqlite3.Connection,
    *,
    session_status: str,
) -> None:
    payload = {
        **_identity_payload(),
        "scorecard_id": "scorecard-v5",
        "session_status": session_status,
        "reconciliation_status": "complete",
    }
    connection.execute(
        """
        INSERT INTO daily_strategy_scorecards
        (scorecard_id, market_date, strategy_id, strategy_version, cohort,
         execution_policy_version, selected_count, delivered_count,
         resolved_count, triggered_count, not_triggered_count, filled_count,
         closed_count, unresolved_count, wins, losses, flats,
         activation_rate_pct, win_rate_pct, average_net_return_pct, net_pnl,
         return_on_allocated_capital_pct, average_r, expectancy_r,
         profit_factor, fees, slippage_cost, reconciliation_status, created_at,
         session_status, no_trade_count, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "scorecard-v5",
            DAY,
            ALPHAOPS_V5_STRATEGY_ID,
            ALPHAOPS_V5_STRATEGY_VERSION,
            "official_telegram",
            ALPHAOPS_V5_POLICY_VERSION,
            0,
            1,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            None,
            0.0,
            None,
            None,
            None,
            None,
            0.0,
            0.0,
            "complete",
            f"{DAY}T21:00:00+00:00",
            session_status,
            1,
            json.dumps(payload, sort_keys=True),
        ),
    )
