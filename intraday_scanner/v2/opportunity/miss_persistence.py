"""Downstream-only immutable missed-opportunity persistence contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from intraday_scanner.v2.opportunity.miss_contracts import (
    MissContract,
    MissSessionDisposition,
    QualificationSourceAuthorityClaim,
    QualificationSourceScopeStatus,
    SessionRunInventoryStatus,
    identity_payload,
    require_hash,
    require_identity,
    require_sanitized,
    require_schema,
    require_unique,
    require_utc,
)
from intraday_scanner.v2.opportunity.miss_reconciliation import MissReconciliationBatch
from intraday_scanner.v2.opportunity.models import stable_identity
from intraday_scanner.v2.opportunity.outcome_persistence import CurrentOutcomeReplay


class MissPersistenceKind(str, Enum):
    """Whether a receipt starts or advances one analysis chain."""

    INITIAL = "initial"
    CORRECTION = "correction"


class MissArtifactFamily(str, Enum):
    """Canonical persisted missed-opportunity artifact families."""

    MISS_RECONCILIATION_BATCH = "miss_reconciliation_batch"
    MISSED_OPPORTUNITY_RECORD = "missed_opportunity_record"
    SESSION_RUN_BINDING = "session_run_binding"


CANONICAL_MISS_ARTIFACT_FAMILIES = tuple(MissArtifactFamily)


@dataclass(frozen=True)
class MissAnalysisHorizonBinding(MissContract):
    """Stable qualification-horizon identity used by an analysis chain."""

    horizon_id: str
    horizon_content_hash_sha256: str
    schema_version: str = "v2.opportunity.miss_analysis_horizon_binding.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.miss_analysis_horizon_binding.v1",
        )
        require_identity(self.horizon_id, "horizon_id")
        require_hash(self.horizon_content_hash_sha256, "horizon_content_hash_sha256")


@dataclass(frozen=True)
class MissArtifactFamilyCount(MissContract):
    """Exact count for one family in a persisted miss revision."""

    family: MissArtifactFamily
    count: int
    schema_version: str = "v2.opportunity.miss_artifact_family_count.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.miss_artifact_family_count.v1",
        )
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 0:
            raise ValueError("miss artifact family count must be a nonnegative integer")


@dataclass(frozen=True)
class OpportunityMissPersistenceReceipt(MissContract):
    """Content-bound append receipt for one miss reconciliation revision."""

    miss_receipt_id: str
    receipt_kind: MissPersistenceKind
    analysis_key: str
    batch_id: str
    batch_content_hash_sha256: str
    batch_schema_version: str
    exchange_session_id: str
    session_open_at: datetime
    session_close_at: datetime
    membership_as_of_at: datetime
    requested_query_start_at: datetime
    requested_through_at: datetime
    requested_symbols: tuple[str, ...]
    empty_eligible_universe: bool
    authority_claim: QualificationSourceAuthorityClaim
    source_scope_status: QualificationSourceScopeStatus
    qualification_source_identity: str
    qualification_source_version: str
    inventory_source_identity: str
    inventory_source_version: str
    inventory_source_method: str
    inventory_status: SessionRunInventoryStatus
    horizon_bindings: tuple[MissAnalysisHorizonBinding, ...]
    qualification_policy_id: str
    qualification_policy_content_hash_sha256: str
    qualification_batch_id: str
    qualification_batch_content_hash_sha256: str
    session_replay_id: str
    session_replay_content_hash_sha256: str
    session_disposition: MissSessionDisposition
    batch_recorded_at: datetime
    persisted_at: datetime
    supersedes_miss_receipt_id: str | None
    supersedes_miss_receipt_content_hash_sha256: str | None
    family_counts: tuple[MissArtifactFamilyCount, ...]
    record_count: int
    run_binding_count: int
    artifact_count: int
    artifact_inventory_hash_sha256: str
    database_schema_version: int = 29
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.miss_persistence_receipt.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.miss_persistence_receipt.v1",
        )
        for identity, identity_name in (
            (self.miss_receipt_id, "miss_receipt_id"),
            (self.analysis_key, "analysis_key"),
            (self.batch_id, "batch_id"),
            (self.qualification_policy_id, "qualification_policy_id"),
            (self.qualification_batch_id, "qualification_batch_id"),
            (self.session_replay_id, "session_replay_id"),
        ):
            require_identity(identity, identity_name)
        for digest, digest_name in (
            (self.batch_content_hash_sha256, "batch_content_hash_sha256"),
            (
                self.qualification_policy_content_hash_sha256,
                "qualification_policy_content_hash_sha256",
            ),
            (
                self.qualification_batch_content_hash_sha256,
                "qualification_batch_content_hash_sha256",
            ),
            (
                self.session_replay_content_hash_sha256,
                "session_replay_content_hash_sha256",
            ),
            (self.artifact_inventory_hash_sha256, "artifact_inventory_hash_sha256"),
        ):
            require_hash(digest, digest_name)
        if self.batch_schema_version != "v2.opportunity.miss_reconciliation_batch.v1":
            raise ValueError("unsupported persisted miss reconciliation batch schema")
        require_sanitized(self.exchange_session_id, "exchange_session_id")
        for source_text, source_name in (
            (self.qualification_source_identity, "qualification_source_identity"),
            (self.qualification_source_version, "qualification_source_version"),
            (self.inventory_source_identity, "inventory_source_identity"),
            (self.inventory_source_version, "inventory_source_version"),
            (self.inventory_source_method, "inventory_source_method"),
        ):
            require_sanitized(source_text, source_name)
        for timestamp, timestamp_name in (
            (self.session_open_at, "session_open_at"),
            (self.session_close_at, "session_close_at"),
            (self.membership_as_of_at, "membership_as_of_at"),
            (self.requested_query_start_at, "requested_query_start_at"),
            (self.requested_through_at, "requested_through_at"),
            (self.batch_recorded_at, "batch_recorded_at"),
            (self.persisted_at, "persisted_at"),
        ):
            require_utc(timestamp, timestamp_name)
        if self.session_open_at >= self.session_close_at:
            raise ValueError("miss persistence session is reversed or empty")
        if self.membership_as_of_at > self.session_open_at:
            raise ValueError("miss persistence membership postdates session open")
        if self.requested_query_start_at > self.session_open_at:
            raise ValueError("logical miss query start cannot postdate session open")
        if self.requested_through_at != self.session_close_at:
            raise ValueError("logical miss query must end at the session close")
        if self.persisted_at < self.batch_recorded_at:
            raise ValueError("miss persistence timestamp precedes batch recording")
        if self.requested_symbols != tuple(sorted(self.requested_symbols)):
            raise ValueError("requested miss symbols must use canonical order")
        require_unique(self.requested_symbols, "requested miss symbol")
        if any(not item or item != item.strip().upper() for item in self.requested_symbols):
            raise ValueError("requested miss symbols must be normalized")
        if not self.requested_symbols:
            if not self.empty_eligible_universe:
                raise ValueError("empty requested symbol scope requires explicit empty universe")
            if (
                self.authority_claim is not QualificationSourceAuthorityClaim.MARKET_COMPLETE
                or self.source_scope_status
                is not QualificationSourceScopeStatus.COMPLETE_MARKET
                or self.inventory_status is not SessionRunInventoryStatus.COMPLETE_AUTHORITATIVE
            ):
                raise ValueError("empty eligible universe requires authoritative complete scope")
        elif self.empty_eligible_universe:
            raise ValueError("nonempty requested symbol scope cannot be marked empty")
        if self.horizon_bindings != tuple(
            sorted(self.horizon_bindings, key=lambda item: item.horizon_id)
        ):
            raise ValueError("miss analysis horizons must use canonical identity order")
        require_unique(
            tuple(item.horizon_id for item in self.horizon_bindings),
            "miss analysis horizon",
        )
        predecessor_paired = (self.supersedes_miss_receipt_id is None) is (
            self.supersedes_miss_receipt_content_hash_sha256 is None
        )
        if not predecessor_paired:
            raise ValueError("superseded miss receipt identity and hash must be paired")
        if self.receipt_kind is MissPersistenceKind.INITIAL:
            if self.supersedes_miss_receipt_id is not None:
                raise ValueError("initial miss receipt cannot supersede another receipt")
        elif self.supersedes_miss_receipt_id is None:
            raise ValueError("correction miss receipt requires a predecessor")
        if self.supersedes_miss_receipt_id is not None:
            require_identity(self.supersedes_miss_receipt_id, "supersedes_miss_receipt_id")
            require_hash(
                self.supersedes_miss_receipt_content_hash_sha256 or "",
                "supersedes_miss_receipt_content_hash_sha256",
            )
        if tuple(item.family for item in self.family_counts) != (
            CANONICAL_MISS_ARTIFACT_FAMILIES
        ):
            raise ValueError("miss artifact family counts must use canonical order")
        for count, count_name in (
            (self.record_count, "record_count"),
            (self.run_binding_count, "run_binding_count"),
            (self.artifact_count, "artifact_count"),
        ):
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"{count_name} must be a nonnegative integer")
        expected_counts = (
            (MissArtifactFamily.MISS_RECONCILIATION_BATCH, 1),
            (MissArtifactFamily.MISSED_OPPORTUNITY_RECORD, self.record_count),
            (MissArtifactFamily.SESSION_RUN_BINDING, self.run_binding_count),
        )
        if tuple((item.family, item.count) for item in self.family_counts) != expected_counts:
            raise ValueError("miss artifact family allocation does not match receipt counts")
        if self.artifact_count != 1 + self.record_count + self.run_binding_count:
            raise ValueError("miss artifact count does not match family counts")
        if self.analysis_key != miss_analysis_key_from_receipt(self):
            raise ValueError("miss analysis key does not match stable logical scope")
        if self.database_schema_version != 29:
            raise ValueError("miss persistence receipt requires database schema 29")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("miss persistence receipt must remain research-only")
        expected_id = stable_identity(
            "opportunity-miss-persistence-receipt",
            identity_payload(self, "miss_receipt_id"),
        )
        if self.miss_receipt_id != expected_id:
            raise ValueError("miss persistence receipt identity does not match content")


def miss_analysis_key_from_receipt(receipt: OpportunityMissPersistenceReceipt) -> str:
    """Recompute the correction-stable logical analysis identity."""

    return stable_identity(
        "opportunity-miss-analysis",
        {
            "exchange_session_id": receipt.exchange_session_id,
            "session_open_at": receipt.session_open_at,
            "session_close_at": receipt.session_close_at,
            "membership_as_of_at": receipt.membership_as_of_at,
            "requested_query_start_at": receipt.requested_query_start_at,
            "requested_through_at": receipt.requested_through_at,
            "requested_symbols": receipt.requested_symbols,
            "empty_eligible_universe": receipt.empty_eligible_universe,
            "authority_claim": receipt.authority_claim,
            "qualification_source_identity": receipt.qualification_source_identity,
            "qualification_source_version": receipt.qualification_source_version,
            "inventory_source_identity": receipt.inventory_source_identity,
            "inventory_source_version": receipt.inventory_source_version,
            "inventory_source_method": receipt.inventory_source_method,
            "horizon_bindings": receipt.horizon_bindings,
            "qualification_policy_id": receipt.qualification_policy_id,
            "qualification_policy_content_hash_sha256": (
                receipt.qualification_policy_content_hash_sha256
            ),
            "schema_version": "v2.opportunity.miss_analysis.v1",
        },
    )


def validate_miss_receipt_batch(
    receipt: OpportunityMissPersistenceReceipt,
    batch: MissReconciliationBatch,
) -> None:
    """Recheck every receipt projection available from its exact batch body."""

    source = batch.qualification_batch.source.scope_receipt
    authority = source.authority
    inventory = batch.session_replay.run_inventory
    inventory_source = inventory.source_receipt
    horizons = tuple(
        sorted(
            (
                MissAnalysisHorizonBinding(
                    horizon_id=item.horizon_id,
                    horizon_content_hash_sha256=item.content_hash(),
                )
                for item in batch.qualification_batch.horizons
            ),
            key=lambda item: item.horizon_id,
        )
    )
    empty = not source.requested_symbols
    exact = (
        receipt.batch_id == batch.batch_id,
        receipt.batch_content_hash_sha256 == batch.content_hash(),
        receipt.batch_schema_version == batch.schema_version,
        receipt.exchange_session_id == source.exchange_session_id,
        receipt.session_open_at == source.session_open_at,
        receipt.session_close_at == source.session_close_at,
        receipt.membership_as_of_at == source.membership_as_of_at,
        receipt.requested_query_start_at == inventory_source.query_started_at,
        receipt.requested_through_at == source.session_close_at,
        receipt.requested_symbols == source.requested_symbols,
        receipt.empty_eligible_universe is empty,
        receipt.authority_claim is authority.claim,
        receipt.source_scope_status is source.scope_status,
        receipt.qualification_source_identity == authority.authority_identity,
        receipt.qualification_source_version == authority.authority_version,
        receipt.inventory_source_identity == inventory_source.source_identity,
        receipt.inventory_source_version == inventory_source.source_version,
        receipt.inventory_source_method == inventory_source.method,
        receipt.inventory_status is inventory.status,
        receipt.horizon_bindings == horizons,
        receipt.qualification_policy_id == batch.qualification_batch.policy.policy_id,
        receipt.qualification_policy_content_hash_sha256
        == batch.qualification_batch.policy.content_hash(),
        receipt.qualification_batch_id == batch.qualification_batch.batch_id,
        receipt.qualification_batch_content_hash_sha256
        == batch.qualification_batch.content_hash(),
        receipt.session_replay_id == batch.session_replay.session_replay_id,
        receipt.session_replay_content_hash_sha256 == batch.session_replay.content_hash(),
        receipt.session_disposition is batch.session_disposition,
        receipt.batch_recorded_at == batch.recorded_at,
        receipt.record_count == len(batch.records),
        receipt.run_binding_count == len(inventory.bindings),
        receipt.research_only is batch.research_only,
        receipt.promotion_eligible is batch.promotion_eligible,
    )
    if not all(exact):
        raise ValueError("miss persistence receipt does not match embedded batch")
    if any(
        item.decision_at.astimezone(receipt.requested_query_start_at.tzinfo)
        < receipt.requested_query_start_at
        for item in inventory.bindings
    ):
        raise ValueError("miss persistence logical query window omits a bound decision")


def _validate_replay(
    *,
    receipt: OpportunityMissPersistenceReceipt,
    batch: MissReconciliationBatch,
    chain_receipts: tuple[OpportunityMissPersistenceReceipt, ...],
    chain_batches: tuple[MissReconciliationBatch, ...],
    parent_outcome_replays: tuple[CurrentOutcomeReplay, ...],
) -> None:
    validate_miss_receipt_batch(receipt, batch)
    if len(chain_receipts) != len(chain_batches) or not chain_receipts:
        raise ValueError("miss replay chain receipt and batch bodies must be paired")
    if chain_receipts[-1] != receipt or chain_batches[-1] != batch:
        raise ValueError("miss replay chain must end at its requested revision")
    require_unique(tuple(item.miss_receipt_id for item in chain_receipts), "miss receipt")
    for index, (item_receipt, item_batch) in enumerate(
        zip(chain_receipts, chain_batches, strict=True)
    ):
        validate_miss_receipt_batch(item_receipt, item_batch)
        if item_receipt.analysis_key != receipt.analysis_key:
            raise ValueError("miss replay contains a cross-analysis receipt")
        if index == 0:
            if item_receipt.receipt_kind is not MissPersistenceKind.INITIAL:
                raise ValueError("miss replay chain must begin with an initial receipt")
            continue
        previous_receipt = chain_receipts[index - 1]
        previous_batch = chain_batches[index - 1]
        if (
            item_receipt.receipt_kind is not MissPersistenceKind.CORRECTION
            or item_receipt.supersedes_miss_receipt_id
            != previous_receipt.miss_receipt_id
            or item_receipt.supersedes_miss_receipt_content_hash_sha256
            != previous_receipt.content_hash()
            or item_receipt.persisted_at <= previous_receipt.persisted_at
            or item_batch.recorded_at <= previous_batch.recorded_at
        ):
            raise ValueError("miss replay receipt chain is not exact and chronological")
    if parent_outcome_replays != batch.session_replay.current_outcome_replays:
        raise ValueError("miss replay parent outcome bodies do not match its session replay")


@dataclass(frozen=True)
class HistoricalMissReplay(MissContract):
    """One stored miss revision with its exact historical chain prefix."""

    replay_id: str
    miss_persistence_receipt: OpportunityMissPersistenceReceipt
    miss_batch: MissReconciliationBatch
    chain_prefix_receipts: tuple[OpportunityMissPersistenceReceipt, ...]
    chain_prefix_batches: tuple[MissReconciliationBatch, ...]
    parent_outcome_replays: tuple[CurrentOutcomeReplay, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.historical_miss_replay.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(self.schema_version, "v2.opportunity.historical_miss_replay.v1")
        require_identity(self.replay_id, "replay_id")
        _validate_replay(
            receipt=self.miss_persistence_receipt,
            batch=self.miss_batch,
            chain_receipts=self.chain_prefix_receipts,
            chain_batches=self.chain_prefix_batches,
            parent_outcome_replays=self.parent_outcome_replays,
        )
        if not self.research_only or self.promotion_eligible:
            raise ValueError("historical miss replay must remain research-only")
        expected = stable_identity(
            "historical-opportunity-miss-replay",
            identity_payload(self, "replay_id"),
        )
        if self.replay_id != expected:
            raise ValueError("historical miss replay identity does not match content")


@dataclass(frozen=True)
class CurrentMissReplay(MissContract):
    """Unique current miss head with its full chain and current parents."""

    replay_id: str
    miss_persistence_receipt: OpportunityMissPersistenceReceipt
    miss_batch: MissReconciliationBatch
    full_chain_receipts: tuple[OpportunityMissPersistenceReceipt, ...]
    full_chain_batches: tuple[MissReconciliationBatch, ...]
    current_parent_outcome_replays: tuple[CurrentOutcomeReplay, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.current_miss_replay.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(self.schema_version, "v2.opportunity.current_miss_replay.v1")
        require_identity(self.replay_id, "replay_id")
        _validate_replay(
            receipt=self.miss_persistence_receipt,
            batch=self.miss_batch,
            chain_receipts=self.full_chain_receipts,
            chain_batches=self.full_chain_batches,
            parent_outcome_replays=self.current_parent_outcome_replays,
        )
        if not self.research_only or self.promotion_eligible:
            raise ValueError("current miss replay must remain research-only")
        expected = stable_identity(
            "current-opportunity-miss-replay",
            identity_payload(self, "replay_id"),
        )
        if self.replay_id != expected:
            raise ValueError("current miss replay identity does not match content")


__all__ = [
    "CANONICAL_MISS_ARTIFACT_FAMILIES",
    "CurrentMissReplay",
    "HistoricalMissReplay",
    "MissAnalysisHorizonBinding",
    "MissArtifactFamily",
    "MissArtifactFamilyCount",
    "MissPersistenceKind",
    "OpportunityMissPersistenceReceipt",
    "miss_analysis_key_from_receipt",
    "validate_miss_receipt_batch",
]
