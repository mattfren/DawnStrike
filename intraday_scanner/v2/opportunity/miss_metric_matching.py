"""Exact strategy-agnostic matching evidence for discovery metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from intraday_scanner.v2.opportunity.miss_contracts import (
    MissContract,
    QualificationClaimKind,
    QualificationStatus,
    identity_payload,
    require_aware,
    require_hash,
    require_identity,
    require_sanitized,
    require_schema,
    require_unique,
)
from intraday_scanner.v2.opportunity.miss_metric_contracts import (
    DiscoveryMetricPolicy,
)
from intraday_scanner.v2.opportunity.miss_qualification import (
    HindsightQualifiedOpportunity,
    QualificationAssessment,
)
from intraday_scanner.v2.opportunity.miss_reconciliation import (
    MissReconciliationBatch,
)
from intraday_scanner.v2.opportunity.models import (
    StrategyDirection,
    TradeDecisionValue,
    stable_identity,
)

_POSITIVE_DECISIONS = {TradeDecisionValue.WATCH, TradeDecisionValue.TAKE}


@dataclass(frozen=True)
class _DiscoveryPredictionEvidence(MissContract):
    prediction_evidence_id: str
    run_id: str
    run_content_hash_sha256: str
    decision_at: datetime
    symbol: str
    direction: StrategyDirection
    evaluation_id: str
    evaluation_content_hash_sha256: str
    ranked_id: str | None
    ranked_content_hash_sha256: str | None
    rank_position: int | None
    decision_id: str | None
    decision_content_hash_sha256: str | None
    decision_value: TradeDecisionValue | None
    on_time: bool
    schema_version: str = "v2.opportunity.discovery_prediction_evidence.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.discovery_prediction_evidence.v1",
        )
        for value, name in (
            (self.prediction_evidence_id, "prediction_evidence_id"),
            (self.run_id, "run_id"),
            (self.symbol, "symbol"),
            (self.evaluation_id, "evaluation_id"),
        ):
            require_identity(value, name)
        for value, name in (
            (self.run_content_hash_sha256, "run_content_hash_sha256"),
            (self.evaluation_content_hash_sha256, "evaluation_content_hash_sha256"),
        ):
            require_hash(value, name)
        require_aware(self.decision_at, "prediction decision_at")
        _require_optional_hash_pair(
            self.ranked_id,
            self.ranked_content_hash_sha256,
            "ranked",
        )
        _require_optional_hash_pair(
            self.decision_id,
            self.decision_content_hash_sha256,
            "decision",
        )
        if (self.rank_position is None) is not (self.ranked_id is None):
            raise ValueError("prediction rank identity and position must be paired")
        if self.rank_position is not None and (
            isinstance(self.rank_position, bool) or self.rank_position <= 0
        ):
            raise ValueError("prediction rank position must be positive")
        if (self.decision_value is None) is not (self.decision_id is None):
            raise ValueError("prediction decision identity and value must be paired")
        if self.decision_value is not None and self.decision_value not in _POSITIVE_DECISIONS:
            raise ValueError("prediction evidence retains only WATCH or TAKE decisions")
        if self.ranked_id is None and self.decision_id is None:
            raise ValueError("prediction evidence requires rank or WATCH/TAKE proof")
        if type(self.on_time) is not bool:
            raise ValueError("prediction on_time must be a strict boolean")
        expected = stable_identity(
            "discovery-prediction-evidence",
            identity_payload(self, "prediction_evidence_id"),
        )
        if self.prediction_evidence_id != expected:
            raise ValueError("prediction evidence identity does not match content")


@dataclass(frozen=True)
class _DiscoveryMetricUnitEvidence(MissContract):
    unit_evidence_id: str
    session_opportunity_key: str
    assessment_id: str
    assessment_content_hash_sha256: str
    assessment: QualificationAssessment
    opportunity_id: str | None
    opportunity_content_hash_sha256: str | None
    opportunity: HindsightQualifiedOpportunity | None
    symbol: str
    direction: StrategyDirection
    horizon_id: str
    latest_useful_cutoff_at: datetime
    qualification_status: QualificationStatus
    claim_kind: QualificationClaimKind
    predictions: tuple[_DiscoveryPredictionEvidence, ...]
    best_on_time_rank_position: int | None
    on_time_watch_or_take: bool
    any_watch_or_take: bool
    schema_version: str = "v2.opportunity.discovery_metric_unit_evidence.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.discovery_metric_unit_evidence.v1",
        )
        for value, name in (
            (self.unit_evidence_id, "unit_evidence_id"),
            (self.session_opportunity_key, "session_opportunity_key"),
            (self.assessment_id, "assessment_id"),
            (self.symbol, "symbol"),
            (self.horizon_id, "horizon_id"),
        ):
            require_identity(value, name)
        require_hash(
            self.assessment_content_hash_sha256,
            "assessment_content_hash_sha256",
        )
        _require_optional_hash_pair(
            self.opportunity_id,
            self.opportunity_content_hash_sha256,
            "opportunity",
        )
        if (self.opportunity_id is None) is not (self.opportunity is None):
            raise ValueError("metric unit opportunity identity and body must be paired")
        require_aware(self.latest_useful_cutoff_at, "latest_useful_cutoff_at")
        if self.direction not in {StrategyDirection.LONG, StrategyDirection.SHORT}:
            raise ValueError("metric unit requires exact LONG or SHORT direction")
        if self.predictions != tuple(sorted(self.predictions, key=_prediction_sort_key)):
            raise ValueError("metric unit predictions must use canonical order")
        require_unique(
            tuple(item.prediction_evidence_id for item in self.predictions),
            "prediction evidence",
        )
        if self.best_on_time_rank_position is not None and (
            isinstance(self.best_on_time_rank_position, bool)
            or self.best_on_time_rank_position <= 0
        ):
            raise ValueError("best on-time rank must be positive")
        if type(self.on_time_watch_or_take) is not bool or type(self.any_watch_or_take) is not bool:
            raise ValueError("metric unit surfacing flags must be strict booleans")
        if self.on_time_watch_or_take and not self.any_watch_or_take:
            raise ValueError("on-time WATCH/TAKE requires all-session WATCH/TAKE")
        if (
            self.session_opportunity_key != self.assessment.session_opportunity_key
            or self.assessment_id != self.assessment.assessment_id
            or self.assessment_content_hash_sha256 != self.assessment.content_hash()
            or self.symbol != self.assessment.member.symbol
            or self.direction is not self.assessment.direction
            or self.horizon_id != self.assessment.horizon.horizon_id
            or self.latest_useful_cutoff_at
            != self.assessment.latest_useful_cutoff_at
            or self.qualification_status is not self.assessment.status
            or self.claim_kind is not self.assessment.claim_kind
        ):
            raise ValueError("metric unit projections do not match assessment")
        if self.qualification_status is QualificationStatus.QUALIFIED:
            if self.opportunity is None:
                raise ValueError("qualified metric unit requires exact opportunity")
            if (
                self.opportunity_id != self.opportunity.opportunity_id
                or self.opportunity_content_hash_sha256
                != self.opportunity.content_hash()
                or self.opportunity.assessment != self.assessment
            ):
                raise ValueError("metric unit opportunity does not match assessment")
        elif self.opportunity is not None:
            raise ValueError("non-qualified metric unit cannot carry opportunity")
        if any(
            item.symbol != self.symbol or item.direction is not self.direction
            for item in self.predictions
        ):
            raise ValueError("metric unit predictions do not match symbol/direction")
        if any(
            item.on_time is not (item.decision_at < self.latest_useful_cutoff_at)
            for item in self.predictions
        ):
            raise ValueError("metric unit prediction cutoff flags do not recompute")
        on_time_ranks = tuple(
            item.rank_position
            for item in self.predictions
            if item.on_time and item.rank_position is not None
        )
        if self.best_on_time_rank_position != (
            min(on_time_ranks) if on_time_ranks else None
        ):
            raise ValueError("metric unit best on-time rank does not recompute")
        if self.on_time_watch_or_take is not any(
            item.on_time and item.decision_value in _POSITIVE_DECISIONS
            for item in self.predictions
        ):
            raise ValueError("metric unit on-time WATCH/TAKE flag does not recompute")
        if self.any_watch_or_take is not any(
            item.decision_value in _POSITIVE_DECISIONS for item in self.predictions
        ):
            raise ValueError("metric unit all-session WATCH/TAKE flag does not recompute")
        expected = stable_identity(
            "discovery-metric-unit-evidence",
            identity_payload(self, "unit_evidence_id"),
        )
        if self.unit_evidence_id != expected:
            raise ValueError("metric unit evidence identity does not match content")


@dataclass(frozen=True)
class DiscoveryMetricSessionEvidence(MissContract):
    session_evidence_id: str
    metric_policy_id: str
    metric_policy_content_hash_sha256: str
    metric_policy: DiscoveryMetricPolicy
    miss_batch_id: str
    miss_batch_content_hash_sha256: str
    miss_batch: MissReconciliationBatch
    selected_horizon_id: str
    selected_horizon_content_hash_sha256: str
    units: tuple[_DiscoveryMetricUnitEvidence, ...]
    unmatched_predictions: tuple[_DiscoveryPredictionEvidence, ...]
    recorded_at: datetime
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.discovery_metric_session_evidence.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.discovery_metric_session_evidence.v1",
        )
        for value, name in (
            (self.session_evidence_id, "session_evidence_id"),
            (self.metric_policy_id, "metric_policy_id"),
            (self.miss_batch_id, "miss_batch_id"),
            (self.selected_horizon_id, "selected_horizon_id"),
        ):
            require_identity(value, name)
        for value, name in (
            (self.metric_policy_content_hash_sha256, "metric_policy_content_hash_sha256"),
            (self.miss_batch_content_hash_sha256, "miss_batch_content_hash_sha256"),
            (
                self.selected_horizon_content_hash_sha256,
                "selected_horizon_content_hash_sha256",
            ),
        ):
            require_hash(value, name)
        require_aware(self.recorded_at, "metric session recorded_at")
        if (
            self.metric_policy_id != self.metric_policy.metric_policy_id
            or self.metric_policy_content_hash_sha256 != self.metric_policy.content_hash()
            or self.miss_batch_id != self.miss_batch.batch_id
            or self.miss_batch_content_hash_sha256 != self.miss_batch.content_hash()
        ):
            raise ValueError("metric session evidence embedded bindings are inconsistent")
        expected = _resolve_session_evidence(self.miss_batch, self.metric_policy)
        _compare_session_evidence(self, expected)
        expected_id = stable_identity(
            "discovery-metric-session-evidence",
            identity_payload(self, "session_evidence_id"),
        )
        if self.session_evidence_id != expected_id:
            raise ValueError("metric session evidence identity does not match content")


def build_discovery_metric_session_evidence(
    miss_batch: MissReconciliationBatch,
    *,
    policy: DiscoveryMetricPolicy,
) -> DiscoveryMetricSessionEvidence:
    resolved = _resolve_session_evidence(miss_batch, policy)
    values: dict[str, Any] = {
        "metric_policy_id": policy.metric_policy_id,
        "metric_policy_content_hash_sha256": policy.content_hash(),
        "metric_policy": policy,
        "miss_batch_id": miss_batch.batch_id,
        "miss_batch_content_hash_sha256": miss_batch.content_hash(),
        "miss_batch": miss_batch,
        **resolved,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.discovery_metric_session_evidence.v1",
    }
    return DiscoveryMetricSessionEvidence(
        session_evidence_id=stable_identity(
            "discovery-metric-session-evidence",
            values,
        ),
        **values,
    )


def _resolve_session_evidence(
    miss_batch: MissReconciliationBatch,
    policy: DiscoveryMetricPolicy,
) -> dict[str, object]:
    qualification_batch = miss_batch.qualification_batch
    if (
        qualification_batch.policy.policy_id != policy.qualification_policy_id
        or qualification_batch.policy.content_hash()
        != policy.qualification_policy_content_hash_sha256
        or qualification_batch.policy != policy.qualification_policy
    ):
        raise ValueError("metric policy does not match exact qualification batch policy")
    matching_horizons = tuple(
        item
        for item in qualification_batch.horizons
        if item.kind is policy.horizon_definition.kind
        and item.elapsed_seconds == policy.horizon_definition.elapsed_seconds
    )
    if len(matching_horizons) != 1:
        raise ValueError("metric policy must select exactly one embedded horizon")
    selected_horizon = matching_horizons[0]
    assessments = tuple(
        item
        for item in qualification_batch.assessments
        if item.horizon.horizon_id == selected_horizon.horizon_id
    )
    assessment_keys = {(item.member.symbol, item.direction): item for item in assessments}
    if len(assessment_keys) != len(assessments):
        raise ValueError("selected metric horizon contains duplicate matching units")
    opportunities = {
        item.assessment_id: item for item in qualification_batch.opportunities
    }
    prediction_map: dict[
        tuple[str, StrategyDirection], list[_DiscoveryPredictionEvidence]
    ] = {key: [] for key in assessment_keys}
    unmatched: list[_DiscoveryPredictionEvidence] = []
    for replay in miss_batch.session_replay.current_outcome_replays:
        result = replay.pipeline_result
        rank_map = {
            item.evaluation_id: item for item in result.ranked_opportunities
        }
        decision_map = {item.evaluation_id: item for item in result.decisions}
        for evaluation in result.evaluations:
            ranked = rank_map.get(evaluation.evaluation_id)
            decision = decision_map.get(evaluation.evaluation_id)
            positive_decision = (
                decision
                if decision is not None and decision.decision in _POSITIVE_DECISIONS
                else None
            )
            if ranked is None and positive_decision is None:
                continue
            prediction = _build_prediction(
                result=result,
                evaluation=evaluation,
                ranked=ranked,
                decision=positive_decision,
                cutoff=selected_horizon.entry_anchor_at,
            )
            key = (evaluation.symbol, evaluation.direction)
            if key in prediction_map:
                prediction_map[key].append(prediction)
            else:
                unmatched.append(prediction)
    units = tuple(
        _build_unit(
            assessment,
            opportunities.get(assessment.assessment_id),
            tuple(
                sorted(
                    prediction_map[(assessment.member.symbol, assessment.direction)],
                    key=_prediction_sort_key,
                )
            ),
        )
        for assessment in sorted(
            assessments,
            key=lambda item: (item.member.symbol, item.direction.value, item.assessment_id),
        )
    )
    unmatched_predictions = tuple(sorted(unmatched, key=_prediction_sort_key))
    limitations = tuple(
        dict.fromkeys(
            (
                *miss_batch.limitations,
                *("unmatched_prediction_truth" for _ in unmatched_predictions[:1]),
            )
        )
    )
    return {
        "selected_horizon_id": selected_horizon.horizon_id,
        "selected_horizon_content_hash_sha256": selected_horizon.content_hash(),
        "units": units,
        "unmatched_predictions": unmatched_predictions,
        "recorded_at": miss_batch.recorded_at,
        "limitations": limitations,
    }


def _build_prediction(
    *, result, evaluation, ranked, decision, cutoff
) -> _DiscoveryPredictionEvidence:
    values: dict[str, Any] = {
        "run_id": result.run_id,
        "run_content_hash_sha256": result.content_hash(),
        "decision_at": result.decision_at,
        "symbol": evaluation.symbol,
        "direction": evaluation.direction,
        "evaluation_id": evaluation.evaluation_id,
        "evaluation_content_hash_sha256": evaluation.content_hash(),
        "ranked_id": ranked.ranked_id if ranked is not None else None,
        "ranked_content_hash_sha256": ranked.content_hash() if ranked is not None else None,
        "rank_position": ranked.relative_rank if ranked is not None else None,
        "decision_id": decision.decision_id if decision is not None else None,
        "decision_content_hash_sha256": decision.content_hash() if decision is not None else None,
        "decision_value": decision.decision if decision is not None else None,
        "on_time": result.decision_at < cutoff,
        "schema_version": "v2.opportunity.discovery_prediction_evidence.v1",
    }
    return _DiscoveryPredictionEvidence(
        prediction_evidence_id=stable_identity("discovery-prediction-evidence", values),
        **values,
    )


def _build_unit(assessment, opportunity, predictions) -> _DiscoveryMetricUnitEvidence:
    if assessment.status is QualificationStatus.QUALIFIED:
        if opportunity is None:
            raise ValueError("qualified metric unit is missing exact opportunity")
        opportunity_id = opportunity.opportunity_id
        opportunity_hash = opportunity.content_hash()
    else:
        if opportunity is not None:
            raise ValueError("non-qualified metric unit cannot carry opportunity")
        opportunity_id = None
        opportunity_hash = None
    on_time_ranks = tuple(
        item.rank_position
        for item in predictions
        if item.on_time and item.rank_position is not None
    )
    values: dict[str, Any] = {
        "session_opportunity_key": assessment.session_opportunity_key,
        "assessment_id": assessment.assessment_id,
        "assessment_content_hash_sha256": assessment.content_hash(),
        "assessment": assessment,
        "opportunity_id": opportunity_id,
        "opportunity_content_hash_sha256": opportunity_hash,
        "opportunity": opportunity,
        "symbol": assessment.member.symbol,
        "direction": assessment.direction,
        "horizon_id": assessment.horizon.horizon_id,
        "latest_useful_cutoff_at": assessment.latest_useful_cutoff_at,
        "qualification_status": assessment.status,
        "claim_kind": assessment.claim_kind,
        "predictions": predictions,
        "best_on_time_rank_position": min(on_time_ranks) if on_time_ranks else None,
        "on_time_watch_or_take": any(
            item.on_time and item.decision_value in _POSITIVE_DECISIONS
            for item in predictions
        ),
        "any_watch_or_take": any(
            item.decision_value in _POSITIVE_DECISIONS for item in predictions
        ),
        "schema_version": "v2.opportunity.discovery_metric_unit_evidence.v1",
    }
    return _DiscoveryMetricUnitEvidence(
        unit_evidence_id=stable_identity("discovery-metric-unit-evidence", values),
        **values,
    )


def _compare_session_evidence(
    value: DiscoveryMetricSessionEvidence,
    expected: dict[str, object],
) -> None:
    for field_name, expected_value in expected.items():
        if getattr(value, field_name) != expected_value:
            raise ValueError(f"metric session {field_name} does not recompute")
    if not value.research_only or value.promotion_eligible:
        raise ValueError("metric session evidence must remain research-only")
    for limitation in value.limitations:
        require_sanitized(limitation, "metric session limitation")


def _prediction_sort_key(value: _DiscoveryPredictionEvidence) -> tuple[object, ...]:
    return (
        value.decision_at,
        value.run_id,
        value.symbol,
        value.direction.value,
        value.rank_position if value.rank_position is not None else 2**31,
        value.evaluation_id,
        value.prediction_evidence_id,
    )


def _require_optional_hash_pair(
    identity: str | None,
    content_hash: str | None,
    label: str,
) -> None:
    if (identity is None) is not (content_hash is None):
        raise ValueError(f"{label} identity and content hash must be paired")
    if identity is not None:
        require_identity(identity, f"{label}_id")
        require_hash(content_hash or "", f"{label}_content_hash")


__all__ = [
    "DiscoveryMetricSessionEvidence",
    "build_discovery_metric_session_evidence",
]
