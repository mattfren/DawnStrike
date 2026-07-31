"""Research-only risk sizing and warning helpers."""

from __future__ import annotations

from dataclasses import dataclass

from intraday_scanner.v2.strategies import Direction, StrategySignal


@dataclass(frozen=True)
class RiskSettings:
    account_equity: float = 100_000.0
    risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.20
    min_reward_risk: float = 1.5
    max_risk_per_trade_pct: float = 0.02
    stale_data_days: int = 5


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    quantity: int
    notional: float
    risk_amount: float
    risk_per_unit: float
    reward: float | None
    reward_risk: float | None
    warnings: tuple[str, ...]


def evaluate_signal_risk(
    signal: StrategySignal,
    *,
    entry_price: float,
    settings: RiskSettings,
    stale: bool = False,
) -> RiskDecision:
    warnings: list[str] = []
    risk_per_unit = abs(entry_price - signal.stop)
    if risk_per_unit <= 0:
        return RiskDecision(
            allowed=False,
            quantity=0,
            notional=0.0,
            risk_amount=0.0,
            risk_per_unit=risk_per_unit,
            reward=None,
            reward_risk=None,
            warnings=("invalid_stop_or_entry",),
        )
    if signal.direction == Direction.LONG and signal.stop >= entry_price:
        warnings.append("long_stop_not_below_entry")
    if signal.direction == Direction.SHORT and signal.stop <= entry_price:
        warnings.append("short_stop_not_above_entry")

    desired_risk = settings.account_equity * settings.risk_per_trade_pct
    max_allowed_risk = settings.account_equity * settings.max_risk_per_trade_pct
    if desired_risk > max_allowed_risk:
        warnings.append("risk_per_trade_exceeds_policy_max")
        desired_risk = max_allowed_risk

    quantity_from_risk = int(desired_risk // risk_per_unit)
    quantity_from_notional = int(
        (settings.account_equity * settings.max_position_pct) // entry_price
    )
    quantity = max(0, min(quantity_from_risk, quantity_from_notional))
    if quantity == 0:
        warnings.append("position_size_zero")

    notional = quantity * entry_price
    risk_amount = quantity * risk_per_unit
    reward = None
    reward_risk = None
    if signal.target is None:
        warnings.append("missing_profit_target")
    else:
        reward = abs(signal.target - entry_price)
        reward_risk = reward / risk_per_unit if risk_per_unit else None
        if reward_risk is not None and reward_risk < settings.min_reward_risk:
            warnings.append("reward_risk_below_minimum")

    if stale:
        warnings.append("stale_data")
    warnings.extend(signal.warnings)

    invalid_direction = signal.direction not in {Direction.LONG, Direction.SHORT}
    allowed = quantity > 0 and not invalid_direction and "invalid_stop_or_entry" not in warnings
    return RiskDecision(
        allowed=allowed,
        quantity=quantity,
        notional=notional,
        risk_amount=risk_amount,
        risk_per_unit=risk_per_unit,
        reward=reward,
        reward_risk=reward_risk,
        warnings=tuple(dict.fromkeys(warnings)),
    )
