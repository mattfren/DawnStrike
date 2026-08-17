"""Authoritative retrospective source contracts for missed-opportunity research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from intraday_scanner.v2.data_truth import MarketQuote
from intraday_scanner.v2.opportunity.capabilities import (
    CapabilityState,
    ProviderCapabilityReceipt,
)
from intraday_scanner.v2.opportunity.miss_contracts import (
    MissContract,
    QualificationExecutionStatus,
    QualificationMemberStatus,
    QualificationSourceAuthorityClaim,
    QualificationSourceScopeStatus,
    identity_payload,
    require_hash,
    require_identity,
    require_sanitized,
    require_schema,
    require_unique,
    require_utc,
)
from intraday_scanner.v2.opportunity.miss_source_validation import (
    regime_measurement_hash as _regime_measurement_hash,
)
from intraday_scanner.v2.opportunity.miss_source_validation import (
    regime_observed_at as _regime_observed_at,
)
from intraday_scanner.v2.opportunity.miss_source_validation import (
    require_paired_inventory as _require_paired_inventory,
)
from intraday_scanner.v2.opportunity.miss_source_validation import (
    require_sanitized_values as _require_sanitized_values,
)
from intraday_scanner.v2.opportunity.miss_source_validation import (
    require_sorted_symbols as _require_sorted_symbols,
)
from intraday_scanner.v2.opportunity.miss_source_validation import (
    require_symbol as _require_symbol,
)
from intraday_scanner.v2.opportunity.miss_source_validation import (
    validate_quote as _validate_quote,
)
from intraday_scanner.v2.opportunity.miss_source_validation import (
    validate_regime_snapshots as _validate_regime_snapshots,
)
from intraday_scanner.v2.opportunity.models import (
    EvidenceKind,
    FeatureSnapshot,
    MarketRegime,
    SecurityRegime,
    StrategyDirection,
    stable_identity,
)
from intraday_scanner.v2.opportunity.outcome_sources import (
    OutcomeBarEvidence,
    OutcomeObservationDataset,
)
from intraday_scanner.v2.opportunity.regimes import (
    classify_market_regime,
    classify_security_regime,
)
from intraday_scanner.v2.opportunity.risk import QuoteEvidenceScope
from intraday_scanner.v2.opportunity.universe import (
    SafetyStatus,
    SecurityType,
)


@dataclass(frozen=True)
class QualificationSourceArtifact(MissContract):
    artifact_id: str
    content_hash_sha256: str
    source_identity: str
    fetched_at: datetime
    schema_version: str = "v2.opportunity.qualification_source_artifact.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.qualification_source_artifact.v1",
        )
        require_identity(self.artifact_id, "artifact_id")
        require_hash(self.content_hash_sha256, "content_hash_sha256")
        require_sanitized(self.source_identity, "source_identity")
        require_utc(self.fetched_at, "fetched_at")


@dataclass(frozen=True)
class QualificationSourceAuthority(MissContract):
    authority_id: str
    authority_identity: str
    authority_version: str
    capability_state: CapabilityState
    claim: QualificationSourceAuthorityClaim
    membership_as_of_at: datetime
    cohort_symbols: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    artifact_hashes: tuple[str, ...]
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.qualification_source_authority.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.qualification_source_authority.v1",
        )
        require_identity(self.authority_id, "authority_id")
        require_sanitized(self.authority_identity, "authority_identity")
        require_sanitized(self.authority_version, "authority_version")
        require_utc(self.membership_as_of_at, "membership_as_of_at")
        _require_sorted_symbols(self.cohort_symbols, "authority cohort symbol")
        _require_paired_inventory(self.artifact_ids, self.artifact_hashes, "authority")
        if self.capability_state is CapabilityState.AVAILABLE and not self.artifact_ids:
            raise ValueError("available source authority requires bound source artifacts")
        _require_sanitized_values(self.limitations, "authority limitation")
        if self.capability_state is CapabilityState.AVAILABLE:
            if self.claim is QualificationSourceAuthorityClaim.NO_AUTHORITY:
                raise ValueError("available source authority must declare a bounded claim")
        elif self.claim is not QualificationSourceAuthorityClaim.NO_AUTHORITY:
            raise ValueError("non-available source authority cannot claim cohort coverage")
        expected = stable_identity(
            "qualification-source-authority",
            identity_payload(self, "authority_id"),
        )
        if self.authority_id != expected:
            raise ValueError("qualification source authority identity does not match content")


@dataclass(frozen=True)
class QualificationSourceScopeReceipt(MissContract):
    scope_receipt_id: str
    exchange_session_id: str
    session_open_at: datetime
    session_close_at: datetime
    membership_as_of_at: datetime
    query_started_at: datetime
    query_ended_at: datetime
    observed_through_at: datetime
    fetched_at: datetime
    authority: QualificationSourceAuthority
    requested_symbols: tuple[str, ...]
    provider_receipts: tuple[ProviderCapabilityReceipt, ...]
    source_artifacts: tuple[QualificationSourceArtifact, ...]
    scope_status: QualificationSourceScopeStatus
    limitations: tuple[str, ...] = ()
    research_only: bool = True
    schema_version: str = "v2.opportunity.qualification_source_scope_receipt.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.qualification_source_scope_receipt.v1",
        )
        require_identity(self.scope_receipt_id, "scope_receipt_id")
        require_sanitized(self.exchange_session_id, "exchange_session_id")
        for value, name in (
            (self.session_open_at, "session_open_at"),
            (self.session_close_at, "session_close_at"),
            (self.membership_as_of_at, "membership_as_of_at"),
            (self.query_started_at, "query_started_at"),
            (self.query_ended_at, "query_ended_at"),
            (self.observed_through_at, "observed_through_at"),
            (self.fetched_at, "fetched_at"),
        ):
            require_utc(value, name)
        if self.session_open_at >= self.session_close_at:
            raise ValueError("qualification source session is reversed or empty")
        if self.membership_as_of_at > self.session_open_at:
            raise ValueError("qualification membership must be effective by session open")
        if self.authority.membership_as_of_at != self.membership_as_of_at:
            raise ValueError("scope and authority membership as-of times differ")
        if not self.query_started_at <= self.query_ended_at <= self.fetched_at:
            raise ValueError("qualification source query chronology is inconsistent")
        if self.observed_through_at > self.fetched_at:
            raise ValueError("qualification source cannot observe beyond fetch time")
        _require_sorted_symbols(self.requested_symbols, "requested symbol")
        receipt_ids = tuple(item.capability_receipt_id for item in self.provider_receipts)
        if receipt_ids != tuple(sorted(receipt_ids)):
            raise ValueError("provider receipts must use canonical identity order")
        require_unique(receipt_ids, "provider receipt")
        artifact_ids = tuple(item.artifact_id for item in self.source_artifacts)
        if artifact_ids != tuple(sorted(artifact_ids)):
            raise ValueError("source artifacts must use canonical identity order")
        require_unique(artifact_ids, "source artifact")
        if self.authority.artifact_ids != artifact_ids:
            raise ValueError("scope artifacts do not match authority inventory")
        if self.authority.artifact_hashes != tuple(
            item.content_hash_sha256 for item in self.source_artifacts
        ):
            raise ValueError("scope artifact hashes do not match authority inventory")
        if any(item.fetched_at > self.fetched_at for item in self.source_artifacts):
            raise ValueError("scope artifact cannot be fetched after receipt")
        if any(item.observed_at > self.fetched_at for item in self.provider_receipts):
            raise ValueError("provider capability cannot postdate source receipt")
        _validate_scope_provider_lineage(self)
        expected_status = _derive_scope_status(self)
        if self.scope_status is not expected_status:
            raise ValueError("qualification source scope status does not match authority")
        _require_sanitized_values(self.limitations, "scope limitation")
        if not self.research_only:
            raise ValueError("qualification source scope must remain research-only")
        expected = stable_identity(
            "qualification-source-scope",
            identity_payload(self, "scope_receipt_id"),
        )
        if self.scope_receipt_id != expected:
            raise ValueError("qualification source scope identity does not match content")


@dataclass(frozen=True)
class QualificationMemberEvidence(MissContract):
    member_id: str
    symbol: str
    security_type: SecurityType
    status: QualificationMemberStatus
    exchange_session_id: str
    membership_as_of_at: datetime
    effective_from_at: datetime | None
    effective_through_at: datetime | None
    observed_at: datetime
    fetched_at: datetime
    source_identity: str
    source_artifact_ids: tuple[str, ...]
    source_artifact_hashes: tuple[str, ...]
    halt_status: SafetyStatus
    corporate_action_status: SafetyStatus
    reasons: tuple[str, ...]
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.qualification_member_evidence.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.qualification_member_evidence.v1",
        )
        require_identity(self.member_id, "member_id")
        _require_symbol(self.symbol)
        require_sanitized(self.exchange_session_id, "exchange_session_id")
        require_sanitized(self.source_identity, "source_identity")
        for value, name in (
            (self.membership_as_of_at, "membership_as_of_at"),
            (self.observed_at, "observed_at"),
            (self.fetched_at, "fetched_at"),
        ):
            require_utc(value, name)
        for optional_value, name in (
            (self.effective_from_at, "effective_from_at"),
            (self.effective_through_at, "effective_through_at"),
        ):
            if optional_value is not None:
                require_utc(optional_value, name)
        if self.observed_at > self.fetched_at:
            raise ValueError("member observation cannot postdate fetch")
        if self.effective_from_at is not None and self.effective_from_at > self.membership_as_of_at:
            raise ValueError("member was not effective at membership as-of time")
        if (
            self.effective_through_at is not None
            and self.effective_through_at <= self.membership_as_of_at
        ):
            raise ValueError("expired member cannot be eligible at membership as-of time")
        _require_paired_inventory(
            self.source_artifact_ids,
            self.source_artifact_hashes,
            "member",
        )
        if not self.source_artifact_ids:
            raise ValueError("qualification member requires source artifact lineage")
        _require_sanitized_values(self.reasons, "member reason")
        _require_sanitized_values(self.limitations, "member limitation")
        if self.status is not QualificationMemberStatus.ELIGIBLE and not self.reasons:
            raise ValueError("non-eligible qualification member requires a reason")
        expected = stable_identity(
            "qualification-member",
            identity_payload(self, "member_id"),
        )
        if self.member_id != expected:
            raise ValueError("qualification member identity does not match content")


@dataclass(frozen=True)
class QualificationExecutionEvidence(MissContract):
    execution_evidence_id: str
    symbol: str
    direction: StrategyDirection
    exchange_session_id: str
    reference_observation: OutcomeBarEvidence
    quote: MarketQuote | None
    quote_scope: QuoteEvidenceScope
    spread_bps: Decimal | None
    entry_slippage_bps: Decimal | None
    exit_slippage_bps: Decimal | None
    round_trip_fee_per_share: Decimal | None
    executable_quantity_shares: int | None
    status: QualificationExecutionStatus
    evidence_kind: EvidenceKind
    observed_at: datetime | None
    source_identity: str
    source_artifact_id: str | None
    source_artifact_hash_sha256: str | None
    method: str
    reason: str | None = None
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.qualification_execution_evidence.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.qualification_execution_evidence.v1",
        )
        require_identity(self.execution_evidence_id, "execution_evidence_id")
        _require_symbol(self.symbol)
        if self.direction not in {StrategyDirection.LONG, StrategyDirection.SHORT}:
            raise ValueError("qualification execution evidence requires an exact direction")
        require_sanitized(self.exchange_session_id, "exchange_session_id")
        require_sanitized(self.source_identity, "source_identity")
        require_sanitized(self.method, "method")
        if self.reference_observation.bar.symbol != self.symbol:
            raise ValueError("execution evidence reference symbol is inconsistent")
        if self.reference_observation.bar.exchange_session_id != self.exchange_session_id:
            raise ValueError("execution evidence reference session is inconsistent")
        paired_source = (self.source_artifact_id is None) is (
            self.source_artifact_hash_sha256 is None
        )
        if not paired_source:
            raise ValueError("execution source artifact identity and hash must be paired")
        if self.source_artifact_id is not None:
            require_identity(self.source_artifact_id, "source_artifact_id")
            require_hash(self.source_artifact_hash_sha256 or "", "source_artifact_hash_sha256")
        values = (
            self.spread_bps,
            self.entry_slippage_bps,
            self.exit_slippage_bps,
            self.round_trip_fee_per_share,
        )
        if self.status is QualificationExecutionStatus.UNAVAILABLE:
            if (
                any(value is not None for value in values)
                or self.executable_quantity_shares is not None
            ):
                raise ValueError("unavailable execution evidence cannot carry numeric values")
            if (
                self.quote is not None
                or self.observed_at is not None
                or self.source_artifact_id is not None
            ):
                raise ValueError("unavailable execution evidence cannot carry source observations")
            if self.quote_scope is not QuoteEvidenceScope.UNAVAILABLE or self.reason is None:
                raise ValueError("unavailable execution evidence requires unavailable scope/reason")
        else:
            if any(
                type(value) is not Decimal or not value.is_finite() or value < 0
                for value in values
            ):
                raise ValueError("execution costs must be finite nonnegative Decimals")
            if (
                isinstance(self.executable_quantity_shares, bool)
                or not isinstance(self.executable_quantity_shares, int)
                or self.executable_quantity_shares <= 0
            ):
                raise ValueError("execution quantity must be a positive integer")
            if self.quote is None or self.observed_at is None or self.source_artifact_id is None:
                raise ValueError("execution evidence requires exact quote lineage")
            _validate_quote(self)
            require_utc(self.observed_at, "execution observed_at")
            if self.reason is not None:
                raise ValueError("available/provisional execution evidence cannot carry a reason")
            if self.status is QualificationExecutionStatus.AVAILABLE:
                if self.quote_scope is not QuoteEvidenceScope.NBBO:
                    raise ValueError("available execution evidence requires NBBO scope")
                if self.evidence_kind is not EvidenceKind.EMPIRICAL:
                    raise ValueError("available execution evidence must be empirical")
            elif self.evidence_kind is not EvidenceKind.HEURISTIC:
                raise ValueError("provisional execution evidence must be heuristic")
        if self.reason is not None:
            require_sanitized(self.reason, "execution reason")
        _require_sanitized_values(self.limitations, "execution limitation")
        expected = stable_identity(
            "qualification-execution",
            identity_payload(self, "execution_evidence_id"),
        )
        if self.execution_evidence_id != expected:
            raise ValueError("qualification execution identity does not match content")


@dataclass(frozen=True)
class RetrospectiveRegimeEvidence(MissContract):
    regime_evidence_id: str
    run_id: str
    run_content_hash: str
    symbol: str
    benchmark_symbol: str
    decision_at: datetime
    benchmark_snapshot: FeatureSnapshot
    security_snapshot: FeatureSnapshot
    market_regime: MarketRegime
    security_regime: SecurityRegime
    observed_at: datetime
    fetched_at: datetime
    source_artifact: QualificationSourceArtifact
    method: str
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.retrospective_regime_evidence.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.retrospective_regime_evidence.v1",
        )
        for value, name in (
            (self.regime_evidence_id, "regime_evidence_id"),
            (self.run_id, "run_id"),
            (self.symbol, "symbol"),
            (self.benchmark_symbol, "benchmark_symbol"),
        ):
            require_identity(value, name)
        require_hash(self.run_content_hash, "run_content_hash")
        require_sanitized(self.method, "regime method")
        for timestamp, name in (
            (self.decision_at, "decision_at"),
            (self.observed_at, "observed_at"),
            (self.fetched_at, "fetched_at"),
        ):
            require_utc(timestamp, name)
        if self.observed_at > self.fetched_at:
            raise ValueError("regime observation cannot postdate fetch")
        _validate_regime_snapshots(self)
        if self.observed_at != _regime_observed_at(self):
            raise ValueError("regime observed_at does not match embedded measurements")
        if self.fetched_at != self.source_artifact.fetched_at:
            raise ValueError("regime fetch time does not match source artifact")
        if self.source_artifact.content_hash_sha256 != _regime_measurement_hash(self):
            raise ValueError("regime source artifact does not bind embedded measurements")
        if (
            self.market_regime != classify_market_regime(self.benchmark_snapshot)
            or self.security_regime != classify_security_regime(self.security_snapshot)
        ):
            raise ValueError("retrospective regime evidence does not recompute exactly")
        _require_sanitized_values(self.limitations, "regime limitation")
        expected = stable_identity(
            "retrospective-regime",
            identity_payload(self, "regime_evidence_id"),
        )
        if self.regime_evidence_id != expected:
            raise ValueError("retrospective regime evidence identity does not match content")


@dataclass(frozen=True)
class HindsightQualificationSource(MissContract):
    source_id: str
    scope_receipt: QualificationSourceScopeReceipt
    members: tuple[QualificationMemberEvidence, ...]
    observation_dataset: OutcomeObservationDataset
    missing_series_symbols: tuple[str, ...]
    execution_evidence: tuple[QualificationExecutionEvidence, ...]
    retrospective_regime_evidence: tuple[RetrospectiveRegimeEvidence, ...]
    frozen_at: datetime
    recorded_at: datetime
    source_artifact_hashes: tuple[str, ...]
    limitations: tuple[str, ...] = ()
    research_only: bool = True
    schema_version: str = "v2.opportunity.hindsight_qualification_source.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.hindsight_qualification_source.v1",
        )
        require_identity(self.source_id, "source_id")
        require_utc(self.frozen_at, "frozen_at")
        require_utc(self.recorded_at, "recorded_at")
        if self.recorded_at < self.frozen_at:
            raise ValueError("qualification source cannot be recorded before freeze")
        if self.observation_dataset.frozen_at > self.frozen_at:
            raise ValueError("qualification observation dataset postdates source freeze")
        if self.scope_receipt.fetched_at > self.frozen_at:
            raise ValueError("qualification scope receipt postdates source freeze")
        expected_cut = self.scope_receipt.session_open_at - timedelta(microseconds=1)
        if self.observation_dataset.decision_at != expected_cut:
            raise ValueError("qualification dataset must use the canonical pre-session cut")
        member_symbols = tuple(item.symbol for item in self.members)
        if member_symbols != tuple(sorted(member_symbols)):
            raise ValueError("qualification members must use canonical symbol order")
        require_unique(member_symbols, "qualification member symbol")
        if member_symbols != self.scope_receipt.requested_symbols:
            raise ValueError("qualification members do not reconcile source scope")
        if any(
            item.exchange_session_id != self.scope_receipt.exchange_session_id
            or item.membership_as_of_at != self.scope_receipt.membership_as_of_at
            or item.membership_as_of_at > self.scope_receipt.session_open_at
            or item.fetched_at > self.frozen_at
            for item in self.members
        ):
            raise ValueError("qualification member source/session chronology is inconsistent")
        scope_artifacts = {
            item.artifact_id: item.content_hash_sha256
            for item in self.scope_receipt.source_artifacts
        }
        for member in self.members:
            for artifact_id, artifact_hash in zip(
                member.source_artifact_ids,
                member.source_artifact_hashes,
                strict=True,
            ):
                if scope_artifacts.get(artifact_id) != artifact_hash:
                    raise ValueError("qualification member artifact is outside source scope")
        series_symbols = tuple(item.symbol for item in self.observation_dataset.series)
        _require_sorted_symbols(series_symbols, "qualification series symbol")
        _require_sorted_symbols(self.missing_series_symbols, "missing series symbol")
        eligible_symbols = tuple(
            item.symbol
            for item in self.members
            if item.status is QualificationMemberStatus.ELIGIBLE
        )
        if tuple(sorted((*series_symbols, *self.missing_series_symbols))) != eligible_symbols:
            raise ValueError("qualification series do not reconcile eligible source members")
        if set(series_symbols) & set(self.missing_series_symbols):
            raise ValueError("qualification series cannot also be declared missing")
        for series in self.observation_dataset.series:
            if (
                series.exchange_session_id != self.scope_receipt.exchange_session_id
                or series.decision_at != expected_cut
                or series.first_expected_interval_start_at
                != self.scope_receipt.session_open_at
            ):
                raise ValueError("qualification series session/anchor is inconsistent")
        if self.scope_receipt.scope_status in {
            QualificationSourceScopeStatus.COMPLETE_MARKET,
            QualificationSourceScopeStatus.COMPLETE_BOUNDED,
        }:
            if self.missing_series_symbols:
                raise ValueError("complete qualification source cannot omit eligible series")
            if any(
                series.requested_through_at < self.scope_receipt.session_close_at
                for series in self.observation_dataset.series
            ):
                raise ValueError("complete qualification source must cover session close")
        execution_keys = tuple(
            (item.symbol, item.direction.value, item.reference_observation.observation_id)
            for item in self.execution_evidence
        )
        if execution_keys != tuple(sorted(execution_keys)):
            raise ValueError("execution evidence must use canonical key order")
        if len(execution_keys) != len(set(execution_keys)):
            raise ValueError("duplicate qualification execution evidence")
        if set(item.symbol for item in self.execution_evidence) - set(eligible_symbols):
            raise ValueError("execution evidence references a non-eligible source member")
        observation_by_id = {
            observation.observation_id: observation
            for series in self.observation_dataset.series
            for observation in series.observations
        }
        if len(observation_by_id) != sum(
            len(series.observations) for series in self.observation_dataset.series
        ):
            raise ValueError("qualification observation IDs must be globally unique")
        if any(
            observation_by_id.get(item.reference_observation.observation_id)
            != item.reference_observation
            for item in self.execution_evidence
        ):
            raise ValueError("execution evidence reference is not an exact source observation")
        if any(
            item.reference_observation.available_at > self.frozen_at
            or (
                item.quote is not None
                and item.quote.source_metadata.fetched_at > self.frozen_at
            )
            for item in self.execution_evidence
        ):
            raise ValueError("execution evidence postdates qualification source freeze")
        regime_keys = tuple(
            (item.run_id, item.symbol, item.regime_evidence_id)
            for item in self.retrospective_regime_evidence
        )
        if regime_keys != tuple(sorted(regime_keys)) or len(regime_keys) != len(set(regime_keys)):
            raise ValueError("retrospective regime evidence must use unique canonical order")
        if set(item.symbol for item in self.retrospective_regime_evidence) - set(
            eligible_symbols
        ):
            raise ValueError("retrospective regime evidence references a non-eligible member")
        if any(item.fetched_at > self.frozen_at for item in self.retrospective_regime_evidence):
            raise ValueError("retrospective regime evidence postdates source freeze")
        expected_hashes = tuple(
            sorted(
                {
                    *(item.content_hash_sha256 for item in self.scope_receipt.source_artifacts),
                    *(value for item in self.members for value in item.source_artifact_hashes),
                    *self.observation_dataset.source_artifact_hashes,
                    *(
                        item.source_artifact_hash_sha256
                        for item in self.execution_evidence
                        if item.source_artifact_hash_sha256 is not None
                    ),
                    *(
                        item.source_artifact.content_hash_sha256
                        for item in self.retrospective_regime_evidence
                    ),
                }
            )
        )
        if self.source_artifact_hashes != expected_hashes:
            raise ValueError("qualification source artifact inventory is inconsistent")
        _require_sanitized_values(self.limitations, "qualification source limitation")
        if not self.research_only:
            raise ValueError("hindsight qualification source must remain research-only")
        expected = stable_identity(
            "hindsight-qualification-source",
            identity_payload(self, "source_id"),
        )
        if self.source_id != expected:
            raise ValueError("hindsight qualification source identity does not match content")


def build_qualification_source_scope_receipt(
    *,
    exchange_session_id: str,
    session_open_at: datetime,
    session_close_at: datetime,
    membership_as_of_at: datetime,
    query_started_at: datetime,
    query_ended_at: datetime,
    observed_through_at: datetime,
    fetched_at: datetime,
    authority: QualificationSourceAuthority,
    requested_symbols: tuple[str, ...],
    provider_receipts: tuple[ProviderCapabilityReceipt, ...],
    source_artifacts: tuple[QualificationSourceArtifact, ...],
    limitations: tuple[str, ...] = (),
) -> QualificationSourceScopeReceipt:
    ordered_symbols = tuple(sorted(symbol.strip().upper() for symbol in requested_symbols))
    ordered_receipts = tuple(
        sorted(provider_receipts, key=lambda item: item.capability_receipt_id)
    )
    ordered_artifacts = tuple(sorted(source_artifacts, key=lambda item: item.artifact_id))
    common = {
        "exchange_session_id": exchange_session_id,
        "session_open_at": session_open_at,
        "session_close_at": session_close_at,
        "membership_as_of_at": membership_as_of_at,
        "query_started_at": query_started_at,
        "query_ended_at": query_ended_at,
        "observed_through_at": observed_through_at,
        "fetched_at": fetched_at,
        "authority": authority,
        "requested_symbols": ordered_symbols,
        "provider_receipts": ordered_receipts,
        "source_artifacts": ordered_artifacts,
        "limitations": limitations,
        "research_only": True,
        "schema_version": "v2.opportunity.qualification_source_scope_receipt.v1",
    }
    status = _derive_scope_status_values(
        authority=authority,
        requested_symbols=ordered_symbols,
        session_open_at=session_open_at,
        session_close_at=session_close_at,
        observed_through_at=observed_through_at,
        fetched_at=fetched_at,
        provider_receipts=ordered_receipts,
        source_artifacts=ordered_artifacts,
    )
    values = {**common, "scope_status": status}
    return QualificationSourceScopeReceipt(
        scope_receipt_id=stable_identity("qualification-source-scope", values),
        exchange_session_id=exchange_session_id,
        session_open_at=session_open_at,
        session_close_at=session_close_at,
        membership_as_of_at=membership_as_of_at,
        query_started_at=query_started_at,
        query_ended_at=query_ended_at,
        observed_through_at=observed_through_at,
        fetched_at=fetched_at,
        authority=authority,
        requested_symbols=ordered_symbols,
        provider_receipts=ordered_receipts,
        source_artifacts=ordered_artifacts,
        scope_status=status,
        limitations=limitations,
    )


def _derive_scope_status(
    receipt: QualificationSourceScopeReceipt,
) -> QualificationSourceScopeStatus:
    return _derive_scope_status_values(
        authority=receipt.authority,
        requested_symbols=receipt.requested_symbols,
        session_open_at=receipt.session_open_at,
        session_close_at=receipt.session_close_at,
        observed_through_at=receipt.observed_through_at,
        fetched_at=receipt.fetched_at,
        provider_receipts=receipt.provider_receipts,
        source_artifacts=receipt.source_artifacts,
    )


def _derive_scope_status_values(
    *,
    authority: QualificationSourceAuthority,
    requested_symbols: tuple[str, ...],
    session_open_at: datetime,
    session_close_at: datetime,
    observed_through_at: datetime,
    fetched_at: datetime,
    provider_receipts: tuple[ProviderCapabilityReceipt, ...],
    source_artifacts: tuple[QualificationSourceArtifact, ...],
) -> QualificationSourceScopeStatus:
    if authority.capability_state is not CapabilityState.AVAILABLE:
        return QualificationSourceScopeStatus.UNAVAILABLE
    if fetched_at < session_close_at:
        return QualificationSourceScopeStatus.PENDING
    if not provider_receipts or not source_artifacts:
        return QualificationSourceScopeStatus.PARTIAL
    if not _provider_lineage_supports_complete_scope(
        provider_receipts=provider_receipts,
        source_artifacts=source_artifacts,
        session_open_at=session_open_at,
        session_close_at=session_close_at,
    ):
        return QualificationSourceScopeStatus.PARTIAL
    if observed_through_at < session_close_at or requested_symbols != authority.cohort_symbols:
        return QualificationSourceScopeStatus.PARTIAL
    if authority.claim is QualificationSourceAuthorityClaim.MARKET_COMPLETE:
        return QualificationSourceScopeStatus.COMPLETE_MARKET
    if authority.claim is QualificationSourceAuthorityClaim.BOUNDED_COHORT:
        return QualificationSourceScopeStatus.COMPLETE_BOUNDED
    return QualificationSourceScopeStatus.UNAVAILABLE


def _validate_scope_provider_lineage(receipt: QualificationSourceScopeReceipt) -> None:
    if not receipt.provider_receipts:
        return
    receipt_sources = {item.source_identity for item in receipt.provider_receipts}
    if any(item.source_identity not in receipt_sources for item in receipt.source_artifacts):
        raise ValueError("scope source artifact is not supported by a provider receipt")


def _provider_lineage_supports_complete_scope(
    *,
    provider_receipts: tuple[ProviderCapabilityReceipt, ...],
    source_artifacts: tuple[QualificationSourceArtifact, ...],
    session_open_at: datetime,
    session_close_at: datetime,
) -> bool:
    receipt_sources = {item.source_identity for item in provider_receipts}
    if any(item.source_identity not in receipt_sources for item in source_artifacts):
        return False
    return all(
        item.bars is CapabilityState.AVAILABLE
        and item.historical_coverage is CapabilityState.AVAILABLE
        and item.coverage_start is not None
        and item.coverage_end is not None
        and item.coverage_start <= session_open_at
        and item.coverage_end >= session_close_at
        for item in provider_receipts
    )


__all__ = [
    "HindsightQualificationSource",
    "QualificationExecutionEvidence",
    "QualificationMemberEvidence",
    "QualificationSourceArtifact",
    "QualificationSourceAuthority",
    "QualificationSourceScopeReceipt",
    "RetrospectiveRegimeEvidence",
    "build_qualification_source_scope_receipt",
]
