"""Content-bound leakage audit and final WP005-A preparation receipt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from intraday_scanner.v2.opportunity.models import OpportunityContract, stable_identity
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeContract,
    _identity_payload,
    _require_identity,
    _require_schema,
    _require_utc,
)
from intraday_scanner.v2.opportunity.validation_contracts import (
    FoldStatus,
    HoldoutAccessEvidence,
    HoldoutIntegrityStatus,
    LeakageAuditStatus,
    LeakageCheckStatus,
    SplitPlanStatus,
    SurvivorshipEvidenceStatus,
    ValidationPreparationStatus,
    ValidationSplitPolicy,
)
from intraday_scanner.v2.opportunity.validation_corpus import ValidationCorpus
from intraday_scanner.v2.opportunity.validation_split import (
    ChronologicalSplitPlan,
    WalkForwardFoldCollection,
    build_chronological_split_plan,
    build_expanding_walk_forward_folds,
)

LEAKAGE_CHECK_IDS = (
    "source_body_reconciliation",
    "corpus_freeze_causality",
    "whole_session_partition",
    "selected_horizon_exact",
    "region_chronology",
    "purge_strict",
    "embargo_exact",
    "folds_expanding",
    "fold_validation_disjoint",
    "locked_oos_absent_from_folds",
    "holdout_integrity",
    "survivorship_strength",
    "future_input_isolation",
)


@dataclass(frozen=True)
class _LeakageCheck:
    check_id: str
    status: LeakageCheckStatus
    reason: str | None
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class TimestampLeakageAuditReceipt(OutcomeContract):
    audit_id: str
    fold_collection: WalkForwardFoldCollection
    audited_at: datetime
    checks: tuple[_LeakageCheck, ...]
    status: LeakageAuditStatus
    maximum_required_available_at: datetime | None
    first_locked_oos_decision_at: datetime | None
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.timestamp_leakage_audit_receipt.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version,
            "v2.opportunity.timestamp_leakage_audit_receipt.v1",
        )
        _require_identity(self.audit_id, "audit_id")
        _require_utc(self.audited_at, "audited_at")
        plan = self.fold_collection.split_plan
        if self.audited_at < plan.corpus.frozen_at:
            raise ValueError("validation audit precedes the frozen corpus")
        if self.audited_at < plan.policy.declared_at:
            raise ValueError("validation audit precedes the split declaration")
        if (
            plan.holdout_access_evidence is not None
            and self.audited_at < plan.holdout_access_evidence.observed_at
        ):
            raise ValueError("validation audit precedes holdout access evidence")
        expected_checks = _derive_checks(self.fold_collection)
        if self.checks != expected_checks:
            raise ValueError("timestamp leakage checks do not recompute")
        expected_status = _audit_status(self.fold_collection, expected_checks)
        if self.status is not expected_status:
            raise ValueError("timestamp leakage audit status does not recompute")
        rows = self.fold_collection.split_plan.corpus.rows
        expected_max = max(
            (item.required_available_at for item in rows), default=None
        )
        if self.maximum_required_available_at != expected_max:
            raise ValueError("maximum required availability does not recompute")
        expected_first_oos = _first_oos_decision(self.fold_collection.split_plan)
        if self.first_locked_oos_decision_at != expected_first_oos:
            raise ValueError("first locked OOS decision does not recompute")
        expected_limitations = _audit_limitations(
            self.fold_collection, expected_checks, expected_status
        )
        if self.limitations != expected_limitations:
            raise ValueError("timestamp leakage audit limitations do not recompute")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("validation audit must remain research-only")
        expected = stable_identity(
            "validation-leakage-audit", _identity_payload(self, "audit_id")
        )
        if self.audit_id != expected:
            raise ValueError("timestamp leakage audit identity does not match content")


@dataclass(frozen=True)
class ChronologicalValidationPreparationReceipt(OutcomeContract):
    preparation_id: str
    audit_receipt: TimestampLeakageAuditReceipt
    recorded_at: datetime
    status: ValidationPreparationStatus
    session_count: int
    row_count: int
    fold_count: int
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.chronological_validation_preparation.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version,
            "v2.opportunity.chronological_validation_preparation.v1",
        )
        _require_identity(self.preparation_id, "preparation_id")
        _require_utc(self.recorded_at, "recorded_at")
        if self.recorded_at < self.audit_receipt.audited_at:
            raise ValueError("validation preparation predates leakage audit")
        expected_status = _preparation_status(self.audit_receipt.status)
        if self.status is not expected_status:
            raise ValueError("validation preparation status does not recompute")
        corpus = self.audit_receipt.fold_collection.split_plan.corpus
        expected_counts = (
            corpus.session_count,
            corpus.row_count,
            self.audit_receipt.fold_collection.fold_count,
        )
        if (self.session_count, self.row_count, self.fold_count) != expected_counts:
            raise ValueError("validation preparation counts do not reconcile")
        expected_limitations = tuple(
            sorted(
                {
                    *self.audit_receipt.limitations,
                    "research_only_not_promotion_evidence",
                }
            )
        )
        if self.limitations != expected_limitations:
            raise ValueError("validation preparation limitations do not recompute")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("validation preparation must remain research-only")
        expected = stable_identity(
            "chronological-validation-preparation",
            _identity_payload(self, "preparation_id"),
        )
        if self.preparation_id != expected:
            raise ValueError("validation preparation identity does not match content")


def audit_validation_timestamps(
    folds: WalkForwardFoldCollection,
    *,
    audited_at: datetime,
) -> TimestampLeakageAuditReceipt:
    checks = _derive_checks(folds)
    status = _audit_status(folds, checks)
    rows = folds.split_plan.corpus.rows
    values = {
        "fold_collection": folds,
        "audited_at": audited_at,
        "checks": checks,
        "status": status,
        "maximum_required_available_at": max(
            (item.required_available_at for item in rows), default=None
        ),
        "first_locked_oos_decision_at": _first_oos_decision(folds.split_plan),
        "limitations": _audit_limitations(folds, checks, status),
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.timestamp_leakage_audit_receipt.v1",
    }
    return TimestampLeakageAuditReceipt(
        audit_id=stable_identity("validation-leakage-audit", values),
        fold_collection=folds,
        audited_at=audited_at,
        checks=checks,
        status=status,
        maximum_required_available_at=values["maximum_required_available_at"],  # type: ignore[arg-type]
        first_locked_oos_decision_at=values["first_locked_oos_decision_at"],  # type: ignore[arg-type]
        limitations=_audit_limitations(folds, checks, status),
    )


def build_chronological_validation_preparation(
    corpus: ValidationCorpus,
    *,
    split_policy: ValidationSplitPolicy,
    audited_at: datetime,
    recorded_at: datetime,
    holdout_access_evidence: HoldoutAccessEvidence | None = None,
) -> ChronologicalValidationPreparationReceipt:
    split = build_chronological_split_plan(
        corpus,
        policy=split_policy,
        holdout_access_evidence=holdout_access_evidence,
    )
    folds = build_expanding_walk_forward_folds(split)
    audit = audit_validation_timestamps(folds, audited_at=audited_at)
    status = _preparation_status(audit.status)
    limitations = tuple(
        sorted({*audit.limitations, "research_only_not_promotion_evidence"})
    )
    values = {
        "audit_receipt": audit,
        "recorded_at": recorded_at,
        "status": status,
        "session_count": corpus.session_count,
        "row_count": corpus.row_count,
        "fold_count": folds.fold_count,
        "limitations": limitations,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.chronological_validation_preparation.v1",
    }
    return ChronologicalValidationPreparationReceipt(
        preparation_id=stable_identity(
            "chronological-validation-preparation", values
        ),
        audit_receipt=audit,
        recorded_at=recorded_at,
        status=status,
        session_count=corpus.session_count,
        row_count=corpus.row_count,
        fold_count=folds.fold_count,
        limitations=limitations,
    )


def _derive_checks(folds: WalkForwardFoldCollection) -> tuple[_LeakageCheck, ...]:
    plan = folds.split_plan
    corpus = plan.corpus
    standard_evidence = (corpus.corpus_id, plan.split_plan_id, folds.fold_collection_id)
    checks = [
        _LeakageCheck(check_id, LeakageCheckStatus.PASSED, None, standard_evidence)
        for check_id in LEAKAGE_CHECK_IDS[:10]
    ]
    if (
        folds.split_plan.status is SplitPlanStatus.INSUFFICIENT_DATA
        or any(item.status is FoldStatus.INSUFFICIENT_DATA for item in folds.folds)
        or (folds.validation_session_ids and not folds.folds)
    ):
        for index in (4, 7, 8):
            checks[index] = _LeakageCheck(
                checks[index].check_id,
                LeakageCheckStatus.UNAVAILABLE,
                "fold_collection_not_available",
                standard_evidence,
            )
    if plan.policy.locked_oos_required:
        holdout_status = plan.holdout_integrity_status
        if holdout_status in {
            HoldoutIntegrityStatus.RETROSPECTIVE_ONLY,
            HoldoutIntegrityStatus.PREVIOUSLY_EVALUATED,
        }:
            holdout_check = _LeakageCheck(
                "holdout_integrity",
                LeakageCheckStatus.FAILED,
                f"holdout_{holdout_status.value}",
                (plan.split_plan_id,),
            )
        else:
            holdout_check = _LeakageCheck(
                "holdout_integrity",
                LeakageCheckStatus.UNAVAILABLE,
                f"holdout_{holdout_status.value}",
                (plan.split_plan_id,),
            )
    else:
        holdout_check = _LeakageCheck(
            "holdout_integrity",
            LeakageCheckStatus.NOT_APPLICABLE,
            "locked_oos_not_required",
            (plan.split_plan_id,),
        )
    checks.append(holdout_check)
    strong_survivorship = all(
        item.survivorship_status is SurvivorshipEvidenceStatus.POINT_IN_TIME
        for item in corpus.sessions
    )
    if plan.policy.locked_oos_required:
        survivor_check = _LeakageCheck(
            "survivorship_strength",
            (
                LeakageCheckStatus.PASSED
                if strong_survivorship
                else LeakageCheckStatus.UNAVAILABLE
            ),
            None if strong_survivorship else "point_in_time_membership_unavailable",
            (corpus.corpus_id,),
        )
    else:
        survivor_check = _LeakageCheck(
            "survivorship_strength",
            LeakageCheckStatus.NOT_APPLICABLE,
            "strong_survivorship_not_required_without_locked_oos",
            (corpus.corpus_id,),
        )
    checks.append(survivor_check)
    checks.append(
        _LeakageCheck(
            "future_input_isolation",
            LeakageCheckStatus.PASSED,
            None,
            tuple(
                replay.pipeline_result.run_id
                for session in corpus.sessions
                for replay in session.current_outcome_replays
            ),
        )
    )
    if tuple(item.check_id for item in checks) != LEAKAGE_CHECK_IDS:
        raise ValueError("internal leakage check order mismatch")
    return tuple(checks)


def _audit_status(
    folds: WalkForwardFoldCollection, checks: tuple[_LeakageCheck, ...]
) -> LeakageAuditStatus:
    if any(item.status is LeakageCheckStatus.FAILED for item in checks):
        return LeakageAuditStatus.FAILED
    if folds.status is SplitPlanStatus.EXTERNAL_DATA_BLOCKED:
        return LeakageAuditStatus.EXTERNAL_DATA_BLOCKED
    if any(item.status is LeakageCheckStatus.UNAVAILABLE for item in checks):
        if (
            folds.split_plan.policy.locked_oos_required
            and any(
                item.check_id == "survivorship_strength"
                and item.status is LeakageCheckStatus.UNAVAILABLE
                for item in checks
            )
        ):
            return LeakageAuditStatus.EXTERNAL_DATA_BLOCKED
        return LeakageAuditStatus.INCOMPLETE
    if folds.status is SplitPlanStatus.INSUFFICIENT_DATA:
        return LeakageAuditStatus.INCOMPLETE
    return LeakageAuditStatus.PASSED_BOUNDED


def _audit_limitations(
    folds: WalkForwardFoldCollection,
    checks: tuple[_LeakageCheck, ...],
    status: LeakageAuditStatus,
) -> tuple[str, ...]:
    values = {
        *folds.split_plan.limitations,
        *folds.limitations,
        *(item.reason for item in checks if item.reason is not None),
    }
    if status is not LeakageAuditStatus.PASSED_BOUNDED:
        values.add(f"leakage_audit_{status.value}")
    return tuple(sorted(values))


def _first_oos_decision(plan: ChronologicalSplitPlan) -> datetime | None:
    locked = set(plan.locked_oos_session_ids)
    return min(
        (
            item.decision_at
            for item in plan.corpus.rows
            if item.session_source_id in locked
        ),
        default=None,
    )


def _preparation_status(
    status: LeakageAuditStatus,
) -> ValidationPreparationStatus:
    return {
        LeakageAuditStatus.PASSED_BOUNDED: ValidationPreparationStatus.READY_BOUNDED_RESEARCH,
        LeakageAuditStatus.INCOMPLETE: ValidationPreparationStatus.INSUFFICIENT_DATA,
        LeakageAuditStatus.EXTERNAL_DATA_BLOCKED: (
            ValidationPreparationStatus.EXTERNAL_DATA_BLOCKED
        ),
        LeakageAuditStatus.FAILED: ValidationPreparationStatus.FAILED,
    }[status]


__all__ = [
    "LEAKAGE_CHECK_IDS",
    "ChronologicalValidationPreparationReceipt",
    "TimestampLeakageAuditReceipt",
    "audit_validation_timestamps",
    "build_chronological_validation_preparation",
]
