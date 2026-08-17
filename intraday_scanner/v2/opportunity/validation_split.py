"""Deterministic whole-session splits and expanding walk-forward folds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from intraday_scanner.v2.opportunity.models import OpportunityContract, stable_identity
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeContract,
    _identity_payload,
    _require_identity,
    _require_schema,
)
from intraday_scanner.v2.opportunity.validation_contracts import (
    DeclaredRegion,
    FoldStatus,
    HoldoutAccessEvidence,
    HoldoutAccessStatus,
    HoldoutIntegrityStatus,
    SplitPlanStatus,
    SplitRole,
    ValidationCorpusStatus,
    ValidationSplitPolicy,
)
from intraday_scanner.v2.opportunity.validation_corpus import (
    ValidationCorpus,
    ValidationSessionSource,
    _ValidationCorpusRow,
)


@dataclass(frozen=True)
class _ValidationSessionAllocation:
    ordinal: int
    session_source_id: str
    session_content_hash_sha256: str
    exchange_session_id: str
    declared_region: DeclaredRegion
    role: SplitRole
    reason: str | None
    row_ids: tuple[str, ...]


@dataclass(frozen=True)
class ChronologicalSplitPlan(OutcomeContract):
    split_plan_id: str
    corpus: ValidationCorpus
    policy: ValidationSplitPolicy
    holdout_access_evidence: HoldoutAccessEvidence | None
    allocations: tuple[_ValidationSessionAllocation, ...]
    locked_oos_session_ids: tuple[str, ...]
    holdout_integrity_status: HoldoutIntegrityStatus
    status: SplitPlanStatus
    train_research_session_count: int
    validation_session_count: int
    locked_oos_session_count: int
    purged_session_count: int
    embargoed_session_count: int
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.chronological_split_plan.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.chronological_split_plan.v1")
        _require_identity(self.split_plan_id, "split_plan_id")
        expected_allocations = _derive_allocations(self.corpus, self.policy)
        if self.allocations != expected_allocations:
            raise ValueError("chronological session allocations do not recompute")
        expected_locked = tuple(
            item.session_source_id
            for item in expected_allocations
            if item.role is SplitRole.LOCKED_OOS
        )
        if self.locked_oos_session_ids != expected_locked:
            raise ValueError("locked OOS session inventory does not recompute")
        expected_holdout = _holdout_integrity(
            self.corpus,
            self.policy,
            expected_allocations,
            self.holdout_access_evidence,
        )
        if self.holdout_integrity_status is not expected_holdout:
            raise ValueError("holdout integrity status does not recompute")
        expected_status = _split_status(
            self.corpus, self.policy, expected_allocations
        )
        if self.status is not expected_status:
            raise ValueError("chronological split status does not recompute")
        expected_counts = _allocation_counts(expected_allocations)
        actual_counts = (
            self.train_research_session_count,
            self.validation_session_count,
            self.locked_oos_session_count,
            self.purged_session_count,
            self.embargoed_session_count,
        )
        if actual_counts != expected_counts:
            raise ValueError("chronological split counts do not reconcile")
        expected_limitations = _split_limitations(
            self.corpus,
            self.policy,
            expected_allocations,
            expected_holdout,
            expected_status,
        )
        if self.limitations != expected_limitations:
            raise ValueError("chronological split limitations do not recompute")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("chronological split must remain research-only")
        expected = stable_identity(
            "chronological-split-plan", _identity_payload(self, "split_plan_id")
        )
        if self.split_plan_id != expected:
            raise ValueError("chronological split plan identity does not match content")


@dataclass(frozen=True)
class _ExpandingWalkForwardFold:
    fold_id: str
    ordinal: int
    train_session_ids: tuple[str, ...]
    train_session_content_hashes: tuple[str, ...]
    validation_session_ids: tuple[str, ...]
    validation_session_content_hashes: tuple[str, ...]
    purged_session_ids: tuple[str, ...]
    embargoed_session_ids: tuple[str, ...]
    train_row_ids: tuple[str, ...]
    validation_row_ids: tuple[str, ...]
    first_validation_decision_at: datetime
    actual_validation_session_count: int
    status: FoldStatus


@dataclass(frozen=True)
class WalkForwardFoldCollection(OutcomeContract):
    fold_collection_id: str
    split_plan: ChronologicalSplitPlan
    folds: tuple[_ExpandingWalkForwardFold, ...]
    validation_session_ids: tuple[str, ...]
    status: SplitPlanStatus
    fold_count: int
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.walk_forward_fold_collection.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version, "v2.opportunity.walk_forward_fold_collection.v1"
        )
        _require_identity(self.fold_collection_id, "fold_collection_id")
        expected_folds = _derive_folds(self.split_plan)
        if self.folds != expected_folds:
            raise ValueError("walk-forward folds do not recompute")
        expected_validation = tuple(
            item.session_source_id
            for item in self.split_plan.allocations
            if item.role is SplitRole.VALIDATION
        )
        if self.validation_session_ids != expected_validation:
            raise ValueError("fold validation inventory does not match split plan")
        flattened = tuple(
            session_id for fold in self.folds for session_id in fold.validation_session_ids
        )
        if flattened != expected_validation:
            raise ValueError("fold windows must cover every validation session exactly once")
        if len(flattened) != len(set(flattened)):
            raise ValueError("fold validation sessions cannot repeat")
        locked = set(self.split_plan.locked_oos_session_ids)
        if any(
            locked.intersection(
                (*fold.train_session_ids, *fold.validation_session_ids)
            )
            for fold in self.folds
        ):
            raise ValueError("locked OOS session appears in a walk-forward fold")
        expected_status = _fold_collection_status(self.split_plan, self.folds)
        if self.status is not expected_status:
            raise ValueError("fold collection status does not recompute")
        if self.fold_count != len(self.folds):
            raise ValueError("fold count does not reconcile")
        expected_limitations = _fold_limitations(expected_status, self.folds)
        if self.limitations != expected_limitations:
            raise ValueError("fold collection limitations do not recompute")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("walk-forward folds must remain research-only")
        expected = stable_identity(
            "walk-forward-folds", _identity_payload(self, "fold_collection_id")
        )
        if self.fold_collection_id != expected:
            raise ValueError("fold collection identity does not match content")


def build_chronological_split_plan(
    corpus: ValidationCorpus,
    *,
    policy: ValidationSplitPolicy,
    holdout_access_evidence: HoldoutAccessEvidence | None = None,
) -> ChronologicalSplitPlan:
    allocations = _derive_allocations(corpus, policy)
    holdout = _holdout_integrity(
        corpus, policy, allocations, holdout_access_evidence
    )
    status = _split_status(corpus, policy, allocations)
    counts = _allocation_counts(allocations)
    locked_oos_ids = tuple(
        item.session_source_id
        for item in allocations
        if item.role is SplitRole.LOCKED_OOS
    )
    values = {
        "corpus": corpus,
        "policy": policy,
        "holdout_access_evidence": holdout_access_evidence,
        "allocations": allocations,
        "locked_oos_session_ids": locked_oos_ids,
        "holdout_integrity_status": holdout,
        "status": status,
        "train_research_session_count": counts[0],
        "validation_session_count": counts[1],
        "locked_oos_session_count": counts[2],
        "purged_session_count": counts[3],
        "embargoed_session_count": counts[4],
        "limitations": _split_limitations(
            corpus, policy, allocations, holdout, status
        ),
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.chronological_split_plan.v1",
    }
    return ChronologicalSplitPlan(
        split_plan_id=stable_identity("chronological-split-plan", values),
        corpus=corpus,
        policy=policy,
        holdout_access_evidence=holdout_access_evidence,
        allocations=allocations,
        locked_oos_session_ids=locked_oos_ids,
        holdout_integrity_status=holdout,
        status=status,
        train_research_session_count=counts[0],
        validation_session_count=counts[1],
        locked_oos_session_count=counts[2],
        purged_session_count=counts[3],
        embargoed_session_count=counts[4],
        limitations=_split_limitations(corpus, policy, allocations, holdout, status),
    )


def build_expanding_walk_forward_folds(
    split_plan: ChronologicalSplitPlan,
) -> WalkForwardFoldCollection:
    folds = _derive_folds(split_plan)
    validation_ids = tuple(
        item.session_source_id
        for item in split_plan.allocations
        if item.role is SplitRole.VALIDATION
    )
    status = _fold_collection_status(split_plan, folds)
    values = {
        "split_plan": split_plan,
        "folds": folds,
        "validation_session_ids": validation_ids,
        "status": status,
        "fold_count": len(folds),
        "limitations": _fold_limitations(status, folds),
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.walk_forward_fold_collection.v1",
    }
    return WalkForwardFoldCollection(
        fold_collection_id=stable_identity("walk-forward-folds", values),
        split_plan=split_plan,
        folds=folds,
        validation_session_ids=validation_ids,
        status=status,
        fold_count=len(folds),
        limitations=_fold_limitations(status, folds),
    )


def _derive_allocations(
    corpus: ValidationCorpus, policy: ValidationSplitPolicy
) -> tuple[_ValidationSessionAllocation, ...]:
    sessions = corpus.sessions
    expected_count = (
        policy.train_research_session_count
        + policy.validation_session_count
        + policy.locked_oos_session_count
    )
    if expected_count != len(sessions):
        raise ValueError("split region counts must exactly cover corpus sessions")
    train_end = policy.train_research_session_count
    validation_end = train_end + policy.validation_session_count
    train = sessions[:train_end]
    validation = sessions[train_end:validation_end]
    oos = sessions[validation_end:]
    declared = {
        item.session_source_id: DeclaredRegion.TRAIN_RESEARCH for item in train
    }
    declared.update(
        {item.session_source_id: DeclaredRegion.VALIDATION for item in validation}
    )
    declared.update({item.session_source_id: DeclaredRegion.LOCKED_OOS for item in oos})
    embargoed = {
        item.session_source_id
        for region in (train, validation)
        for item in _last_n(region, policy.region_embargo_session_count)
    }
    train_candidates = tuple(item for item in train if item.session_source_id not in embargoed)
    validation_candidates = tuple(
        item for item in validation if item.session_source_id not in embargoed
    )
    purged = {
        item.session_source_id
        for item in _purged_sessions(train_candidates, validation_candidates, corpus)
    }
    purged.update(
        item.session_source_id
        for item in _purged_sessions(validation_candidates, oos, corpus)
    )
    rows_by_session = _rows_by_session(corpus)
    allocations: list[_ValidationSessionAllocation] = []
    for ordinal, session in enumerate(sessions, start=1):
        region = declared[session.session_source_id]
        reason: str | None = None
        if session.session_source_id in embargoed:
            role = SplitRole.EMBARGOED
            reason = "declared_positional_embargo"
        elif session.session_source_id in purged:
            role = SplitRole.PURGED
            reason = "label_or_availability_overlaps_next_region"
        else:
            role = SplitRole(region.value)
        allocations.append(
            _ValidationSessionAllocation(
                ordinal=ordinal,
                session_source_id=session.session_source_id,
                session_content_hash_sha256=session.content_hash(),
                exchange_session_id=session.exchange_session_id,
                declared_region=region,
                role=role,
                reason=reason,
                row_ids=tuple(item.row_id for item in rows_by_session[session.session_source_id]),
            )
        )
    return tuple(allocations)


def _purged_sessions(
    earlier: tuple[ValidationSessionSource, ...],
    later: tuple[ValidationSessionSource, ...],
    corpus: ValidationCorpus,
) -> tuple[ValidationSessionSource, ...]:
    if not earlier or not later:
        return ()
    rows_by_session = _rows_by_session(corpus)
    later_rows = tuple(
        row for session in later for row in rows_by_session[session.session_source_id]
    )
    if not later_rows:
        return ()
    first_decision = min(item.decision_at for item in later_rows)
    result = []
    for session in earlier:
        rows = rows_by_session[session.session_source_id]
        if not rows or max(
            max(item.label_end_at, item.required_available_at) for item in rows
        ) >= first_decision:
            result.append(session)
    return tuple(result)


def _derive_folds(
    plan: ChronologicalSplitPlan,
) -> tuple[_ExpandingWalkForwardFold, ...]:
    session_by_id = {item.session_source_id: item for item in plan.corpus.sessions}
    base_train = tuple(
        session_by_id[item.session_source_id]
        for item in plan.allocations
        if item.role is SplitRole.TRAIN_RESEARCH
    )
    validation = tuple(
        session_by_id[item.session_source_id]
        for item in plan.allocations
        if item.role is SplitRole.VALIDATION
    )
    rows_by_session = _rows_by_session(plan.corpus)
    window = plan.policy.validation_window_session_count
    folds: list[_ExpandingWalkForwardFold] = []
    for start in range(0, len(validation), window):
        validation_window = validation[start : start + window]
        previous_validation = validation[:start]
        raw_train = (*base_train, *previous_validation)
        fold_embargo = _last_n(raw_train, plan.policy.fold_embargo_session_count)
        embargo_ids = {item.session_source_id for item in fold_embargo}
        candidates = tuple(
            item for item in raw_train if item.session_source_id not in embargo_ids
        )
        purged = _purged_sessions(candidates, validation_window, plan.corpus)
        purged_ids = {item.session_source_id for item in purged}
        train = tuple(
            item for item in candidates if item.session_source_id not in purged_ids
        )
        validation_rows = tuple(
            row
            for item in validation_window
            for row in rows_by_session[item.session_source_id]
        )
        if not validation_rows:
            first_decision = validation_window[0].session_open_at
        else:
            first_decision = min(item.decision_at for item in validation_rows)
        status = (
            FoldStatus.AVAILABLE
            if len(train) >= plan.policy.minimum_fold_training_sessions
            and bool(validation_rows)
            else FoldStatus.INSUFFICIENT_DATA
        )
        values = {
            "ordinal": len(folds) + 1,
            "train_session_ids": tuple(item.session_source_id for item in train),
            "train_session_content_hashes": tuple(item.content_hash() for item in train),
            "validation_session_ids": tuple(
                item.session_source_id for item in validation_window
            ),
            "validation_session_content_hashes": tuple(
                item.content_hash() for item in validation_window
            ),
            "purged_session_ids": tuple(item.session_source_id for item in purged),
            "embargoed_session_ids": tuple(
                item.session_source_id for item in fold_embargo
            ),
            "train_row_ids": tuple(
                row.row_id for item in train for row in rows_by_session[item.session_source_id]
            ),
            "validation_row_ids": tuple(item.row_id for item in validation_rows),
            "first_validation_decision_at": first_decision,
            "actual_validation_session_count": len(validation_window),
            "status": status,
        }
        folds.append(
            _ExpandingWalkForwardFold(
                fold_id=stable_identity("validation-fold", values),
                ordinal=len(folds) + 1,
                train_session_ids=tuple(item.session_source_id for item in train),
                train_session_content_hashes=tuple(
                    item.content_hash() for item in train
                ),
                validation_session_ids=tuple(
                    item.session_source_id for item in validation_window
                ),
                validation_session_content_hashes=tuple(
                    item.content_hash() for item in validation_window
                ),
                purged_session_ids=tuple(item.session_source_id for item in purged),
                embargoed_session_ids=tuple(
                    item.session_source_id for item in fold_embargo
                ),
                train_row_ids=tuple(
                    row.row_id
                    for item in train
                    for row in rows_by_session[item.session_source_id]
                ),
                validation_row_ids=tuple(item.row_id for item in validation_rows),
                first_validation_decision_at=first_decision,
                actual_validation_session_count=len(validation_window),
                status=status,
            )
        )
    return tuple(folds)


def _holdout_integrity(
    corpus: ValidationCorpus,
    policy: ValidationSplitPolicy,
    allocations: tuple[_ValidationSessionAllocation, ...],
    evidence: HoldoutAccessEvidence | None,
) -> HoldoutIntegrityStatus:
    oos_ids = {
        item.session_source_id
        for item in allocations
        if item.role is SplitRole.LOCKED_OOS
    }
    rows = tuple(item for item in corpus.rows if item.session_source_id in oos_ids)
    if not rows:
        return HoldoutIntegrityStatus.UNAVAILABLE
    first_event = min(item.decision_at for item in rows)
    if policy.declared_at >= first_event:
        return HoldoutIntegrityStatus.RETROSPECTIVE_ONLY
    if evidence is None or evidence.status is HoldoutAccessStatus.NO_DURABLE_EVIDENCE:
        return HoldoutIntegrityStatus.DECLARED_BEFORE_OOS_NOT_DURABLY_VERIFIED
    if evidence.status is HoldoutAccessStatus.PREVIOUSLY_EVALUATED:
        return HoldoutIntegrityStatus.PREVIOUSLY_EVALUATED
    if evidence.status is HoldoutAccessStatus.UNKNOWN:
        return HoldoutIntegrityStatus.CONSUMPTION_UNKNOWN
    return HoldoutIntegrityStatus.UNAVAILABLE


def _split_status(
    corpus: ValidationCorpus,
    policy: ValidationSplitPolicy,
    allocations: tuple[_ValidationSessionAllocation, ...],
) -> SplitPlanStatus:
    if (
        policy.locked_oos_required
        and corpus.status is ValidationCorpusStatus.EXTERNAL_DATA_BLOCKED
    ):
        return SplitPlanStatus.EXTERNAL_DATA_BLOCKED
    if corpus.status in {
        ValidationCorpusStatus.EMPTY,
        ValidationCorpusStatus.INCOMPLETE,
    }:
        return SplitPlanStatus.INSUFFICIENT_DATA
    available = {
        role: sum(item.role is role for item in allocations)
        for role in (
            SplitRole.TRAIN_RESEARCH,
            SplitRole.VALIDATION,
            SplitRole.LOCKED_OOS,
        )
    }
    required = (
        (policy.train_research_required, SplitRole.TRAIN_RESEARCH),
        (policy.validation_required, SplitRole.VALIDATION),
        (policy.locked_oos_required, SplitRole.LOCKED_OOS),
    )
    if any(is_required and available[role] == 0 for is_required, role in required):
        return SplitPlanStatus.INSUFFICIENT_DATA
    if any(not item.row_ids for item in allocations if item.role in available):
        return SplitPlanStatus.INSUFFICIENT_DATA
    return SplitPlanStatus.AVAILABLE


def _allocation_counts(
    allocations: tuple[_ValidationSessionAllocation, ...],
) -> tuple[int, int, int, int, int]:
    return tuple(
        sum(item.role is role for item in allocations)
        for role in (
            SplitRole.TRAIN_RESEARCH,
            SplitRole.VALIDATION,
            SplitRole.LOCKED_OOS,
            SplitRole.PURGED,
            SplitRole.EMBARGOED,
        )
    )  # type: ignore[return-value]


def _split_limitations(
    corpus: ValidationCorpus,
    policy: ValidationSplitPolicy,
    allocations: tuple[_ValidationSessionAllocation, ...],
    holdout: HoldoutIntegrityStatus,
    status: SplitPlanStatus,
) -> tuple[str, ...]:
    values = set(corpus.limitations)
    if status is not SplitPlanStatus.AVAILABLE:
        values.add(f"split_{status.value}")
    if policy.locked_oos_required:
        values.add(f"holdout_{holdout.value}")
    if any(item.role is SplitRole.PURGED for item in allocations):
        values.add("whole_session_purge_applied")
    if any(item.role is SplitRole.EMBARGOED for item in allocations):
        values.add("positional_session_embargo_applied")
    return tuple(sorted(values))


def _fold_collection_status(
    plan: ChronologicalSplitPlan, folds: tuple[_ExpandingWalkForwardFold, ...]
) -> SplitPlanStatus:
    if plan.status is not SplitPlanStatus.AVAILABLE:
        return plan.status
    if not folds or any(item.status is FoldStatus.INSUFFICIENT_DATA for item in folds):
        return SplitPlanStatus.INSUFFICIENT_DATA
    return SplitPlanStatus.AVAILABLE


def _fold_limitations(
    status: SplitPlanStatus, folds: tuple[_ExpandingWalkForwardFold, ...]
) -> tuple[str, ...]:
    values: set[str] = set()
    if status is not SplitPlanStatus.AVAILABLE:
        values.add(f"fold_collection_{status.value}")
    if any(item.actual_validation_session_count == 0 for item in folds):
        values.add("empty_validation_fold")
    return tuple(sorted(values))


def _rows_by_session(
    corpus: ValidationCorpus,
) -> dict[str, tuple[_ValidationCorpusRow, ...]]:
    result: dict[str, tuple[_ValidationCorpusRow, ...]] = {}
    for session in corpus.sessions:
        result[session.session_source_id] = tuple(
            item for item in corpus.rows if item.session_source_id == session.session_source_id
        )
    return result


def _last_n(
    values: tuple[ValidationSessionSource, ...], count: int
) -> tuple[ValidationSessionSource, ...]:
    if count <= 0:
        return ()
    return values[-count:]


__all__ = [
    "ChronologicalSplitPlan",
    "WalkForwardFoldCollection",
    "build_chronological_split_plan",
    "build_expanding_walk_forward_folds",
]
