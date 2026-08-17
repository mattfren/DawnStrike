"""Strict downstream contracts for chronological validation research.

This module is intentionally not imported by the opportunity package root or
any decision-time module.  It contains immutable inputs and policies only; the
corpus, split, and audit modules derive every status and allocation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from intraday_scanner.v2.opportunity.models import (
    OpportunityContract,
    contract_to_json,
    stable_identity,
)
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeContract,
    OutcomeHorizonKind,
    _identity_payload,
    _require_hash,
    _require_identity,
    _require_sanitized_text,
    _require_schema,
    _require_unique,
    _require_utc,
)
from intraday_scanner.v2.opportunity.universe import UniverseMember, UniverseSnapshot


class ValidationHorizonSelectionKind(str, Enum):
    ELAPSED_SECONDS = "elapsed_seconds"
    SESSION_CLOSE = "session_close"


class SurvivorshipEvidenceStatus(str, Enum):
    POINT_IN_TIME = "point_in_time"
    CURRENT_MEMBERSHIP_PROXY = "current_membership_proxy"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class ValidationCorpusStatus(str, Enum):
    AVAILABLE = "available"
    EMPTY = "empty"
    INCOMPLETE = "incomplete"
    EXTERNAL_DATA_BLOCKED = "external_data_blocked"


class DeclaredRegion(str, Enum):
    TRAIN_RESEARCH = "train_research"
    VALIDATION = "validation"
    LOCKED_OOS = "locked_oos"


class SplitRole(str, Enum):
    TRAIN_RESEARCH = "train_research"
    VALIDATION = "validation"
    LOCKED_OOS = "locked_oos"
    PURGED = "purged"
    EMBARGOED = "embargoed"


class SplitPlanStatus(str, Enum):
    AVAILABLE = "available"
    INSUFFICIENT_DATA = "insufficient_data"
    EXTERNAL_DATA_BLOCKED = "external_data_blocked"


class FoldStatus(str, Enum):
    AVAILABLE = "available"
    INSUFFICIENT_DATA = "insufficient_data"


class HoldoutAccessStatus(str, Enum):
    NO_DURABLE_EVIDENCE = "no_durable_evidence"
    PREVIOUSLY_EVALUATED = "previously_evaluated"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class HoldoutIntegrityStatus(str, Enum):
    DECLARED_BEFORE_OOS_NOT_DURABLY_VERIFIED = (
        "declared_before_oos_not_durably_verified"
    )
    RETROSPECTIVE_ONLY = "retrospective_only"
    PREVIOUSLY_EVALUATED = "previously_evaluated"
    CONSUMPTION_UNKNOWN = "consumption_unknown"
    UNAVAILABLE = "unavailable"


class LeakageCheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class LeakageAuditStatus(str, Enum):
    PASSED_BOUNDED = "passed_bounded"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    EXTERNAL_DATA_BLOCKED = "external_data_blocked"


class ValidationPreparationStatus(str, Enum):
    READY_BOUNDED_RESEARCH = "ready_bounded_research"
    INSUFFICIENT_DATA = "insufficient_data"
    EXTERNAL_DATA_BLOCKED = "external_data_blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class ValidationSourceArtifact(OutcomeContract):
    artifact_id: str
    raw_artifact_hash_sha256: str
    normalized_artifact_hash_sha256: str
    provider: str
    source_identity: str
    source_version: str
    method: str
    observed_at: datetime
    fetched_at: datetime
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.validation_source_artifact.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.validation_source_artifact.v1")
        _require_identity(self.artifact_id, "artifact_id")
        _require_hash(self.raw_artifact_hash_sha256, "raw artifact hash")
        _require_hash(self.normalized_artifact_hash_sha256, "normalized artifact hash")
        for value, name in (
            (self.provider, "provider"),
            (self.source_identity, "source_identity"),
            (self.source_version, "source_version"),
            (self.method, "method"),
        ):
            _require_sanitized_text(value, name)
        _require_utc(self.observed_at, "observed_at")
        _require_utc(self.fetched_at, "fetched_at")
        if self.fetched_at < self.observed_at:
            raise ValueError("validation source cannot be fetched before it is observed")
        _validate_limitations(self.limitations, "validation source artifact")
        expected = stable_identity(
            "validation-source-artifact", _identity_payload(self, "artifact_id")
        )
        if self.artifact_id != expected:
            raise ValueError("validation source artifact identity does not match content")


@dataclass(frozen=True)
class ValidationMembershipBody(OutcomeContract):
    membership_body_id: str
    requested_symbols: tuple[str, ...]
    included_members: tuple[UniverseMember, ...]
    excluded_members: tuple[UniverseMember, ...]
    benchmark_member: UniverseMember | None
    membership_effective_at: datetime
    observed_at: datetime
    provider: str
    source_identity: str
    source_version: str
    method: str
    normalized_content_hash_sha256: str
    source_artifacts: tuple[ValidationSourceArtifact, ...]
    schema_version: str = "v2.opportunity.validation_membership_body.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.validation_membership_body.v1")
        _require_identity(self.membership_body_id, "membership_body_id")
        _require_hash(self.normalized_content_hash_sha256, "normalized membership hash")
        _require_utc(self.membership_effective_at, "membership_effective_at")
        _require_utc(self.observed_at, "observed_at")
        for value, name in (
            (self.provider, "provider"),
            (self.source_identity, "source_identity"),
            (self.source_version, "source_version"),
            (self.method, "method"),
        ):
            _require_sanitized_text(value, name)
        if self.requested_symbols != tuple(sorted(set(self.requested_symbols))):
            raise ValueError("membership requested symbols must use canonical unique order")
        included = tuple(item.symbol for item in self.included_members)
        excluded = tuple(item.symbol for item in self.excluded_members)
        if tuple(sorted((*included, *excluded))) != self.requested_symbols:
            raise ValueError("membership bodies do not reconcile requested symbols")
        if len(set((*included, *excluded))) != len(self.requested_symbols):
            raise ValueError("duplicate normalized membership symbol")
        if not self.source_artifacts or self.source_artifacts != tuple(
            sorted(self.source_artifacts, key=lambda item: item.artifact_id)
        ):
            raise ValueError("membership source artifacts must use nonempty canonical order")
        _require_unique(
            [item.artifact_id for item in self.source_artifacts],
            "membership source artifact",
        )
        normalized_payload = _membership_normalized_payload(self)
        expected_hash = hashlib.sha256(
            contract_to_json(normalized_payload).encode("utf-8")
        ).hexdigest()
        if self.normalized_content_hash_sha256 != expected_hash:
            raise ValueError("normalized membership hash does not match body")
        for artifact in self.source_artifacts:
            if (
                artifact.normalized_artifact_hash_sha256 != expected_hash
                or artifact.provider != self.provider
                or artifact.source_identity != self.source_identity
                or artifact.source_version != self.source_version
                or artifact.method != self.method
                or artifact.observed_at != self.observed_at
                or artifact.fetched_at < self.observed_at
            ):
                raise ValueError("membership source artifact lineage does not match body")
        expected = stable_identity(
            "validation-membership-body", _identity_payload(self, "membership_body_id")
        )
        if self.membership_body_id != expected:
            raise ValueError("validation membership identity does not match content")


@dataclass(frozen=True)
class ValidationSurvivorshipEvidence(OutcomeContract):
    evidence_id: str
    universe_snapshot_id: str
    universe_snapshot_content_hash_sha256: str
    universe_snapshot: UniverseSnapshot
    status: SurvivorshipEvidenceStatus
    membership_body: ValidationMembershipBody | None
    source_artifacts: tuple[ValidationSourceArtifact, ...]
    reason: str | None
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.validation_survivorship_evidence.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version, "v2.opportunity.validation_survivorship_evidence.v1"
        )
        _require_identity(self.evidence_id, "evidence_id")
        _require_identity(self.universe_snapshot_id, "universe_snapshot_id")
        _require_hash(self.universe_snapshot_content_hash_sha256, "universe snapshot hash")
        if (
            self.universe_snapshot_id != self.universe_snapshot.universe_snapshot_id
            or self.universe_snapshot_content_hash_sha256
            != self.universe_snapshot.content_hash()
        ):
            raise ValueError("survivorship evidence does not bind the universe snapshot")
        if self.reason is not None:
            _require_sanitized_text(self.reason, "survivorship reason")
        _validate_limitations(self.limitations, "survivorship evidence")
        if self.source_artifacts != tuple(
            sorted(self.source_artifacts, key=lambda item: item.artifact_id)
        ):
            raise ValueError("validation source artifacts must use canonical order")
        _require_unique([item.artifact_id for item in self.source_artifacts], "source artifact")
        if self.status in {
            SurvivorshipEvidenceStatus.POINT_IN_TIME,
            SurvivorshipEvidenceStatus.CURRENT_MEMBERSHIP_PROXY,
        }:
            if self.membership_body is None or not self.source_artifacts:
                raise ValueError(
                    "available survivorship evidence requires normalized body and artifacts"
                )
            _validate_membership_snapshot(self.membership_body, self.universe_snapshot)
            if self.membership_body.source_artifacts != self.source_artifacts:
                raise ValueError("membership body source artifacts do not reconcile")
            effective_at = self.membership_body.membership_effective_at
            if self.status is SurvivorshipEvidenceStatus.POINT_IN_TIME:
                if effective_at > self.universe_snapshot.as_of:
                    raise ValueError("post-decision membership truth is only a current proxy")
            elif effective_at <= self.universe_snapshot.as_of:
                raise ValueError("current-membership proxy must postdate snapshot as_of")
            if self.reason is not None:
                raise ValueError("available survivorship evidence cannot carry a reason")
        else:
            if self.membership_body is not None or self.source_artifacts:
                raise ValueError("unknown survivorship evidence cannot carry source truth")
            if self.reason is None:
                raise ValueError("unknown survivorship evidence requires a reason")
        expected = stable_identity(
            "validation-survivorship", _identity_payload(self, "evidence_id")
        )
        if self.evidence_id != expected:
            raise ValueError("survivorship evidence identity does not match content")


@dataclass(frozen=True)
class ValidationCorpusPolicy(OutcomeContract):
    policy_id: str
    policy_version: str
    horizon_kind: ValidationHorizonSelectionKind
    elapsed_seconds: int | None
    outcome_label_policy_id: str
    outcome_label_policy_content_hash_sha256: str
    retain_all_evaluations: bool = True
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.validation_corpus_policy.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.validation_corpus_policy.v1")
        _require_identity(self.policy_id, "policy_id")
        _require_sanitized_text(self.policy_version, "policy_version")
        _require_identity(self.outcome_label_policy_id, "outcome_label_policy_id")
        _require_hash(
            self.outcome_label_policy_content_hash_sha256,
            "outcome label policy content hash",
        )
        if self.horizon_kind is ValidationHorizonSelectionKind.ELAPSED_SECONDS:
            _require_positive_int(self.elapsed_seconds, "elapsed_seconds")
        elif self.elapsed_seconds is not None:
            raise ValueError("session-close selection cannot carry elapsed seconds")
        if not self.retain_all_evaluations or not self.research_only or self.promotion_eligible:
            raise ValueError("validation corpus policy must retain all research-only evaluations")
        expected = stable_identity(
            "validation-corpus-policy", _identity_payload(self, "policy_id")
        )
        if self.policy_id != expected:
            raise ValueError("validation corpus policy identity does not match content")

    @property
    def accepted_horizon_kind(self) -> OutcomeHorizonKind:
        if self.horizon_kind is ValidationHorizonSelectionKind.ELAPSED_SECONDS:
            return OutcomeHorizonKind.ELAPSED_SECONDS
        return OutcomeHorizonKind.SESSION_CLOSE


@dataclass(frozen=True)
class ValidationSplitPolicy(OutcomeContract):
    policy_id: str
    policy_version: str
    declared_at: datetime
    train_research_session_count: int
    validation_session_count: int
    locked_oos_session_count: int
    train_research_required: bool
    validation_required: bool
    locked_oos_required: bool
    region_embargo_session_count: int
    fold_embargo_session_count: int
    minimum_fold_training_sessions: int
    validation_window_session_count: int
    validation_step_session_count: int
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.validation_split_policy.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.validation_split_policy.v1")
        _require_identity(self.policy_id, "policy_id")
        _require_sanitized_text(self.policy_version, "policy_version")
        _require_utc(self.declared_at, "declared_at")
        for value, name in (
            (self.train_research_session_count, "train_research_session_count"),
            (self.validation_session_count, "validation_session_count"),
            (self.locked_oos_session_count, "locked_oos_session_count"),
            (self.region_embargo_session_count, "region_embargo_session_count"),
            (self.fold_embargo_session_count, "fold_embargo_session_count"),
        ):
            _require_nonnegative_int(value, name)
        for value, name in (
            (self.minimum_fold_training_sessions, "minimum_fold_training_sessions"),
            (self.validation_window_session_count, "validation_window_session_count"),
            (self.validation_step_session_count, "validation_step_session_count"),
        ):
            _require_positive_int(value, name)
        if self.validation_step_session_count != self.validation_window_session_count:
            raise ValueError("v1 validation windows must be disjoint")
        for required, count, name in (
            (self.train_research_required, self.train_research_session_count, "train"),
            (self.validation_required, self.validation_session_count, "validation"),
            (self.locked_oos_required, self.locked_oos_session_count, "locked OOS"),
        ):
            if required and count == 0:
                raise ValueError(f"required {name} region cannot declare zero sessions")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("validation split policy must remain research-only")
        expected = stable_identity(
            "validation-split-policy", _identity_payload(self, "policy_id")
        )
        if self.policy_id != expected:
            raise ValueError("validation split policy identity does not match content")


@dataclass(frozen=True)
class HoldoutAccessEvidence(OutcomeContract):
    evidence_id: str
    status: HoldoutAccessStatus
    observed_at: datetime
    source_identity: str
    source_version: str
    method: str
    artifact_ids: tuple[str, ...]
    artifact_content_hashes: tuple[str, ...]
    reason: str | None
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.validation_holdout_access_evidence.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version, "v2.opportunity.validation_holdout_access_evidence.v1"
        )
        _require_identity(self.evidence_id, "evidence_id")
        _require_utc(self.observed_at, "observed_at")
        for value, name in (
            (self.source_identity, "source_identity"),
            (self.source_version, "source_version"),
            (self.method, "method"),
        ):
            _require_sanitized_text(value, name)
        _validate_artifact_inventory(self.artifact_ids, self.artifact_content_hashes)
        _validate_limitations(self.limitations, "holdout access evidence")
        if self.reason is not None:
            _require_sanitized_text(self.reason, "holdout access reason")
        if self.status is HoldoutAccessStatus.PREVIOUSLY_EVALUATED:
            if not self.artifact_ids or self.reason is not None:
                raise ValueError("previous evaluation requires exact artifacts and no reason")
        elif self.reason is None:
            raise ValueError("non-observed holdout access status requires a reason")
        expected = stable_identity(
            "validation-holdout-access", _identity_payload(self, "evidence_id")
        )
        if self.evidence_id != expected:
            raise ValueError("holdout access evidence identity does not match content")


def build_validation_source_artifact(
    *,
    raw_artifact_hash_sha256: str,
    normalized_artifact_hash_sha256: str,
    provider: str,
    source_identity: str,
    source_version: str,
    method: str,
    observed_at: datetime,
    fetched_at: datetime,
    limitations: tuple[str, ...] = (),
) -> ValidationSourceArtifact:
    values = {**locals(), "schema_version": "v2.opportunity.validation_source_artifact.v1"}
    return ValidationSourceArtifact(
        artifact_id=stable_identity("validation-source-artifact", values), **values
    )


def build_validation_membership_body(
    *,
    universe_snapshot: UniverseSnapshot,
    membership_effective_at: datetime,
    observed_at: datetime,
    provider: str,
    source_identity: str,
    source_version: str,
    method: str,
    source_artifacts: tuple[ValidationSourceArtifact, ...],
) -> ValidationMembershipBody:
    artifacts = tuple(sorted(source_artifacts, key=lambda item: item.artifact_id))
    base = {
        "requested_symbols": universe_snapshot.requested_symbols,
        "included_members": universe_snapshot.included_members,
        "excluded_members": universe_snapshot.excluded_members,
        "benchmark_member": universe_snapshot.benchmark_member,
        "membership_effective_at": membership_effective_at,
        "observed_at": observed_at,
        "provider": provider,
        "source_identity": source_identity,
        "source_version": source_version,
        "method": method,
        "source_artifacts": artifacts,
        "schema_version": "v2.opportunity.validation_membership_body.v1",
    }
    normalized_hash = validation_membership_normalized_hash(
        universe_snapshot=universe_snapshot,
        membership_effective_at=membership_effective_at,
        observed_at=observed_at,
        provider=provider,
        source_identity=source_identity,
        source_version=source_version,
        method=method,
    )
    values = {**base, "normalized_content_hash_sha256": normalized_hash}
    return ValidationMembershipBody(
        membership_body_id=stable_identity("validation-membership-body", values),
        requested_symbols=universe_snapshot.requested_symbols,
        included_members=universe_snapshot.included_members,
        excluded_members=universe_snapshot.excluded_members,
        benchmark_member=universe_snapshot.benchmark_member,
        membership_effective_at=membership_effective_at,
        observed_at=observed_at,
        provider=provider,
        source_identity=source_identity,
        source_version=source_version,
        method=method,
        normalized_content_hash_sha256=normalized_hash,
        source_artifacts=artifacts,
    )


def build_validation_survivorship_evidence(
    *,
    universe_snapshot: UniverseSnapshot,
    status: SurvivorshipEvidenceStatus,
    membership_body: ValidationMembershipBody | None,
    source_artifacts: tuple[ValidationSourceArtifact, ...],
    reason: str | None = None,
    limitations: tuple[str, ...] = (),
) -> ValidationSurvivorshipEvidence:
    artifacts = tuple(sorted(source_artifacts, key=lambda item: item.artifact_id))
    values = {
        "universe_snapshot_id": universe_snapshot.universe_snapshot_id,
        "universe_snapshot_content_hash_sha256": universe_snapshot.content_hash(),
        "universe_snapshot": universe_snapshot,
        "status": status,
        "membership_body": membership_body,
        "source_artifacts": artifacts,
        "reason": reason,
        "limitations": limitations,
        "schema_version": "v2.opportunity.validation_survivorship_evidence.v1",
    }
    return ValidationSurvivorshipEvidence(
        evidence_id=stable_identity("validation-survivorship", values),
        universe_snapshot_id=universe_snapshot.universe_snapshot_id,
        universe_snapshot_content_hash_sha256=universe_snapshot.content_hash(),
        universe_snapshot=universe_snapshot,
        status=status,
        membership_body=membership_body,
        source_artifacts=artifacts,
        reason=reason,
        limitations=limitations,
    )


def build_validation_corpus_policy(
    *,
    policy_version: str,
    horizon_kind: ValidationHorizonSelectionKind,
    elapsed_seconds: int | None,
    outcome_label_policy_id: str,
    outcome_label_policy_content_hash_sha256: str,
) -> ValidationCorpusPolicy:
    values = {
        "policy_version": policy_version,
        "horizon_kind": horizon_kind,
        "elapsed_seconds": elapsed_seconds,
        "outcome_label_policy_id": outcome_label_policy_id,
        "outcome_label_policy_content_hash_sha256": (
            outcome_label_policy_content_hash_sha256
        ),
        "retain_all_evaluations": True,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.validation_corpus_policy.v1",
    }
    return ValidationCorpusPolicy(
        policy_id=stable_identity("validation-corpus-policy", values),
        policy_version=policy_version,
        horizon_kind=horizon_kind,
        elapsed_seconds=elapsed_seconds,
        outcome_label_policy_id=outcome_label_policy_id,
        outcome_label_policy_content_hash_sha256=(
            outcome_label_policy_content_hash_sha256
        ),
    )


def build_validation_split_policy(
    *,
    policy_version: str,
    declared_at: datetime,
    train_research_session_count: int,
    validation_session_count: int,
    locked_oos_session_count: int,
    train_research_required: bool = True,
    validation_required: bool = True,
    locked_oos_required: bool = True,
    region_embargo_session_count: int = 0,
    fold_embargo_session_count: int = 0,
    minimum_fold_training_sessions: int = 1,
    validation_window_session_count: int = 1,
    validation_step_session_count: int = 1,
) -> ValidationSplitPolicy:
    values = {
        "policy_version": policy_version,
        "declared_at": declared_at,
        "train_research_session_count": train_research_session_count,
        "validation_session_count": validation_session_count,
        "locked_oos_session_count": locked_oos_session_count,
        "train_research_required": train_research_required,
        "validation_required": validation_required,
        "locked_oos_required": locked_oos_required,
        "region_embargo_session_count": region_embargo_session_count,
        "fold_embargo_session_count": fold_embargo_session_count,
        "minimum_fold_training_sessions": minimum_fold_training_sessions,
        "validation_window_session_count": validation_window_session_count,
        "validation_step_session_count": validation_step_session_count,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.validation_split_policy.v1",
    }
    return ValidationSplitPolicy(
        policy_id=stable_identity("validation-split-policy", values),
        policy_version=policy_version,
        declared_at=declared_at,
        train_research_session_count=train_research_session_count,
        validation_session_count=validation_session_count,
        locked_oos_session_count=locked_oos_session_count,
        train_research_required=train_research_required,
        validation_required=validation_required,
        locked_oos_required=locked_oos_required,
        region_embargo_session_count=region_embargo_session_count,
        fold_embargo_session_count=fold_embargo_session_count,
        minimum_fold_training_sessions=minimum_fold_training_sessions,
        validation_window_session_count=validation_window_session_count,
        validation_step_session_count=validation_step_session_count,
    )


def build_holdout_access_evidence(
    *,
    status: HoldoutAccessStatus,
    observed_at: datetime,
    source_identity: str,
    source_version: str,
    method: str,
    artifact_ids: tuple[str, ...] = (),
    artifact_content_hashes: tuple[str, ...] = (),
    reason: str | None = None,
    limitations: tuple[str, ...] = (),
) -> HoldoutAccessEvidence:
    values = {
        **locals(),
        "schema_version": "v2.opportunity.validation_holdout_access_evidence.v1",
    }
    return HoldoutAccessEvidence(
        evidence_id=stable_identity("validation-holdout-access", values), **values
    )


def _membership_normalized_payload(body: ValidationMembershipBody) -> dict[str, object]:
    return {
        name: value
        for name, value in body.__dict__.items()
        if name
        not in {
            "membership_body_id",
            "normalized_content_hash_sha256",
            "source_artifacts",
        }
    }


def validation_membership_normalized_hash(
    *,
    universe_snapshot: UniverseSnapshot,
    membership_effective_at: datetime,
    observed_at: datetime,
    provider: str,
    source_identity: str,
    source_version: str,
    method: str,
) -> str:
    payload = {
        "requested_symbols": universe_snapshot.requested_symbols,
        "included_members": universe_snapshot.included_members,
        "excluded_members": universe_snapshot.excluded_members,
        "benchmark_member": universe_snapshot.benchmark_member,
        "membership_effective_at": membership_effective_at,
        "observed_at": observed_at,
        "provider": provider,
        "source_identity": source_identity,
        "source_version": source_version,
        "method": method,
        "schema_version": "v2.opportunity.validation_membership_body.v1",
    }
    return hashlib.sha256(contract_to_json(payload).encode("utf-8")).hexdigest()


def _validate_membership_snapshot(
    body: ValidationMembershipBody, snapshot: UniverseSnapshot
) -> None:
    if (
        body.requested_symbols != snapshot.requested_symbols
        or body.included_members != snapshot.included_members
        or body.excluded_members != snapshot.excluded_members
        or body.benchmark_member != snapshot.benchmark_member
    ):
        raise ValueError("normalized membership body does not match universe snapshot")


def _validate_artifact_inventory(ids: tuple[str, ...], hashes: tuple[str, ...]) -> None:
    if len(ids) != len(hashes):
        raise ValueError("artifact identity and hash counts do not match")
    for value in ids:
        _require_identity(value, "artifact_id")
    for value in hashes:
        _require_hash(value, "artifact content hash")
    if ids != tuple(sorted(set(ids))):
        raise ValueError("artifact IDs must use canonical unique order")


def _validate_limitations(values: tuple[str, ...], label: str) -> None:
    _require_unique(list(values), f"{label} limitation")
    for value in values:
        _require_sanitized_text(value, f"{label} limitation")


def _require_nonnegative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")


def _require_positive_int(value: int | None, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


__all__ = [
    "DeclaredRegion",
    "FoldStatus",
    "HoldoutAccessEvidence",
    "HoldoutAccessStatus",
    "HoldoutIntegrityStatus",
    "LeakageAuditStatus",
    "LeakageCheckStatus",
    "SplitPlanStatus",
    "SplitRole",
    "SurvivorshipEvidenceStatus",
    "ValidationCorpusPolicy",
    "ValidationCorpusStatus",
    "ValidationHorizonSelectionKind",
    "ValidationMembershipBody",
    "ValidationPreparationStatus",
    "ValidationSourceArtifact",
    "ValidationSplitPolicy",
    "ValidationSurvivorshipEvidence",
    "build_holdout_access_evidence",
    "build_validation_corpus_policy",
    "build_validation_membership_body",
    "build_validation_source_artifact",
    "build_validation_split_policy",
    "build_validation_survivorship_evidence",
    "validation_membership_normalized_hash",
]
