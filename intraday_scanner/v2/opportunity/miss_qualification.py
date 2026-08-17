"""Pure strategy-agnostic hindsight qualification and path resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from intraday_scanner.v2.data_truth import IntradayCoverageStatus, PriceAdjustmentBasis
from intraday_scanner.v2.opportunity.miss_contracts import (
    MissContract,
    MissQualificationPolicy,
    QualificationClaimKind,
    QualificationExecutionStatus,
    QualificationMemberStatus,
    QualificationMetric,
    QualificationNumericEvidence,
    QualificationPathStatus,
    QualificationSourceScopeStatus,
    QualificationStatus,
    SessionQualificationHorizon,
    identity_payload,
    require_hash,
    require_identity,
    require_sanitized,
    require_schema,
    require_unique,
    require_utc,
)
from intraday_scanner.v2.opportunity.miss_qualification_logic import (
    ResolvedAssessment as _ResolvedAssessment,
)
from intraday_scanner.v2.opportunity.miss_qualification_logic import (
    build_metrics as _metrics,
)
from intraday_scanner.v2.opportunity.miss_qualification_logic import (
    continuity_gap as _continuity_gap,
)
from intraday_scanner.v2.opportunity.miss_qualification_logic import (
    earliest_censor as _earliest_censor,
)
from intraday_scanner.v2.opportunity.miss_qualification_logic import (
    empty_resolution as _empty_resolution,
)
from intraday_scanner.v2.opportunity.miss_qualification_logic import (
    execution_for as _execution_for,
)
from intraday_scanner.v2.opportunity.miss_qualification_logic import (
    per_share_cost as _per_share_cost,
)
from intraday_scanner.v2.opportunity.miss_qualification_logic import (
    resolution_reasons as _resolution_reasons,
)
from intraday_scanner.v2.opportunity.miss_qualification_logic import (
    resolve_path as _resolve_path,
)
from intraday_scanner.v2.opportunity.miss_qualification_logic import (
    session_opportunity_key as _session_opportunity_key,
)
from intraday_scanner.v2.opportunity.miss_qualification_logic import (
    touch as _touch,
)
from intraday_scanner.v2.opportunity.miss_qualification_logic import (
    unavailable_resolution as _unavailable_resolution,
)
from intraday_scanner.v2.opportunity.miss_sources import (
    HindsightQualificationSource,
    QualificationMemberEvidence,
)
from intraday_scanner.v2.opportunity.models import StrategyDirection, stable_identity
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeTouchInterval,
)
from intraday_scanner.v2.opportunity.outcome_sources import OutcomeBarEvidence
from intraday_scanner.v2.opportunity.universe import SafetyStatus

_DIRECTIONS = (StrategyDirection.LONG, StrategyDirection.SHORT)
_HARD_UNAVAILABLE_COVERAGE = {
    IntradayCoverageStatus.NO_DATA,
    IntradayCoverageStatus.ENTITLEMENT_DENIED,
    IntradayCoverageStatus.SOURCE_CONFLICT,
    IntradayCoverageStatus.HASH_MISMATCH,
    IntradayCoverageStatus.FUTURE_DATA_REJECTED,
    IntradayCoverageStatus.DATA_INELIGIBLE,
}


@dataclass(frozen=True)
class QualificationAssessment(MissContract):
    assessment_id: str
    session_opportunity_key: str
    source: HindsightQualificationSource
    policy: MissQualificationPolicy
    horizon: SessionQualificationHorizon
    member: QualificationMemberEvidence
    direction: StrategyDirection
    source_series_id: str | None
    source_series_content_hash: str | None
    source_observations: tuple[OutcomeBarEvidence, ...]
    latest_useful_cutoff_at: datetime
    status: QualificationStatus
    claim_kind: QualificationClaimKind
    path_status: QualificationPathStatus
    reference_price: Decimal | None
    stop_price: Decimal | None
    target_price: Decimal | None
    entry_interval: OutcomeTouchInterval | None
    target_touch_interval: OutcomeTouchInterval | None
    stop_touch_interval: OutcomeTouchInterval | None
    metrics: tuple[QualificationNumericEvidence, ...]
    reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    retrospective_research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.qualification_assessment.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.qualification_assessment.v1",
        )
        require_identity(self.assessment_id, "assessment_id")
        require_identity(self.session_opportunity_key, "session_opportunity_key")
        require_utc(self.latest_useful_cutoff_at, "latest_useful_cutoff_at")
        _validate_horizons(self.source, self.policy, (self.horizon,))
        source_member = next(
            (item for item in self.source.members if item.symbol == self.member.symbol),
            None,
        )
        if source_member != self.member:
            raise ValueError("qualification member is not the exact source member")
        if self.direction not in _DIRECTIONS:
            raise ValueError("qualification assessment requires exact LONG or SHORT")
        if self.latest_useful_cutoff_at != self.horizon.entry_anchor_at:
            raise ValueError("qualification cutoff must equal the source entry anchor")
        paired_series = (self.source_series_id is None) is (
            self.source_series_content_hash is None
        )
        if not paired_series:
            raise ValueError("qualification source series identity/hash must be paired")
        if self.source_series_id is not None:
            require_identity(self.source_series_id, "source_series_id")
            require_hash(self.source_series_content_hash or "", "source_series_content_hash")
        observation_ids = tuple(item.observation_id for item in self.source_observations)
        require_unique(observation_ids, "qualification source observation")
        if self.source_observations != tuple(
            sorted(
                self.source_observations,
                key=lambda item: (item.interval_start_at, item.observation_id),
            )
        ):
            raise ValueError("qualification observations must use chronological canonical order")
        if tuple(item.metric for item in self.metrics) != tuple(QualificationMetric):
            raise ValueError("qualification metrics must use canonical complete order")
        require_unique(tuple(item.metric.value for item in self.metrics), "qualification metric")
        _require_sanitized_values(self.reasons, "qualification reason")
        _require_sanitized_values(self.limitations, "qualification limitation")
        if self.status is QualificationStatus.QUALIFIED:
            if self.claim_kind is QualificationClaimKind.NONE:
                raise ValueError("qualified assessment requires an explicit claim kind")
            if self.path_status is not QualificationPathStatus.TARGET_FIRST:
                raise ValueError("qualified assessment requires target-first path")
        elif self.claim_kind is not QualificationClaimKind.NONE:
            raise ValueError("non-qualified assessment cannot carry a claim kind")
        expected = _resolve_assessment(
            source=self.source,
            policy=self.policy,
            horizon=self.horizon,
            member=self.member,
            direction=self.direction,
        )
        _compare_resolved(self, expected)
        expected_id = stable_identity(
            "qualification-assessment",
            identity_payload(self, "assessment_id"),
        )
        if self.assessment_id != expected_id:
            raise ValueError("qualification assessment identity does not match content")


@dataclass(frozen=True)
class HindsightQualifiedOpportunity(MissContract):
    opportunity_id: str
    session_opportunity_key: str
    assessment_id: str
    assessment_content_hash: str
    assessment: QualificationAssessment
    symbol: str
    direction: StrategyDirection
    horizon_id: str
    claim_kind: QualificationClaimKind
    latest_useful_cutoff_at: datetime
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.hindsight_qualified_opportunity.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.hindsight_qualified_opportunity.v1",
        )
        require_identity(self.opportunity_id, "opportunity_id")
        require_identity(self.session_opportunity_key, "session_opportunity_key")
        require_identity(self.assessment_id, "assessment_id")
        require_hash(self.assessment_content_hash, "assessment_content_hash")
        if self.assessment.status is not QualificationStatus.QUALIFIED:
            raise ValueError("hindsight opportunity requires a qualified assessment")
        if (
            self.session_opportunity_key != self.assessment.session_opportunity_key
            or self.assessment_id != self.assessment.assessment_id
            or self.assessment_content_hash != self.assessment.content_hash()
            or self.symbol != self.assessment.member.symbol
            or self.direction is not self.assessment.direction
            or self.horizon_id != self.assessment.horizon.horizon_id
            or self.claim_kind is not self.assessment.claim_kind
            or self.latest_useful_cutoff_at != self.assessment.latest_useful_cutoff_at
        ):
            raise ValueError("hindsight opportunity projections do not match assessment")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("hindsight opportunity must remain research-only")
        expected = stable_identity(
            "hindsight-qualified-opportunity",
            identity_payload(self, "opportunity_id"),
        )
        if self.opportunity_id != expected:
            raise ValueError("hindsight opportunity identity does not match content")


@dataclass(frozen=True)
class QualificationBatch(MissContract):
    batch_id: str
    source: HindsightQualificationSource
    policy: MissQualificationPolicy
    horizons: tuple[SessionQualificationHorizon, ...]
    assessments: tuple[QualificationAssessment, ...]
    opportunities: tuple[HindsightQualifiedOpportunity, ...]
    recorded_at: datetime
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.qualification_batch.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(self.schema_version, "v2.opportunity.qualification_batch.v1")
        require_identity(self.batch_id, "batch_id")
        require_utc(self.recorded_at, "recorded_at")
        if self.recorded_at != self.source.recorded_at:
            raise ValueError("qualification batch recorded_at must equal its source")
        if self.horizons != tuple(
            sorted(self.horizons, key=lambda item: (item.end_at, item.horizon_id))
        ):
            raise ValueError("qualification horizons must use canonical order")
        require_unique(tuple(item.horizon_id for item in self.horizons), "qualification horizon")
        _validate_horizons(self.source, self.policy, self.horizons)
        expected_assessments = tuple(
            _build_assessment(self.source, self.policy, horizon, member, direction)
            for member in self.source.members
            if member.status is not QualificationMemberStatus.INELIGIBLE
            for direction in _DIRECTIONS
            for horizon in self.horizons
        )
        if self.assessments != expected_assessments:
            raise ValueError("qualification assessments do not match the canonical product")
        keys = tuple(item.session_opportunity_key for item in self.assessments)
        require_unique(keys, "session opportunity key")
        expected_opportunities = tuple(
            _build_opportunity(item)
            for item in self.assessments
            if item.status is QualificationStatus.QUALIFIED
        )
        if self.opportunities != expected_opportunities:
            raise ValueError("qualified opportunities do not match assessments")
        if self.limitations != _batch_limitations(self.source):
            raise ValueError("qualification batch limitations are not canonical")
        _require_sanitized_values(self.limitations, "qualification batch limitation")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("qualification batch must remain research-only")
        expected_id = stable_identity(
            "qualification-batch",
            identity_payload(self, "batch_id"),
        )
        if self.batch_id != expected_id:
            raise ValueError("qualification batch identity does not match content")


def qualify_session_opportunities(
    source: HindsightQualificationSource,
    *,
    policy: MissQualificationPolicy,
    horizons: tuple[SessionQualificationHorizon, ...],
) -> QualificationBatch:
    ordered_horizons = tuple(sorted(horizons, key=lambda item: (item.end_at, item.horizon_id)))
    _validate_horizons(source, policy, ordered_horizons)
    assessments = tuple(
        _build_assessment(source, policy, horizon, member, direction)
        for member in source.members
        if member.status is not QualificationMemberStatus.INELIGIBLE
        for direction in _DIRECTIONS
        for horizon in ordered_horizons
    )
    opportunities = tuple(
        _build_opportunity(item)
        for item in assessments
        if item.status is QualificationStatus.QUALIFIED
    )
    limitations = _batch_limitations(source)
    common = {
        "source": source,
        "policy": policy,
        "horizons": ordered_horizons,
        "assessments": assessments,
        "opportunities": opportunities,
        "recorded_at": source.recorded_at,
        "limitations": limitations,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.qualification_batch.v1",
    }
    return QualificationBatch(
        batch_id=stable_identity("qualification-batch", common),
        source=source,
        policy=policy,
        horizons=ordered_horizons,
        assessments=assessments,
        opportunities=opportunities,
        recorded_at=source.recorded_at,
        limitations=limitations,
    )


def _build_assessment(
    source: HindsightQualificationSource,
    policy: MissQualificationPolicy,
    horizon: SessionQualificationHorizon,
    member: QualificationMemberEvidence,
    direction: StrategyDirection,
) -> QualificationAssessment:
    resolved = _resolve_assessment(
        source=source,
        policy=policy,
        horizon=horizon,
        member=member,
        direction=direction,
    )
    common = {
        "source": source,
        "policy": policy,
        "horizon": horizon,
        "member": member,
        "direction": direction,
        **resolved.__dict__,
        "retrospective_research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.qualification_assessment.v1",
    }
    return QualificationAssessment(
        assessment_id=stable_identity("qualification-assessment", common),
        source=source,
        policy=policy,
        horizon=horizon,
        member=member,
        direction=direction,
        **resolved.__dict__,
    )


def _build_opportunity(
    assessment: QualificationAssessment,
) -> HindsightQualifiedOpportunity:
    common = {
        "session_opportunity_key": assessment.session_opportunity_key,
        "assessment_id": assessment.assessment_id,
        "assessment_content_hash": assessment.content_hash(),
        "assessment": assessment,
        "symbol": assessment.member.symbol,
        "direction": assessment.direction,
        "horizon_id": assessment.horizon.horizon_id,
        "claim_kind": assessment.claim_kind,
        "latest_useful_cutoff_at": assessment.latest_useful_cutoff_at,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.hindsight_qualified_opportunity.v1",
    }
    return HindsightQualifiedOpportunity(
        opportunity_id=stable_identity("hindsight-qualified-opportunity", common),
        session_opportunity_key=assessment.session_opportunity_key,
        assessment_id=assessment.assessment_id,
        assessment_content_hash=assessment.content_hash(),
        assessment=assessment,
        symbol=assessment.member.symbol,
        direction=assessment.direction,
        horizon_id=assessment.horizon.horizon_id,
        claim_kind=assessment.claim_kind,
        latest_useful_cutoff_at=assessment.latest_useful_cutoff_at,
    )


def _resolve_assessment(
    *,
    source: HindsightQualificationSource,
    policy: MissQualificationPolicy,
    horizon: SessionQualificationHorizon,
    member: QualificationMemberEvidence,
    direction: StrategyDirection,
) -> _ResolvedAssessment:
    key = _session_opportunity_key(policy, horizon, member.symbol, direction)
    series = next(
        (item for item in source.observation_dataset.series if item.symbol == member.symbol),
        None,
    )
    if member.status is QualificationMemberStatus.UNKNOWN:
        return _unavailable_resolution(
            key,
            horizon,
            QualificationPathStatus.UNSUPPORTED_EVIDENCE,
            "qualification_membership_unknown",
        )
    if member.halt_status is SafetyStatus.UNKNOWN:
        return _unavailable_resolution(
            key,
            horizon,
            QualificationPathStatus.UNSUPPORTED_EVIDENCE,
            "qualification_halt_status_unknown",
        )
    if member.corporate_action_status is SafetyStatus.UNKNOWN:
        return _unavailable_resolution(
            key,
            horizon,
            QualificationPathStatus.UNSUPPORTED_EVIDENCE,
            "qualification_corporate_action_status_unknown",
        )
    if series is None:
        status = (
            QualificationStatus.PENDING
            if source.scope_receipt.scope_status is QualificationSourceScopeStatus.PENDING
            else QualificationStatus.UNAVAILABLE
        )
        return _empty_resolution(
            key,
            horizon,
            status,
            QualificationPathStatus.MISSING_BARS,
            "qualification_series_unavailable",
        )
    observations = tuple(
        item
        for item in series.observations
        if horizon.entry_anchor_at <= item.interval_start_at
        and item.interval_end_at <= horizon.end_at
    )
    if member.halt_status is SafetyStatus.BLOCKED:
        return _empty_resolution(
            key,
            horizon,
            QualificationStatus.CENSORED,
            QualificationPathStatus.HALT_CENSORED,
            "qualification_member_halt_blocked",
            series=series,
            observations=observations,
        )
    if member.corporate_action_status is SafetyStatus.BLOCKED:
        return _empty_resolution(
            key,
            horizon,
            QualificationStatus.CENSORED,
            QualificationPathStatus.CORPORATE_ACTION_CENSORED,
            "qualification_member_corporate_action_blocked",
            series=series,
            observations=observations,
        )
    hard_status = series.coverage_receipt.status
    if hard_status in _HARD_UNAVAILABLE_COVERAGE:
        return _empty_resolution(
            key,
            horizon,
            QualificationStatus.UNAVAILABLE,
            QualificationPathStatus.UNSUPPORTED_EVIDENCE,
            f"coverage_unavailable:{hard_status.value.lower()}",
            series=series,
            observations=observations,
        )
    if hard_status is IntradayCoverageStatus.CORPORATE_ACTION_UNRESOLVED:
        return _empty_resolution(
            key,
            horizon,
            QualificationStatus.CENSORED,
            QualificationPathStatus.CORPORATE_ACTION_CENSORED,
            "corporate_action_coverage_unresolved",
            series=series,
            observations=observations,
        )
    if any(
        item.bar.price_adjustment_basis is PriceAdjustmentBasis.UNKNOWN
        for item in observations
    ) or len({item.bar.price_adjustment_basis for item in observations}) > 1:
        return _empty_resolution(
            key,
            horizon,
            QualificationStatus.UNAVAILABLE,
            QualificationPathStatus.UNSUPPORTED_EVIDENCE,
            "unknown_or_mixed_price_adjustment_basis",
            series=series,
            observations=observations,
        )
    pending = source.frozen_at < horizon.end_at or series.requested_through_at < horizon.end_at
    continuity_gap = _continuity_gap(observations, horizon, policy)
    continuity_reason = continuity_gap[1] if continuity_gap is not None else None
    if not observations or observations[0].interval_start_at != horizon.entry_anchor_at:
        continuity_gap = (
            horizon.entry_anchor_at,
            "missing_entry_anchor_observation",
        )
        continuity_reason = "missing_entry_anchor_observation"
    reference = observations[0] if observations else None
    if reference is None:
        return _empty_resolution(
            key,
            horizon,
            QualificationStatus.PENDING if pending else QualificationStatus.CENSORED,
            (
                QualificationPathStatus.PENDING_HORIZON
                if pending
                else QualificationPathStatus.MISSING_BARS
            ),
            continuity_reason or "missing_entry_anchor_observation",
            series=series,
            observations=observations,
        )
    entry = reference.bar.open_price
    distance = entry * policy.stop_distance_fraction
    sign = Decimal("1") if direction is StrategyDirection.LONG else Decimal("-1")
    stop = entry - sign * distance
    target = entry + sign * distance * policy.minimum_gross_reward_risk
    if min(entry, stop, target, distance) <= 0:
        return _empty_resolution(
            key,
            horizon,
            QualificationStatus.UNAVAILABLE,
            QualificationPathStatus.UNSUPPORTED_EVIDENCE,
            "invalid_qualification_geometry",
            series=series,
            observations=observations,
        )
    censor = _earliest_censor(series, horizon, continuity_gap)
    path, target_touch, stop_touch = _resolve_path(
        observations,
        direction=direction,
        target=target,
        stop=stop,
        censor_at=censor[0] if censor is not None else None,
    )
    if path is QualificationPathStatus.TARGET_FIRST:
        status = QualificationStatus.QUALIFIED
    elif path in {QualificationPathStatus.STOP_FIRST, QualificationPathStatus.NO_TARGET}:
        status = QualificationStatus.NOT_QUALIFIED
    elif path is QualificationPathStatus.PENDING_HORIZON:
        status = QualificationStatus.PENDING
    elif path is QualificationPathStatus.UNSUPPORTED_EVIDENCE:
        status = QualificationStatus.UNAVAILABLE
    else:
        status = QualificationStatus.CENSORED
    if path in {QualificationPathStatus.NO_TARGET, QualificationPathStatus.PENDING_HORIZON}:
        if censor is not None:
            path = censor[1]
            status = QualificationStatus.CENSORED
        elif continuity_reason is not None:
            path = QualificationPathStatus.MISSING_BARS
            status = QualificationStatus.CENSORED
        elif pending:
            path = QualificationPathStatus.PENDING_HORIZON
            status = QualificationStatus.PENDING
    execution = _execution_for(source, member.symbol, direction, reference.observation_id)
    per_share_cost = _per_share_cost(entry, execution)
    after_cost = (
        (abs(target - entry) - per_share_cost) / (distance + per_share_cost)
        if per_share_cost is not None
        else None
    )
    executable = (
        status is QualificationStatus.QUALIFIED
        and execution is not None
        and execution.status is QualificationExecutionStatus.AVAILABLE
        and execution.executable_quantity_shares is not None
        and execution.executable_quantity_shares
        >= policy.minimum_executable_quantity_shares
        and after_cost is not None
        and after_cost >= policy.minimum_after_cost_reward_risk
        and member.halt_status is SafetyStatus.CLEAR
        and member.corporate_action_status is SafetyStatus.CLEAR
    )
    claim_kind = (
        QualificationClaimKind.EXECUTABLE_TRADE
        if executable
        else (
            QualificationClaimKind.PRICE_MOVE_PROXY
            if status is QualificationStatus.QUALIFIED
            else QualificationClaimKind.NONE
        )
    )
    reasons = _resolution_reasons(
        status=status,
        claim_kind=claim_kind,
        path=path,
        censor=censor,
        continuity_reason=continuity_reason,
        execution=execution,
        after_cost=after_cost,
        policy=policy,
    )
    metrics = _metrics(
        reference=reference,
        entry=entry,
        stop=stop,
        target=target,
        distance=distance,
        execution=execution,
        per_share_cost=per_share_cost,
        after_cost=after_cost,
    )
    return _ResolvedAssessment(
        session_opportunity_key=key,
        source_series_id=series.series_id,
        source_series_content_hash=series.content_hash(),
        source_observations=observations,
        latest_useful_cutoff_at=horizon.entry_anchor_at,
        status=status,
        claim_kind=claim_kind,
        path_status=path,
        reference_price=entry,
        stop_price=stop,
        target_price=target,
        entry_interval=_touch(reference),
        target_touch_interval=_touch(target_touch),
        stop_touch_interval=_touch(stop_touch),
        metrics=metrics,
        reasons=reasons,
        limitations=tuple(dict.fromkeys((*source.limitations, *series.limitations))),
    )


def _validate_horizons(
    source: HindsightQualificationSource,
    policy: MissQualificationPolicy,
    horizons: tuple[SessionQualificationHorizon, ...],
) -> None:
    if not horizons:
        raise ValueError("qualification requires at least one declared horizon")
    expected_anchor = source.scope_receipt.session_open_at + timedelta(
        seconds=policy.entry_anchor_offset_seconds
    )
    interval = timedelta(seconds=policy.expected_bar_interval_seconds)
    for horizon in horizons:
        if (
            horizon.exchange_session_id != source.scope_receipt.exchange_session_id
            or horizon.session_open_at != source.scope_receipt.session_open_at
            or horizon.session_close_at != source.scope_receipt.session_close_at
            or horizon.entry_anchor_at != expected_anchor
        ):
            raise ValueError("qualification horizon does not match source/policy session")
        if horizon.end_at < expected_anchor + interval:
            raise ValueError("qualification horizon must include the complete entry interval")
        delta = horizon.end_at - expected_anchor
        if delta.microseconds:
            raise ValueError("qualification horizon cannot contain fractional seconds")
        seconds = delta.days * 86_400 + delta.seconds
        if seconds % policy.expected_bar_interval_seconds:
            raise ValueError("qualification horizon must align to expected bar intervals")


def _compare_resolved(
    value: QualificationAssessment,
    expected: _ResolvedAssessment,
) -> None:
    for field_name, expected_value in expected.__dict__.items():
        if getattr(value, field_name) != expected_value:
            raise ValueError(f"qualification assessment {field_name} does not recompute")


def _batch_limitations(source: HindsightQualificationSource) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                "retrospective_research_only_not_promotion_evidence",
                *source.limitations,
            )
        )
    )


def _require_sanitized_values(values: tuple[str, ...], label: str) -> None:
    require_unique(values, label)
    for value in values:
        require_sanitized(value, label)


__all__ = [
    "HindsightQualifiedOpportunity",
    "QualificationAssessment",
    "QualificationBatch",
    "qualify_session_opportunities",
]
