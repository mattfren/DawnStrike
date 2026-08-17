"""Content-bound contracts for validation persistence and one-time OOS use."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from weakref import ReferenceType, ref

from intraday_scanner.v2.contracts import contract_to_json
from intraday_scanner.v2.opportunity.models import OpportunityContract, stable_identity
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeContract,
    _identity_payload,
    _require_hash,
    _require_identity,
    _require_sanitized_text,
    _require_schema,
    _require_unique,
    _require_utc,
)
from intraday_scanner.v2.opportunity.validation_audit import (
    ChronologicalValidationPreparationReceipt,
)
from intraday_scanner.v2.opportunity.validation_contracts import (
    HoldoutAccessEvidence,
    HoldoutAccessStatus,
    HoldoutIntegrityStatus,
)
from intraday_scanner.v2.opportunity.validation_metric_report import (
    ValidationTradingMetricReport,
)
from intraday_scanner.v2.opportunity.validation_robustness_report import (
    ValidationRobustnessReport,
)

VALIDATION_DATABASE_SCHEMA_VERSION = 30
_CODE_IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}")
_CONTENT_HASH_CACHE: dict[
    int, tuple[ReferenceType[OpportunityContract], str]
] = {}


class ValidationPersistenceStatus(str, Enum):
    RESEARCH_EVIDENCE = "research_evidence"
    LOCKED_OOS_CONSUMED = "locked_oos_consumed"
    INVALID_LOCK = "invalid_lock"
    RETROSPECTIVE = "retrospective"
    REUSED = "reused"
    MISSING_EVIDENCE = "missing_evidence"
    NON_PREDECLARED = "non_predeclared"


@dataclass(frozen=True)
class LockedOOSSessionBinding(OutcomeContract):
    session_ordinal: int
    session_source_id: str
    session_content_hash_sha256: str
    exchange_session_id: str
    session_open_at: datetime
    session_close_at: datetime
    role: str = "locked_oos"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        if type(self.session_ordinal) is not int or self.session_ordinal < 0:
            raise ValueError("locked OOS ordinal must be nonnegative")
        _require_identity(self.session_source_id, "locked OOS session source")
        _require_hash(self.session_content_hash_sha256, "locked OOS session hash")
        _require_sanitized_text(self.exchange_session_id, "locked OOS exchange session")
        _require_utc(self.session_open_at, "locked OOS session open")
        _require_utc(self.session_close_at, "locked OOS session close")
        if self.session_open_at >= self.session_close_at:
            raise ValueError("locked OOS session window is invalid")
        if self.role != "locked_oos":
            raise ValueError("locked OOS binding role is invalid")

    def identity_tuple(self) -> tuple[object, ...]:
        return (
            self.session_ordinal,
            self.session_source_id,
            self.session_content_hash_sha256,
            self.exchange_session_id,
            self.session_open_at,
            self.session_close_at,
            self.role,
        )


@dataclass(frozen=True)
class ValidationPersistenceReceipt(OutcomeContract):
    validation_receipt_id: str
    semantic_lock_key: str
    lock_authority_key: str
    holdout_inventory_key: str
    status: ValidationPersistenceStatus
    fresh_lock_eligible: bool
    preparation_id: str
    preparation_content_hash_sha256: str
    metric_report_id: str
    metric_report_content_hash_sha256: str
    robustness_report_id: str
    robustness_report_content_hash_sha256: str
    holdout_access_evidence_id: str
    holdout_access_content_hash_sha256: str
    corpus_id: str
    split_plan_id: str
    split_policy_id: str
    split_policy_content_hash_sha256: str
    split_policy_declared_at: datetime
    code_identity: str
    code_content_hash_sha256: str
    strategy_id: str
    strategy_version: str
    confirmatory_unit_id: str
    confirmatory_unit_content_hash_sha256: str
    corpus_policy_id: str
    corpus_policy_content_hash_sha256: str
    metric_policy_id: str
    metric_policy_content_hash_sha256: str
    robustness_policy_id: str
    robustness_policy_content_hash_sha256: str
    oos_sessions: tuple[LockedOOSSessionBinding, ...]
    oos_session_inventory_hash_sha256: str
    result_set_hash_sha256: str
    persisted_at: datetime
    lifecycle_mutation_count: int = 0
    take_authorization: bool = False
    research_only: bool = True
    promotion_eligible: bool = False
    database_schema_version: int = VALIDATION_DATABASE_SCHEMA_VERSION
    schema_version: str = "v2.opportunity.validation_persistence_receipt.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version,
            "v2.opportunity.validation_persistence_receipt.v1",
        )
        for value, label in (
            (self.validation_receipt_id, "validation receipt"),
            (self.preparation_id, "validation preparation"),
            (self.metric_report_id, "validation metric report"),
            (self.robustness_report_id, "validation robustness report"),
            (self.holdout_access_evidence_id, "holdout access evidence"),
            (self.corpus_id, "validation corpus"),
            (self.split_plan_id, "validation split plan"),
            (self.split_policy_id, "validation split policy"),
            (self.strategy_id, "strategy"),
            (self.confirmatory_unit_id, "confirmatory unit"),
            (self.corpus_policy_id, "validation corpus policy"),
            (self.metric_policy_id, "validation metric policy"),
            (self.robustness_policy_id, "validation robustness policy"),
        ):
            _require_identity(value, label)
        for value, label in (
            (self.semantic_lock_key, "validation semantic lock"),
            (self.lock_authority_key, "validation lock authority"),
            (self.holdout_inventory_key, "validation holdout inventory key"),
            (self.preparation_content_hash_sha256, "preparation hash"),
            (self.metric_report_content_hash_sha256, "metric report hash"),
            (self.robustness_report_content_hash_sha256, "robustness report hash"),
            (self.holdout_access_content_hash_sha256, "holdout access hash"),
            (self.split_policy_content_hash_sha256, "split policy hash"),
            (self.code_content_hash_sha256, "code hash"),
            (self.confirmatory_unit_content_hash_sha256, "confirmatory unit hash"),
            (self.corpus_policy_content_hash_sha256, "corpus policy hash"),
            (self.metric_policy_content_hash_sha256, "metric policy hash"),
            (self.robustness_policy_content_hash_sha256, "robustness policy hash"),
            (self.oos_session_inventory_hash_sha256, "OOS inventory hash"),
            (self.result_set_hash_sha256, "validation result-set hash"),
        ):
            _require_hash(value, label)
        if not _CODE_IDENTITY_PATTERN.fullmatch(self.code_identity):
            raise ValueError("code identity is invalid")
        _require_sanitized_text(self.strategy_version, "strategy version")
        _require_utc(self.split_policy_declared_at, "split policy declaration")
        _require_utc(self.persisted_at, "validation persistence time")
        if self.oos_sessions != tuple(
            sorted(self.oos_sessions, key=lambda item: item.session_ordinal)
        ):
            raise ValueError("locked OOS inventory must use canonical order")
        if tuple(item.session_ordinal for item in self.oos_sessions) != tuple(
            range(len(self.oos_sessions))
        ):
            raise ValueError("locked OOS ordinals must be contiguous")
        _require_unique(
            [item.session_source_id for item in self.oos_sessions],
            "locked OOS session",
        )
        if self.oos_session_inventory_hash_sha256 != locked_oos_inventory_hash(
            self.oos_sessions
        ):
            raise ValueError("locked OOS inventory hash does not match body")
        if (
            self.status is ValidationPersistenceStatus.LOCKED_OOS_CONSUMED
            and (not self.fresh_lock_eligible or not self.oos_sessions)
        ):
            raise ValueError("consumed locked OOS receipt requires an eligible inventory")
        if self.status in {
            ValidationPersistenceStatus.INVALID_LOCK,
            ValidationPersistenceStatus.RETROSPECTIVE,
            ValidationPersistenceStatus.REUSED,
            ValidationPersistenceStatus.MISSING_EVIDENCE,
            ValidationPersistenceStatus.NON_PREDECLARED,
        } and self.fresh_lock_eligible:
            raise ValueError("invalid locked OOS status cannot be fresh-lock eligible")
        if (
            self.lifecycle_mutation_count != 0
            or self.take_authorization
            or not self.research_only
            or self.promotion_eligible
        ):
            raise ValueError("validation persistence cannot promote or mutate lifecycle state")
        if self.database_schema_version != VALIDATION_DATABASE_SCHEMA_VERSION:
            raise ValueError("validation receipt database schema version is invalid")
        expected = stable_identity(
            "opportunity-validation-persistence-receipt",
            _identity_payload(self, "validation_receipt_id"),
        )
        if self.validation_receipt_id != expected:
            raise ValueError("validation persistence identity does not match content")


@dataclass(frozen=True)
class ValidationPersistenceReplay(OutcomeContract):
    replay_id: str
    persistence_receipt: ValidationPersistenceReceipt
    preparation: ChronologicalValidationPreparationReceipt
    metric_report: ValidationTradingMetricReport
    robustness_report: ValidationRobustnessReport
    holdout_access_evidence: HoldoutAccessEvidence
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.validation_persistence_replay.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version,
            "v2.opportunity.validation_persistence_replay.v1",
        )
        _verify_bundle_bindings(
            self.preparation,
            self.metric_report,
            self.robustness_report,
            self.holdout_access_evidence,
        )
        receipt = self.persistence_receipt
        if (
            receipt.preparation_id != self.preparation.preparation_id
            or receipt.preparation_content_hash_sha256 != _content_hash(self.preparation)
            or receipt.metric_report_id != self.metric_report.report_id
            or receipt.metric_report_content_hash_sha256 != _content_hash(self.metric_report)
            or receipt.robustness_report_id != self.robustness_report.report_id
            or receipt.robustness_report_content_hash_sha256
            != _content_hash(self.robustness_report)
            or receipt.holdout_access_evidence_id
            != self.holdout_access_evidence.evidence_id
            or receipt.holdout_access_content_hash_sha256
            != _content_hash(self.holdout_access_evidence)
            or receipt.result_set_hash_sha256
            != validation_result_set_hash(
                self.preparation,
                self.metric_report,
                self.robustness_report,
                self.holdout_access_evidence,
            )
        ):
            raise ValueError("validation replay bodies do not bind the receipt")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("validation replay must remain research-only")
        expected = stable_identity(
            "opportunity-validation-persistence-replay",
            _identity_payload(self, "replay_id"),
        )
        if self.replay_id != expected:
            raise ValueError("validation replay identity does not match content")


def build_validation_persistence_receipt(
    preparation: ChronologicalValidationPreparationReceipt,
    metric_report: ValidationTradingMetricReport,
    robustness_report: ValidationRobustnessReport,
    holdout_access_evidence: HoldoutAccessEvidence,
    *,
    code_identity: str,
    code_content_hash_sha256: str,
    persisted_at: datetime,
    status: ValidationPersistenceStatus,
) -> ValidationPersistenceReceipt:
    _verify_bundle_bindings(
        preparation,
        metric_report,
        robustness_report,
        holdout_access_evidence,
    )
    _require_utc(persisted_at, "validation persistence time")
    if persisted_at < max(
        preparation.recorded_at,
        metric_report.recorded_at,
        robustness_report.recorded_at,
        holdout_access_evidence.observed_at,
    ):
        raise ValueError("validation persistence time predates evidence")
    _require_hash(code_content_hash_sha256, "code hash")
    if not _CODE_IDENTITY_PATTERN.fullmatch(code_identity):
        raise ValueError("code identity is invalid")
    plan = preparation.audit_receipt.fold_collection.split_plan
    corpus = plan.corpus
    unit = robustness_report.policy.confirmatory_unit
    sessions = build_locked_oos_inventory(preparation)
    eligible, derived_failure = locked_oos_eligibility(
        preparation,
        holdout_access_evidence,
        robustness_report,
    )
    if status is ValidationPersistenceStatus.LOCKED_OOS_CONSUMED and not eligible:
        raise ValueError(
            "locked OOS is not eligible for fresh consumption: "
            f"{derived_failure.value}"
        )
    if status in {
        ValidationPersistenceStatus.INVALID_LOCK,
        ValidationPersistenceStatus.RETROSPECTIVE,
        ValidationPersistenceStatus.REUSED,
        ValidationPersistenceStatus.MISSING_EVIDENCE,
        ValidationPersistenceStatus.NON_PREDECLARED,
    } and status is not derived_failure:
        raise ValueError("locked OOS failure status does not recompute")
    values = {
        "semantic_lock_key": semantic_locked_oos_key(
            preparation,
            holdout_access_evidence,
            robustness_report,
            code_content_hash_sha256=code_content_hash_sha256,
        ),
        "lock_authority_key": locked_oos_authority_key(
            preparation,
            robustness_report,
            code_content_hash_sha256=code_content_hash_sha256,
        ),
        "holdout_inventory_key": locked_oos_inventory_use_key(preparation),
        "status": status,
        "fresh_lock_eligible": eligible,
        "preparation_id": preparation.preparation_id,
        "preparation_content_hash_sha256": _content_hash(preparation),
        "metric_report_id": metric_report.report_id,
        "metric_report_content_hash_sha256": _content_hash(metric_report),
        "robustness_report_id": robustness_report.report_id,
        "robustness_report_content_hash_sha256": _content_hash(robustness_report),
        "holdout_access_evidence_id": holdout_access_evidence.evidence_id,
        "holdout_access_content_hash_sha256": _content_hash(holdout_access_evidence),
        "corpus_id": corpus.corpus_id,
        "split_plan_id": plan.split_plan_id,
        "split_policy_id": plan.policy.policy_id,
        "split_policy_content_hash_sha256": _content_hash(plan.policy),
        "split_policy_declared_at": plan.policy.declared_at,
        "code_identity": code_identity,
        "code_content_hash_sha256": code_content_hash_sha256,
        "strategy_id": unit.strategy_id,
        "strategy_version": unit.strategy_version,
        "confirmatory_unit_id": unit.unit_id,
        "confirmatory_unit_content_hash_sha256": _content_hash(unit),
        "corpus_policy_id": corpus.policy.policy_id,
        "corpus_policy_content_hash_sha256": _content_hash(corpus.policy),
        "metric_policy_id": metric_report.policy.policy_id,
        "metric_policy_content_hash_sha256": _content_hash(metric_report.policy),
        "robustness_policy_id": robustness_report.policy.policy_id,
        "robustness_policy_content_hash_sha256": _content_hash(robustness_report.policy),
        "oos_sessions": sessions,
        "oos_session_inventory_hash_sha256": locked_oos_inventory_hash(sessions),
        "result_set_hash_sha256": validation_result_set_hash(
            preparation,
            metric_report,
            robustness_report,
            holdout_access_evidence,
        ),
        "persisted_at": persisted_at,
        "lifecycle_mutation_count": 0,
        "take_authorization": False,
        "research_only": True,
        "promotion_eligible": False,
        "database_schema_version": VALIDATION_DATABASE_SCHEMA_VERSION,
        "schema_version": "v2.opportunity.validation_persistence_receipt.v1",
    }
    return ValidationPersistenceReceipt(
        validation_receipt_id=stable_identity(
            "opportunity-validation-persistence-receipt", values
        ),
        **values,  # type: ignore[arg-type]
    )


def build_validation_persistence_replay(
    receipt: ValidationPersistenceReceipt,
    preparation: ChronologicalValidationPreparationReceipt,
    metric_report: ValidationTradingMetricReport,
    robustness_report: ValidationRobustnessReport,
    holdout_access_evidence: HoldoutAccessEvidence,
) -> ValidationPersistenceReplay:
    values = {
        "persistence_receipt": receipt,
        "preparation": preparation,
        "metric_report": metric_report,
        "robustness_report": robustness_report,
        "holdout_access_evidence": holdout_access_evidence,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.validation_persistence_replay.v1",
    }
    return ValidationPersistenceReplay(
        replay_id=stable_identity(
            "opportunity-validation-persistence-replay", values
        ),
        persistence_receipt=receipt,
        preparation=preparation,
        metric_report=metric_report,
        robustness_report=robustness_report,
        holdout_access_evidence=holdout_access_evidence,
    )


def build_locked_oos_inventory(
    preparation: ChronologicalValidationPreparationReceipt,
) -> tuple[LockedOOSSessionBinding, ...]:
    plan = preparation.audit_receipt.fold_collection.split_plan
    sessions = {item.session_source_id: item for item in plan.corpus.sessions}
    result = []
    for ordinal, session_id in enumerate(plan.locked_oos_session_ids):
        session = sessions.get(session_id)
        if session is None:
            raise ValueError("locked OOS inventory references a missing corpus session")
        result.append(
            LockedOOSSessionBinding(
                session_ordinal=ordinal,
                session_source_id=session.session_source_id,
                session_content_hash_sha256=_content_hash(session),
                exchange_session_id=session.exchange_session_id,
                session_open_at=session.session_open_at,
                session_close_at=session.session_close_at,
            )
        )
    return tuple(result)


def locked_oos_inventory_hash(
    sessions: tuple[LockedOOSSessionBinding, ...],
) -> str:
    payload = contract_to_json(tuple(item.identity_tuple() for item in sessions))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validation_result_set_hash(
    preparation: ChronologicalValidationPreparationReceipt,
    metric_report: ValidationTradingMetricReport,
    robustness_report: ValidationRobustnessReport,
    holdout_access_evidence: HoldoutAccessEvidence,
) -> str:
    payload = contract_to_json(
        (
            _content_hash(preparation),
            _content_hash(metric_report),
            _content_hash(robustness_report),
            _content_hash(holdout_access_evidence),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def semantic_locked_oos_key(
    preparation: ChronologicalValidationPreparationReceipt,
    holdout_access_evidence: HoldoutAccessEvidence,
    robustness_report: ValidationRobustnessReport,
    *,
    code_content_hash_sha256: str,
) -> str:
    plan = preparation.audit_receipt.fold_collection.split_plan
    corpus = plan.corpus
    metric = robustness_report.population.metric_report
    unit = robustness_report.policy.confirmatory_unit
    sessions = build_locked_oos_inventory(preparation)
    payload = {
        # The preparation hash transitively binds the exact corpus, split,
        # leakage audit, and lock body without reserializing those large nested
        # immutable objects independently.
        "preparation": (preparation.preparation_id, _content_hash(preparation)),
        "corpus_id": corpus.corpus_id,
        "split_plan_id": plan.split_plan_id,
        "split_policy": (plan.policy.policy_id, _content_hash(plan.policy)),
        "holdout_access": (
            holdout_access_evidence.evidence_id,
            _content_hash(holdout_access_evidence),
        ),
        "code_content_hash_sha256": code_content_hash_sha256,
        "strategy": (
            unit.strategy_id,
            unit.strategy_version,
            unit.direction.value,
            unit.unit_id,
            _content_hash(unit),
        ),
        "policies": (
            (corpus.policy.policy_id, _content_hash(corpus.policy)),
            (metric.policy.policy_id, _content_hash(metric.policy)),
            (robustness_report.policy.policy_id, _content_hash(robustness_report.policy)),
        ),
        "oos_sessions": tuple(item.identity_tuple() for item in sessions),
        "schema_version": "v2.opportunity.semantic_locked_oos_key.v1",
    }
    return hashlib.sha256(contract_to_json(payload).encode("utf-8")).hexdigest()


def locked_oos_authority_key(
    preparation: ChronologicalValidationPreparationReceipt,
    robustness_report: ValidationRobustnessReport,
    *,
    code_content_hash_sha256: str,
) -> str:
    """Freeze the declaration authorities independently of later projections."""

    plan = preparation.audit_receipt.fold_collection.split_plan
    corpus = plan.corpus
    metric = robustness_report.population.metric_report
    unit = robustness_report.policy.confirmatory_unit
    payload = {
        "split_policy": (plan.policy.policy_id, _content_hash(plan.policy)),
        "code_content_hash_sha256": code_content_hash_sha256,
        "strategy": (
            unit.strategy_id,
            unit.strategy_version,
            unit.direction.value,
            unit.unit_id,
            _content_hash(unit),
        ),
        "policies": (
            (corpus.policy.policy_id, _content_hash(corpus.policy)),
            (metric.policy.policy_id, _content_hash(metric.policy)),
            (robustness_report.policy.policy_id, _content_hash(robustness_report.policy)),
        ),
        "schema_version": "v2.opportunity.locked_oos_authority_key.v1",
    }
    return hashlib.sha256(contract_to_json(payload).encode("utf-8")).hexdigest()


def locked_oos_inventory_use_key(
    preparation: ChronologicalValidationPreparationReceipt,
) -> str:
    """Prevent one exact holdout inventory from being consumed under new aliases."""

    sessions = build_locked_oos_inventory(preparation)
    payload = {
        "oos_sessions": tuple(item.identity_tuple() for item in sessions),
        "schema_version": "v2.opportunity.locked_oos_inventory_use_key.v1",
    }
    return hashlib.sha256(contract_to_json(payload).encode("utf-8")).hexdigest()


def locked_oos_eligibility(
    preparation: ChronologicalValidationPreparationReceipt,
    holdout_access_evidence: HoldoutAccessEvidence,
    robustness_report: ValidationRobustnessReport,
) -> tuple[bool, ValidationPersistenceStatus]:
    plan = preparation.audit_receipt.fold_collection.split_plan
    sessions = build_locked_oos_inventory(preparation)
    if (
        not plan.policy.locked_oos_required
        or not sessions
        or len(sessions) != plan.policy.locked_oos_session_count
    ):
        return False, ValidationPersistenceStatus.NON_PREDECLARED
    if plan.holdout_integrity_status is HoldoutIntegrityStatus.RETROSPECTIVE_ONLY:
        return False, ValidationPersistenceStatus.RETROSPECTIVE
    if (
        plan.holdout_integrity_status is HoldoutIntegrityStatus.PREVIOUSLY_EVALUATED
        or holdout_access_evidence.status is HoldoutAccessStatus.PREVIOUSLY_EVALUATED
    ):
        return False, ValidationPersistenceStatus.REUSED
    if plan.holdout_integrity_status in {
        HoldoutIntegrityStatus.CONSUMPTION_UNKNOWN,
        HoldoutIntegrityStatus.UNAVAILABLE,
    }:
        return False, ValidationPersistenceStatus.MISSING_EVIDENCE
    if (
        plan.holdout_integrity_status
        is not HoldoutIntegrityStatus.DECLARED_BEFORE_OOS_NOT_DURABLY_VERIFIED
        or holdout_access_evidence.status is not HoldoutAccessStatus.NO_DURABLE_EVIDENCE
    ):
        return False, ValidationPersistenceStatus.INVALID_LOCK
    first_oos = min(item.session_open_at for item in sessions)
    if (
        plan.policy.declared_at >= first_oos
        or robustness_report.policy.declared_at >= first_oos
    ):
        return False, ValidationPersistenceStatus.RETROSPECTIVE
    observed = {
        item.session_source_id for item in robustness_report.population.observations
    }
    if observed.intersection(item.session_source_id for item in sessions):
        return False, ValidationPersistenceStatus.INVALID_LOCK
    return True, ValidationPersistenceStatus.INVALID_LOCK


def _verify_bundle_bindings(
    preparation: ChronologicalValidationPreparationReceipt,
    metric_report: ValidationTradingMetricReport,
    robustness_report: ValidationRobustnessReport,
    holdout_access_evidence: HoldoutAccessEvidence,
) -> None:
    plan = preparation.audit_receipt.fold_collection.split_plan
    if metric_report.preparation != preparation:
        raise ValueError("validation metric report does not bind exact preparation")
    if robustness_report.population.metric_report != metric_report:
        raise ValueError("validation robustness report does not bind exact metric report")
    if plan.holdout_access_evidence != holdout_access_evidence:
        raise ValueError("holdout access body does not bind exact split plan")
    if (
        not preparation.research_only
        or preparation.promotion_eligible
        or not metric_report.research_only
        or metric_report.promotion_eligible
        or not robustness_report.research_only
        or robustness_report.promotion_eligible
        or robustness_report.lifecycle_mutation_count != 0
        or robustness_report.take_authorization
    ):
        raise ValueError("validation evidence must remain research-only")


def _content_hash(value: OpportunityContract) -> str:
    """Memoize hashes of immutable, already self-verifying validation bodies."""

    key = id(value)
    cached = _CONTENT_HASH_CACHE.get(key)
    if cached is not None and cached[0]() is value:
        return cached[1]
    digest = value.content_hash()

    def discard(reference: ReferenceType[OpportunityContract]) -> None:
        current = _CONTENT_HASH_CACHE.get(key)
        if current is not None and current[0] is reference:
            _CONTENT_HASH_CACHE.pop(key, None)

    reference = ref(value, discard)
    _CONTENT_HASH_CACHE[key] = (reference, digest)
    return digest


__all__ = [
    "LockedOOSSessionBinding",
    "VALIDATION_DATABASE_SCHEMA_VERSION",
    "ValidationPersistenceReceipt",
    "ValidationPersistenceReplay",
    "ValidationPersistenceStatus",
    "build_locked_oos_inventory",
    "build_validation_persistence_receipt",
    "build_validation_persistence_replay",
    "locked_oos_authority_key",
    "locked_oos_eligibility",
    "locked_oos_inventory_use_key",
    "locked_oos_inventory_hash",
    "semantic_locked_oos_key",
    "validation_result_set_hash",
]
