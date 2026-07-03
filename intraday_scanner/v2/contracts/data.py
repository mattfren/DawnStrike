"""Data ingestion contracts for Dawnstrike v2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from intraday_scanner.v2.contracts.serialization import ContractMixin


class AssetClass(str, Enum):
    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"
    OPTION = "option"
    FUTURE = "future"
    CRYPTO = "crypto"
    UNKNOWN = "unknown"


class Timeframe(str, Enum):
    TICK = "tick"
    MINUTE_1 = "1m"
    MINUTE_5 = "5m"
    MINUTE_15 = "15m"
    HOUR_1 = "1h"
    DAILY = "1d"
    PREMARKET_SNAPSHOT = "premarket_snapshot"


class DataValidationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DataValidationStatus(str, Enum):
    PASSED = "passed"
    PASSED_WITH_WARNINGS = "passed_with_warnings"
    FAILED = "failed"


@dataclass(frozen=True)
class Bar(ContractMixin):
    symbol: Symbol
    timeframe: Timeframe
    timestamp: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int
    source_id: DataSourceId
    adjusted: bool = False
    schema_version: str = "v2.bar.v1"


@dataclass(frozen=True)
class BarBatch(ContractMixin):
    batch_id: str
    source_id: DataSourceId
    symbol: Symbol
    asset_class: AssetClass
    timeframe: Timeframe
    bars: tuple[Bar, ...]
    created_at: datetime
    schema_version: str = "v2.bar_batch.v1"


@dataclass(frozen=True)
class DataSnapshot(ContractMixin):
    snapshot_id: str
    source_id: DataSourceId
    created_at: datetime
    as_of: datetime
    asset_class: AssetClass
    timeframe: Timeframe
    symbols: tuple[Symbol, ...]
    batches: tuple[BarBatch, ...]
    warnings: tuple[str, ...] = ()
    schema_version: str = "v2.data_snapshot.v1"


@dataclass(frozen=True)
class DataValidationIssue(ContractMixin):
    issue_id: str
    severity: DataValidationSeverity
    code: str
    message: str
    symbol: Symbol | None = None
    field_name: str | None = None
    source_id: DataSourceId | None = None
    schema_version: str = "v2.data_validation_issue.v1"


@dataclass(frozen=True)
class DataValidationReport(ContractMixin):
    report_id: str
    snapshot_id: str
    source_id: DataSourceId
    created_at: datetime
    status: DataValidationStatus
    issues: tuple[DataValidationIssue, ...] = ()
    schema_version: str = "v2.data_validation_report.v1"


from intraday_scanner.v2.contracts.common import DataSourceId, Symbol  # noqa: E402
