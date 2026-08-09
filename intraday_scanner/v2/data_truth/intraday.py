"""Immutable contracts for retained intraday market-data evidence.

These contracts describe source facts only.  They intentionally contain no
strategy, signal, order, or result status fields.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from intraday_scanner.v2.contracts.serialization import ContractMixin


class PriceAdjustmentBasis(str, Enum):
    UNADJUSTED = "unadjusted"
    SPLIT_ADJUSTED = "split_adjusted"
    DIVIDEND_ADJUSTED = "dividend_adjusted"
    TOTAL_RETURN = "total_return"
    UNKNOWN = "unknown"


class IntradayCoverageStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL_MISSING_INTERVALS = "PARTIAL_MISSING_INTERVALS"
    NO_DATA = "NO_DATA"
    KNOWN_HALT_GAPS = "KNOWN_HALT_GAPS"
    ENTITLEMENT_DENIED = "ENTITLEMENT_DENIED"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    CORPORATE_ACTION_UNRESOLVED = "CORPORATE_ACTION_UNRESOLVED"
    HASH_MISMATCH = "HASH_MISMATCH"
    FUTURE_DATA_REJECTED = "FUTURE_DATA_REJECTED"
    DATA_INELIGIBLE = "DATA_INELIGIBLE"


class _UtcContract(ContractMixin):
    def __post_init__(self) -> None:
        super().__post_init__()
        for field in fields(self):  # type: ignore[arg-type]
            value = getattr(self, field.name)
            if isinstance(value, datetime):
                _require_utc(value, field.name)
            elif isinstance(value, tuple):
                for item in value:
                    if isinstance(item, tuple):
                        for nested in item:
                            if isinstance(nested, datetime):
                                _require_utc(nested, field.name)


def _require_utc(value: datetime, field_name: str) -> None:
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC")


@dataclass(frozen=True)
class IntradaySourceMetadata(_UtcContract):
    provider: str
    feed: str
    entitlement: str
    exchange_session_id: str
    request_start: datetime
    request_end: datetime
    fetched_at: datetime
    code_sha: str
    raw_artifact_hash_sha256: str
    normalized_artifact_hash_sha256: str
    retention_status: str
    schema_version: str = "v2.intraday_source_metadata.v1"


@dataclass(frozen=True)
class IntradayBar(_UtcContract):
    symbol: str
    exchange_session_id: str
    timestamp: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int
    vwap: Decimal
    price_adjustment_basis: PriceAdjustmentBasis
    source_metadata: IntradaySourceMetadata
    trade_count: int | None = None
    schema_version: str = "v2.intraday_bar.v1"


@dataclass(frozen=True)
class TradePrint(_UtcContract):
    symbol: str
    exchange_session_id: str
    timestamp: datetime
    price: Decimal
    size: int
    exchange: str
    conditions: tuple[str, ...]
    sequence: int | None
    price_adjustment_basis: PriceAdjustmentBasis
    source_metadata: IntradaySourceMetadata
    schema_version: str = "v2.trade_print.v1"


@dataclass(frozen=True)
class MarketQuote(_UtcContract):
    symbol: str
    exchange_session_id: str
    timestamp: datetime
    feed: str
    bid_price: Decimal | None
    ask_price: Decimal | None
    bid_size: int | None
    ask_size: int | None
    bid_exchange: str | None
    ask_exchange: str | None
    price_adjustment_basis: PriceAdjustmentBasis
    source_metadata: IntradaySourceMetadata
    schema_version: str = "v2.market_quote.v1"


@dataclass(frozen=True)
class MarketStatusInterval(_UtcContract):
    symbol: str
    exchange_session_id: str
    status: str
    start: datetime
    end: datetime
    reason: str
    source_metadata: IntradaySourceMetadata
    schema_version: str = "v2.market_status_interval.v1"


@dataclass(frozen=True)
class CorporateActionRecord(_UtcContract):
    symbol: str
    mapped_symbol: str
    action_type: str
    effective_at: datetime
    exchange_session_id: str
    price_adjustment_basis: PriceAdjustmentBasis
    source_metadata: IntradaySourceMetadata
    details: dict[str, Any] | None = None
    schema_version: str = "v2.corporate_action_record.v1"


@dataclass(frozen=True)
class IntradayArtifactManifest(_UtcContract):
    artifact_manifest_id: str
    artifact_identity: str
    provider: str
    feed: str
    artifact_kind: str
    symbol: str
    market_date: str
    exchange_session_id: str
    request_start: datetime
    request_end: datetime
    fetched_at: datetime
    code_sha: str
    raw_artifact_hash_sha256: str
    normalized_artifact_hash_sha256: str
    raw_artifact_path: str
    normalized_artifact_path: str
    retention_status: str
    created_at: datetime
    metadata: dict[str, Any] | None = None
    schema_version: str = "v2.intraday_artifact_manifest.v1"


@dataclass(frozen=True)
class IntradayCoverageReceipt(_UtcContract):
    coverage_receipt_id: str
    provider: str
    feed: str
    entitlement: str
    symbol: str
    market_date: str
    exchange_session_id: str
    request_start: datetime
    request_end: datetime
    status: IntradayCoverageStatus
    source_metadata: IntradaySourceMetadata
    observed_start: datetime | None = None
    observed_end: datetime | None = None
    missing_intervals: tuple[tuple[datetime, datetime], ...] = ()
    artifact_manifest_ids: tuple[str, ...] = ()
    reason: str = ""
    created_at: datetime | None = None
    schema_version: str = "v2.intraday_coverage_receipt.v1"


@dataclass(frozen=True)
class IntradayProviderCapabilityReceipt(_UtcContract):
    """Sanitized provider/entitlement probe receipt."""

    capability_receipt_id: str
    provider: str
    feed: str
    entitlement: str
    requested_at: datetime
    request_start: datetime
    request_end: datetime
    fetched_at: datetime
    code_sha: str
    raw_artifact_hash_sha256: str
    normalized_artifact_hash_sha256: str
    retention_status: str
    capabilities: dict[str, Any]
    receipt_hash_sha256: str
    schema_version: str = "v2.intraday_provider_capability_receipt.v1"


__all__ = [
    "CorporateActionRecord",
    "IntradayArtifactManifest",
    "IntradayBar",
    "IntradayCoverageReceipt",
    "IntradayCoverageStatus",
    "IntradayProviderCapabilityReceipt",
    "IntradaySourceMetadata",
    "MarketQuote",
    "MarketStatusInterval",
    "PriceAdjustmentBasis",
    "TradePrint",
]
