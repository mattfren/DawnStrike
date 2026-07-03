"""Audit manifest contracts for deterministic v2 runs and reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from intraday_scanner.v2.contracts.common import (
    DataSourceId,
    ScalarValue,
    StrategyId,
    StrategyVersion,
    Symbol,
)
from intraday_scanner.v2.contracts.data import Timeframe
from intraday_scanner.v2.contracts.outcomes import ReportArtifact
from intraday_scanner.v2.contracts.serialization import ContractMixin


class RunType(str, Enum):
    DATA_INGESTION = "data_ingestion"
    SCAN = "scan"
    BACKTEST = "backtest"
    REPORT = "report"
    RISK_REVIEW = "risk_review"


class ReportType(str, Enum):
    SCAN_SUMMARY = "scan_summary"
    BACKTEST_SUMMARY = "backtest_summary"
    HISTORICAL_RETURNS = "historical_returns"
    RISK_SUMMARY = "risk_summary"
    DAILY_REVIEW = "daily_review"


@dataclass(frozen=True)
class DataLineage(ContractMixin):
    data_snapshot_id: str
    source_id: DataSourceId
    source_kind: str
    rows_read: int
    rows_accepted: int
    rows_rejected: int
    source_refs: tuple[str, ...]
    validation_report_id: str | None = None
    schema_version: str = "v2.data_lineage.v1"


@dataclass(frozen=True)
class CodeLineage(ContractMixin):
    code_version: str | None = None
    git_commit: str | None = None
    app_version: str | None = None
    dirty_tree: bool | None = None
    schema_version: str = "v2.code_lineage.v1"


@dataclass(frozen=True)
class ExecutionAssumptions(ContractMixin):
    assumption_id: str
    research_only: bool
    order_type: str
    fill_model: str
    allow_live_execution: bool = False
    schema_version: str = "v2.execution_assumptions.v1"


@dataclass(frozen=True)
class FeeAssumptions(ContractMixin):
    model_id: str
    commission_per_trade: Decimal
    regulatory_fees_bps: Decimal
    notes: str | None = None
    schema_version: str = "v2.fee_assumptions.v1"


@dataclass(frozen=True)
class SlippageAssumptions(ContractMixin):
    model_id: str
    slippage_bps: Decimal
    model_description: str
    schema_version: str = "v2.slippage_assumptions.v1"


@dataclass(frozen=True)
class RunManifest(ContractMixin):
    run_id: str
    run_type: RunType
    created_at: datetime
    code_lineage: CodeLineage
    data_snapshot_id: str
    timeframe: Timeframe
    strategy_id: StrategyId
    strategy_version: StrategyVersion
    parameters: dict[str, ScalarValue]
    fee_assumptions: FeeAssumptions
    slippage_assumptions: SlippageAssumptions
    execution_assumptions: ExecutionAssumptions
    source_data: tuple[DataLineage, ...]
    output_artifacts: tuple[ReportArtifact, ...]
    universe_id: str | None = None
    symbols: tuple[Symbol, ...] = ()
    warnings: tuple[str, ...] = ()
    schema_version: str = "v2.run_manifest.v1"


@dataclass(frozen=True)
class ReportManifest(ContractMixin):
    report_id: str
    report_type: ReportType
    created_at: datetime
    source_run_id: str
    source_data_snapshot_id: str
    generated_artifacts: tuple[ReportArtifact, ...]
    execution_assumptions: ExecutionAssumptions
    fee_assumptions: FeeAssumptions
    slippage_assumptions: SlippageAssumptions
    warnings: tuple[str, ...] = ()
    schema_version: str = "v2.report_manifest.v1"
