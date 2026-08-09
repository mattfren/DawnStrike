"""Transparent execution-cost assumptions for AlphaOps research."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class CostModelStatus(str, Enum):
    PROVISIONAL = "COST_MODEL_PROVISIONAL"
    PENDING_EMPIRICAL = "NOT_EVALUABLE_PENDING_EMPIRICAL_COST"
    VERIFIED = "EMPIRICAL_COST_VERIFIED"


@dataclass(frozen=True, slots=True)
class ExecutionCostModel:
    version: str = "alphaops-v5-cost-model-50bps-0.005ps"
    entry_slippage_bps: float = 50.0
    exit_slippage_bps: float = 50.0
    commission_per_share_per_side: float = 0.005
    status: CostModelStatus = CostModelStatus.PROVISIONAL

    def __post_init__(self) -> None:
        for name, value in (
            ("entry_slippage_bps", self.entry_slippage_bps),
            ("exit_slippage_bps", self.exit_slippage_bps),
            ("commission_per_share_per_side", self.commission_per_share_per_side),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")

    @property
    def evaluation_status(self) -> str:
        if self.status is CostModelStatus.PROVISIONAL:
            return CostModelStatus.PENDING_EMPIRICAL.value
        return self.status.value

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["evaluation_status"] = self.evaluation_status
        return payload


DEFAULT_EXECUTION_COST_MODEL = ExecutionCostModel()


@dataclass(frozen=True, slots=True)
class ExecutionCostEstimate:
    raw_entry_price: float
    raw_exit_price: float
    entry_fill_price: float
    exit_fill_price: float
    quantity: int
    entry_slippage: float
    exit_slippage: float
    commission: float
    total_cost: float
    model_version: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_round_trip_cost(
    raw_entry_price: float,
    raw_exit_price: float,
    quantity: int,
    *,
    direction: str = "long",
    model: ExecutionCostModel = DEFAULT_EXECUTION_COST_MODEL,
    slippage_multiplier: float = 1.0,
) -> ExecutionCostEstimate:
    """Estimate cost and retain the model status alongside the number."""

    if quantity < 1:
        raise ValueError("quantity must be positive")
    if not math.isfinite(slippage_multiplier) or slippage_multiplier < 0:
        raise ValueError("slippage_multiplier must be finite and non-negative")
    normalized_direction = direction.lower()
    if normalized_direction not in {"long", "short"}:
        raise ValueError("direction must be long or short")
    entry_rate = model.entry_slippage_bps * slippage_multiplier / 10_000
    exit_rate = model.exit_slippage_bps * slippage_multiplier / 10_000
    if normalized_direction == "long":
        entry_fill = raw_entry_price * (1 + entry_rate)
        exit_fill = raw_exit_price * (1 - exit_rate)
    else:
        entry_fill = raw_entry_price * (1 - entry_rate)
        exit_fill = raw_exit_price * (1 + exit_rate)
    entry_slippage = abs(entry_fill - raw_entry_price) * quantity
    exit_slippage = abs(exit_fill - raw_exit_price) * quantity
    commission = model.commission_per_share_per_side * quantity * 2
    return ExecutionCostEstimate(
        raw_entry_price=raw_entry_price,
        raw_exit_price=raw_exit_price,
        entry_fill_price=entry_fill,
        exit_fill_price=exit_fill,
        quantity=quantity,
        entry_slippage=entry_slippage,
        exit_slippage=exit_slippage,
        commission=commission,
        total_cost=entry_slippage + exit_slippage + commission,
        model_version=model.version,
        status=model.evaluation_status,
    )


def cost_stress_grid(
    *,
    model: ExecutionCostModel = DEFAULT_EXECUTION_COST_MODEL,
    multipliers: tuple[float, ...] = (1.0, 1.5, 2.0),
) -> tuple[dict[str, Any], ...]:
    """Return named stress assumptions without declaring any profitable case."""

    return tuple(
        {
            "multiplier": multiplier,
            "status": model.evaluation_status,
            "model_version": model.version,
            "description": f"slippage and spread proxy at {multiplier:g}x",
        }
        for multiplier in multipliers
    )


__all__ = [
    "CostModelStatus",
    "DEFAULT_EXECUTION_COST_MODEL",
    "ExecutionCostEstimate",
    "ExecutionCostModel",
    "cost_stress_grid",
    "estimate_round_trip_cost",
]
