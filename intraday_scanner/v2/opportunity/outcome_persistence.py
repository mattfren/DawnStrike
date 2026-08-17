"""Downstream-only contracts for immutable outcome storage and replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from intraday_scanner.storage.opportunity_store import OpportunityPersistenceReceipt
from intraday_scanner.v2.opportunity.models import stable_identity
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeContract,
    _identity_payload,
    _require_hash,
    _require_identity,
    _require_schema,
    _require_utc,
)
from intraday_scanner.v2.opportunity.outcome_replay import OutcomeLabelBatch
from intraday_scanner.v2.opportunity.pipeline import PipelineResult


class OutcomePersistenceKind(str, Enum):
    """Whether a receipt begins or advances one immutable correction chain."""

    INITIAL = "initial"
    CORRECTION = "correction"


class OutcomeArtifactFamily(str, Enum):
    """Canonical order for the two stored outcome artifact families."""

    OUTCOME_LABEL_BATCH = "outcome_label_batch"
    OUTCOME_RECORD = "outcome_record"


CANONICAL_OUTCOME_ARTIFACT_FAMILIES = tuple(OutcomeArtifactFamily)


@dataclass(frozen=True)
class OutcomeArtifactFamilyCount(OutcomeContract):
    """Exact persisted count for one outcome artifact family."""

    family: OutcomeArtifactFamily
    count: int
    schema_version: str = "v2.opportunity.outcome_artifact_family_count.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_schema(
            self.schema_version,
            "v2.opportunity.outcome_artifact_family_count.v1",
        )
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 0:
            raise ValueError("outcome artifact family count must be a nonnegative integer")


@dataclass(frozen=True)
class OpportunityOutcomePersistenceReceipt(OutcomeContract):
    """Content-bound append receipt for one outcome batch revision."""

    outcome_receipt_id: str
    receipt_kind: OutcomePersistenceKind
    batch_id: str
    batch_content_hash_sha256: str
    batch_schema_version: str
    run_id: str
    run_content_hash_sha256: str
    run_persistence_receipt_id: str
    run_persistence_receipt_content_hash_sha256: str
    source_dataset_id: str
    source_dataset_content_hash_sha256: str
    policy_id: str
    policy_content_hash_sha256: str
    decision_at: datetime
    batch_recorded_at: datetime
    persisted_at: datetime
    supersedes_outcome_receipt_id: str | None
    supersedes_outcome_receipt_content_hash_sha256: str | None
    family_counts: tuple[OutcomeArtifactFamilyCount, ...]
    record_count: int
    artifact_count: int
    artifact_inventory_hash_sha256: str
    database_schema_version: int = 28
    research_only: bool = True
    schema_version: str = "v2.opportunity.outcome_persistence_receipt.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_schema(
            self.schema_version,
            "v2.opportunity.outcome_persistence_receipt.v1",
        )
        for value, field_name in (
            (self.outcome_receipt_id, "outcome_receipt_id"),
            (self.batch_id, "batch_id"),
            (self.run_id, "run_id"),
            (self.run_persistence_receipt_id, "run_persistence_receipt_id"),
            (self.source_dataset_id, "source_dataset_id"),
            (self.policy_id, "policy_id"),
        ):
            _require_identity(value, field_name)
        for value, field_name in (
            (self.batch_content_hash_sha256, "batch_content_hash_sha256"),
            (self.run_content_hash_sha256, "run_content_hash_sha256"),
            (
                self.run_persistence_receipt_content_hash_sha256,
                "run_persistence_receipt_content_hash_sha256",
            ),
            (
                self.source_dataset_content_hash_sha256,
                "source_dataset_content_hash_sha256",
            ),
            (self.policy_content_hash_sha256, "policy_content_hash_sha256"),
            (
                self.artifact_inventory_hash_sha256,
                "artifact_inventory_hash_sha256",
            ),
        ):
            _require_hash(value, field_name)
        if self.batch_schema_version != "v2.opportunity.outcome_label_batch.v2":
            raise ValueError("unsupported persisted outcome label batch schema")
        _require_utc(self.batch_recorded_at, "batch_recorded_at")
        _require_utc(self.persisted_at, "persisted_at")
        if self.decision_at.tzinfo is None or self.decision_at.utcoffset() is None:
            raise ValueError("decision_at must be timezone-aware")
        if self.batch_recorded_at < self.decision_at:
            raise ValueError("batch_recorded_at cannot precede decision_at")
        if self.persisted_at < self.batch_recorded_at:
            raise ValueError("persisted_at cannot precede batch_recorded_at")
        predecessor_paired = (self.supersedes_outcome_receipt_id is None) is (
            self.supersedes_outcome_receipt_content_hash_sha256 is None
        )
        if not predecessor_paired:
            raise ValueError("superseded outcome receipt identity and hash must be paired")
        if self.receipt_kind is OutcomePersistenceKind.INITIAL:
            if self.supersedes_outcome_receipt_id is not None:
                raise ValueError("initial outcome receipt cannot supersede another receipt")
        elif self.supersedes_outcome_receipt_id is None:
            raise ValueError("correction outcome receipt requires a predecessor")
        if self.supersedes_outcome_receipt_id is not None:
            predecessor_hash = self.supersedes_outcome_receipt_content_hash_sha256
            if predecessor_hash is None:
                raise ValueError("superseded outcome receipt hash is required")
            _require_identity(
                self.supersedes_outcome_receipt_id,
                "supersedes_outcome_receipt_id",
            )
            _require_hash(
                predecessor_hash,
                "supersedes_outcome_receipt_content_hash_sha256",
            )
        if tuple(item.family for item in self.family_counts) != (
            CANONICAL_OUTCOME_ARTIFACT_FAMILIES
        ):
            raise ValueError("outcome artifact family counts must use canonical order")
        if (
            isinstance(self.record_count, bool)
            or not isinstance(self.record_count, int)
            or self.record_count < 0
        ):
            raise ValueError("record_count must be a nonnegative integer")
        if (
            isinstance(self.artifact_count, bool)
            or not isinstance(self.artifact_count, int)
            or self.artifact_count < 0
        ):
            raise ValueError("artifact_count must be a nonnegative integer")
        if self.artifact_count != self.record_count + 1:
            raise ValueError("artifact_count must equal record_count plus the batch artifact")
        expected_counts = (
            (OutcomeArtifactFamily.OUTCOME_LABEL_BATCH, 1),
            (OutcomeArtifactFamily.OUTCOME_RECORD, self.record_count),
        )
        if tuple((item.family, item.count) for item in self.family_counts) != expected_counts:
            raise ValueError("outcome artifact family allocation does not match batch records")
        if self.database_schema_version != 28:
            raise ValueError("outcome persistence receipt requires database schema 28")
        if not self.research_only:
            raise ValueError("outcome persistence receipt must remain research_only")
        expected_id = stable_identity(
            "opportunity-outcome-persistence-receipt",
            _identity_payload(self, "outcome_receipt_id"),
        )
        if self.outcome_receipt_id != expected_id:
            raise ValueError("outcome persistence receipt identity does not match content")


def _validate_replay_common(
    *,
    pipeline_result: PipelineResult,
    run_persistence_receipt: OpportunityPersistenceReceipt,
    outcome_persistence_receipt: OpportunityOutcomePersistenceReceipt,
    outcome_batch: OutcomeLabelBatch,
    chain_receipts: tuple[OpportunityOutcomePersistenceReceipt, ...],
) -> None:
    if outcome_batch.pipeline_result != pipeline_result:
        raise ValueError("stored replay pipeline result does not match outcome batch")
    if outcome_batch.persistence_receipt != run_persistence_receipt:
        raise ValueError("stored replay run receipt does not match outcome batch")
    source_dataset = outcome_batch.source_dataset
    policy = outcome_batch.policy
    receipt_bindings = (
        outcome_persistence_receipt.batch_id == outcome_batch.batch_id,
        outcome_persistence_receipt.batch_content_hash_sha256
        == outcome_batch.content_hash(),
        outcome_persistence_receipt.batch_schema_version == outcome_batch.schema_version,
        outcome_persistence_receipt.run_id == pipeline_result.run_id,
        outcome_persistence_receipt.run_content_hash_sha256
        == pipeline_result.content_hash(),
        outcome_persistence_receipt.run_persistence_receipt_id
        == run_persistence_receipt.receipt_id,
        outcome_persistence_receipt.run_persistence_receipt_content_hash_sha256
        == run_persistence_receipt.content_hash(),
        outcome_persistence_receipt.source_dataset_id == source_dataset.source_dataset_id,
        outcome_persistence_receipt.source_dataset_content_hash_sha256
        == source_dataset.content_hash(),
        outcome_persistence_receipt.policy_id == policy.policy_id,
        outcome_persistence_receipt.policy_content_hash_sha256 == policy.content_hash(),
        outcome_persistence_receipt.decision_at == pipeline_result.decision_at,
        outcome_persistence_receipt.batch_recorded_at == outcome_batch.recorded_at,
        outcome_persistence_receipt.record_count == len(outcome_batch.outcomes),
        outcome_persistence_receipt.research_only
        is outcome_batch.research_only
        is pipeline_result.research_only,
    )
    if not all(receipt_bindings):
        raise ValueError("outcome persistence receipt does not match embedded replay objects")
    if not chain_receipts or chain_receipts[-1] != outcome_persistence_receipt:
        raise ValueError("stored replay chain must end at its requested receipt")
    receipt_ids = tuple(item.outcome_receipt_id for item in chain_receipts)
    if len(receipt_ids) != len(set(receipt_ids)):
        raise ValueError("stored replay chain contains duplicate receipt identities")
    for index, receipt in enumerate(chain_receipts):
        if receipt.run_id != pipeline_result.run_id:
            raise ValueError("stored replay chain contains a cross-run receipt")
        if index == 0:
            if receipt.receipt_kind is not OutcomePersistenceKind.INITIAL:
                raise ValueError("stored replay chain must begin with an initial receipt")
            continue
        predecessor = chain_receipts[index - 1]
        if (
            receipt.receipt_kind is not OutcomePersistenceKind.CORRECTION
            or receipt.supersedes_outcome_receipt_id != predecessor.outcome_receipt_id
            or receipt.supersedes_outcome_receipt_content_hash_sha256
            != predecessor.content_hash()
            or receipt.persisted_at <= predecessor.persisted_at
        ):
            raise ValueError("stored replay receipt chain is not exact and chronological")


@dataclass(frozen=True)
class HistoricalOutcomeReplay(OutcomeContract):
    """Pure reconstruction of one historical receipt and its verified chain prefix."""

    replay_id: str
    pipeline_result: PipelineResult
    run_persistence_receipt: OpportunityPersistenceReceipt
    outcome_persistence_receipt: OpportunityOutcomePersistenceReceipt
    outcome_batch: OutcomeLabelBatch
    chain_prefix: tuple[OpportunityOutcomePersistenceReceipt, ...]
    research_only: bool = True
    schema_version: str = "v2.opportunity.historical_outcome_replay.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_schema(
            self.schema_version,
            "v2.opportunity.historical_outcome_replay.v1",
        )
        _require_identity(self.replay_id, "replay_id")
        _validate_replay_common(
            pipeline_result=self.pipeline_result,
            run_persistence_receipt=self.run_persistence_receipt,
            outcome_persistence_receipt=self.outcome_persistence_receipt,
            outcome_batch=self.outcome_batch,
            chain_receipts=self.chain_prefix,
        )
        if not self.research_only:
            raise ValueError("stored outcome replay must remain research_only")
        expected_id = stable_identity(
            "historical-opportunity-outcome-replay",
            _identity_payload(self, "replay_id"),
        )
        if self.replay_id != expected_id:
            raise ValueError("historical outcome replay identity does not match content")


@dataclass(frozen=True)
class CurrentOutcomeReplay(OutcomeContract):
    """Pure reconstruction of the unique stored chain head and full chain."""

    replay_id: str
    pipeline_result: PipelineResult
    run_persistence_receipt: OpportunityPersistenceReceipt
    outcome_persistence_receipt: OpportunityOutcomePersistenceReceipt
    outcome_batch: OutcomeLabelBatch
    full_chain: tuple[OpportunityOutcomePersistenceReceipt, ...]
    research_only: bool = True
    schema_version: str = "v2.opportunity.current_outcome_replay.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_schema(self.schema_version, "v2.opportunity.current_outcome_replay.v1")
        _require_identity(self.replay_id, "replay_id")
        _validate_replay_common(
            pipeline_result=self.pipeline_result,
            run_persistence_receipt=self.run_persistence_receipt,
            outcome_persistence_receipt=self.outcome_persistence_receipt,
            outcome_batch=self.outcome_batch,
            chain_receipts=self.full_chain,
        )
        if not self.research_only:
            raise ValueError("stored outcome replay must remain research_only")
        expected_id = stable_identity(
            "current-opportunity-outcome-replay",
            _identity_payload(self, "replay_id"),
        )
        if self.replay_id != expected_id:
            raise ValueError("current outcome replay identity does not match content")
