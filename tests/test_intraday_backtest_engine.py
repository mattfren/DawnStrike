from __future__ import annotations

from datetime import datetime, timedelta, timezone

from intraday_scanner.v2.backtest.intraday_engine import (
    CausalMarketEvent,
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
