"""Risk and operator decision contracts for Dawnstrike v2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from intraday_scanner.v2.contracts.common import StrategyId, StrategyVersion, Symbol
from intraday_scanner.v2.contracts.scan import (
    SignalDirection,
    SignalEvidence,
    SignalStatus,
)
from intraday_scanner.v2.contracts.serialization import ContractMixin


@dataclass(frozen=True)
class RiskConfig(ContractMixin):
    config_id: str
    max_position_pct: Decimal
    max_daily_loss_pct: Decimal | None = None
    max_open_positions: int | None = None
    hard_block_codes: tuple[str, ...] = ()
    allow_live_execution: bool = False
    schema_version: str = "v2.risk_config.v1"


@dataclass(frozen=True)
class PositionSizingInput(ContractMixin):
    request_id: str
    symbol: Symbol
    account_equity: Decimal
    entry_price: Decimal
    stop_price: Decimal
    risk_per_trade_pct: Decimal
    risk_config: RiskConfig
    schema_version: str = "v2.position_sizing_input.v1"


@dataclass(frozen=True)
class PositionSizingResult(ContractMixin):
    result_id: str
    request_id: str
    allowed: bool
    quantity: int
    notional: Decimal
    risk_amount: Decimal
    reject_reasons: tuple[str, ...] = ()
    schema_version: str = "v2.position_sizing_result.v1"


@dataclass(frozen=True)
class TradePlan(ContractMixin):
    plan_id: str
    candidate_id: str
    symbol: Symbol
    direction: SignalDirection
    entry_price: Decimal
    stop_price: Decimal
    target_prices: tuple[Decimal, ...]
    position_size: PositionSizingResult
    invalidation: str
    created_at: datetime
    research_only: bool = True
    schema_version: str = "v2.trade_plan.v1"


@dataclass(frozen=True)
class DecisionCard(ContractMixin):
    card_id: str
    symbol: Symbol
    strategy_id: StrategyId
    strategy_version: StrategyVersion
    generated_at: datetime
    signal_status: SignalStatus
    direction: SignalDirection
    summary: str
    evidence: tuple[SignalEvidence, ...]
    trade_plan: TradePlan | None = None
    risk_result: PositionSizingResult | None = None
    warnings: tuple[str, ...] = ()
    research_only: bool = True
    schema_version: str = "v2.decision_card.v1"
