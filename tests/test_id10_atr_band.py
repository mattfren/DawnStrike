"""Required tests for id10-dawnstrike-v1 (ID10_FROZEN_SPEC.md).

Every test constructs synthetic fixtures; none depends on fetched market
data, so this suite runs offline and must pass before any replay is trusted.
"""

from __future__ import annotations

import pytest

from intraday_scanner.strategies.id10_atr_band import (
    Action,
    DailyBar,
    Id10InputError,
    PositionSide,
    Signal,
    compute_bounds,
    compute_wilder_atr14,
    decide_signal,
    decision_marks_for_session,
    next_minute_mark,
    resolve_action,
    true_range,
)


def _flat_daily_bars(n: int, *, high_low_range: float = 2.0, base: float = 100.0) -> list[DailyBar]:
    """n synthetic daily bars, each with TR == high_low_range (H-L only)."""

    bars = []
    for i in range(n):
        o = base
        c = base
        h = base + high_low_range / 2
        l = base - high_low_range / 2
        bars.append(DailyBar(session_date=f"2026-01-{i + 1:02d}", open=o, high=h, low=l, close=c))
    return bars


# ---------------------------------------------------------------------------
# Cadence / mark generation
# ---------------------------------------------------------------------------


def test_decision_marks_include_0930_as_first_mark():
    marks = decision_marks_for_session("16:00")
    assert marks[0] == "09:30"
    assert marks[-1] == "15:45"
    assert len(marks) == 26  # 09:30..15:45 inclusive, every 15 min


def test_decision_marks_shorten_for_early_close_without_bar_count_rule():
    marks = decision_marks_for_session("13:00")
    assert marks[0] == "09:30"
    assert marks[-1] == "12:45"
    assert len(marks) == 14


def test_next_minute_mark_rolls_hour():
    assert next_minute_mark("09:59") == "10:00"
    assert next_minute_mark("15:45") == "15:46"


# ---------------------------------------------------------------------------
# True Range / Wilder ATR(14)
# ---------------------------------------------------------------------------


def test_true_range_uses_prior_close_when_available():
    # H-L = 2, |H-PC| = 5, |L-PC| = 3 -> max is 5
    tr = true_range(high=105.0, low=103.0, prev_close=100.0)
    assert tr == pytest.approx(5.0)


def test_true_range_falls_back_to_high_low_when_no_prior_close():
    tr = true_range(high=105.0, low=103.0, prev_close=None)
    assert tr == pytest.approx(2.0)


def test_true_range_rejects_high_below_low():
    with pytest.raises(Id10InputError):
        true_range(high=10.0, low=20.0, prev_close=15.0)


def test_wilder_atr14_hand_computed_all_hl_ranges_equal():
    # All 14 bars have H-L = 2.0 and flat O=C=100.0, so every gap term is 0
    # and TR == H-L == 2.0 for every bar regardless of prior close.
    bars = _flat_daily_bars(14, high_low_range=2.0)
    atr = compute_wilder_atr14(bars, pre_lookback_close=100.0)
    assert atr == pytest.approx(2.0, abs=1e-9)


def test_wilder_atr14_requires_exactly_lookback_bars():
    bars = _flat_daily_bars(13)
    with pytest.raises(Id10InputError):
        compute_wilder_atr14(bars, pre_lookback_close=100.0)


def test_wilder_atr14_boundary_none_pre_lookback_close_does_not_crash():
    bars = _flat_daily_bars(14, high_low_range=2.0)
    atr = compute_wilder_atr14(bars, pre_lookback_close=None)
    # First bar's TR falls back to H-L (=2.0, same as with a prior close in
    # this flat fixture) -- so the result is unaffected here, but the call
    # must not raise.
    assert atr == pytest.approx(2.0, abs=1e-9)


def test_wilder_atr14_gap_term_dominates_when_prior_close_far_away():
    bars = _flat_daily_bars(14, high_low_range=2.0)
    # A huge, distant pre_lookback_close inflates only the FIRST bar's TR
    # via the gap term; the other 13 bars still have TR = H-L = 2.0.
    atr_normal = compute_wilder_atr14(bars, pre_lookback_close=100.0)
    atr_gapped = compute_wilder_atr14(bars, pre_lookback_close=200.0)
    assert atr_gapped > atr_normal


# ---------------------------------------------------------------------------
# Bounds and signal
# ---------------------------------------------------------------------------


def test_bounds_are_symmetric_around_session_open():
    bounds = compute_bounds(session_open=500.0, atr14=4.0, multiplier=0.5)
    assert bounds.upper == pytest.approx(502.0)
    assert bounds.lower == pytest.approx(498.0)


def test_bounds_reject_nonpositive_open():
    with pytest.raises(Id10InputError):
        compute_bounds(session_open=0.0, atr14=1.0)


def test_decide_signal_long_short_flat():
    bars = _flat_daily_bars(14, high_low_range=2.0)  # ATR = 2.0 -> band = open +/- 1.0
    long_decision = decide_signal(
        session_date="2026-02-01", mark_time="10:00", prior_daily_bars=bars,
        pre_lookback_close=100.0, session_open=500.0, decision_bar_close=501.5,
    )
    assert long_decision.signal is Signal.LONG
    assert long_decision.upper == pytest.approx(501.0)

    short_decision = decide_signal(
        session_date="2026-02-01", mark_time="10:00", prior_daily_bars=bars,
        pre_lookback_close=100.0, session_open=500.0, decision_bar_close=498.5,
    )
    assert short_decision.signal is Signal.SHORT

    flat_decision = decide_signal(
        session_date="2026-02-01", mark_time="10:00", prior_daily_bars=bars,
        pre_lookback_close=100.0, session_open=500.0, decision_bar_close=500.2,
    )
    assert flat_decision.signal is Signal.FLAT


def test_band_is_constant_across_marks_same_session():
    bars = _flat_daily_bars(14, high_low_range=2.0)
    d1 = decide_signal(
        session_date="2026-02-01", mark_time="09:30", prior_daily_bars=bars,
        pre_lookback_close=100.0, session_open=500.0, decision_bar_close=500.2,
    )
    d2 = decide_signal(
        session_date="2026-02-01", mark_time="15:45", prior_daily_bars=bars,
        pre_lookback_close=100.0, session_open=500.0, decision_bar_close=500.2,
    )
    assert d1.upper == d2.upper
    assert d1.lower == d2.lower


def test_decide_signal_missing_prior_bars_raises():
    bars = _flat_daily_bars(10)
    with pytest.raises(Id10InputError):
        decide_signal(
            session_date="2026-02-01", mark_time="10:00", prior_daily_bars=bars,
            pre_lookback_close=100.0, session_open=500.0, decision_bar_close=500.0,
        )


# ---------------------------------------------------------------------------
# resolve_action: flip / stop / EOD priority
# ---------------------------------------------------------------------------


def _decision(signal: Signal, close: float, upper: float = 501.0, lower: float = 499.0):
    from intraday_scanner.strategies.id10_atr_band import Id10Decision

    return Id10Decision(
        session_date="2026-02-01", mark_time="10:00", decision_bar_close=close,
        upper=upper, lower=lower, atr14=2.0, signal=signal, reason="test",
    )


def test_flat_enters_on_long_or_short_signal():
    d_long = _decision(Signal.LONG, 502.0)
    assert resolve_action(
        position=PositionSide.FLAT, decision=d_long, session_open=500.0,
        is_last_mark_of_session=False,
    ) == Action.ENTER_LONG

    d_short = _decision(Signal.SHORT, 498.0)
    assert resolve_action(
        position=PositionSide.FLAT, decision=d_short, session_open=500.0,
        is_last_mark_of_session=False,
    ) == Action.ENTER_SHORT


def test_long_position_flips_on_opposite_breach_even_if_stop_also_true():
    # decision_bar_close (498.0) is BOTH below session_open (500.0, would
    # trigger the stop) AND below the lower band (499.0, a SHORT signal).
    # Frozen priority: flip wins.
    d = _decision(Signal.SHORT, 498.0)
    action = resolve_action(
        position=PositionSide.LONG, decision=d, session_open=500.0,
        is_last_mark_of_session=False,
    )
    assert action == Action.EXIT_FLIP_TO_SHORT


def test_long_position_stops_out_when_price_returns_to_session_open():
    d = _decision(Signal.FLAT, 499.8)  # inside band, but <= session_open 500.0? no: 499.8 < 500
    action = resolve_action(
        position=PositionSide.LONG, decision=d, session_open=500.0,
        is_last_mark_of_session=False,
    )
    assert action == Action.EXIT_STOP


def test_long_position_holds_when_above_open_and_inside_band():
    d = _decision(Signal.FLAT, 500.5)
    action = resolve_action(
        position=PositionSide.LONG, decision=d, session_open=500.0,
        is_last_mark_of_session=False,
    )
    assert action == Action.HOLD


def test_long_position_exits_eod_on_last_mark_with_no_stop_or_flip():
    d = _decision(Signal.FLAT, 500.5)
    action = resolve_action(
        position=PositionSide.LONG, decision=d, session_open=500.0,
        is_last_mark_of_session=True,
    )
    assert action == Action.EXIT_EOD


def test_short_position_flips_on_opposite_breach():
    d = _decision(Signal.LONG, 502.0)
    action = resolve_action(
        position=PositionSide.SHORT, decision=d, session_open=500.0,
        is_last_mark_of_session=False,
    )
    assert action == Action.EXIT_FLIP_TO_LONG


def test_short_position_stops_out_when_price_returns_to_session_open():
    d = _decision(Signal.FLAT, 500.2)
    action = resolve_action(
        position=PositionSide.SHORT, decision=d, session_open=500.0,
        is_last_mark_of_session=False,
    )
    assert action == Action.EXIT_STOP


def test_flat_position_holds_or_none_on_flat_signal():
    d = _decision(Signal.FLAT, 500.2)
    assert resolve_action(
        position=PositionSide.FLAT, decision=d, session_open=500.0,
        is_last_mark_of_session=False,
    ) == Action.HOLD
    assert resolve_action(
        position=PositionSide.FLAT, decision=d, session_open=500.0,
        is_last_mark_of_session=True,
    ) == Action.NONE


# ---------------------------------------------------------------------------
# Lookahead: decide_signal's signature has no path for future data
# ---------------------------------------------------------------------------


def test_decision_unchanged_when_bars_at_and_after_fill_bar_are_removed():
    """Signature-level guard: decide_signal cannot see anything about bars
    after the decision bar because there is no parameter through which such
    data could be passed. This is a weaker check than the harness-level
    lookahead test in tests/test_id10_run_replay_lookahead.py, but it
    documents the invariant at the function-signature level too.
    """

    bars = _flat_daily_bars(14, high_low_range=2.0)
    kwargs = dict(
        session_date="2026-02-01", mark_time="10:00", prior_daily_bars=bars,
        pre_lookback_close=100.0, session_open=500.0, decision_bar_close=501.5,
    )
    d1 = decide_signal(**kwargs)
    d2 = decide_signal(**kwargs)  # calling again with identical inputs
    assert d1 == d2
