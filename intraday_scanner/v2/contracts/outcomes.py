"""Outcome and report contracts for Dawnstrike v2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from intraday_scanner.v2.contracts.common import StrategyId, StrategyVersion, Symbol
from intraday_scanner.v2.contracts.serialization import ContractMixin


class OutcomeLabel(str, Enum):
    WIN = "win"
    LOSS = "loss"
    SCRATCH = "scratch"
    NO_TRADE = "no_trade"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TradeOutcome(ContractMixin):
    outcome_id: str
    candidate_id: str
    symbol: Symbol
    label: OutcomeLabel
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None
    return_pct: Decimal | None = None
    fees_paid: Decimal | None = None
    slippage_bps: Decimal | None = None
    notes: str | None = None
    schema_version: str = "v2.trade_outcome.v1"


@dataclass(frozen=True)
class PerformanceMetric(ContractMixin):
    metric_id: str
    name: str
    value: Decimal
    unit: str
    sample_size: int | None = None
    evidence_status: str = "unknown"
    schema_version: str = "v2.performance_metric.v1"


@dataclass(frozen=True)
class BacktestSummary(ContractMixin):
    summary_id: str
    run_id: str
    created_at: datetime
    strategy_id: StrategyId
    strategy_version: StrategyVersion
    start_at: datetime
    end_at: datetime
    trade_count: int
    metrics: tuple[PerformanceMetric, ...]
    fees_assumption: str
    slippage_assumption: str
    warnings: tuple[str, ...] = ()
    schema_version: str = "v2.backtest_summary.v1"


@dataclass(frozen=True)
class ReportArtifact(ContractMixin):
    artifact_id: str
    artifact_type: str
    uri: str
    content_type: str
    sha256: str | None = None
    created_at: datetime | None = None
    schema_version: str = "v2.report_artifact.v1"


@dataclass(frozen=True)
class HistoricalReturnSummary(ContractMixin):
    summary_id: str
    created_at: datetime
    source_run_id: str
    period_start: datetime
    period_end: datetime
    selected_count: int
    outcome_count: int
    missing_outcome_count: int
    metrics: tuple[PerformanceMetric, ...]
    warnings: tuple[str, ...] = ()
    schema_version: str = "v2.historical_return_summary.v1"
