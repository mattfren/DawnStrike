"""Pure, deterministic path truth for one decision and one bar sequence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class PathTruthStatus(str, Enum):
    RESOLVED_TARGET_FIRST = "RESOLVED_TARGET_FIRST"
    RESOLVED_STOP_FIRST = "RESOLVED_STOP_FIRST"
    SAME_MINUTE_AMBIGUOUS = "SAME_MINUTE_AMBIGUOUS"
    ENTRY_BAR_AMBIGUOUS = "ENTRY_BAR_AMBIGUOUS"
    NOT_TRIGGERED = "NOT_TRIGGERED"
    MISSING_DECISION_TIME = "MISSING_DECISION_TIME"
    MISSING_LEVELS = "MISSING_LEVELS"
    MISSING_BARS = "MISSING_BARS"
    KNOWN_HALT_WINDOW = "KNOWN_HALT_WINDOW"
    CORPORATE_ACTION_UNRESOLVED = "CORPORATE_ACTION_UNRESOLVED"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    DATA_INELIGIBLE = "DATA_INELIGIBLE"


@dataclass(frozen=True)
class PathReplayResult:
    path_truth_status: PathTruthStatus
    conservative_policy_result: str | None
    entry_time: datetime | None
    entry_price: float | None
    target_touched_at: datetime | None
    stop_touched_at: datetime | None
    exit_time: datetime | None
    exit_price: float | None
    mfe_price: float | None
    mfe_at: datetime | None
    mae_price: float | None
    mae_at: datetime | None
    post_entry_bar_count: int
    entry_bar_excluded: bool
    bounds: dict[str, float | None]
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_truth_status": self.path_truth_status.value,
            "conservative_policy_result": self.conservative_policy_result,
            "entry_time": _iso(self.entry_time),
            "entry_price": self.entry_price,
            "target_touched_at": _iso(self.target_touched_at),
            "stop_touched_at": _iso(self.stop_touched_at),
            "exit_time": _iso(self.exit_time),
            "exit_price": self.exit_price,
            "mfe_price": self.mfe_price,
            "mfe_at": _iso(self.mfe_at),
            "mae_price": self.mae_price,
            "mae_at": _iso(self.mae_at),
            "post_entry_bar_count": self.post_entry_bar_count,
            "entry_bar_excluded": self.entry_bar_excluded,
            "bounds": dict(self.bounds),
            "notes": list(self.notes),
        }


def resolve_path(
    bars: list[Any] | tuple[Any, ...],
    *,
    decision_at: datetime | None,
    trigger: float | None,
    target: float | None,
    stop: float | None,
    source_conflict: bool = False,
    corporate_action_unresolved: bool = False,
    halt_intervals: tuple[tuple[datetime, datetime], ...] = (),
) -> PathReplayResult:
    """Resolve first-touch truth without using the trigger bar's extrema.

    The input sequence is observational evidence.  Same-minute ordering is
    intentionally unknown; the conservative paper policy is recorded
    separately from ``path_truth_status``.
    """

    if source_conflict:
        return _empty(PathTruthStatus.SOURCE_CONFLICT, "source hashes conflict")
    if corporate_action_unresolved:
        return _empty(
            PathTruthStatus.CORPORATE_ACTION_UNRESOLVED,
            "corporate action mapping is unresolved",
        )
    if decision_at is None:
        return _empty(PathTruthStatus.MISSING_DECISION_TIME, "decision timestamp is missing")
    if trigger is None or target is None or stop is None:
        return _empty(PathTruthStatus.MISSING_LEVELS, "trigger, target, and stop are required")
    timestamped: list[tuple[datetime, Any]] = []
    for bar in bars:
        timestamp = _timestamp(bar)
        if timestamp is not None and timestamp >= decision_at:
            timestamped.append((timestamp, bar))
    timestamped.sort(key=lambda item: item[0])
    ordered = [bar for _, bar in timestamped]
    if not ordered:
        return _empty(PathTruthStatus.MISSING_BARS, "no bars are available after the decision")
    trigger_bar = None
    for bar in ordered:
        high = _number(_value(bar, "high"))
        if high is not None and high >= trigger:
            trigger_bar = bar
            break
    if trigger_bar is None:
        return _empty(PathTruthStatus.NOT_TRIGGERED, "no observed high reached the trigger")
    entry_at = _timestamp(trigger_bar)
    entry_open = _number(_value(trigger_bar, "open"))
    if entry_at is None or entry_open is None:
        return _empty(
            PathTruthStatus.ENTRY_BAR_AMBIGUOUS,
            "trigger bar lacks entry ordering evidence",
        )
    entry_price = max(trigger, entry_open)
    if target <= entry_price or stop >= entry_price:
        return _empty(PathTruthStatus.DATA_INELIGIBLE, "saved levels are dislocated from entry")
    post_entry = [
        bar
        for bar in ordered
        if (timestamp := _timestamp(bar)) is not None and timestamp > entry_at
    ]
    if not post_entry:
        return PathReplayResult(
            path_truth_status=PathTruthStatus.ENTRY_BAR_AMBIGUOUS,
            conservative_policy_result="stop_first",
            entry_time=entry_at,
            entry_price=entry_price,
            target_touched_at=None,
            stop_touched_at=None,
            exit_time=None,
            exit_price=None,
            mfe_price=None,
            mfe_at=None,
            mae_price=None,
            mae_at=None,
            post_entry_bar_count=0,
            entry_bar_excluded=True,
            bounds={
                "mfe_upper": _number(_value(trigger_bar, "high")),
                "mae_lower": _number(_value(trigger_bar, "low")),
            },
            notes=("trigger-bar extrema excluded; trade sequence unavailable",),
        )
    if _overlaps_halt(entry_at, _timestamp(post_entry[0]), halt_intervals):
        return _empty(
            PathTruthStatus.KNOWN_HALT_WINDOW,
            "post-trigger evidence begins across a sourced halt interval",
            entry_time=entry_at,
            entry_price=entry_price,
        )
    target_at = _first_touch(post_entry, "high", target, at_or_above=True)
    stop_at = _first_touch(post_entry, "low", stop, at_or_above=False)
    if target_at is not None and stop_at is not None and target_at == stop_at:
        status = PathTruthStatus.SAME_MINUTE_AMBIGUOUS
        policy = "stop_first"
    elif stop_at is not None and (target_at is None or stop_at < target_at):
        status = PathTruthStatus.RESOLVED_STOP_FIRST
        policy = "stop_first"
    elif target_at is not None:
        status = PathTruthStatus.RESOLVED_TARGET_FIRST
        policy = "target_first"
    else:
        status = PathTruthStatus.DATA_INELIGIBLE
        policy = "session_close"
    mfe_bar = max(
        (bar for bar in post_entry if _number(_value(bar, "high")) is not None),
        key=lambda bar: _number(_value(bar, "high")) or 0.0,
        default=None,
    )
    mae_bar = min(
        (bar for bar in post_entry if _number(_value(bar, "low")) is not None),
        key=lambda bar: _number(_value(bar, "low")) or float("inf"),
        default=None,
    )
    exit_at = stop_at if policy == "stop_first" else target_at
    exit_price = (
        stop
        if policy == "stop_first" and stop_at
        else target
        if policy == "target_first" and target_at
        else _number(_value(post_entry[-1], "close"))
    )
    return PathReplayResult(
        path_truth_status=status,
        conservative_policy_result=policy,
        entry_time=entry_at,
        entry_price=entry_price,
        target_touched_at=target_at,
        stop_touched_at=stop_at,
        exit_time=exit_at or _timestamp(post_entry[-1]),
        exit_price=exit_price,
        mfe_price=_number(_value(mfe_bar, "high")) if mfe_bar else None,
        mfe_at=_timestamp(mfe_bar) if mfe_bar else None,
        mae_price=_number(_value(mae_bar, "low")) if mae_bar else None,
        mae_at=_timestamp(mae_bar) if mae_bar else None,
        post_entry_bar_count=len(post_entry),
        entry_bar_excluded=True,
        bounds={},
        notes=("trigger-bar extrema excluded",),
    )


def _empty(
    status: PathTruthStatus,
    note: str,
    *,
    entry_time: datetime | None = None,
    entry_price: float | None = None,
) -> PathReplayResult:
    return PathReplayResult(
        path_truth_status=status,
        conservative_policy_result=None,
        entry_time=entry_time,
        entry_price=entry_price,
        target_touched_at=None,
        stop_touched_at=None,
        exit_time=None,
        exit_price=None,
        mfe_price=None,
        mfe_at=None,
        mae_price=None,
        mae_at=None,
        post_entry_bar_count=0,
        entry_bar_excluded=True,
        bounds={},
        notes=(note,),
    )


def _first_touch(
    bars: list[Any], field: str, level: float, *, at_or_above: bool
) -> datetime | None:
    for bar in bars:
        value = _number(_value(bar, field))
        if value is not None and ((value >= level) if at_or_above else (value <= level)):
            return _timestamp(bar)
    return None


def _overlaps_halt(
    start: datetime | None,
    end: datetime | None,
    intervals: tuple[tuple[datetime, datetime], ...],
) -> bool:
    if start is None or end is None:
        return False
    return any(start < halt_end and end > halt_start for halt_start, halt_end in intervals)


def _value(value: Any, key: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _timestamp(value: Any) -> datetime | None:
    item = _value(value, "observed_at")
    return item if isinstance(item, datetime) else None


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


__all__ = ["PathReplayResult", "PathTruthStatus", "resolve_path"]
