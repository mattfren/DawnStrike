"""Pure authoritative session-run inventory and stored replay contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from intraday_scanner.v2.contracts.serialization import contract_to_json
from intraday_scanner.v2.opportunity.capabilities import CapabilityState
from intraday_scanner.v2.opportunity.miss_contracts import (
    MissContract,
    SessionRunInventoryStatus,
    identity_payload,
    require_aware,
    require_hash,
    require_identity,
    require_sanitized,
    require_schema,
    require_unique,
    require_utc,
)
from intraday_scanner.v2.opportunity.miss_sources import QualificationSourceArtifact
from intraday_scanner.v2.opportunity.models import stable_identity
from intraday_scanner.v2.opportunity.outcome_persistence import CurrentOutcomeReplay


@dataclass(frozen=True)
class SessionRunBinding(MissContract):
    binding_id: str
    run_id: str
    run_content_hash_sha256: str
    preparation_id: str
    preparation_content_hash_sha256: str
    run_persistence_receipt_id: str
    run_persistence_receipt_content_hash_sha256: str
    outcome_replay_id: str
    outcome_replay_content_hash_sha256: str
    outcome_head_receipt_id: str
    outcome_head_receipt_content_hash_sha256: str
    decision_at: datetime
    schema_version: str = "v2.opportunity.session_run_binding.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(self.schema_version, "v2.opportunity.session_run_binding.v1")
        for value, name in (
            (self.binding_id, "binding_id"),
            (self.run_id, "run_id"),
            (self.preparation_id, "preparation_id"),
            (self.run_persistence_receipt_id, "run_persistence_receipt_id"),
            (self.outcome_replay_id, "outcome_replay_id"),
            (self.outcome_head_receipt_id, "outcome_head_receipt_id"),
        ):
            require_identity(value, name)
        for value, name in (
            (self.run_content_hash_sha256, "run_content_hash_sha256"),
            (
                self.preparation_content_hash_sha256,
                "preparation_content_hash_sha256",
            ),
            (
                self.run_persistence_receipt_content_hash_sha256,
                "run_persistence_receipt_content_hash_sha256",
            ),
            (self.outcome_replay_content_hash_sha256, "outcome_replay_content_hash_sha256"),
            (
                self.outcome_head_receipt_content_hash_sha256,
                "outcome_head_receipt_content_hash_sha256",
            ),
        ):
            require_hash(value, name)
        require_aware(self.decision_at, "decision_at")
        expected = stable_identity(
            "session-run-binding",
            identity_payload(self, "binding_id"),
        )
        if self.binding_id != expected:
            raise ValueError("session run binding identity does not match content")


@dataclass(frozen=True)
class SessionRunInventorySourceReceipt(MissContract):
    """Authoritative inventory query with a logical requested lower boundary."""

    source_receipt_id: str
    source_identity: str
    source_version: str
    method: str
    capability_state: CapabilityState
    authoritative: bool
    scope_complete: bool
    query_started_at: datetime
    query_ended_at: datetime
    observed_through_at: datetime
    fetched_at: datetime
    source_artifact: QualificationSourceArtifact
    limitations: tuple[str, ...]
    research_only: bool = True
    schema_version: str = "v2.opportunity.session_run_inventory_source_receipt.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.session_run_inventory_source_receipt.v1",
        )
        require_identity(self.source_receipt_id, "source_receipt_id")
        for value, name in (
            (self.source_identity, "source_identity"),
            (self.source_version, "source_version"),
            (self.method, "method"),
        ):
            require_sanitized(value, name)
        for timestamp, name in (
            (self.query_started_at, "query_started_at"),
            (self.query_ended_at, "query_ended_at"),
            (self.observed_through_at, "observed_through_at"),
            (self.fetched_at, "fetched_at"),
        ):
            require_utc(timestamp, name)
        if not self.query_started_at <= self.query_ended_at <= self.fetched_at:
            raise ValueError("session run inventory query chronology is inconsistent")
        if self.observed_through_at > self.fetched_at:
            raise ValueError("session run inventory cannot observe beyond fetch time")
        if self.source_artifact.source_identity != self.source_identity:
            raise ValueError("session inventory artifact source identity is inconsistent")
        if self.source_artifact.fetched_at != self.fetched_at:
            raise ValueError("session inventory artifact fetch time is inconsistent")
        require_unique(self.limitations, "session run inventory source limitation")
        for limitation in self.limitations:
            require_sanitized(limitation, "session run inventory source limitation")
        if not self.research_only:
            raise ValueError("session run inventory source must remain research-only")
        expected = stable_identity(
            "session-run-inventory-source",
            identity_payload(self, "source_receipt_id"),
        )
        if self.source_receipt_id != expected:
            raise ValueError("session inventory source receipt identity does not match content")


@dataclass(frozen=True)
class SessionRunInventoryEvidence(MissContract):
    inventory_id: str
    exchange_session_id: str
    session_open_at: datetime
    session_close_at: datetime
    source_receipt: SessionRunInventorySourceReceipt
    status: SessionRunInventoryStatus
    bindings: tuple[SessionRunBinding, ...]
    limitations: tuple[str, ...]
    research_only: bool = True
    schema_version: str = "v2.opportunity.session_run_inventory_evidence.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.session_run_inventory_evidence.v1",
        )
        require_identity(self.inventory_id, "inventory_id")
        require_sanitized(self.exchange_session_id, "exchange_session_id")
        require_utc(self.session_open_at, "session_open_at")
        require_utc(self.session_close_at, "session_close_at")
        if self.session_open_at >= self.session_close_at:
            raise ValueError("session run inventory session is reversed or empty")
        if self.bindings != tuple(sorted(self.bindings, key=_binding_sort_key)):
            raise ValueError("session run bindings must use canonical decision order")
        require_unique(tuple(item.run_id for item in self.bindings), "session run")
        if any(
            not self.source_receipt.query_started_at
            <= item.decision_at.astimezone(self.session_open_at.tzinfo)
            < self.session_close_at
            for item in self.bindings
        ):
            raise ValueError("session run decision lies outside the session query interval")
        if self.status is not _inventory_status(self):
            raise ValueError("session run inventory status does not match source scope")
        artifact_payload = _inventory_artifact_payload(
            exchange_session_id=self.exchange_session_id,
            session_open_at=self.session_open_at,
            session_close_at=self.session_close_at,
            bindings=self.bindings,
        )
        if (
            self.source_receipt.source_artifact.artifact_id
            != stable_identity("session-run-inventory-artifact", artifact_payload)
            or self.source_receipt.source_artifact.content_hash_sha256
            != _content_hash(artifact_payload)
        ):
            raise ValueError("session inventory source artifact does not bind exact runs")
        require_unique(self.limitations, "session run inventory limitation")
        for limitation in self.limitations:
            require_sanitized(limitation, "session run inventory limitation")
        if self.limitations != self.source_receipt.limitations:
            raise ValueError("session run inventory limitations must match source receipt")
        if not self.research_only:
            raise ValueError("session run inventory must remain research-only")
        expected = stable_identity(
            "session-run-inventory",
            identity_payload(self, "inventory_id"),
        )
        if self.inventory_id != expected:
            raise ValueError("session run inventory identity does not match content")


@dataclass(frozen=True)
class SessionReplay(MissContract):
    session_replay_id: str
    run_inventory: SessionRunInventoryEvidence
    current_outcome_replays: tuple[CurrentOutcomeReplay, ...]
    recorded_at: datetime
    limitations: tuple[str, ...]
    research_only: bool = True
    schema_version: str = "v2.opportunity.session_replay.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(self.schema_version, "v2.opportunity.session_replay.v1")
        require_identity(self.session_replay_id, "session_replay_id")
        require_utc(self.recorded_at, "recorded_at")
        if self.current_outcome_replays != tuple(
            sorted(self.current_outcome_replays, key=_replay_sort_key)
        ):
            raise ValueError("session outcome replays must use canonical decision order")
        require_unique(
            tuple(item.pipeline_result.run_id for item in self.current_outcome_replays),
            "session replay run",
        )
        expected_bindings = tuple(
            build_session_run_binding(item) for item in self.current_outcome_replays
        )
        if self.run_inventory.bindings != expected_bindings:
            raise ValueError("session replay does not match authoritative run inventory")
        expected_recorded_at = max(
            (
                self.run_inventory.source_receipt.fetched_at,
                *(
                    item.outcome_persistence_receipt.persisted_at
                    for item in self.current_outcome_replays
                ),
            )
        )
        if self.recorded_at != expected_recorded_at:
            raise ValueError("session replay recorded_at does not match source facts")
        require_unique(self.limitations, "session replay limitation")
        for limitation in self.limitations:
            require_sanitized(limitation, "session replay limitation")
        if not self.research_only:
            raise ValueError("session replay must remain research-only")
        expected = stable_identity(
            "miss-session-replay",
            identity_payload(self, "session_replay_id"),
        )
        if self.session_replay_id != expected:
            raise ValueError("session replay identity does not match content")


def build_session_run_binding(replay: CurrentOutcomeReplay) -> SessionRunBinding:
    result = replay.pipeline_result
    values = {
        "run_id": result.run_id,
        "run_content_hash_sha256": result.content_hash(),
        "preparation_id": result.preparation.preparation_id,
        "preparation_content_hash_sha256": result.preparation.content_hash(),
        "run_persistence_receipt_id": replay.run_persistence_receipt.receipt_id,
        "run_persistence_receipt_content_hash_sha256": (
            replay.run_persistence_receipt.content_hash()
        ),
        "outcome_replay_id": replay.replay_id,
        "outcome_replay_content_hash_sha256": replay.content_hash(),
        "outcome_head_receipt_id": replay.outcome_persistence_receipt.outcome_receipt_id,
        "outcome_head_receipt_content_hash_sha256": (
            replay.outcome_persistence_receipt.content_hash()
        ),
        "decision_at": result.decision_at,
        "schema_version": "v2.opportunity.session_run_binding.v1",
    }
    return SessionRunBinding(
        binding_id=stable_identity("session-run-binding", values),
        run_id=result.run_id,
        run_content_hash_sha256=result.content_hash(),
        preparation_id=result.preparation.preparation_id,
        preparation_content_hash_sha256=result.preparation.content_hash(),
        run_persistence_receipt_id=replay.run_persistence_receipt.receipt_id,
        run_persistence_receipt_content_hash_sha256=(
            replay.run_persistence_receipt.content_hash()
        ),
        outcome_replay_id=replay.replay_id,
        outcome_replay_content_hash_sha256=replay.content_hash(),
        outcome_head_receipt_id=(
            replay.outcome_persistence_receipt.outcome_receipt_id
        ),
        outcome_head_receipt_content_hash_sha256=(
            replay.outcome_persistence_receipt.content_hash()
        ),
        decision_at=result.decision_at,
    )


def build_session_run_inventory(
    *,
    exchange_session_id: str,
    session_open_at: datetime,
    session_close_at: datetime,
    current_outcome_replays: tuple[CurrentOutcomeReplay, ...],
    source_identity: str,
    source_version: str,
    method: str,
    capability_state: CapabilityState,
    authoritative: bool,
    scope_complete: bool,
    query_started_at: datetime,
    query_ended_at: datetime,
    observed_through_at: datetime,
    fetched_at: datetime,
    limitations: tuple[str, ...] = (),
) -> SessionRunInventoryEvidence:
    ordered_replays = tuple(sorted(current_outcome_replays, key=_replay_sort_key))
    bindings = tuple(build_session_run_binding(item) for item in ordered_replays)
    artifact_payload = _inventory_artifact_payload(
        exchange_session_id=exchange_session_id,
        session_open_at=session_open_at,
        session_close_at=session_close_at,
        bindings=bindings,
    )
    artifact = QualificationSourceArtifact(
        artifact_id=stable_identity("session-run-inventory-artifact", artifact_payload),
        content_hash_sha256=_content_hash(artifact_payload),
        source_identity=source_identity,
        fetched_at=fetched_at,
    )
    source_values = {
        "source_identity": source_identity,
        "source_version": source_version,
        "method": method,
        "capability_state": capability_state,
        "authoritative": authoritative,
        "scope_complete": scope_complete,
        "query_started_at": query_started_at,
        "query_ended_at": query_ended_at,
        "observed_through_at": observed_through_at,
        "fetched_at": fetched_at,
        "source_artifact": artifact,
        "limitations": limitations,
        "research_only": True,
        "schema_version": "v2.opportunity.session_run_inventory_source_receipt.v1",
    }
    source_receipt = SessionRunInventorySourceReceipt(
        source_receipt_id=stable_identity("session-run-inventory-source", source_values),
        source_identity=source_identity,
        source_version=source_version,
        method=method,
        capability_state=capability_state,
        authoritative=authoritative,
        scope_complete=scope_complete,
        query_started_at=query_started_at,
        query_ended_at=query_ended_at,
        observed_through_at=observed_through_at,
        fetched_at=fetched_at,
        source_artifact=artifact,
        limitations=limitations,
    )
    status = _inventory_status_values(
        source_receipt=source_receipt,
        session_open_at=session_open_at,
        session_close_at=session_close_at,
    )
    values = {
        "exchange_session_id": exchange_session_id,
        "session_open_at": session_open_at,
        "session_close_at": session_close_at,
        "source_receipt": source_receipt,
        "status": status,
        "bindings": bindings,
        "limitations": limitations,
        "research_only": True,
        "schema_version": "v2.opportunity.session_run_inventory_evidence.v1",
    }
    return SessionRunInventoryEvidence(
        inventory_id=stable_identity("session-run-inventory", values),
        exchange_session_id=exchange_session_id,
        session_open_at=session_open_at,
        session_close_at=session_close_at,
        source_receipt=source_receipt,
        status=status,
        bindings=bindings,
        limitations=limitations,
    )


def build_session_replay(
    inventory: SessionRunInventoryEvidence,
    *,
    current_outcome_replays: tuple[CurrentOutcomeReplay, ...],
) -> SessionReplay:
    ordered = tuple(sorted(current_outcome_replays, key=_replay_sort_key))
    recorded_at = max(
        (
            inventory.source_receipt.fetched_at,
            *(item.outcome_persistence_receipt.persisted_at for item in ordered),
        )
    )
    limitations = tuple(
        dict.fromkeys(("stored_current_outcome_heads_only", *inventory.limitations))
    )
    values = {
        "run_inventory": inventory,
        "current_outcome_replays": ordered,
        "recorded_at": recorded_at,
        "limitations": limitations,
        "research_only": True,
        "schema_version": "v2.opportunity.session_replay.v1",
    }
    return SessionReplay(
        session_replay_id=stable_identity("miss-session-replay", values),
        run_inventory=inventory,
        current_outcome_replays=ordered,
        recorded_at=recorded_at,
        limitations=limitations,
    )


def _inventory_status(
    inventory: SessionRunInventoryEvidence,
) -> SessionRunInventoryStatus:
    return _inventory_status_values(
        source_receipt=inventory.source_receipt,
        session_open_at=inventory.session_open_at,
        session_close_at=inventory.session_close_at,
    )


def _inventory_status_values(
    *,
    source_receipt: SessionRunInventorySourceReceipt,
    session_open_at: datetime,
    session_close_at: datetime,
) -> SessionRunInventoryStatus:
    if source_receipt.capability_state is not CapabilityState.AVAILABLE:
        return SessionRunInventoryStatus.UNAVAILABLE
    if (
        source_receipt.fetched_at < session_close_at
        or source_receipt.observed_through_at < session_close_at
    ):
        return SessionRunInventoryStatus.PENDING
    if (
        not source_receipt.scope_complete
        or source_receipt.query_started_at > session_open_at
        or source_receipt.query_ended_at != session_close_at
    ):
        return SessionRunInventoryStatus.PARTIAL
    if source_receipt.authoritative:
        return SessionRunInventoryStatus.COMPLETE_AUTHORITATIVE
    return SessionRunInventoryStatus.COMPLETE_BOUNDED


def _inventory_artifact_payload(
    *,
    exchange_session_id: str,
    session_open_at: datetime,
    session_close_at: datetime,
    bindings: tuple[SessionRunBinding, ...],
) -> dict[str, object]:
    return {
        "exchange_session_id": exchange_session_id,
        "session_open_at": session_open_at,
        "session_close_at": session_close_at,
        "bindings": bindings,
        "schema_version": "v2.opportunity.session_run_inventory_artifact.v1",
    }


def _content_hash(value: object) -> str:
    return hashlib.sha256(contract_to_json(value).encode("utf-8")).hexdigest()


def _binding_sort_key(binding: SessionRunBinding) -> tuple[datetime, str]:
    return binding.decision_at, binding.run_id


def _replay_sort_key(replay: CurrentOutcomeReplay) -> tuple[datetime, str]:
    return replay.pipeline_result.decision_at, replay.pipeline_result.run_id


__all__ = [
    "SessionReplay",
    "SessionRunBinding",
    "SessionRunInventoryEvidence",
    "SessionRunInventorySourceReceipt",
    "build_session_replay",
    "build_session_run_binding",
    "build_session_run_inventory",
]
