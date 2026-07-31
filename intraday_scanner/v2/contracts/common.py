"""Shared v2 contract primitives."""

from __future__ import annotations

from dataclasses import dataclass

from intraday_scanner.v2.contracts.serialization import ContractMixin

ScalarValue = str | int | float | bool | None


@dataclass(frozen=True)
class DataSourceId(ContractMixin):
    value: str
    schema_version: str = "v2.data_source_id.v1"


@dataclass(frozen=True)
class StrategyId(ContractMixin):
    value: str
    schema_version: str = "v2.strategy_id.v1"


@dataclass(frozen=True)
class StrategyVersion(ContractMixin):
    value: str
    schema_version: str = "v2.strategy_version.v1"


@dataclass(frozen=True)
class Symbol(ContractMixin):
    ticker: str
    asset_class: AssetClass
    exchange: str | None = None
    schema_version: str = "v2.symbol.v1"


from intraday_scanner.v2.contracts.data import AssetClass  # noqa: E402
