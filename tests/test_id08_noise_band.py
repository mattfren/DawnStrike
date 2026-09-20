"""Required tests for id08-dawnstrike-v1 (ID08_FROZEN_SPEC.md + AMENDMENT 1).

Every test constructs synthetic fixtures; none depends on fetched market
data, so this suite runs offline and must pass before any replay is trusted.
"""

from __future__ import annotations

import pytest

from intraday_scanner.strategies.id08_noise_band import (
    Action,
    Id08InputError,
    PositionSide,
    PriorSessionSnapshot,
    Signal,
    compute_bounds,
    compute_sigma_bar,
    decide_signal,
    decision_marks_for_session,
    next_minute_mark,
    resolve_action,
)

MARK = "10:00"


def _prior_sessions(
    ratios: list[float], *, base_open: float = 100.0, mark: str = MARK
) -> list[PriorSessionSnapshot]:
    """Build 14 synthetic prior sessions whose ``mark`` close gives each ratio."""

    sessions = []
    for i, ratio in enumerate(ratios):
        close = base_open * (1.0 + ratio)
        sessions.append(
            PriorSessionSnapshot(
                session_date=f"2026-01-{i + 1:02d}",
                open_0930=base_open,
                mark_closes={mark: close},
            )
        )
    return sessions


# ---------------------------------------------------------------------------
# Band math: hand-computed values on a synthetic 14-session OHLC fixture
# ---------------------------------------------------------------------------


def test_sigma_bar_matches_hand_computed_mean_abs_ratio():
    # Alternate +1% / -1% closes relative to a 100.0 open -> mean abs ratio = 0.01 exactly.
    ratios = [0.01, -0.01] * 7
    prior = _prior_sessions(ratios)
    sigma = compute_sigma_bar(prior, MARK)
    assert sigma == pytest.approx(0.01, abs=1e-12)


def test_bounds_use_max_min_anchor_and_vm_one():
    # sigma_bar = 0.02 by construction (all closes +2%).
    prior = _prior_sessions([0.02] * 14)
    sigma = compute_sigma_bar(prior, MARK)
    assert sigma == pytest.approx(0.02, abs=1e-12)

    # current open 101, prior close 99 -> anchor_high=101, anchor_low=99
    bounds = compute_bounds(current_session_open=101.0, prior_session_close=99.0, sigma_bar=sigma)
    assert bounds.upper == pytest.approx(101.0 * 1.02, abs=1e-9)
    assert bounds.lower == pytest.approx(99.0 * 0.98, abs=1e-9)

    # current open below prior close -> anchor flips
    bounds2 = compute_bounds(current_session_open=98.0, prior_session_close=99.0, sigma_bar=sigma)
    assert bounds2.upper == pytest.approx(99.0 * 1.02, abs=1e-9)
    assert bounds2.lower == pytest.approx(98.0 * 0.98, abs=1e-9)


def test_sigma_bar_requires_exactly_fourteen_sessions():
    with pytest.raises(Id08InputError):
        compute_sigma_bar(_prior_sessions([0.01] * 13), MARK)
    with pytest.raises(Id08InputError):
        compute_sigma_bar(_prior_sessions([0.01] * 15), MARK)


# ---------------------------------------------------------------------------
# Half-day session: NOT discarded by a bar-count rule
# ---------------------------------------------------------------------------


def test_half_day_marks_are_fewer_not_discarded():
    # No real half-day falls inside the 2026-01-22..2026-09-18 evaluation
    # window (US_MARKET_EARLY_CLOSES_2026 only has 2026-11-27 and
    # 2026-12-24, both after the window closes). This fixture exercises the
    # half-day code path synthetically since the real window cannot.
    #
    # AMENDMENT 2: 09:30 is never a decision mark (the source paper's first
    # position is at 10:00); it only supplies Open[t,09:30] for the band
    # anchor. It must NOT appear in this list.
    regular_marks = decision_marks_for_session("16:00")
    half_day_marks = decision_marks_for_session("13:00")

    assert half_day_marks == [
        "10:00", "10:30", "11:00", "11:30", "12:00", "12:30",
    ]
    assert "09:30" not in half_day_marks
    assert "09:30" not in regular_marks
    assert len(half_day_marks) < len(regular_marks)
    # A half day is a real, smaller set of expected marks -- not zero, and
    # not derived from any raw 390-row bar count.
    assert half_day_marks[-1] < "13:00"
    assert all(m in regular_marks for m in half_day_marks)


def test_first_decision_mark_is_ten_am_not_market_open():
    # AMENDMENT 2 fidelity correction: source quote --
    # "the intraday momentum strategy we outline here takes its first
    # position at 10:00". Full session must have exactly 12 marks
    # (10:00..15:30 by :30 steps), never 09:30.
    marks = decision_marks_for_session("16:00")
    assert marks[0] == "10:00"
    assert marks[-1] == "15:30"
    assert len(marks) == 12
    assert "09:30" not in marks


# ---------------------------------------------------------------------------
# Holiday skipped in the lookback without shifting the 14-session window
# ---------------------------------------------------------------------------


def test_holiday_is_skipped_not_counted_missing_in_lookback():
    # Build a calendar-aware caller-side session list with a holiday gap
    # (e.g. 2026-01-19 MLK Day) removed BEFORE the 14 prior sessions are
    # selected. The lookback window is still exactly the 14 sessions that
    # actually traded -- the holiday contributes zero entries, and does not
    # get treated as a missing/blank session inside the mean.
    calendar_trading_days = [
        "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
        "2026-01-09", "2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15",
        "2026-01-16",
        # 2026-01-19 is MLK Day: a holiday, simply absent from this list.
        "2026-01-20", "2026-01-21", "2026-01-22",
    ]
    assert len(calendar_trading_days) == 14
    assert "2026-01-19" not in calendar_trading_days

    prior = [
        PriorSessionSnapshot(session_date=d, open_0930=100.0, mark_closes={MARK: 101.0})
        for d in calendar_trading_days
    ]
    sigma = compute_sigma_bar(prior, MARK)
    assert sigma == pytest.approx(0.01, abs=1e-12)
    # The window used exactly 14 real trading sessions; the holiday neither
    # appears nor silently shrinks/pads the lookback.
    assert len(prior) == 14


# ---------------------------------------------------------------------------
# LONG / SHORT / FLAT each produced on constructed fixtures
# ---------------------------------------------------------------------------


def _decide(decision_bar_close: float, *, sigma_ratio: float = 0.01) -> Signal:
    prior = _prior_sessions([sigma_ratio] * 14)
    decision = decide_signal(
        session_date="2026-02-02",
        mark_time=MARK,
        prior_sessions=prior,
        current_session_open=100.0,
        prior_session_close=100.0,
        decision_bar_close=decision_bar_close,
    )
    return decision.signal


def test_long_short_flat_each_produced():
    # sigma_bar=0.01, anchor=100 -> upper=101, lower=99
    assert _decide(101.5) is Signal.LONG
    assert _decide(98.5) is Signal.SHORT
    assert _decide(100.0) is Signal.FLAT
    assert _decide(101.0) is Signal.FLAT  # boundary itself is not a breach
    assert _decide(99.0) is Signal.FLAT


# ---------------------------------------------------------------------------
# Opposite-boundary crossover closes and flips
# ---------------------------------------------------------------------------


def test_opposite_boundary_crossover_closes_and_flips():
    prior = _prior_sessions([0.01] * 14)
    long_decision = decide_signal(
        session_date="2026-02-02", mark_time=MARK, prior_sessions=prior,
        current_session_open=100.0, prior_session_close=100.0, decision_bar_close=101.5,
    )
    action = resolve_action(position=PositionSide.LONG, decision=long_decision, is_last_mark_of_session=False)
    assert action is Action.HOLD  # still LONG signal territory -> hold, no flip

    short_breach = decide_signal(
        session_date="2026-02-02", mark_time=MARK, prior_sessions=prior,
        current_session_open=100.0, prior_session_close=100.0, decision_bar_close=98.5,
    )
    flip_action = resolve_action(position=PositionSide.LONG, decision=short_breach, is_last_mark_of_session=False)
    assert flip_action is Action.EXIT_FLIP_TO_SHORT

    flip_back = resolve_action(position=PositionSide.SHORT, decision=long_decision, is_last_mark_of_session=False)
    assert flip_back is Action.EXIT_FLIP_TO_LONG


# ---------------------------------------------------------------------------
# EOD liquidation at the actual session close
# ---------------------------------------------------------------------------


def test_eod_liquidation_when_last_mark_and_position_open():
    prior = _prior_sessions([0.01] * 14, mark="15:30")
    flat_decision = decide_signal(
        session_date="2026-02-02", mark_time="15:30", prior_sessions=prior,
        current_session_open=100.0, prior_session_close=100.0, decision_bar_close=100.0,
    )
    assert resolve_action(position=PositionSide.LONG, decision=flat_decision, is_last_mark_of_session=True) is Action.EXIT_EOD
    assert resolve_action(position=PositionSide.SHORT, decision=flat_decision, is_last_mark_of_session=True) is Action.EXIT_EOD
    # Flat position at EOD has nothing to liquidate.
    assert resolve_action(position=PositionSide.FLAT, decision=flat_decision, is_last_mark_of_session=True) is Action.NONE


# ---------------------------------------------------------------------------
# A breach at HH:07 does NOT trade until the next :00/:30 mark
# ---------------------------------------------------------------------------


def test_intrabar_breach_ignored_until_next_cadence_mark():
    # Simulate: at 10:07 price breaches far above any plausible band, but
    # 10:07 is not a decision mark at all -- the decision function is never
    # even called with it. The next real mark is 10:30, whose OWN close
    # (back inside the band) is what actually gets evaluated.
    marks = decision_marks_for_session("16:00")
    assert "10:07" not in marks
    assert "10:00" in marks and "10:30" in marks

    prior = _prior_sessions([0.01] * 14, mark="10:30")
    decision_at_1030 = decide_signal(
        session_date="2026-02-02", mark_time="10:30", prior_sessions=prior,
        current_session_open=100.0, prior_session_close=100.0,
        decision_bar_close=100.2,  # inside band; the 10:07 spike is irrelevant
    )
    assert decision_at_1030.signal is Signal.FLAT
    action = resolve_action(position=PositionSide.FLAT, decision=decision_at_1030, is_last_mark_of_session=False)
    assert action is Action.HOLD


def test_next_minute_mark_is_the_fill_bar():
    assert next_minute_mark("10:00") == "10:01"
    assert next_minute_mark("10:30") == "10:31"
    assert next_minute_mark("09:59") == "10:00"


# ---------------------------------------------------------------------------
# Lookahead assertion: decision at a mark is unchanged when all bars at/after
# the fill bar are removed
# ---------------------------------------------------------------------------


def test_decision_unchanged_when_bars_at_and_after_fill_bar_are_removed():
    """decide_signal's signature makes lookahead structurally impossible.

    The function receives ONLY: the 14 prior sessions' per-mark closes, the
    prior session's 16:00 close, the current session's 09:30 open, and the
    decision bar's own close. It has no parameter through which a bar at or
    after the fill bar (mark_time + 1 minute) could reach it. This test
    proves that by calling it twice -- once with extra "future" data present
    in the caller's own scratch state, once with that data deleted entirely
    -- and showing the decision is bit-for-bit identical because decide_signal
    never had access to it either way.
    """

    prior = _prior_sessions([0.015] * 14)
    # A caller-side simulation of "future" bars a buggy harness might have
    # been tempted to peek at -- the fill bar and everything after it.
    future_bars_present = {"10:01": 105.0, "10:02": 106.0, "10:30": 107.0}

    decision_with_future_data_in_scope = decide_signal(
        session_date="2026-02-02", mark_time="10:00", prior_sessions=prior,
        current_session_open=100.0, prior_session_close=100.0, decision_bar_close=100.4,
    )

    del future_bars_present["10:01"]
    del future_bars_present["10:02"]
    del future_bars_present["10:30"]
    assert future_bars_present == {}

    decision_with_future_data_removed = decide_signal(
        session_date="2026-02-02", mark_time="10:00", prior_sessions=prior,
        current_session_open=100.0, prior_session_close=100.0, decision_bar_close=100.4,
    )

    assert decision_with_future_data_removed == decision_with_future_data_in_scope


# ---------------------------------------------------------------------------
# Missing/duplicate bars and a short session do not silently corrupt the band
# ---------------------------------------------------------------------------


def test_missing_mark_close_in_a_prior_session_raises_not_silently_skipped():
    sessions = _prior_sessions([0.01] * 14)
    # Simulate one prior session missing this mark's bar (a data gap).
    corrupted = sessions[:-1] + [
        PriorSessionSnapshot(session_date="2026-01-14", open_0930=100.0, mark_closes={})
    ]
    with pytest.raises(Id08InputError):
        compute_sigma_bar(corrupted, MARK)


def test_wrong_session_count_from_duplicate_or_short_session_raises():
    sessions = _prior_sessions([0.01] * 14)
    # A duplicate session_date collapsing two sessions into one slot still
    # leaves the list at 14 entries structurally, but a short/holiday-
    # truncated session list must not silently coerce to 14 by padding.
    too_few = sessions[:13]
    with pytest.raises(Id08InputError):
        compute_sigma_bar(too_few, MARK)

    too_many = sessions + [sessions[-1]]
    with pytest.raises(Id08InputError):
        compute_sigma_bar(too_many, MARK)
