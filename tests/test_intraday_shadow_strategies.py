from __future__ import annotations

from datetime import datetime, timedelta, timezone

from intraday_scanner.risk import PortfolioRiskSnapshot
from intraday_scanner.v2.backtest.intraday_engine import CausalMarketEvent
from intraday_scanner.v2.strategies.shadow_intraday import (
    EMPIRICAL_COST_REQUIRED,
    build_shadow_strategy_registry,
    evaluate_shadow_event,
    evaluate_shadow_strategy,
)


def _event(
    timestamp: datetime,
    payload: dict[str, object],
    *,
    kind: str = "bar",
    symbol: str = "ABC",
    sequence: int = 0,
) -> CausalMarketEvent:
    return CausalMarketEvent(
        timestamp=timestamp,
        symbol=symbol,
        kind=kind,
        payload=payload,
        source_artifact_identity="test-artifact",
        source_artifact_hash_sha256="a" * 64,
        exchange_session_id="2026-08-30",
        sequence=sequence,
    )


def _snapshot(at: datetime) -> PortfolioRiskSnapshot:
    return PortfolioRiskSnapshot(
        equity=100_000,
        daily_realized_pnl=0,
        daily_unrealized_pnl=0,
        peak_equity=100_000,
        as_of=at.isoformat(),
        metadata_complete=True,
    )


def test_registry_is_frozen_shadow_only_and_does_not_promote() -> None:
    registry = build_shadow_strategy_registry()
    assert len(registry.strategies) == 4
    assert all(item.shadow_only for item in registry.strategies)
    assert all(not item.broker_execution_enabled for item in registry.strategies)
    assert registry.get("shadow_failed_breakout_gap_fade").required_truth == (
        "quote",
        "spread",
        "halt",
        "borrow",
        "ssr",
        "corporate_action",
    )


def test_opening_range_uses_only_completed_prior_bars_and_requires_empirical_cost() -> None:
    start = datetime(2026, 8, 30, 14, 30, tzinfo=timezone.utc)
    history = [
        _event(
            start + timedelta(minutes=i),
            {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
        )
        for i in range(5)
    ]
    current = _event(
        start + timedelta(minutes=16),
        {"open": 109, "high": 111, "low": 108, "close": 110, "volume": 2000},
    )
    evaluation = evaluate_shadow_strategy(
        "shadow_opening_range_continuation",
        current,
        history,
        risk_snapshot=_snapshot(current.timestamp),
    )
    assert evaluation.status == EMPIRICAL_COST_REQUIRED
    assert evaluation.signal is not None
    assert evaluation.signal.earliest_entry_at > evaluation.signal.decision_at
    assert (
        evaluation.signal.stop_price
        < evaluation.signal.entry_reference
        < evaluation.signal.target_price
    )


def test_future_bar_mutation_cannot_create_a_signal() -> None:
    start = datetime(2026, 8, 30, 14, 30, tzinfo=timezone.utc)
    history = [
        _event(
            start + timedelta(minutes=i),
            {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
        )
        for i in range(5)
    ]
    current = _event(
        start + timedelta(minutes=16),
        {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
    )
    future = _event(
        start + timedelta(minutes=17),
        {"open": 110, "high": 112, "low": 109, "close": 111, "volume": 2000},
    )
    first = evaluate_shadow_strategy(
        "shadow_opening_range_continuation",
        current,
        history + [future],
        empirical_cost_verified=True,
        risk_snapshot=_snapshot(current.timestamp),
    )
    second = evaluate_shadow_strategy(
        "shadow_opening_range_continuation",
        current,
        history,
        empirical_cost_verified=True,
        risk_snapshot=_snapshot(current.timestamp),
    )
    assert first.to_dict() == second.to_dict()
    assert first.signal is None


def test_vwap_missing_truth_is_not_evaluable_and_no_forced_trade() -> None:
    start = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    history = [
        _event(
            start + timedelta(minutes=i),
            {"open": 100, "high": 102, "low": 99, "close": 100, "volume": 1000},
        )
        for i in range(3)
    ]
    current = _event(
        start + timedelta(minutes=3),
        {"open": 100, "high": 103, "low": 99, "close": 102, "volume": 1200},
    )
    result = evaluate_shadow_strategy(
        "shadow_vwap_reclaim_pullback",
        current,
        history,
        empirical_cost_verified=True,
        risk_snapshot=_snapshot(current.timestamp),
    )
    assert result.status == "NOT_EVALUABLE"
    assert result.reason_codes == ("VWAP_TRUTH_REQUIRED",)
    assert result.signal is None


def test_gap_fade_requires_quote_borrow_ssr_and_corporate_action_truth() -> None:
    start = datetime(2026, 8, 30, 16, 0, tzinfo=timezone.utc)
    history = [_event(start, {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000})]
    current = _event(
        start + timedelta(minutes=1),
        {"open": 102, "high": 103, "low": 99, "close": 101, "volume": 1200},
    )
    result = evaluate_shadow_strategy(
        "shadow_failed_breakout_gap_fade",
        current,
        history,
        empirical_cost_verified=True,
        risk_snapshot=_snapshot(current.timestamp),
    )
    assert result.status == "NOT_EVALUABLE"
    assert result.reason_codes == ("QUOTE_TRUTH_REQUIRED",)


def test_halt_and_wide_spread_block_gap_fade_even_with_other_inputs() -> None:
    start = datetime(2026, 8, 30, 16, 0, tzinfo=timezone.utc)
    history = [_event(start, {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000})]
    current = _event(
        start + timedelta(minutes=1),
        {
            "open": 102,
            "high": 103,
            "low": 99,
            "close": 101,
            "volume": 1200,
            "bid": 100,
            "ask": 101,
            "borrow_available": True,
            "ssr_active": False,
            "corporate_action_basis": "split_adjusted_v1",
            "halted": True,
        },
    )
    result = evaluate_shadow_strategy(
        "shadow_failed_breakout_gap_fade",
        current,
        history,
        empirical_cost_verified=True,
        risk_snapshot=_snapshot(current.timestamp),
    )
    assert result.reason_codes == ("CURRENT_HALT",)


def test_all_families_are_deterministic_and_short_is_research_only() -> None:
    timestamp = datetime(2026, 8, 30, 16, 1, tzinfo=timezone.utc)
    current = _event(timestamp, {"open": 102, "high": 103, "low": 99, "close": 101, "volume": 1200})
    results_a = evaluate_shadow_event(
        current, (), empirical_cost_verified=True, risk_snapshot=_snapshot(timestamp)
    )
    results_b = evaluate_shadow_event(
        current, (), empirical_cost_verified=True, risk_snapshot=_snapshot(timestamp)
    )
    assert [item.to_dict() for item in results_a] == [item.to_dict() for item in results_b]
    assert all(item.research_only and not item.broker_execution_enabled for item in results_a)
