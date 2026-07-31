"""Versioned, causal position management for future PaperOps challengers.

Legacy PaperOps series retain their historical generic lifecycle. These policies
are opt-in and can only be attached to a new strategy/version experiment.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any

from intraday_scanner.market_calendar import market_session
from intraday_scanner.v2.data import MarketBar
from intraday_scanner.v2.strategies import Direction

InvalidationCallback = Callable[[MarketBar, dict[str, Any]], bool]


@dataclass(frozen=True, slots=True)
class PositionManagementPolicy:
    strategy_id: str
    strategy_version: str
    policy_version: str
    timeout_trading_sessions: int
    invalidation_rule: str
    invalidation_callback: InvalidationCallback
    stop_behavior: str
    profit_taking_behavior: str
    trailing_behavior: str
    end_of_day_behavior: str
    fee_bps_per_side: float
    slippage_bps_per_side: float
    require_verified_short_borrow: bool
    borrow_cost_bps_per_session: float | None
    same_bar_policy: str = "stop_first_conservative"
    research_only: bool = True
    broker_execution_enabled: bool = False

    def fingerprint_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("invalidation_callback")
        try:
            callback_source = inspect.getsource(self.invalidation_callback)
        except (OSError, TypeError):
            callback_source = self.invalidation_callback.__qualname__
        payload["invalidation_callback"] = self.invalidation_callback.__qualname__
        payload["invalidation_callback_sha256"] = hashlib.sha256(
            callback_source.encode()
        ).hexdigest()
        return payload

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.fingerprint_payload(),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class PositionManagementDecision:
    action: str
    reason: str
    raw_exit_price: float | None
    trading_sessions_held: int
    policy_version: str
    policy_fingerprint: str
    evidence: tuple[str, ...]
    research_only: bool = True
    broker_execution_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence"] = list(self.evidence)
        return payload


@dataclass(frozen=True, slots=True)
class BorrowAvailability:
    status: str
    located_at: str | None = None
    borrow_cost_bps_per_session: float | None = None
    source_ref: str | None = None


def evaluate_entry_availability(
    policy: PositionManagementPolicy,
    *,
    direction: str,
    borrow: BorrowAvailability | None,
) -> PositionManagementDecision:
    if direction != Direction.SHORT:
        return _decision(policy, "ALLOW_ENTRY", "long_does_not_require_borrow", 0)
    if not policy.require_verified_short_borrow:
        return _decision(policy, "ALLOW_ENTRY", "policy_does_not_require_borrow", 0)
    if (
        borrow is None
        or borrow.status != "verified_available"
        or not borrow.located_at
        or not borrow.source_ref
        or borrow.borrow_cost_bps_per_session is None
    ):
        return _decision(
            policy,
            "BLOCK_ENTRY",
            "short_borrow_not_verified",
            0,
            evidence=("Missing verified availability, timestamp, cost, or source.",),
        )
    return _decision(
        policy,
        "ALLOW_ENTRY",
        "short_borrow_verified",
        0,
        evidence=(
            f"borrow source {borrow.source_ref}",
            f"borrow cost {borrow.borrow_cost_bps_per_session:.4f} bps/session",
        ),
    )


def evaluate_position_management(
    policy: PositionManagementPolicy,
    *,
    direction: str,
    opened_at: datetime,
    stop: float,
    target: float | None,
    bar: MarketBar,
    context: dict[str, Any] | None = None,
    is_final_session_bar: bool = False,
) -> PositionManagementDecision:
    """Evaluate only information available on or before ``bar``."""

    if bar.timestamp < opened_at:
        raise ValueError("position policy cannot inspect a bar before the open")
    if policy.timeout_trading_sessions < 1:
        raise ValueError("timeout_trading_sessions must be positive")
    facts = dict(context or {})
    sessions = trading_sessions_elapsed(opened_at.date(), bar.timestamp.date())
    stop_gap = (
        bar.open <= stop if direction == Direction.LONG else bar.open >= stop
    )
    if stop_gap:
        return _decision(
            policy,
            "CLOSE",
            "stop_gap",
            sessions,
            raw_exit_price=bar.open,
        )
    stop_hit = (
        bar.low <= stop if direction == Direction.LONG else bar.high >= stop
    )
    if stop_hit:
        return _decision(
            policy,
            "CLOSE",
            "stop",
            sessions,
            raw_exit_price=stop,
        )
    trailing_stop = _number(facts.get("trailing_stop"))
    if policy.trailing_behavior != "none" and trailing_stop is not None:
        trailing_hit = (
            bar.low <= trailing_stop
            if direction == Direction.LONG
            else bar.high >= trailing_stop
        )
        if trailing_hit:
            return _decision(
                policy,
                "CLOSE",
                "trailing_stop",
                sessions,
                raw_exit_price=trailing_stop,
            )
    if target is not None:
        target_gap = (
            bar.open >= target
            if direction == Direction.LONG
            else bar.open <= target
        )
        if target_gap:
            return _decision(
                policy,
                "CLOSE",
                "target_gap",
                sessions,
                raw_exit_price=bar.open,
            )
        target_hit = (
            bar.high >= target
            if direction == Direction.LONG
            else bar.low <= target
        )
        if target_hit:
            return _decision(
                policy,
                "CLOSE",
                "target",
                sessions,
                raw_exit_price=target,
            )
    if policy.invalidation_callback(bar, facts):
        return _decision(
            policy,
            "CLOSE",
            "strategy_invalidation",
            sessions,
            raw_exit_price=bar.close,
            evidence=(policy.invalidation_rule,),
        )
    if sessions >= policy.timeout_trading_sessions:
        return _decision(
            policy,
            "CLOSE",
            "trading_session_timeout",
            sessions,
            raw_exit_price=bar.close,
        )
    if is_final_session_bar and policy.end_of_day_behavior == "flat_each_session":
        return _decision(
            policy,
            "CLOSE",
            "eod_flat",
            sessions,
            raw_exit_price=bar.close,
        )
    return _decision(policy, "HOLD", "no_exit_condition", sessions)


def trading_sessions_elapsed(open_date: date, current_date: date) -> int:
    if current_date < open_date:
        raise ValueError("current_date cannot precede open_date")
    cursor = open_date + timedelta(days=1)
    count = 0
    while cursor <= current_date:
        if market_session(cursor).is_trading_day:
            count += 1
        cursor += timedelta(days=1)
    return count


def challenger_position_policies() -> tuple[PositionManagementPolicy, ...]:
    """Return frozen policies referenced by the experiment registry."""

    return (
        PositionManagementPolicy(
            strategy_id="ts_momentum_sma_atr",
            strategy_version="v2.0-challenger",
            policy_version="pm-ts-momentum-trading-session-v1",
            timeout_trading_sessions=10,
            invalidation_rule="Close below causal SMA50.",
            invalidation_callback=_close_below_sma50,
            stop_behavior="initial_atr_stop_stop_first",
            profit_taking_behavior="fixed_target",
            trailing_behavior="causal_atr_trailing_after_1r",
            end_of_day_behavior="hold",
            fee_bps_per_side=1.0,
            slippage_bps_per_side=5.0,
            require_verified_short_borrow=True,
            borrow_cost_bps_per_session=None,
        ),
        PositionManagementPolicy(
            strategy_id="donchian_breakout_20_10",
            strategy_version="v2.0-challenger",
            policy_version="pm-donchian-causal-channel-v1",
            timeout_trading_sessions=10,
            invalidation_rule="Close below prior causal 10-session Donchian low.",
            invalidation_callback=_close_below_prior_channel,
            stop_behavior="initial_channel_or_atr_stop_stop_first",
            profit_taking_behavior="partial_at_2r_then_trail",
            trailing_behavior="causal_10_session_channel",
            end_of_day_behavior="hold",
            fee_bps_per_side=1.0,
            slippage_bps_per_side=5.0,
            require_verified_short_borrow=True,
            borrow_cost_bps_per_session=None,
        ),
        PositionManagementPolicy(
            strategy_id="pullback_reclaim_uptrend",
            strategy_version="v2.0-challenger",
            policy_version="pm-pullback-reclaim-v1",
            timeout_trading_sessions=5,
            invalidation_rule="Close below the saved reclaim low.",
            invalidation_callback=_close_below_reclaim_low,
            stop_behavior="saved_reclaim_low_stop_first",
            profit_taking_behavior="midline_then_trail",
            trailing_behavior="causal_sma20",
            end_of_day_behavior="hold",
            fee_bps_per_side=1.0,
            slippage_bps_per_side=5.0,
            require_verified_short_borrow=True,
            borrow_cost_bps_per_session=None,
        ),
        PositionManagementPolicy(
            strategy_id="gap_up_continuation",
            strategy_version="v2.0-challenger",
            policy_version="pm-gap-up-continuation-v1",
            timeout_trading_sessions=3,
            invalidation_rule="Close below the saved gap-support level.",
            invalidation_callback=_close_below_gap_support,
            stop_behavior="gap_support_stop_first",
            profit_taking_behavior="partial_at_1r_then_trail",
            trailing_behavior="causal_prior_session_low",
            end_of_day_behavior="hold",
            fee_bps_per_side=1.0,
            slippage_bps_per_side=5.0,
            require_verified_short_borrow=True,
            borrow_cost_bps_per_session=None,
        ),
    )


def policy_for(
    strategy_id: str,
    strategy_version: str,
) -> PositionManagementPolicy | None:
    return next(
        (
            policy
            for policy in challenger_position_policies()
            if policy.strategy_id == strategy_id
            and policy.strategy_version == strategy_version
        ),
        None,
    )


def _close_below_sma50(bar: MarketBar, context: dict[str, Any]) -> bool:
    level = _number(context.get("sma50"))
    return level is not None and bar.close < level


def _close_below_prior_channel(bar: MarketBar, context: dict[str, Any]) -> bool:
    level = _number(context.get("prior_donchian_low_10"))
    return level is not None and bar.close < level


def _close_below_reclaim_low(bar: MarketBar, context: dict[str, Any]) -> bool:
    level = _number(context.get("reclaim_low"))
    return level is not None and bar.close < level


def _close_below_gap_support(bar: MarketBar, context: dict[str, Any]) -> bool:
    level = _number(context.get("gap_support"))
    return level is not None and bar.close < level


def _decision(
    policy: PositionManagementPolicy,
    action: str,
    reason: str,
    sessions: int,
    *,
    raw_exit_price: float | None = None,
    evidence: tuple[str, ...] = (),
) -> PositionManagementDecision:
    return PositionManagementDecision(
        action=action,
        reason=reason,
        raw_exit_price=raw_exit_price,
        trading_sessions_held=sessions,
        policy_version=policy.policy_version,
        policy_fingerprint=policy.fingerprint,
        evidence=evidence,
    )


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BorrowAvailability",
    "PositionManagementDecision",
    "PositionManagementPolicy",
    "challenger_position_policies",
    "evaluate_entry_availability",
    "evaluate_position_management",
    "policy_for",
    "trading_sessions_elapsed",
]
