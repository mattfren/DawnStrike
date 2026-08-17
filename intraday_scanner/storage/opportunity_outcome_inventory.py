"""Pure canonical inventory and receipt construction for stored outcomes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from intraday_scanner.v2.contracts import contract_to_json
from intraday_scanner.v2.opportunity.models import stable_identity
from intraday_scanner.v2.opportunity.outcome_persistence import (
    CANONICAL_OUTCOME_ARTIFACT_FAMILIES,
    OpportunityOutcomePersistenceReceipt,
    OutcomeArtifactFamily,
    OutcomeArtifactFamilyCount,
    OutcomePersistenceKind,
)
from intraday_scanner.v2.opportunity.outcome_records import OutcomeRecord
from intraday_scanner.v2.opportunity.outcome_replay import OutcomeLabelBatch


@dataclass(frozen=True)
class OutcomeInventoryItem:
    inventory_ordinal: int
    family: OutcomeArtifactFamily
    family_ordinal: int
    artifact_id: str
    run_id: str
    evaluation_id: str | None
    decision_id: str | None
    horizon_id: str | None
    schema_version: str
    content_hash_sha256: str
    supersedes_outcome_receipt_id: str | None
    supersedes_outcome_id: str | None
    supersedes_outcome_content_hash_sha256: str | None

    def identity_tuple(
        self,
    ) -> tuple[
        int,
        str,
        int,
        str,
        str,
        str | None,
        str | None,
        str | None,
        str,
        str,
        str | None,
        str | None,
        str | None,
    ]:
        return (
            self.inventory_ordinal,
            self.family.value,
            self.family_ordinal,
            self.artifact_id,
            self.run_id,
            self.evaluation_id,
            self.decision_id,
            self.horizon_id,
            self.schema_version,
            self.content_hash_sha256,
            self.supersedes_outcome_receipt_id,
            self.supersedes_outcome_id,
            self.supersedes_outcome_content_hash_sha256,
        )


def build_outcome_inventory(
    batch: OutcomeLabelBatch,
    *,
    predecessor_receipt: OpportunityOutcomePersistenceReceipt | None,
    predecessor_by_pair: dict[tuple[str, str], OutcomeRecord],
) -> tuple[OutcomeInventoryItem, ...]:
    inventory = [
        OutcomeInventoryItem(
            inventory_ordinal=0,
            family=OutcomeArtifactFamily.OUTCOME_LABEL_BATCH,
            family_ordinal=0,
            artifact_id=batch.batch_id,
            run_id=batch.pipeline_result.run_id,
            evaluation_id=None,
            decision_id=None,
            horizon_id=None,
            schema_version=batch.schema_version,
            content_hash_sha256=batch.content_hash(),
            supersedes_outcome_receipt_id=(
                predecessor_receipt.outcome_receipt_id
                if predecessor_receipt is not None
                else None
            ),
            supersedes_outcome_id=None,
            supersedes_outcome_content_hash_sha256=None,
        )
    ]
    for family_ordinal, record in enumerate(batch.outcomes):
        predecessor = predecessor_by_pair.get(
            (record.evaluation_id, record.horizon_id)
        )
        inventory.append(
            OutcomeInventoryItem(
                inventory_ordinal=len(inventory),
                family=OutcomeArtifactFamily.OUTCOME_RECORD,
                family_ordinal=family_ordinal,
                artifact_id=record.outcome_id,
                run_id=batch.pipeline_result.run_id,
                evaluation_id=record.evaluation_id,
                decision_id=record.decision_id,
                horizon_id=record.horizon_id,
                schema_version=record.schema_version,
                content_hash_sha256=record.content_hash(),
                supersedes_outcome_receipt_id=(
                    predecessor_receipt.outcome_receipt_id
                    if predecessor is not None and predecessor_receipt is not None
                    else None
                ),
                supersedes_outcome_id=(
                    predecessor.outcome_id if predecessor is not None else None
                ),
                supersedes_outcome_content_hash_sha256=(
                    predecessor.content_hash() if predecessor is not None else None
                ),
            )
        )
    return tuple(inventory)


def outcome_inventory_hash(inventory: tuple[OutcomeInventoryItem, ...]) -> str:
    canonical = contract_to_json(tuple(item.identity_tuple() for item in inventory))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_outcome_receipt(
    batch: OutcomeLabelBatch,
    *,
    persisted_at: datetime,
    inventory: tuple[OutcomeInventoryItem, ...],
    inventory_hash: str,
    predecessor: OpportunityOutcomePersistenceReceipt | None,
) -> OpportunityOutcomePersistenceReceipt:
    values: dict[str, Any] = {
        "receipt_kind": (
            OutcomePersistenceKind.CORRECTION
            if predecessor is not None
            else OutcomePersistenceKind.INITIAL
        ),
        "batch_id": batch.batch_id,
        "batch_content_hash_sha256": batch.content_hash(),
        "batch_schema_version": batch.schema_version,
        "run_id": batch.pipeline_result.run_id,
        "run_content_hash_sha256": batch.pipeline_result.content_hash(),
        "run_persistence_receipt_id": batch.persistence_receipt.receipt_id,
        "run_persistence_receipt_content_hash_sha256": (
            batch.persistence_receipt.content_hash()
        ),
        "source_dataset_id": batch.source_dataset.source_dataset_id,
        "source_dataset_content_hash_sha256": batch.source_dataset.content_hash(),
        "policy_id": batch.policy.policy_id,
        "policy_content_hash_sha256": batch.policy.content_hash(),
        "decision_at": batch.pipeline_result.decision_at,
        "batch_recorded_at": batch.recorded_at,
        "persisted_at": persisted_at,
        "supersedes_outcome_receipt_id": (
            predecessor.outcome_receipt_id if predecessor is not None else None
        ),
        "supersedes_outcome_receipt_content_hash_sha256": (
            predecessor.content_hash() if predecessor is not None else None
        ),
        "family_counts": tuple(
            OutcomeArtifactFamilyCount(
                family=family,
                count=sum(item.family is family for item in inventory),
            )
            for family in CANONICAL_OUTCOME_ARTIFACT_FAMILIES
        ),
        "record_count": len(batch.outcomes),
        "artifact_count": len(inventory),
        "artifact_inventory_hash_sha256": inventory_hash,
        "database_schema_version": 28,
        "research_only": True,
        "schema_version": "v2.opportunity.outcome_persistence_receipt.v1",
    }
    return OpportunityOutcomePersistenceReceipt(
        outcome_receipt_id=stable_identity(
            "opportunity-outcome-persistence-receipt",
            values,
        ),
        **values,
    )
