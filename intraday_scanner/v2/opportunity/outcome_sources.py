"""Strict downstream source-evidence wrappers for outcome labeling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from intraday_scanner.v2.data_truth.intraday import (
    CorporateActionRecord,
    IntradayBar,
    IntradayCoverageReceipt,
    IntradayCoverageStatus,
    IntradaySourceMetadata,
    MarketStatusInterval,
)
from intraday_scanner.v2.opportunity.models import OpportunityContract, stable_identity
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeContract,
    OutcomeMarketStatusKind,
    _contract_hash,
    _identity_payload,
    _require_aware,
    _require_hash,
    _require_identity,
    _require_positive_decimal,
    _require_sanitized_text,
    _require_schema,
    _require_unique,
    _require_utc,
    _validate_safe_nested,
)


@dataclass(frozen=True)
class OutcomeBarEvidence(OutcomeContract):
    observation_id: str
    bar_content_hash_sha256: str
    interval_start_at: datetime
    interval_end_at: datetime
    available_at: datetime
    bar: IntradayBar
    schema_version: str = "v2.opportunity.outcome_bar_evidence.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.outcome_bar_evidence.v1")
        _require_identity(self.observation_id, "observation_id")
        _require_hash(self.bar_content_hash_sha256, "bar_content_hash_sha256")
        for timestamp, name in (
            (self.interval_start_at, "interval_start_at"),
            (self.interval_end_at, "interval_end_at"),
            (self.available_at, "available_at"),
            (self.bar.timestamp, "bar.timestamp"),
        ):
            _require_utc(timestamp, name)
        if not self.interval_start_at < self.interval_end_at <= self.available_at:
            raise ValueError("outcome bar interval/availability chronology is invalid")
        if self.bar.timestamp != self.interval_end_at:
            raise ValueError("outcome bar timestamp must identify the interval end")
        metadata = self.bar.source_metadata
        _require_schema(self.bar.schema_version, "v2.intraday_bar.v1")
        _validate_source_metadata(metadata)
        if metadata.fetched_at < self.interval_end_at:
            raise ValueError("outcome bar source cannot be fetched before interval end")
        if self.available_at != metadata.fetched_at:
            raise ValueError("outcome bar availability must match interval/source availability")
        if (
            self.bar.exchange_session_id != metadata.exchange_session_id
            or metadata.request_start > self.interval_start_at
            or metadata.request_end < self.interval_end_at
        ):
            raise ValueError("outcome bar interval is outside its source request/session scope")
        for price, name in (
            (self.bar.open_price, "open_price"),
            (self.bar.high_price, "high_price"),
            (self.bar.low_price, "low_price"),
            (self.bar.close_price, "close_price"),
            (self.bar.vwap, "vwap"),
        ):
            _require_positive_decimal(price, name)
        if self.bar.high_price < max(
            self.bar.open_price,
            self.bar.close_price,
            self.bar.low_price,
        ) or self.bar.low_price > min(
            self.bar.open_price,
            self.bar.close_price,
            self.bar.high_price,
        ):
            raise ValueError("outcome bar OHLC geometry is invalid")
        if not self.bar.low_price <= self.bar.vwap <= self.bar.high_price:
            raise ValueError("outcome bar VWAP must lie inside bar low/high")
        if type(self.bar.volume) is not int or self.bar.volume < 0:
            raise ValueError("outcome bar volume must be a nonnegative integer")
        if self.bar.trade_count is not None and (
            type(self.bar.trade_count) is not int or self.bar.trade_count < 0
        ):
            raise ValueError("outcome bar trade_count must be a nonnegative integer")
        for digest, name in (
            (metadata.raw_artifact_hash_sha256, "raw_artifact_hash_sha256"),
            (metadata.normalized_artifact_hash_sha256, "normalized_artifact_hash_sha256"),
        ):
            _require_hash(digest, name)
        expected_bar_hash = _contract_hash(self.bar)
        if self.bar_content_hash_sha256 != expected_bar_hash:
            raise ValueError("outcome bar content hash does not match embedded bar")
        expected = stable_identity("outcome-observation", _identity_payload(self, "observation_id"))
        if self.observation_id != expected:
            raise ValueError("outcome observation identity does not match content")


@dataclass(frozen=True)
class OutcomeObservationSeries(OutcomeContract):
    series_id: str
    symbol: str
    exchange_session_id: str
    decision_at: datetime
    first_expected_interval_start_at: datetime
    requested_through_at: datetime
    coverage_receipt_id: str
    coverage_receipt_content_hash_sha256: str
    coverage_receipt: IntradayCoverageReceipt
    observations: tuple[OutcomeBarEvidence, ...]
    market_status_interval_hashes: tuple[str, ...]
    market_status_intervals: tuple[MarketStatusInterval, ...]
    corporate_action_hashes: tuple[str, ...]
    corporate_actions: tuple[CorporateActionRecord, ...]
    source_identity: str
    method: str
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.outcome_observation_series.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version,
            "v2.opportunity.outcome_observation_series.v1",
        )
        for value, name in (
            (self.series_id, "series_id"),
            (self.symbol, "symbol"),
            (self.exchange_session_id, "exchange_session_id"),
            (self.coverage_receipt_id, "coverage_receipt_id"),
        ):
            _require_identity(value, name)
        _require_aware(self.decision_at, "decision_at")
        _require_utc(
            self.first_expected_interval_start_at,
            "first_expected_interval_start_at",
        )
        _require_utc(self.requested_through_at, "requested_through_at")
        if not self.decision_at < self.first_expected_interval_start_at < self.requested_through_at:
            raise ValueError("outcome observation request must extend after decision_at")
        _require_hash(
            self.coverage_receipt_content_hash_sha256,
            "coverage_receipt_content_hash_sha256",
        )
        _require_sanitized_text(self.source_identity, "source_identity")
        _require_sanitized_text(self.method, "method")
        receipt = self.coverage_receipt
        _require_schema(receipt.schema_version, "v2.intraday_coverage_receipt.v1")
        receipt_source = receipt.source_metadata
        _validate_source_metadata(receipt_source)
        for text_value, text_name in (
            (receipt.provider, "coverage provider"),
            (receipt.feed, "coverage feed"),
            (receipt.entitlement, "coverage entitlement"),
            (receipt.reason, "coverage reason"),
        ):
            _require_sanitized_text(text_value, text_name)
        for manifest_id in receipt.artifact_manifest_ids:
            _require_identity(manifest_id, "artifact_manifest_id")
        if receipt.artifact_manifest_ids != tuple(sorted(set(receipt.artifact_manifest_ids))):
            raise ValueError("artifact manifest IDs must use canonical unique order")
        receipt_available_at = max(
            receipt_source.fetched_at,
            receipt.created_at or receipt_source.fetched_at,
        )
        if (
            self.coverage_receipt_id != receipt.coverage_receipt_id
            or self.coverage_receipt_content_hash_sha256 != _contract_hash(receipt)
            or receipt.symbol != self.symbol
            or receipt.exchange_session_id != self.exchange_session_id
            or receipt_source.exchange_session_id != self.exchange_session_id
            or receipt.provider != receipt_source.provider
            or receipt.feed != receipt_source.feed
            or receipt.entitlement != receipt_source.entitlement
            or receipt.request_start != self.first_expected_interval_start_at
            or receipt_source.request_start > receipt.request_start
            or receipt_source.request_end < receipt.request_end
            or receipt.request_end < self.requested_through_at
            or (
                receipt.observed_end is not None
                and receipt.observed_end > receipt_available_at
            )
        ):
            raise ValueError("outcome coverage receipt does not match observation series")
        for digest in (
            receipt_source.raw_artifact_hash_sha256,
            receipt_source.normalized_artifact_hash_sha256,
        ):
            _require_hash(digest, "coverage receipt source artifact hash")
        previous_end: datetime | None = None
        observation_ids: list[str] = []
        for observation in self.observations:
            if (
                observation.bar.symbol != self.symbol
                or observation.bar.exchange_session_id != self.exchange_session_id
                or observation.bar.source_metadata.provider != receipt.provider
                or observation.bar.source_metadata.feed != receipt.feed
                or observation.bar.source_metadata.entitlement != receipt.entitlement
                or observation.interval_start_at <= self.decision_at
                or observation.interval_end_at > self.requested_through_at
            ):
                raise ValueError("outcome observation does not match series scope")
            if previous_end is not None and observation.interval_start_at < previous_end:
                raise ValueError("outcome observations overlap or are out of order")
            previous_end = observation.interval_end_at
            observation_ids.append(observation.observation_id)
        if self.observations and (
            self.observations[0].interval_start_at < self.first_expected_interval_start_at
        ):
            raise ValueError("outcome observation precedes the expected initial interval")
        _require_unique(observation_ids, "outcome observation ID")
        _validate_embedded_hashes(
            self.market_status_interval_hashes,
            self.market_status_intervals,
            "market status interval",
        )
        status_order = tuple(
            sorted(
                self.market_status_intervals,
                key=lambda item: (item.start, item.end, item.status, _contract_hash(item)),
            )
        )
        if self.market_status_intervals != status_order or len(
            set(self.market_status_interval_hashes)
        ) != len(self.market_status_interval_hashes):
            raise ValueError("market status intervals must use canonical unique order")
        action_order = tuple(
            sorted(
                self.corporate_actions,
                key=lambda item: (
                    item.effective_at,
                    item.action_type,
                    item.mapped_symbol,
                    _contract_hash(item),
                ),
            )
        )
        if self.corporate_actions != action_order or len(set(self.corporate_action_hashes)) != len(
            self.corporate_action_hashes
        ):
            raise ValueError("corporate actions must use canonical unique order")
        _validate_embedded_hashes(
            self.corporate_action_hashes,
            self.corporate_actions,
            "corporate action",
        )
        for interval in self.market_status_intervals:
            _require_schema(interval.schema_version, "v2.market_status_interval.v1")
            _require_sanitized_text(interval.reason, "market status reason")
            if interval.status not in {item.value for item in OutcomeMarketStatusKind}:
                raise ValueError("unsupported outcome market status interval value")
            source = interval.source_metadata
            _validate_source_metadata(source)
            if (
                interval.symbol != self.symbol
                or interval.exchange_session_id != self.exchange_session_id
                or source.exchange_session_id != self.exchange_session_id
                or source.request_start > interval.start
                or source.request_end < interval.end
                or source.fetched_at < interval.end
                or interval.start >= interval.end
                or interval.end <= self.decision_at
                or interval.start >= self.requested_through_at
            ):
                raise ValueError("market status interval does not overlap outcome series")
        for action in self.corporate_actions:
            _require_schema(action.schema_version, "v2.corporate_action_record.v1")
            source = action.source_metadata
            _validate_source_metadata(source)
            _require_identity(action.mapped_symbol, "corporate action mapped_symbol")
            _require_sanitized_text(action.action_type, "corporate action type")
            _validate_safe_nested(action.details, "corporate action details")
            if (
                action.symbol != self.symbol
                or action.exchange_session_id != self.exchange_session_id
                or source.exchange_session_id != self.exchange_session_id
                or not source.request_start <= action.effective_at <= source.request_end
                or not self.decision_at < action.effective_at <= self.requested_through_at
            ):
                raise ValueError("corporate action does not match outcome series")
        _validate_coverage_body(self)
        _require_unique(list(self.limitations), "outcome series limitation")
        for limitation in self.limitations:
            _require_sanitized_text(limitation, "outcome series limitation")
        expected = stable_identity("outcome-series", _identity_payload(self, "series_id"))
        if self.series_id != expected:
            raise ValueError("outcome observation series identity does not match content")


@dataclass(frozen=True)
class OutcomeObservationDataset(OutcomeContract):
    source_dataset_id: str
    decision_at: datetime
    frozen_at: datetime
    series: tuple[OutcomeObservationSeries, ...]
    source_artifact_hashes: tuple[str, ...]
    limitations: tuple[str, ...] = ()
    research_only: bool = True
    schema_version: str = "v2.opportunity.outcome_observation_dataset.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version,
            "v2.opportunity.outcome_observation_dataset.v1",
        )
        _require_identity(self.source_dataset_id, "source_dataset_id")
        _require_aware(self.decision_at, "decision_at")
        _require_utc(self.frozen_at, "frozen_at")
        if self.frozen_at <= self.decision_at:
            raise ValueError("outcome observation dataset must be frozen after decision_at")
        symbols = tuple(item.symbol for item in self.series)
        if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
            raise ValueError("outcome observation series must use unique symbol order")
        if any(item.decision_at != self.decision_at for item in self.series):
            raise ValueError("outcome series decision_at must match dataset")
        if any(
            observation.available_at > self.frozen_at
            for item in self.series
            for observation in item.observations
        ):
            raise ValueError("outcome observation was unavailable when dataset was frozen")
        for item in self.series:
            receipt = item.coverage_receipt
            receipt_available_at = max(
                receipt.source_metadata.fetched_at,
                receipt.created_at or receipt.source_metadata.fetched_at,
            )
            fact_times = (
                receipt_available_at,
                *(status.source_metadata.fetched_at for status in item.market_status_intervals),
                *(action.source_metadata.fetched_at for action in item.corporate_actions),
            )
            if any(value > self.frozen_at for value in fact_times):
                raise ValueError("outcome source fact was unavailable when dataset was frozen")
        for value in self.source_artifact_hashes:
            _require_hash(value, "source_artifact_hash")
        if self.source_artifact_hashes != tuple(sorted(set(self.source_artifact_hashes))):
            raise ValueError("source artifact hashes must use canonical unique order")
        embedded_hashes = _source_artifact_hashes(self.series)
        if set(self.source_artifact_hashes) != embedded_hashes:
            raise ValueError("source artifact hashes do not match embedded observations")
        _require_unique(list(self.limitations), "outcome dataset limitation")
        for limitation in self.limitations:
            _require_sanitized_text(limitation, "outcome dataset limitation")
        if not self.research_only:
            raise ValueError("outcome observation dataset must remain research_only")
        expected = stable_identity(
            "outcome-source-dataset",
            _identity_payload(self, "source_dataset_id"),
        )
        if self.source_dataset_id != expected:
            raise ValueError("outcome source dataset identity does not match content")


def build_outcome_bar_evidence(
    *,
    bar: IntradayBar,
    interval_start_at: datetime,
    interval_end_at: datetime,
    available_at: datetime | None = None,
) -> OutcomeBarEvidence:
    if bar.source_metadata.fetched_at < interval_end_at:
        raise ValueError("bar source cannot be fetched before its interval ends")
    derived_available_at = bar.source_metadata.fetched_at
    if available_at is not None and available_at != derived_available_at:
        raise ValueError("available_at must equal interval/source-derived availability")
    values = {
        "bar_content_hash_sha256": _contract_hash(bar),
        "interval_start_at": interval_start_at,
        "interval_end_at": interval_end_at,
        "available_at": derived_available_at,
        "bar": bar,
        "schema_version": "v2.opportunity.outcome_bar_evidence.v1",
    }
    return OutcomeBarEvidence(
        observation_id=stable_identity("outcome-observation", values),
        bar_content_hash_sha256=_contract_hash(bar),
        interval_start_at=interval_start_at,
        interval_end_at=interval_end_at,
        available_at=derived_available_at,
        bar=bar,
    )


def build_outcome_observation_series(
    *,
    symbol: str,
    exchange_session_id: str,
    decision_at: datetime,
    requested_through_at: datetime,
    first_expected_interval_start_at: datetime | None = None,
    coverage_receipt: IntradayCoverageReceipt,
    observations: tuple[OutcomeBarEvidence, ...],
    market_status_intervals: tuple[MarketStatusInterval, ...] = (),
    corporate_actions: tuple[CorporateActionRecord, ...] = (),
    source_identity: str,
    method: str,
    limitations: tuple[str, ...] = (),
) -> OutcomeObservationSeries:
    expected_start = first_expected_interval_start_at or coverage_receipt.request_start
    values = {
        "symbol": symbol,
        "exchange_session_id": exchange_session_id,
        "decision_at": decision_at,
        "first_expected_interval_start_at": expected_start,
        "requested_through_at": requested_through_at,
        "coverage_receipt_id": coverage_receipt.coverage_receipt_id,
        "coverage_receipt_content_hash_sha256": _contract_hash(coverage_receipt),
        "coverage_receipt": coverage_receipt,
        "observations": observations,
        "market_status_interval_hashes": tuple(
            _contract_hash(item) for item in market_status_intervals
        ),
        "market_status_intervals": market_status_intervals,
        "corporate_action_hashes": tuple(_contract_hash(item) for item in corporate_actions),
        "corporate_actions": corporate_actions,
        "source_identity": source_identity,
        "method": method,
        "limitations": limitations,
        "schema_version": "v2.opportunity.outcome_observation_series.v1",
    }
    return OutcomeObservationSeries(
        series_id=stable_identity("outcome-series", values),
        symbol=symbol,
        exchange_session_id=exchange_session_id,
        decision_at=decision_at,
        first_expected_interval_start_at=expected_start,
        requested_through_at=requested_through_at,
        coverage_receipt_id=coverage_receipt.coverage_receipt_id,
        coverage_receipt_content_hash_sha256=_contract_hash(coverage_receipt),
        coverage_receipt=coverage_receipt,
        observations=observations,
        market_status_interval_hashes=tuple(
            _contract_hash(item) for item in market_status_intervals
        ),
        market_status_intervals=market_status_intervals,
        corporate_action_hashes=tuple(_contract_hash(item) for item in corporate_actions),
        corporate_actions=corporate_actions,
        source_identity=source_identity,
        method=method,
        limitations=limitations,
    )


def build_outcome_observation_dataset(
    *,
    decision_at: datetime,
    frozen_at: datetime,
    series: tuple[OutcomeObservationSeries, ...],
    limitations: tuple[str, ...] = (),
) -> OutcomeObservationDataset:
    ordered = tuple(sorted(series, key=lambda item: item.symbol))
    source_hashes = tuple(sorted(_source_artifact_hashes(ordered)))
    values = {
        "decision_at": decision_at,
        "frozen_at": frozen_at,
        "series": ordered,
        "source_artifact_hashes": source_hashes,
        "limitations": limitations,
        "research_only": True,
        "schema_version": "v2.opportunity.outcome_observation_dataset.v1",
    }
    return OutcomeObservationDataset(
        source_dataset_id=stable_identity("outcome-source-dataset", values),
        decision_at=decision_at,
        frozen_at=frozen_at,
        series=ordered,
        source_artifact_hashes=source_hashes,
        limitations=limitations,
    )


def _validate_coverage_body(series: OutcomeObservationSeries) -> None:
    receipt = series.coverage_receipt
    observed_pair = (receipt.observed_start, receipt.observed_end)
    if (observed_pair[0] is None) is not (observed_pair[1] is None):
        raise ValueError("coverage observed bounds must be present together")
    if receipt.observed_start is not None and receipt.observed_end is not None:
        if not (
            receipt.request_start
            <= receipt.observed_start
            <= receipt.observed_end
            <= receipt.request_end
        ):
            raise ValueError("coverage observed bounds lie outside request scope")
    if series.observations:
        if (
            receipt.observed_start != series.observations[0].interval_start_at
            or receipt.observed_end != series.observations[-1].interval_end_at
        ):
            raise ValueError("coverage observed bounds do not match retained bar body")
    elif any(value is not None for value in observed_pair):
        raise ValueError("empty coverage body requires null observed bounds")
    ordered_missing = tuple(sorted(receipt.missing_intervals))
    if receipt.missing_intervals != ordered_missing:
        raise ValueError("coverage missing intervals must use canonical order")
    previous_end: datetime | None = None
    for start, end in receipt.missing_intervals:
        if not receipt.request_start <= start < end <= receipt.request_end:
            raise ValueError("coverage missing interval lies outside request scope")
        if previous_end is not None and start < previous_end:
            raise ValueError("coverage missing intervals overlap")
        previous_end = end
    hard_unavailable = {
        IntradayCoverageStatus.NO_DATA,
        IntradayCoverageStatus.ENTITLEMENT_DENIED,
        IntradayCoverageStatus.HASH_MISMATCH,
        IntradayCoverageStatus.FUTURE_DATA_REJECTED,
        IntradayCoverageStatus.DATA_INELIGIBLE,
        IntradayCoverageStatus.SOURCE_CONFLICT,
    }
    if receipt.status in hard_unavailable:
        if series.observations or any(value is not None for value in observed_pair):
            raise ValueError("hard-unavailable coverage cannot carry usable observations")
        return
    if receipt.status is IntradayCoverageStatus.COMPLETE:
        if receipt.missing_intervals or not series.observations:
            raise ValueError("complete coverage requires observations and no missing intervals")
        if (
            receipt.observed_start != series.observations[0].interval_start_at
            or receipt.observed_end != series.observations[-1].interval_end_at
            or receipt.observed_start != receipt.request_start
            or receipt.observed_end != series.requested_through_at
            or receipt.request_end != series.requested_through_at
        ):
            raise ValueError("complete coverage bounds do not exactly match observations")
    elif receipt.status is IntradayCoverageStatus.PARTIAL_MISSING_INTERVALS:
        actual_gaps = _observation_request_gaps(series)
        if not series.observations or not actual_gaps:
            raise ValueError("partial coverage requires retained bars and explicit gaps")
        if receipt.missing_intervals != actual_gaps:
            raise ValueError("partial coverage gaps do not match retained bar body")
    elif receipt.status is IntradayCoverageStatus.KNOWN_HALT_GAPS:
        has_halt = any(
            item.status == OutcomeMarketStatusKind.HALTED.value
            for item in series.market_status_intervals
        )
        if not receipt.missing_intervals and not has_halt:
            raise ValueError("known-halt coverage requires bounded gap or halted interval proof")
        if receipt.missing_intervals and (
            receipt.missing_intervals != _observation_request_gaps(series)
        ):
            raise ValueError("known-halt gaps do not match retained bar body")


def _observation_request_gaps(
    series: OutcomeObservationSeries,
) -> tuple[tuple[datetime, datetime], ...]:
    cursor = series.coverage_receipt.request_start
    gaps: list[tuple[datetime, datetime]] = []
    for observation in series.observations:
        if observation.interval_start_at > cursor:
            gaps.append((cursor, observation.interval_start_at))
        cursor = max(cursor, observation.interval_end_at)
    if cursor < series.coverage_receipt.request_end:
        gaps.append((cursor, series.coverage_receipt.request_end))
    return tuple(gaps)


def _source_artifact_hashes(
    series: tuple[OutcomeObservationSeries, ...],
) -> set[str]:
    values: set[str] = set()
    for item in series:
        sources = [item.coverage_receipt.source_metadata]
        sources.extend(observation.bar.source_metadata for observation in item.observations)
        sources.extend(status.source_metadata for status in item.market_status_intervals)
        sources.extend(action.source_metadata for action in item.corporate_actions)
        for source in sources:
            values.add(source.raw_artifact_hash_sha256)
            values.add(source.normalized_artifact_hash_sha256)
    return values


def _validate_embedded_hashes(
    hashes: tuple[str, ...],
    values: tuple[OpportunityContract, ...]
    | tuple[MarketStatusInterval, ...]
    | tuple[CorporateActionRecord, ...],
    label: str,
) -> None:
    if len(hashes) != len(values):
        raise ValueError(f"{label} hashes do not reconcile")
    for supplied, value in zip(hashes, values, strict=True):
        _require_hash(supplied, f"{label} hash")
        if supplied != _contract_hash(value):
            raise ValueError(f"{label} hash does not match embedded content")


def _validate_source_metadata(metadata: IntradaySourceMetadata) -> None:
    _require_schema(metadata.schema_version, "v2.intraday_source_metadata.v1")
    for value, name in (
        (metadata.provider, "source provider"),
        (metadata.feed, "source feed"),
        (metadata.entitlement, "source entitlement"),
        (metadata.exchange_session_id, "source exchange session"),
        (metadata.code_sha, "source code identity"),
        (metadata.retention_status, "source retention status"),
    ):
        _require_sanitized_text(value, name)
    for digest in (
        metadata.raw_artifact_hash_sha256,
        metadata.normalized_artifact_hash_sha256,
    ):
        _require_hash(digest, "source artifact hash")
    if not (
        metadata.request_start <= metadata.request_end
        and metadata.request_start <= metadata.fetched_at
    ):
        raise ValueError("source request/fetch chronology is invalid")
