"""id08-dawnstrike-v1: a fixed-notional intraday noise-band policy for SPY.

Adaptation of Zarattini, Aziz, Barbon, "Beat the Market: An Effective Intraday
Momentum Strategy for S&P500 ETF (SPY)" (Concretum Research). This module
implements ONLY the frozen rules recorded in
``C:\\r\\dsos-00-v3\\ID08_FROZEN_SPEC.md`` (including AMENDMENT 1). It may
NEVER be used to claim the source paper's returns; see the module identity
below.

This is a pure, side-effect-free decision module. Both the replay harness and
any future live runtime MUST call the functions in this file rather than
reimplementing the signal or position logic, so there is exactly one decision
implementation shared by both paths.

Deliberate, named deviations from the source paper (see frozen spec for the
full rationale):
  * Feed: IEX for both replay and live (source used IQFeed).
  * Entry fill: NEXT bar's open after the decision bar closes (source is
    ambiguous; same-bar-close fill is not actionable in real time and is
    reported only as a diagnostic).
  * VWAP trailing-stop variant: EXCLUDED (source formula not available).
  * Sizing: fixed dollar notional, no leverage (source's 100%-of-equity base
    and 4x volatility-targeted variant are NOT imported).
  * VIX: never enters the signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence

STRATEGY_ID = "id08-dawnstrike-v1"
STRATEGY_SOURCE = (
    "Adaptation of Zarattini, Aziz, Barbon, 'Beat the Market: An Effective "
    "Intraday Momentum Strategy for S&P500 ETF (SPY)' (Concretum Research). "
    "Does NOT claim the source paper's returns."
)

# Frozen constants. Do not tune.
LOOKBACK_SESSIONS = 14
VOLATILITY_MULTIPLIER = 1.0
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
    EXIT_EOD = "EXIT_EOD"
    HOLD = "HOLD"


@dataclass(frozen=True)
class PriorSessionSnapshot:
    """One prior completed session's data needed for the noise band.

    ``mark_closes`` must contain the close price of the 1-minute bar at each
    HH:MM decision mark. ``open_0930`` is that session's own regular-hours
    open, ``Open[t-i,09:30]``.
    """

    session_date: str
    open_0930: float
    mark_closes: Mapping[str, float]


@dataclass(frozen=True)
class BoundsResult:
    sigma_bar: float
    upper: float
    lower: float
    sessions_used: int


@dataclass(frozen=True)
class Id08Decision:
    """The single auditable output of the pure decision function."""

    session_date: str
    mark_time: str
    decision_bar_close: float
    upper: float
    lower: float
    sigma_bar: float
    signal: Signal
    reason: str


class Id08InputError(ValueError):
    """Raised when the caller supplies data that violates a frozen rule."""


def decision_marks_for_session(close_time: str = SESSION_CLOSE_TIME) -> list[str]:
    """Return the ordered `HH:00`/`HH:30` marks within RTH for a session.

    Regular-hours open is 09:30 (itself a `:30` mark) and the cadence runs
    through the last `:00`/`:30` mark strictly before the session's actual
    close. This is used for both the regular 16:00 close and an early close
    (e.g. 13:00), so a half-day session naturally gets fewer marks rather
    than being discarded by any bar-count rule.
    """

    close_h, close_m = (int(p) for p in close_time.split(":"))
    marks: list[str] = []
    h, m = 9, 30
    while (h, m) < (close_h, close_m):
        marks.append(f"{h:02d}:{m:02d}")
        if m == 30:
            h += 1
            m = 0
        else:
            m = 30
    return marks


def next_minute_mark(mark_time: str) -> str:
    """Return the HH:MM one minute after ``mark_time`` (the fill bar)."""

    h, m = (int(p) for p in mark_time.split(":"))
    m += 1
    if m == 60:
        m = 0
        h += 1
    return f"{h:02d}:{m:02d}"


def compute_sigma_bar(
    prior_sessions: Sequence[PriorSessionSnapshot],
    mark_time: str,
    *,
    lookback: int = LOOKBACK_SESSIONS,
) -> float:
    """``sigma_bar[t,HH:MM] = mean over the 14 prior sessions of abs(Close/Open-1)``.

    Price-only; no volume term. ``prior_sessions`` must contain EXACTLY
    ``lookback`` sessions -- the 14 completed sessions immediately preceding
    session t, oldest first. Missing a mark's close in any of them is a hard
    error (never silently dropped or forward-filled).
    """

    if len(prior_sessions) != lookback:
        raise Id08InputError(
            f"compute_sigma_bar requires exactly {lookback} prior sessions, "
            f"got {len(prior_sessions)}"
        )
    ratios: list[float] = []
    for snapshot in prior_sessions:
        if snapshot.open_0930 <= 0:
            raise Id08InputError(
                f"session {snapshot.session_date} has a non-positive 09:30 open"
            )
        close = snapshot.mark_closes.get(mark_time)
        if close is None:
            raise Id08InputError(
                f"session {snapshot.session_date} is missing a close for mark "
                f"{mark_time}; band math must not silently substitute or skip it"
            )
        ratios.append(abs(close / snapshot.open_0930 - 1.0))
    return sum(ratios) / len(ratios)


def compute_bounds(
    current_session_open: float,
    prior_session_close: float,
    sigma_bar: float,
    *,
    vm: float = VOLATILITY_MULTIPLIER,
) -> BoundsResult:
    """``Upper/Lower = max/min(Open[t,09:30], Close[t-1,16:00]) * (1 +/- VM*sigma_bar)``."""

    if current_session_open <= 0 or prior_session_close <= 0:
        raise Id08InputError("session open and prior close must be positive")
    anchor_high = max(current_session_open, prior_session_close)
    anchor_low = min(current_session_open, prior_session_close)
    upper = anchor_high * (1.0 + vm * sigma_bar)
    lower = anchor_low * (1.0 - vm * sigma_bar)
    return BoundsResult(sigma_bar=sigma_bar, upper=upper, lower=lower, sessions_used=0)


def decide_signal(
    *,
    session_date: str,
    mark_time: str,
    prior_sessions: Sequence[PriorSessionSnapshot],
    current_session_open: float,
    prior_session_close: float,
    decision_bar_close: float,
    vm: float = VOLATILITY_MULTIPLIER,
    lookback: int = LOOKBACK_SESSIONS,
) -> Id08Decision:
    """Pure decision function: LONG / SHORT / FLAT for one HH:MM mark.

    Uses ONLY: the 14 prior completed sessions (``prior_sessions``),
    ``Close[t-1,16:00]`` (``prior_session_close``), ``Open[t,09:30]``
    (``current_session_open``), and the decision bar's own close
    (``decision_bar_close``). Nothing from later in session t may be passed
    in by the caller; this function has no way to see it.
    """

    sigma_bar = compute_sigma_bar(prior_sessions, mark_time, lookback=lookback)
    bounds = compute_bounds(current_session_open, prior_session_close, sigma_bar, vm=vm)
    if decision_bar_close > bounds.upper:
        signal = Signal.LONG
        reason = (
            f"close {decision_bar_close:.4f} > upper {bounds.upper:.4f} "
            f"(sigma_bar={sigma_bar:.6f})"
        )
    elif decision_bar_close < bounds.lower:
        signal = Signal.SHORT
        reason = (
            f"close {decision_bar_close:.4f} < lower {bounds.lower:.4f} "
            f"(sigma_bar={sigma_bar:.6f})"
        )
    else:
        signal = Signal.FLAT
        reason = (
            f"close {decision_bar_close:.4f} within band "
            f"[{bounds.lower:.4f}, {bounds.upper:.4f}] (sigma_bar={sigma_bar:.6f})"
        )
    return Id08Decision(
        session_date=session_date,
        mark_time=mark_time,
        decision_bar_close=decision_bar_close,
        upper=bounds.upper,
        lower=bounds.lower,
        sigma_bar=sigma_bar,
        signal=signal,
        reason=reason,
    )


def resolve_action(
    *,
    position: PositionSide,
    decision: Id08Decision,
    is_last_mark_of_session: bool,
) -> Action:
    """Pure state-transition step shared by replay and any live runtime.

    Opposite-boundary crossover closes and flips in the SAME step (one
    exit + one immediate re-entry), matching the frozen spec's "close and
    flip" exit rule. EOD liquidation is signalled separately by the caller
    passing ``is_last_mark_of_session=True``; this function never inspects
    wall-clock time itself, so replay and runtime share the identical
    branch logic driven by the same inputs.
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
        if is_last_mark_of_session:
            return Action.EXIT_EOD
        return Action.HOLD

    if position is PositionSide.SHORT:
        if decision.signal is Signal.LONG:
            return Action.EXIT_FLIP_TO_LONG
        if is_last_mark_of_session:
            return Action.EXIT_EOD
        return Action.HOLD

    raise Id08InputError(f"unknown position side: {position}")
