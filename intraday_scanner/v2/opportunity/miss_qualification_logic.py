"""Internal path, censor, and formula logic for hindsight qualification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from intraday_scanner.v2.data_truth import IntradayCoverageStatus
from intraday_scanner.v2.opportunity.miss_contracts import (
    MissQualificationPolicy,
    QualificationClaimKind,
    QualificationExecutionStatus,
    QualificationMetric,
    QualificationNumericEvidence,
    QualificationPathStatus,
    QualificationStatus,
    QualificationUnit,
    QualificationValueStatus,
    SessionQualificationHorizon,
)
from intraday_scanner.v2.opportunity.miss_sources import (
    HindsightQualificationSource,
    QualificationExecutionEvidence,
)
from intraday_scanner.v2.opportunity.models import (
    EvidenceKind,
    StrategyDirection,
    stable_identity,
)
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeMarketStatusKind,
    OutcomeTouchInterval,
)
from intraday_scanner.v2.opportunity.outcome_sources import (
    OutcomeBarEvidence,
    OutcomeObservationSeries,
)


@dataclass(frozen=True)
class ResolvedAssessment:
    session_opportunity_key: str
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


def resolve_path(
    observations: tuple[OutcomeBarEvidence, ...],
    *,
    direction: StrategyDirection,
    target: Decimal,
    stop: Decimal,
    censor_at: datetime | None,
) -> tuple[
    QualificationPathStatus,
    OutcomeBarEvidence | None,
    OutcomeBarEvidence | None,
]:
    for observation in observations:
        if censor_at is not None and observation.interval_start_at >= censor_at:
            break
        bar = observation.bar
        if direction is StrategyDirection.LONG:
            if bar.open_price > target or bar.open_price < stop:
                return QualificationPathStatus.GAP_THROUGH_AMBIGUOUS, None, None
            if bar.open_price == target:
                return QualificationPathStatus.TARGET_FIRST, observation, None
            if bar.open_price == stop:
                return QualificationPathStatus.STOP_FIRST, None, observation
            target_hit = bar.high_price >= target
            stop_hit = bar.low_price <= stop
        else:
            if bar.open_price < target or bar.open_price > stop:
                return QualificationPathStatus.GAP_THROUGH_AMBIGUOUS, None, None
            if bar.open_price == target:
                return QualificationPathStatus.TARGET_FIRST, observation, None
            if bar.open_price == stop:
                return QualificationPathStatus.STOP_FIRST, None, observation
            target_hit = bar.low_price <= target
            stop_hit = bar.high_price >= stop
        if observation is observations[0] and (target_hit or stop_hit):
            return (
                QualificationPathStatus.ENTRY_BAR_AMBIGUOUS,
                observation if target_hit else None,
                observation if stop_hit else None,
            )
        if target_hit and stop_hit:
            path = (
                QualificationPathStatus.ENTRY_BAR_AMBIGUOUS
                if observation is observations[0]
                else QualificationPathStatus.SAME_BAR_AMBIGUOUS
            )
            return path, observation, observation
        if target_hit:
            return QualificationPathStatus.TARGET_FIRST, observation, None
        if stop_hit:
            return QualificationPathStatus.STOP_FIRST, None, observation
    return QualificationPathStatus.NO_TARGET, None, None


def earliest_censor(
    series: OutcomeObservationSeries,
    horizon: SessionQualificationHorizon,
    continuity_gap: tuple[datetime, str] | None,
) -> tuple[datetime, QualificationPathStatus, str] | None:
    values: list[tuple[datetime, QualificationPathStatus, str]] = []
    for start, end in series.coverage_receipt.missing_intervals:
        if start < horizon.end_at and end > horizon.entry_anchor_at:
            gap_path = (
                QualificationPathStatus.HALT_CENSORED
                if series.coverage_receipt.status
                is IntradayCoverageStatus.KNOWN_HALT_GAPS
                else QualificationPathStatus.MISSING_BARS
            )
            reason = (
                "known_halt_gap"
                if gap_path is QualificationPathStatus.HALT_CENSORED
                else "missing_interval"
            )
            values.append((max(start, horizon.entry_anchor_at), gap_path, reason))
    for status_interval in series.market_status_intervals:
        if (
            status_interval.status == OutcomeMarketStatusKind.HALTED.value
            and status_interval.start < horizon.end_at
            and status_interval.end > horizon.entry_anchor_at
        ):
            values.append(
                (
                    max(status_interval.start, horizon.entry_anchor_at),
                    QualificationPathStatus.HALT_CENSORED,
                    "halt_interval",
                )
            )
        if status_interval.status in {
            OutcomeMarketStatusKind.CLOSED.value,
            OutcomeMarketStatusKind.AUCTION.value,
        } and (
            status_interval.start < horizon.end_at
            and status_interval.end > horizon.entry_anchor_at
        ):
            values.append(
                (
                    max(status_interval.start, horizon.entry_anchor_at),
                    QualificationPathStatus.HALT_CENSORED,
                    f"market_status:{status_interval.status}",
                )
            )
    for action in series.corporate_actions:
        if horizon.entry_anchor_at <= action.effective_at <= horizon.end_at:
            values.append(
                (
                    action.effective_at,
                    QualificationPathStatus.CORPORATE_ACTION_CENSORED,
                    "corporate_action",
                )
            )
    if continuity_gap is not None:
        values.append(
            (
                continuity_gap[0],
                QualificationPathStatus.MISSING_BARS,
                continuity_gap[1],
            )
        )
    return min(values, key=lambda item: (item[0], item[1].value)) if values else None


def continuity_gap(
    observations: tuple[OutcomeBarEvidence, ...],
    horizon: SessionQualificationHorizon,
    policy: MissQualificationPolicy,
) -> tuple[datetime, str] | None:
    expected = horizon.entry_anchor_at
    interval = timedelta(seconds=policy.expected_bar_interval_seconds)
    for observation in observations:
        if observation.interval_start_at != expected:
            return expected, "missing_or_misaligned_qualification_interval"
        if observation.interval_end_at != observation.interval_start_at + interval:
            return observation.interval_start_at, "unexpected_qualification_bar_interval"
        expected = observation.interval_end_at
    if expected != horizon.end_at:
        return expected, "missing_qualification_horizon_tail"
    return None


def build_metrics(
    *,
    reference: OutcomeBarEvidence,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    distance: Decimal,
    execution: QualificationExecutionEvidence | None,
    per_share_cost: Decimal | None,
    after_cost: Decimal | None,
) -> tuple[QualificationNumericEvidence, ...]:
    ref_id = reference.observation_id
    observed_at = reference.available_at
    gross_r = abs(target - entry) / distance
    values: dict[QualificationMetric, QualificationNumericEvidence] = {
        QualificationMetric.REFERENCE_PRICE: _available_metric(
            QualificationMetric.REFERENCE_PRICE,
            entry,
            QualificationValueStatus.OBSERVED,
            EvidenceKind.EMPIRICAL,
            observed_at,
            (ref_id,),
            "first_complete_anchor_bar_open",
        ),
        QualificationMetric.STOP_PRICE: _available_metric(
            QualificationMetric.STOP_PRICE,
            stop,
            QualificationValueStatus.DERIVED,
            EvidenceKind.HEURISTIC,
            observed_at,
            (ref_id,),
            "reference_price-directional_stop_fraction",
        ),
        QualificationMetric.TARGET_PRICE: _available_metric(
            QualificationMetric.TARGET_PRICE,
            target,
            QualificationValueStatus.DERIVED,
            EvidenceKind.HEURISTIC,
            observed_at,
            (ref_id,),
            "reference_price-stop_distance-minimum_gross_r",
        ),
        QualificationMetric.STOP_DISTANCE: _available_metric(
            QualificationMetric.STOP_DISTANCE,
            distance,
            QualificationValueStatus.DERIVED,
            EvidenceKind.HEURISTIC,
            observed_at,
            (ref_id,),
            "abs(reference_price-stop_price)",
        ),
        QualificationMetric.GROSS_REWARD_RISK: _available_metric(
            QualificationMetric.GROSS_REWARD_RISK,
            gross_r,
            QualificationValueStatus.DERIVED,
            EvidenceKind.HEURISTIC,
            observed_at,
            (ref_id,),
            "absolute_target_minus_reference_divided_by_stop_distance",
        ),
        QualificationMetric.REQUIRED_MOVE_FRACTION: _available_metric(
            QualificationMetric.REQUIRED_MOVE_FRACTION,
            abs(target - entry) / entry,
            QualificationValueStatus.DERIVED,
            EvidenceKind.HEURISTIC,
            observed_at,
            (ref_id,),
            "absolute_target_minus_reference_divided_by_reference_price",
        ),
    }
    if execution is None or execution.status is QualificationExecutionStatus.UNAVAILABLE:
        values[QualificationMetric.PER_SHARE_COST] = _unavailable_metric(
            QualificationMetric.PER_SHARE_COST, "execution_cost_evidence_unavailable"
        )
        values[QualificationMetric.AFTER_COST_REWARD_RISK] = _unavailable_metric(
            QualificationMetric.AFTER_COST_REWARD_RISK,
            "execution_cost_evidence_unavailable",
        )
        values[QualificationMetric.EXECUTABLE_QUANTITY] = _unavailable_metric(
            QualificationMetric.EXECUTABLE_QUANTITY,
            "execution_quantity_evidence_unavailable",
        )
    else:
        assert per_share_cost is not None
        assert after_cost is not None
        assert execution.observed_at is not None
        assert execution.executable_quantity_shares is not None
        evidence_ids = (ref_id, execution.execution_evidence_id)
        values[QualificationMetric.PER_SHARE_COST] = _available_metric(
            QualificationMetric.PER_SHARE_COST,
            per_share_cost,
            QualificationValueStatus.DERIVED,
            execution.evidence_kind,
            execution.observed_at,
            evidence_ids,
            "entry_times_total_basis_point_cost_plus_round_trip_fee",
        )
        values[QualificationMetric.AFTER_COST_REWARD_RISK] = _available_metric(
            QualificationMetric.AFTER_COST_REWARD_RISK,
            after_cost,
            QualificationValueStatus.DERIVED,
            execution.evidence_kind,
            execution.observed_at,
            evidence_ids,
            "net_reward_divided_by_stop_distance_plus_cost",
        )
        values[QualificationMetric.EXECUTABLE_QUANTITY] = _available_metric(
            QualificationMetric.EXECUTABLE_QUANTITY,
            Decimal(execution.executable_quantity_shares),
            QualificationValueStatus.OBSERVED,
            execution.evidence_kind,
            execution.observed_at,
            (execution.execution_evidence_id,),
            "source_executable_quantity",
        )
    return tuple(values[item] for item in QualificationMetric)


def per_share_cost(
    entry: Decimal,
    execution: QualificationExecutionEvidence | None,
) -> Decimal | None:
    if execution is None or execution.status is QualificationExecutionStatus.UNAVAILABLE:
        return None
    assert execution.spread_bps is not None
    assert execution.entry_slippage_bps is not None
    assert execution.exit_slippage_bps is not None
    assert execution.round_trip_fee_per_share is not None
    return entry * (
        execution.spread_bps
        + execution.entry_slippage_bps
        + execution.exit_slippage_bps
    ) / Decimal("10000") + execution.round_trip_fee_per_share


def execution_for(
    source: HindsightQualificationSource,
    symbol: str,
    direction: StrategyDirection,
    observation_id: str,
) -> QualificationExecutionEvidence | None:
    return next(
        (
            item
            for item in source.execution_evidence
            if item.symbol == symbol
            and item.direction is direction
            and item.reference_observation.observation_id == observation_id
        ),
        None,
    )


def resolution_reasons(
    *,
    status: QualificationStatus,
    claim_kind: QualificationClaimKind,
    path: QualificationPathStatus,
    censor: tuple[datetime, QualificationPathStatus, str] | None,
    continuity_reason: str | None,
    execution: QualificationExecutionEvidence | None,
    after_cost: Decimal | None,
    policy: MissQualificationPolicy,
) -> tuple[str, ...]:
    values = [f"qualification_path:{path.value}"]
    if status is QualificationStatus.QUALIFIED:
        values.append(f"qualification_claim:{claim_kind.value}")
    if censor is not None:
        values.append(f"censor:{censor[2]}")
    if continuity_reason is not None:
        values.append(f"coverage:{continuity_reason}")
    if claim_kind is QualificationClaimKind.PRICE_MOVE_PROXY:
        if execution is None or execution.status is QualificationExecutionStatus.UNAVAILABLE:
            values.append("execution_evidence_unavailable_proxy_only")
        elif execution.status is QualificationExecutionStatus.PROVISIONAL:
            values.append("execution_evidence_provisional_proxy_only")
        elif after_cost is not None and after_cost < policy.minimum_after_cost_reward_risk:
            values.append("after_cost_threshold_not_met_proxy_only")
    return tuple(dict.fromkeys(values))


def empty_resolution(
    key: str,
    horizon: SessionQualificationHorizon,
    status: QualificationStatus,
    path: QualificationPathStatus,
    reason: str,
    *,
    series: OutcomeObservationSeries | None = None,
    observations: tuple[OutcomeBarEvidence, ...] = (),
) -> ResolvedAssessment:
    return ResolvedAssessment(
        session_opportunity_key=key,
        source_series_id=series.series_id if series else None,
        source_series_content_hash=series.content_hash() if series else None,
        source_observations=observations,
        latest_useful_cutoff_at=horizon.entry_anchor_at,
        status=status,
        claim_kind=QualificationClaimKind.NONE,
        path_status=path,
        reference_price=None,
        stop_price=None,
        target_price=None,
        entry_interval=None,
        target_touch_interval=None,
        stop_touch_interval=None,
        metrics=tuple(
            _unavailable_metric(metric, reason) for metric in QualificationMetric
        ),
        reasons=(reason,),
        limitations=(),
    )


def unavailable_resolution(
    key: str,
    horizon: SessionQualificationHorizon,
    path: QualificationPathStatus,
    reason: str,
) -> ResolvedAssessment:
    return empty_resolution(
        key,
        horizon,
        QualificationStatus.UNAVAILABLE,
        path,
        reason,
    )


def session_opportunity_key(
    policy: MissQualificationPolicy,
    horizon: SessionQualificationHorizon,
    symbol: str,
    direction: StrategyDirection,
) -> str:
    return stable_identity(
        "session-opportunity-key",
        {
            "policy_id": policy.policy_id,
            "exchange_session_id": horizon.exchange_session_id,
            "symbol": symbol,
            "direction": direction,
            "horizon": horizon,
        },
    )


def touch(observation: OutcomeBarEvidence | None) -> OutcomeTouchInterval | None:
    if observation is None:
        return None
    return OutcomeTouchInterval(
        observation_id=observation.observation_id,
        observation_content_hash_sha256=observation.content_hash(),
        interval_start_at=observation.interval_start_at,
        interval_end_at=observation.interval_end_at,
    )


def _available_metric(
    metric: QualificationMetric,
    value: Decimal,
    status: QualificationValueStatus,
    evidence_kind: EvidenceKind,
    observed_at: datetime,
    source_ids: tuple[str, ...],
    method: str,
) -> QualificationNumericEvidence:
    return QualificationNumericEvidence(
        metric=metric,
        unit=_metric_unit(metric),
        value=value,
        status=status,
        evidence_kind=evidence_kind,
        observed_at=observed_at,
        source_ids=tuple(dict.fromkeys(source_ids)),
        method=method,
    )


def _unavailable_metric(
    metric: QualificationMetric, reason: str
) -> QualificationNumericEvidence:
    return QualificationNumericEvidence(
        metric=metric,
        unit=_metric_unit(metric),
        value=None,
        status=QualificationValueStatus.UNAVAILABLE,
        evidence_kind=EvidenceKind.HEURISTIC,
        observed_at=None,
        source_ids=(),
        method="unavailable",
        reason=reason,
    )


def _metric_unit(metric: QualificationMetric) -> QualificationUnit:
    if metric in {
        QualificationMetric.REFERENCE_PRICE,
        QualificationMetric.STOP_PRICE,
        QualificationMetric.TARGET_PRICE,
        QualificationMetric.STOP_DISTANCE,
        QualificationMetric.PER_SHARE_COST,
    }:
        return QualificationUnit.USD_PER_SHARE
    if metric is QualificationMetric.REQUIRED_MOVE_FRACTION:
        return QualificationUnit.FRACTION
    if metric is QualificationMetric.EXECUTABLE_QUANTITY:
        return QualificationUnit.SHARES
    return QualificationUnit.RATIO


__all__ = [
    "ResolvedAssessment",
    "build_metrics",
    "continuity_gap",
    "earliest_censor",
    "empty_resolution",
    "execution_for",
    "per_share_cost",
    "resolution_reasons",
    "resolve_path",
    "session_opportunity_key",
    "touch",
    "unavailable_resolution",
]
