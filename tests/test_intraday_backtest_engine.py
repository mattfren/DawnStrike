from __future__ import annotations

from datetime import datetime, timedelta, timezone

from intraday_scanner.v2.backtest.intraday_engine import (
    CausalMarketEvent,
    IntradayBacktestSettings,
    build_expanding_walk_forward_folds,
    run_intraday_backtest,
)

UTC = timezone.utc
START = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)


def _event(index: int, **payload: object) -> CausalMarketEvent:
    return CausalMarketEvent(
        timestamp=START + timedelta(minutes=index),
        symbol="NOVA",
        kind="bar",
        payload=payload,
        source_artifact_identity="fixture:bars:NOVA:2026-08-03",
        source_artifact_hash_sha256="bars-hash",
        exchange_session_id="XNYS:2026-08-03:regular",
    )


def _symbol_event(
    symbol: str,
    index: int,
    *,
    kind: str = "bar",
    **payload: object,
) -> CausalMarketEvent:
    return CausalMarketEvent(
        timestamp=START + timedelta(minutes=index),
        symbol=symbol,
        kind=kind,
        payload=payload,
        source_artifact_identity=f"fixture:bars:{symbol}:2026-08-03",
        source_artifact_hash_sha256=f"bars-hash-{symbol}",
        exchange_session_id="XNYS:2026-08-03:regular",
    )


def test_intraday_engine_enters_only_after_signal_event_and_records_cost_status() -> None:
    events = (
        _event(0, open=10.0, high=10.2, low=9.9, close=10.1),
        _event(1, open=10.2, high=10.4, low=10.1, close=10.3),
        _event(2, open=10.3, high=11.1, low=10.2, close=10.8),
    )

    def provider(event: CausalMarketEvent, history: tuple[CausalMarketEvent, ...]):
        if event.timestamp == START:
            assert history == ()
            return {
                "policy_eligible": True,
                "direction": "long",
                "stop": 9.0,
                "target": 11.0,
                "quantity": 1,
            }
        return None

    result = run_intraday_backtest(events, signal_provider=provider)

    assert len(result.trades) == 1
    assert result.trades[0].entry_at == START + timedelta(minutes=1)
    assert result.trades[0].exit_reason == "target_first"
    assert "COST_MODEL_PROVISIONAL" in result.statuses
    assert "NOT_EVALUABLE_PENDING_EMPIRICAL_COST" in result.statuses
    assert result.metrics["broker_execution_enabled"] is False


def test_same_bar_target_and_stop_is_conservative_and_not_silent() -> None:
    events = (
        _event(0, open=10.0, high=10.1, low=9.9, close=10.0),
        _event(1, open=10.0, high=11.1, low=8.9, close=10.0),
    )

    result = run_intraday_backtest(
        events,
        signal_provider=lambda event, _history: {
            "policy_eligible": True,
            "direction": "long",
            "stop": 9.0,
            "target": 11.0,
        }
        if event.timestamp == START
        else None,
    )

    assert result.trades[0].path_truth_status == "SAME_MINUTE_AMBIGUOUS"
    assert result.trades[0].exit_reason == "same_minute_ambiguous_stop_first"


def test_walk_forward_folds_are_expanding_and_disjoint() -> None:
    dates = [f"2026-08-{day:02d}" for day in range(1, 32)]
    folds = build_expanding_walk_forward_folds(
        dates,
        minimum_training_sessions=10,
        validation_sessions=3,
        holdout_sessions=3,
        purge_sessions=1,
        embargo_sessions=1,
    )

    assert folds
    assert len(folds[1].training_dates) > len(folds[0].training_dates)
    for fold in folds:
        all_sets = (
            set(fold.training_dates),
            set(fold.validation_dates),
            set(fold.holdout_dates),
            set(fold.purged_dates),
            set(fold.embargoed_dates),
        )
        assert all_sets[0].isdisjoint(set().union(*all_sets[1:]))
        assert fold.no_lookahead is True


def _two_position_events(
    *, reverse_same_clock_bars: bool = False
) -> tuple[CausalMarketEvent, ...]:
    decisions = (
        _symbol_event(
            "ALFA",
            0,
            kind="decision",
            signal={
                "policy_eligible": True,
                "direction": "long",
                "stop": 0.0,
                "target": 100.0,
                "quantity": 1,
            },
        ),
        _symbol_event(
            "BRAVO",
            0,
            kind="decision",
            signal={
                "policy_eligible": True,
                "direction": "short",
                "stop": 100.0,
                "target": 1.0,
                "quantity": 1,
            },
        ),
    )
    bars = (
        _symbol_event("ALFA", 1, open=10.0, high=11.0, low=9.5, close=11.0),
        _symbol_event("BRAVO", 1, open=20.0, high=20.5, low=17.5, close=18.0),
    )
    if reverse_same_clock_bars:
        bars = tuple(reversed(bars))
    return decisions + bars


def test_mark_to_market_includes_simultaneous_long_and_short_positions() -> None:
    result = run_intraday_backtest(
        _two_position_events(),
        settings=IntradayBacktestSettings(
            entry_slippage_bps=0.0,
            exit_slippage_bps=0.0,
            commission_per_share_per_side=0.0,
        ),
    )

    # The old event.symbol-only mark omitted ALFA at BRAVO's event and
    # produced 100002.  Both open positions now contribute: +1 and +2.
    assert result.equity_curve[-1].equity == 100003.0
    assert result.equity_curve[-1].open_positions == 2


def test_same_clock_mark_order_is_deterministic() -> None:
    settings = IntradayBacktestSettings(
        entry_slippage_bps=0.0,
        exit_slippage_bps=0.0,
        commission_per_share_per_side=0.0,
    )
    forward = run_intraday_backtest(_two_position_events(), settings=settings)
    reverse = run_intraday_backtest(
        _two_position_events(reverse_same_clock_bars=True), settings=settings
    )

    assert forward.equity_curve == reverse.equity_curve
    assert forward.trades == reverse.trades


def test_mark_uses_latest_causal_price_and_keeps_missing_price_unmarked() -> None:
    events = (
        _symbol_event(
            "ALFA",
            0,
            kind="decision",
            signal={
                "policy_eligible": True,
                "direction": "long",
                "stop": 0.0,
                "target": 100.0,
            },
        ),
        _symbol_event(
            "BRAVO",
            0,
            kind="decision",
            signal={
                "policy_eligible": True,
                "direction": "short",
                "stop": 100.0,
                "target": 1.0,
            },
        ),
        _symbol_event("ALFA", 1, open=10.0, high=11.0, low=9.5, close=11.0),
        # BRAVO has an open position but no close mark at this event.  Its
        # missing price must not be replaced by zero or its entry price.
        _symbol_event("BRAVO", 1, open=20.0, high=20.5, low=17.5),
        # The ALFA mark is stale but still causally available and remains in
        # the account equity until ALFA receives a newer mark or closes.
        _symbol_event("BRAVO", 2, open=18.0, high=18.5, low=17.5, close=18.0),
    )
    result = run_intraday_backtest(
        events,
        settings=IntradayBacktestSettings(
            entry_slippage_bps=0.0,
            exit_slippage_bps=0.0,
            commission_per_share_per_side=0.0,
        ),
    )

    assert result.equity_curve[1].equity == 100001.0
    assert result.equity_curve[1].open_positions == 2
    assert result.equity_curve[2].equity == 100003.0


def test_closed_position_is_removed_and_its_mark_is_not_double_counted() -> None:
    events = (
        _symbol_event(
            "ALFA",
            0,
            kind="decision",
            signal={
                "policy_eligible": True,
                "direction": "long",
                "stop": 0.0,
                "target": 12.0,
            },
        ),
        _symbol_event("ALFA", 1, open=10.0, high=10.5, low=9.5, close=11.0),
        _symbol_event("ALFA", 2, open=11.0, high=12.1, low=10.5, close=12.0),
    )
    result = run_intraday_backtest(
        events,
        settings=IntradayBacktestSettings(
            entry_slippage_bps=0.0,
            exit_slippage_bps=0.0,
            commission_per_share_per_side=0.0,
        ),
    )

    assert result.equity_curve[-1].equity == 100002.0
    assert result.equity_curve[-1].open_positions == 0
    assert len(result.trades) == 1
