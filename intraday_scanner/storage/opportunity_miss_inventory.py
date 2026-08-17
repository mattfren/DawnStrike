"""Pure inventory and receipt construction for miss persistence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from intraday_scanner.v2.contracts import contract_to_json
from intraday_scanner.v2.opportunity.miss_persistence import (
    CANONICAL_MISS_ARTIFACT_FAMILIES,
    MissAnalysisHorizonBinding,
    MissArtifactFamily,
    MissArtifactFamilyCount,
    MissPersistenceKind,
    OpportunityMissPersistenceReceipt,
)
from intraday_scanner.v2.opportunity.miss_reconciliation import MissReconciliationBatch
from intraday_scanner.v2.opportunity.models import stable_identity


@dataclass(frozen=True)
class MissInventoryItem:
    inventory_ordinal: int
    family: MissArtifactFamily
    family_ordinal: int
    artifact_id: str
    session_opportunity_key: str | None
    run_id: str | None
    schema_version: str
    content_hash_sha256: str

    def identity_tuple(self) -> tuple[int, str, int, str, str | None, str | None, str, str]:
        return (
            self.inventory_ordinal,
            self.family.value,
            self.family_ordinal,
            self.artifact_id,
            self.session_opportunity_key,
            self.run_id,
            self.schema_version,
            self.content_hash_sha256,
        )


def build_miss_inventory(batch: MissReconciliationBatch) -> tuple[MissInventoryItem, ...]:
    inventory = [
        MissInventoryItem(
            inventory_ordinal=0,
            family=MissArtifactFamily.MISS_RECONCILIATION_BATCH,
            family_ordinal=0,
            artifact_id=batch.batch_id,
            session_opportunity_key=None,
            run_id=None,
            schema_version=batch.schema_version,
            content_hash_sha256=batch.content_hash(),
        )
    ]
    for family_ordinal, record in enumerate(batch.records):
        inventory.append(
            MissInventoryItem(
                inventory_ordinal=len(inventory),
                family=MissArtifactFamily.MISSED_OPPORTUNITY_RECORD,
                family_ordinal=family_ordinal,
                artifact_id=record.miss_record_id,
                session_opportunity_key=record.opportunity.session_opportunity_key,
                run_id=None,
                schema_version=record.schema_version,
                content_hash_sha256=record.content_hash(),
            )
        )
    for family_ordinal, binding in enumerate(batch.session_replay.run_inventory.bindings):
        inventory.append(
            MissInventoryItem(
                inventory_ordinal=len(inventory),
                family=MissArtifactFamily.SESSION_RUN_BINDING,
                family_ordinal=family_ordinal,
                artifact_id=binding.binding_id,
                session_opportunity_key=None,
                run_id=binding.run_id,
                schema_version=binding.schema_version,
                content_hash_sha256=binding.content_hash(),
            )
        )
    return tuple(inventory)


def miss_inventory_hash(inventory: tuple[MissInventoryItem, ...]) -> str:
    canonical = contract_to_json(tuple(item.identity_tuple() for item in inventory))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_miss_receipt(
    batch: MissReconciliationBatch,
    *,
    persisted_at: datetime,
    inventory: tuple[MissInventoryItem, ...],
    predecessor: OpportunityMissPersistenceReceipt | None,
) -> OpportunityMissPersistenceReceipt:
    scope = batch.qualification_batch.source.scope_receipt
    authority = scope.authority
    run_inventory = batch.session_replay.run_inventory
    inventory_source = run_inventory.source_receipt
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
    family_counts = tuple(
        MissArtifactFamilyCount(
            family=family,
            count=sum(item.family is family for item in inventory),
        )
        for family in CANONICAL_MISS_ARTIFACT_FAMILIES
    )
    scope_values: dict[str, Any] = {
        "exchange_session_id": scope.exchange_session_id,
        "session_open_at": scope.session_open_at,
        "session_close_at": scope.session_close_at,
        "membership_as_of_at": scope.membership_as_of_at,
        "requested_query_start_at": inventory_source.query_started_at,
        "requested_through_at": scope.session_close_at,
        "requested_symbols": scope.requested_symbols,
        "empty_eligible_universe": not scope.requested_symbols,
        "authority_claim": authority.claim,
        "qualification_source_identity": authority.authority_identity,
        "qualification_source_version": authority.authority_version,
        "inventory_source_identity": inventory_source.source_identity,
        "inventory_source_version": inventory_source.source_version,
        "inventory_source_method": inventory_source.method,
        "horizon_bindings": horizons,
        "qualification_policy_id": batch.qualification_batch.policy.policy_id,
        "qualification_policy_content_hash_sha256": (
            batch.qualification_batch.policy.content_hash()
        ),
        "schema_version": "v2.opportunity.miss_analysis.v1",
    }
    analysis_key = stable_identity("opportunity-miss-analysis", scope_values)
    values: dict[str, Any] = {
        "receipt_kind": (
            MissPersistenceKind.CORRECTION if predecessor else MissPersistenceKind.INITIAL
        ),
        "analysis_key": analysis_key,
        "batch_id": batch.batch_id,
        "batch_content_hash_sha256": batch.content_hash(),
        "batch_schema_version": batch.schema_version,
        "exchange_session_id": scope.exchange_session_id,
        "session_open_at": scope.session_open_at,
        "session_close_at": scope.session_close_at,
        "membership_as_of_at": scope.membership_as_of_at,
        "requested_query_start_at": inventory_source.query_started_at,
        "requested_through_at": scope.session_close_at,
        "requested_symbols": scope.requested_symbols,
        "empty_eligible_universe": not scope.requested_symbols,
        "authority_claim": authority.claim,
        "source_scope_status": scope.scope_status,
        "qualification_source_identity": authority.authority_identity,
        "qualification_source_version": authority.authority_version,
        "inventory_source_identity": inventory_source.source_identity,
        "inventory_source_version": inventory_source.source_version,
        "inventory_source_method": inventory_source.method,
        "inventory_status": run_inventory.status,
        "horizon_bindings": horizons,
        "qualification_policy_id": batch.qualification_batch.policy.policy_id,
        "qualification_policy_content_hash_sha256": (
            batch.qualification_batch.policy.content_hash()
        ),
        "qualification_batch_id": batch.qualification_batch.batch_id,
        "qualification_batch_content_hash_sha256": batch.qualification_batch.content_hash(),
        "session_replay_id": batch.session_replay.session_replay_id,
        "session_replay_content_hash_sha256": batch.session_replay.content_hash(),
        "session_disposition": batch.session_disposition,
        "batch_recorded_at": batch.recorded_at,
        "persisted_at": persisted_at,
        "supersedes_miss_receipt_id": (
            predecessor.miss_receipt_id if predecessor else None
        ),
        "supersedes_miss_receipt_content_hash_sha256": (
            predecessor.content_hash() if predecessor else None
        ),
        "family_counts": family_counts,
        "record_count": len(batch.records),
        "run_binding_count": len(run_inventory.bindings),
        "artifact_count": len(inventory),
        "artifact_inventory_hash_sha256": miss_inventory_hash(inventory),
        "database_schema_version": 29,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.miss_persistence_receipt.v1",
    }
    return OpportunityMissPersistenceReceipt(
        miss_receipt_id=stable_identity(
            "opportunity-miss-persistence-receipt",
            values,
        ),
        **values,
    )


__all__ = [
    "MissInventoryItem",
    "build_miss_inventory",
    "build_miss_receipt",
    "miss_inventory_hash",
]
