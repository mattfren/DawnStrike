"""Local validators for retrospective miss source contracts."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Protocol, cast

from intraday_scanner.v2.contracts.serialization import contract_to_json
from intraday_scanner.v2.data_truth import MarketQuote
from intraday_scanner.v2.opportunity.miss_contracts import (
    QualificationExecutionStatus,
    require_hash,
    require_identity,
    require_sanitized,
    require_unique,
)
from intraday_scanner.v2.opportunity.models import (
    FeatureSnapshot,
    FeatureStage,
    MarketRegime,
    SecurityRegime,
    stable_identity,
)
from intraday_scanner.v2.opportunity.outcome_sources import OutcomeBarEvidence
from intraday_scanner.v2.opportunity.risk import QuoteEvidenceScope


class _ExecutionEvidence(Protocol):
    symbol: str
    exchange_session_id: str
    reference_observation: OutcomeBarEvidence
    quote: MarketQuote | None
    quote_scope: QuoteEvidenceScope
    spread_bps: Decimal | None
    status: QualificationExecutionStatus
    observed_at: datetime | None
    source_identity: str
    source_artifact_id: str | None
    source_artifact_hash_sha256: str | None


class _SourceArtifact(Protocol):
    content_hash_sha256: str
    fetched_at: datetime


class _RegimeEvidence(Protocol):
    symbol: str
    benchmark_symbol: str
    decision_at: datetime
    benchmark_snapshot: FeatureSnapshot
    security_snapshot: FeatureSnapshot
    market_regime: MarketRegime
    security_regime: SecurityRegime
    observed_at: datetime
    fetched_at: datetime
    source_artifact: _SourceArtifact
    method: str


def validate_quote(raw_value: object) -> None:
    value = cast("_ExecutionEvidence", raw_value)
    quote = value.quote
    if quote is None:
        raise ValueError("execution quote is required")
    if quote.schema_version != "v2.market_quote.v1":
        raise ValueError("unsupported execution quote schema")
    if quote.symbol != value.symbol or quote.exchange_session_id != value.exchange_session_id:
        raise ValueError("execution quote symbol/session is inconsistent")
    if not (
        value.reference_observation.interval_start_at
        <= quote.timestamp
        < value.reference_observation.interval_end_at
    ):
        raise ValueError("execution quote must fall inside the reference interval")
    if quote.bid_price is None or quote.ask_price is None:
        raise ValueError("execution quote requires both sides")
    if quote.bid_price <= 0 or quote.ask_price <= 0 or quote.ask_price < quote.bid_price:
        raise ValueError("execution quote prices are invalid")
    if quote.source_metadata.fetched_at < quote.timestamp:
        raise ValueError("execution quote cannot be fetched before observation")
    metadata = quote.source_metadata
    if metadata.schema_version != "v2.intraday_source_metadata.v1":
        raise ValueError("unsupported execution quote source schema")
    if (
        quote.feed != metadata.feed
        or metadata.exchange_session_id != value.exchange_session_id
        or not metadata.request_start <= quote.timestamp <= metadata.request_end
    ):
        raise ValueError("execution quote source scope is inconsistent")
    for text, name in (
        (metadata.provider, "quote provider"),
        (metadata.feed, "quote feed"),
        (metadata.entitlement, "quote entitlement"),
        (metadata.code_sha, "quote code sha"),
        (metadata.retention_status, "quote retention status"),
    ):
        require_sanitized(text, name)
    require_hash(metadata.raw_artifact_hash_sha256, "quote raw artifact hash")
    require_hash(metadata.normalized_artifact_hash_sha256, "quote normalized artifact hash")
    expected_source_identity = quote_source_identity(quote)
    expected_artifact_id = stable_identity("qualification-quote-source", metadata)
    if value.source_identity != expected_source_identity:
        raise ValueError("execution source identity does not match quote metadata")
    if value.source_artifact_id != expected_artifact_id:
        raise ValueError("execution source artifact ID does not match quote metadata")
    if value.source_artifact_hash_sha256 != metadata.normalized_artifact_hash_sha256:
        raise ValueError("execution source artifact hash does not match quote metadata")
    if value.status is QualificationExecutionStatus.PROVISIONAL and value.quote_scope not in {
        QuoteEvidenceScope.PROVISIONAL,
        QuoteEvidenceScope.NONCONSOLIDATED,
    }:
        raise ValueError("provisional execution evidence requires provisional quote scope")
    if value.observed_at != quote.timestamp:
        raise ValueError("execution observed_at must equal quote timestamp")
    expected_spread = (quote.ask_price - quote.bid_price) / (
        (quote.ask_price + quote.bid_price) / Decimal("2")
    ) * Decimal("10000")
    if value.spread_bps != expected_spread:
        raise ValueError("execution spread does not match embedded quote")


def validate_regime_snapshots(raw_value: object) -> None:
    value = cast("_RegimeEvidence", raw_value)
    benchmark = value.benchmark_snapshot
    security = value.security_snapshot
    if (
        benchmark.schema_version != "v2.opportunity.feature_snapshot.v1"
        or security.schema_version != "v2.opportunity.feature_snapshot.v1"
    ):
        raise ValueError("unsupported retrospective feature snapshot schema")
    if benchmark.stage is not FeatureStage.RICH or security.stage is not FeatureStage.RICH:
        raise ValueError("retrospective regime evidence requires rich feature snapshots")
    if (
        benchmark.symbol != value.benchmark_symbol
        or security.symbol != value.symbol
        or benchmark.decision_at != value.decision_at
        or security.decision_at != value.decision_at
        or benchmark.dataset_id != security.dataset_id
        or benchmark.universe_id != security.universe_id
    ):
        raise ValueError("retrospective regime feature snapshots are inconsistent")


def regime_observed_at(raw_value: object) -> datetime:
    value = cast("_RegimeEvidence", raw_value)
    timestamps = [
        value.benchmark_snapshot.latest_bar_at,
        value.security_snapshot.latest_bar_at,
        *(item.observed_at for item in value.benchmark_snapshot.numerical),
        *(item.observed_at for item in value.benchmark_snapshot.categorical),
        *(item.observed_at for item in value.security_snapshot.numerical),
        *(item.observed_at for item in value.security_snapshot.categorical),
    ]
    return max(timestamps)


def regime_measurement_hash(raw_value: object) -> str:
    value = cast("_RegimeEvidence", raw_value)
    payload = {
        "benchmark_snapshot": value.benchmark_snapshot,
        "security_snapshot": value.security_snapshot,
        "method": value.method,
    }
    return hashlib.sha256(contract_to_json(payload).encode("utf-8")).hexdigest()


def quote_source_identity(quote: MarketQuote) -> str:
    metadata = quote.source_metadata
    return f"{metadata.provider}:{metadata.feed}:{metadata.entitlement}"


def require_symbol(value: str) -> None:
    if not value or value != value.strip().upper():
        raise ValueError("symbol must be canonical uppercase text")


def require_sorted_symbols(values: tuple[str, ...], label: str) -> None:
    for value in values:
        require_symbol(value)
    if values != tuple(sorted(values)):
        raise ValueError(f"{label} values must use canonical order")
    require_unique(values, label)


def require_paired_inventory(
    ids: tuple[str, ...], hashes: tuple[str, ...], label: str
) -> None:
    if len(ids) != len(hashes) or ids != tuple(sorted(ids)):
        raise ValueError(f"{label} source inventory is not canonical")
    require_unique(ids, f"{label} source artifact")
    for value in ids:
        require_identity(value, f"{label} source artifact ID")
    for value in hashes:
        require_hash(value, f"{label} source artifact hash")


def require_sanitized_values(values: tuple[str, ...], label: str) -> None:
    require_unique(values, label)
    for value in values:
        require_sanitized(value, label)


__all__ = [
    "regime_measurement_hash",
    "regime_observed_at",
    "require_paired_inventory",
    "require_sanitized_values",
    "require_sorted_symbols",
    "require_symbol",
    "validate_quote",
    "validate_regime_snapshots",
]
