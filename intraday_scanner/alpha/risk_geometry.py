"""Risk geometry and challenger inputs for AlphaOps research."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class RiskGeometryStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    DATA_INELIGIBLE = "DATA_INELIGIBLE"
    RISK_LIMIT_EXCEEDED = "RISK_LIMIT_EXCEEDED"
    TARGET_NOT_INDEPENDENT = "TARGET_NOT_INDEPENDENT"
    LIQUIDITY_INELIGIBLE = "LIQUIDITY_INELIGIBLE"


@dataclass(frozen=True, slots=True)
class RiskGeometry:
    direction: str
    entry_price: float | None
    stop_price: float | None
    target_price: float | None
    stop_distance_pct: float | None
    gross_reward_risk: float | None
    risk_budget: float | None
    modeled_risk: float | None
    proposed_notional: float | None
    status: RiskGeometryStatus
    reasons: tuple[str, ...]
    challenger_id: str = "baseline"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["reasons"] = list(self.reasons)
        return payload


def evaluate_risk_geometry(
    *,
    entry_price: float | None,
    stop_price: float | None,
    target_price: float | None,
    direction: str = "long",
    equity: float | None = None,
    risk_pct: float = 0.25,
    max_notional_pct: float = 10.0,
    target_independent: bool = True,
    liquidity_status: str = "verified",
    challenger_id: str = "baseline",
    requested_notional: float | None = None,
) -> RiskGeometry:
    """Evaluate geometry only; this function never places or routes an order."""

    normalized_direction = direction.lower()
    reasons: list[str] = []
    if normalized_direction not in {"long", "short"}:
        reasons.append("direction_unknown")
    valid_prices = (
        _positive(entry_price) and _positive(stop_price) and _positive(target_price)
    )
    if not valid_prices:
        reasons.append("entry_stop_target_unknown")
    else:
        assert entry_price is not None
        assert stop_price is not None
        assert target_price is not None
        if normalized_direction == "long" and not target_price > entry_price > stop_price:
            reasons.append("long_geometry_invalid")
        elif normalized_direction == "short" and not target_price < entry_price < stop_price:
            reasons.append("short_geometry_invalid")
    if not target_independent:
        reasons.append("target_not_independent")
    if str(liquidity_status).lower() not in {"clear", "verified", "ok", "pass"}:
        reasons.append("liquidity_not_verified")
    stop_distance = (
        abs(entry_price - stop_price) / entry_price * 100
        if entry_price is not None and entry_price > 0 and stop_price is not None
        else None
    )
    reward = (
        abs(target_price - entry_price) / abs(entry_price - stop_price)
        if entry_price is not None
        and stop_price is not None
        and target_price is not None
        and abs(entry_price - stop_price) > 0
        else None
    )
    risk_budget = equity * risk_pct / 100 if equity and equity > 0 else None
    notional_limit = equity * max_notional_pct / 100 if equity and equity > 0 else None
    risk_sized_notional = (
        risk_budget / (abs(entry_price - stop_price) / entry_price)
        if risk_budget is not None
        and entry_price is not None
        and stop_price is not None
        and entry_price > 0
        and abs(entry_price - stop_price) > 0
        else None
    )
    proposed_notional = (
        requested_notional
        if requested_notional is not None
        else min(notional_limit, risk_sized_notional)
        if notional_limit is not None and risk_sized_notional is not None
        else notional_limit or risk_sized_notional
    )
    modeled_risk = (
        proposed_notional * abs(entry_price - stop_price) / entry_price
        if proposed_notional is not None
        and entry_price is not None
        and stop_price is not None
        and entry_price > 0
        else None
    )
    if equity is None or equity <= 0:
        reasons.append("equity_unknown")
    elif modeled_risk is None or risk_budget is None:
        reasons.append("risk_unknown")
    elif requested_notional is not None and modeled_risk > risk_budget + 1e-9:
        reasons.append("risk_budget_exceeded")
    if reasons:
        status = (
            RiskGeometryStatus.TARGET_NOT_INDEPENDENT
            if "target_not_independent" in reasons
            else RiskGeometryStatus.LIQUIDITY_INELIGIBLE
            if "liquidity_not_verified" in reasons
            else RiskGeometryStatus.RISK_LIMIT_EXCEEDED
            if "risk_budget_exceeded" in reasons
            else RiskGeometryStatus.DATA_INELIGIBLE
        )
    else:
        status = RiskGeometryStatus.ELIGIBLE
    return RiskGeometry(
        direction=normalized_direction,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        stop_distance_pct=stop_distance,
        gross_reward_risk=reward,
        risk_budget=risk_budget,
        modeled_risk=modeled_risk,
        proposed_notional=proposed_notional,
        status=status,
        reasons=tuple(dict.fromkeys(reasons)),
        challenger_id=challenger_id,
    )


def build_risk_challenger_geometries(
    *,
    entry_price: float,
    stop_price: float,
    target_price: float,
    atr: float | None,
    equity: float,
    liquidity_status: str = "verified",
) -> tuple[RiskGeometry, ...]:
    """Build frozen baseline, ATR, and liquidity-aware challenger contracts."""

    candidates: list[RiskGeometry] = [
        evaluate_risk_geometry(
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            equity=equity,
            liquidity_status=liquidity_status,
            challenger_id="v5_baseline",
        )
    ]
    if atr is not None and math.isfinite(atr) and atr > 0:
        candidates.append(
            evaluate_risk_geometry(
                entry_price=entry_price,
                stop_price=entry_price - atr,
                target_price=entry_price + 2 * atr,
                equity=equity,
                liquidity_status=liquidity_status,
                challenger_id="atr_1x_stop_2x_target",
            )
        )
    candidates.append(
        evaluate_risk_geometry(
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            equity=equity,
            max_notional_pct=5.0 if liquidity_status.lower() != "high_liquidity" else 10.0,
            liquidity_status=liquidity_status,
            challenger_id="liquidity_aware_notional",
        )
    )
    return tuple(candidates)


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


__all__ = [
    "RiskGeometry",
    "RiskGeometryStatus",
    "build_risk_challenger_geometries",
    "evaluate_risk_geometry",
]
