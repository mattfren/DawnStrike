"""id10-dawnstrike-v1: a fixed-notional session-open half-ATR-band policy for SPY.

Adaptation of Carlo Zarattini, Alberto Pagani, "Improving Performance with Fast
Alphas: A Tactical Overlay for Intraday Trend Trading" (Concretum Group,
QuanTips #2, Feb 2026), Section 3 baseline only. This module implements ONLY
the frozen rules recorded in ``C:\\r\\dsos-00-v3\\ID10_FROZEN_SPEC.md``, cross
walked in ``id10_crosswalk.md``. It may NEVER be used to claim the source
paper's returns; see the module identity below.

This is a pure, side-effect-free decision module. Both the replay harness
(``scripts/id10_run_replay.py``) and any future live runtime MUST call the
functions in this file rather than reimplementing the signal or position
logic, so there is exactly one decision implementation shared by both paths.

Deliberate, named deviations / orchestrator decisions from the source paper
(see the frozen spec for full rationale):
  * ATR granularity: DAILY bars, Wilder's ATR(14) -- the paper never states
    the bar granularity for the ATR term. This is the single largest
    fidelity risk in ID 10 and is UNTESTED as an intraday-ATR alternative.
  * Feed: IEX for both replay and live (source used IQFeed).
  * Entry fill: NEXT bar's open after the decision bar closes (source
    implies a same-bar-close fill, which is not executable in real time;
    reported only as a diagnostic).
  * Exits: the source explicitly states a session-open stop ("closed if the
    price returns to the session's opening level, which acts as a stop").
    The source is silent on what happens to an open position when an
    OPPOSITE band breach occurs on the same decision mark; per the frozen
    spec, that silence is resolved as flip-on-opposite-breach, checked
    BEFORE the stop on any mark where both conditions could be true. EOD
    liquidation is the third, independently explicit rule.
  * Sizing: fixed dollar notional, no leverage (the paper's 2%-vol-target
    sizing, which can imply >1x notional, is NOT imported).
  * First decision mark: 09:30 is a decision mark (NOT excluded), because
    this band's width does not depend on elapsed time since the open --
    unlike ID08's noise band, it cannot be degenerate at 09:30.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

STRATEGY_ID = "id10-dawnstrike-v1"
STRATEGY_SOURCE = (
    "Adaptation of Zarattini, Pagani, 'Improving Performance with Fast Alphas: "
    "A Tactical Overlay for Intraday Trend Trading' (Concretum Group, QuanTips "
    "#2, Feb 2026), Section 3 baseline only. Does NOT claim the source paper's "
    "returns."
)

# Frozen constants. Do not tune.
LOOKBACK_SESSIONS = 14
BAND_MULTIPLIER = 0.5
UNIVERSE = ("SPY",)
SESSION_OPEN_MARK = "09:30"
SESSION_CLOSE_TIME = "16:00"


class Signal(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class Action(str, Enum):
    """What the position-resolution step instructs the replay/runtime to do."""

    NONE = "NONE"
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT_FLIP_TO_LONG = "EXIT_FLIP_TO_LONG"
    EXIT_FLIP_TO_SHORT = "EXIT_FLIP_TO_SHORT"
    EXIT_STOP = "EXIT_STOP"
    EXIT_EOD = "EXIT_EOD"
    HOLD = "HOLD"


@dataclass(frozen=True)
class DailyBar:
    """One completed daily session's OHLC, aggregated from RTH 1-min bars."""

    session_date: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class BoundsResult:
    atr14: float
    upper: float
    lower: float
    sessions_used: int


@dataclass(frozen=True)
class Id10Decision:
    """The single auditable output of the pure decision function."""

    session_date: str
    mark_time: str
    decision_bar_close: float
    upper: float
    lower: float
    atr14: float
    signal: Signal
    reason: str


class Id10InputError(ValueError):
    """Raised when the caller supplies data that violates a frozen rule."""


def decision_marks_for_session(close_time: str = SESSION_CLOSE_TIME) -> list[str]:
    """Return the ordered `HH:00/15/30/45` decision marks within RTH for a session.

    Per the frozen spec, 09:30 IS a decision mark for ID 10 (contrast with
    ID08, where 09:30 is excluded because that band degenerates at the
    open). ID 10's band has a fixed width derived from daily bars and is
    non-degenerate at 09:30. Marks run 09:30, 09:45, 10:00, ... through the
    last :00/:15/:30/:45 mark strictly before the session's actual close, so
    a half-day session naturally gets fewer marks rather than being
    discarded by any bar-count rule.
    """

    close_h, close_m = (int(p) for p in close_time.split(":"))
    marks: list[str] = []
    h, m = 9, 30
    while (h, m) < (close_h, close_m):
        marks.append(f"{h:02d}:{m:02d}")
        if m == 45:
            h += 1
            m = 0
        else:
            m += 15
    return marks


def next_minute_mark(mark_time: str) -> str:
    """Return the HH:MM one minute after ``mark_time`` (the fill bar)."""

    h, m = (int(p) for p in mark_time.split(":"))
    m += 1
    if m == 60:
        m = 0
        h += 1
    return f"{h:02d}:{m:02d}"


def true_range(high: float, low: float, prev_close: float | None) -> float:
    """Wilder's True Range = max[(H-L), |H-prev_close|, |L-prev_close|].

    ``prev_close`` is ``None`` only at the absolute start of the cached
    dataset, where no earlier session's close exists at all (a boundary
    condition, not an invented value). In that case TR falls back to the
    single well-defined term, H-L.
    """

    if high < low:
        raise Id10InputError(f"high {high} < low {low}")
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def compute_wilder_atr14(
    prior_daily_bars: Sequence[DailyBar],
    pre_lookback_close: float | None,
    *,
    lookback: int = LOOKBACK_SESSIONS,
) -> float:
    """ATR(14) = simple mean of 14 True Range values (the standard Wilder seed).

    ``prior_daily_bars`` must contain EXACTLY ``lookback`` completed daily
    sessions, oldest first -- the 14 sessions immediately preceding session
    t. ``pre_lookback_close`` is the close of the session immediately before
    the oldest of those 14 (needed only for that oldest session's TR gap
    terms); it is ``None`` only when the oldest of the 14 is the very first
    session in the entire cached dataset (see ``true_range``).
    """

    if len(prior_daily_bars) != lookback:
        raise Id10InputError(
            f"compute_wilder_atr14 requires exactly {lookback} prior daily "
            f"bars, got {len(prior_daily_bars)}"
        )
    trs: list[float] = []
    prev_close = pre_lookback_close
    for bar in prior_daily_bars:
        trs.append(true_range(bar.high, bar.low, prev_close))
        prev_close = bar.close
    return sum(trs) / len(trs)


def compute_bounds(
    session_open: float,
    atr14: float,
    *,
    multiplier: float = BAND_MULTIPLIER,
) -> BoundsResult:
    """``Upper/Lower = SessionOpen +/- multiplier*ATR(14)``. Constant for the session."""

    if session_open <= 0:
        raise Id10InputError("session open must be positive")
    if atr14 < 0:
        raise Id10InputError("atr14 must be non-negative")
    upper = session_open + multiplier * atr14
    lower = session_open - multiplier * atr14
    return BoundsResult(atr14=atr14, upper=upper, lower=lower, sessions_used=0)


def decide_signal(
    *,
    session_date: str,
    mark_time: str,
    prior_daily_bars: Sequence[DailyBar],
    pre_lookback_close: float | None,
    session_open: float,
    decision_bar_close: float,
    multiplier: float = BAND_MULTIPLIER,
    lookback: int = LOOKBACK_SESSIONS,
) -> Id10Decision:
    """Pure decision function: LONG / SHORT / FLAT for one HH:MM mark.

    Uses ONLY: the 14 prior completed daily sessions (``prior_daily_bars``),
    the close immediately before those 14 (``pre_lookback_close``, for the
    oldest TR gap term only), ``SessionOpen[t]`` (``session_open``), and the
    decision bar's own close (``decision_bar_close``). Nothing from later in
    session t may be passed in by the caller; this function has no way to
    see it. The band (``upper``/``lower``) is constant across the whole
    session -- it does not depend on ``mark_time`` -- because it is derived
    entirely from daily bars and the session open.
    """

    atr14 = compute_wilder_atr14(prior_daily_bars, pre_lookback_close, lookback=lookback)
    bounds = compute_bounds(session_open, atr14, multiplier=multiplier)
    if decision_bar_close > bounds.upper:
        signal = Signal.LONG
        reason = (
            f"close {decision_bar_close:.4f} > upper {bounds.upper:.4f} "
            f"(atr14={atr14:.6f})"
        )
    elif decision_bar_close < bounds.lower:
        signal = Signal.SHORT
        reason = (
            f"close {decision_bar_close:.4f} < lower {bounds.lower:.4f} "
            f"(atr14={atr14:.6f})"
        )
    else:
        signal = Signal.FLAT
        reason = (
            f"close {decision_bar_close:.4f} within band "
            f"[{bounds.lower:.4f}, {bounds.upper:.4f}] (atr14={atr14:.6f})"
        )
    return Id10Decision(
        session_date=session_date,
        mark_time=mark_time,
        decision_bar_close=decision_bar_close,
        upper=bounds.upper,
        lower=bounds.lower,
        atr14=atr14,
        signal=signal,
        reason=reason,
    )


def resolve_action(
    *,
    position: PositionSide,
    decision: Id10Decision,
    session_open: float,
    is_last_mark_of_session: bool,
) -> Action:
    """Pure state-transition step shared by replay and any live runtime.

    Priority when a position is open, per the frozen spec's exit rule:
      1. Opposite-band breach -> FLIP (source-silent point, resolved by the
         frozen spec as flip-on-opposite-breach; checked first because it
         represents the largest, most decisive price move).
      2. Session-open stop touch -> EXIT_STOP (the source's own explicit
         rule: "closed if the price returns to the session's opening
         level, which acts as a stop").
      3. Last decision mark of the session -> EXIT_EOD (the source's own
         explicit end-of-day flatten rule).
      4. Otherwise HOLD.

    This function never inspects wall-clock time itself; ``is_last_mark_of_
    session`` is supplied by the caller, so replay and runtime share
    identical branch logic driven by the same inputs.
    """

    if position is PositionSide.FLAT:
        if decision.signal is Signal.LONG:
            return Action.ENTER_LONG
        if decision.signal is Signal.SHORT:
            return Action.ENTER_SHORT
        return Action.HOLD if not is_last_mark_of_session else Action.NONE

    if position is PositionSide.LONG:
        if decision.signal is Signal.SHORT:
            return Action.EXIT_FLIP_TO_SHORT
        if decision.decision_bar_close <= session_open:
            return Action.EXIT_STOP
        if is_last_mark_of_session:
            return Action.EXIT_EOD
        return Action.HOLD

    if position is PositionSide.SHORT:
        if decision.signal is Signal.LONG:
            return Action.EXIT_FLIP_TO_LONG
        if decision.decision_bar_close >= session_open:
            return Action.EXIT_STOP
        if is_last_mark_of_session:
            return Action.EXIT_EOD
        return Action.HOLD

    raise Id10InputError(f"unknown position side: {position}")
