"""Pure horizon-local outcome state, path, and metric resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from intraday_scanner.v2.data_truth.intraday import (
    IntradayCoverageStatus,
    PriceAdjustmentBasis,
)
from intraday_scanner.v2.opportunity.models import (
    EvaluationStatus,
    StrategyDirection,
    TradeDecision,
)
from intraday_scanner.v2.opportunity.outcome_contracts import (
    _METRIC_UNITS,
    CANONICAL_OUTCOME_METRICS,
    OutcomeCompleteness,
    OutcomeEntryStatus,
    OutcomeHorizon,
    OutcomeLabelPolicy,
    OutcomeMarketStatusKind,
    OutcomeMetric,
    OutcomeNumericEvidence,
    OutcomePathStatus,
    OutcomeTouchInterval,
    OutcomeValueStatus,
    _after_cost_value,
    _direction_sign,
    _timedelta_decimal_seconds,
)
from intraday_scanner.v2.opportunity.outcome_sources import (
    OutcomeBarEvidence,
    OutcomeObservationSeries,
)
from intraday_scanner.v2.opportunity.risk import ExecutionRiskEvidence


@dataclass(frozen=True)
class _RecordResolution:
    observations: tuple[OutcomeBarEvidence, ...]
    reference: OutcomeBarEvidence | None
    resolution_observations: tuple[OutcomeBarEvidence, ...]
    completeness: OutcomeCompleteness
    entry_status: OutcomeEntryStatus
    path_status: OutcomePathStatus
    reasons: tuple[str, ...]
    close_observation: OutcomeBarEvidence | None


def _resolve_record_state(
    *,
    decision: TradeDecision,
    series: OutcomeObservationSeries,
    policy: OutcomeLabelPolicy,
    horizon: OutcomeHorizon,
    source_frozen_at: datetime,
    recorded_at: datetime,
) -> _RecordResolution:
    observations = tuple(
        item for item in series.observations if item.interval_end_at <= horizon.end_at
    )
    reference = observations[0] if observations else None
    complete, coverage_reason = _horizon_is_complete(
        series=series,
        observations=observations,
        horizon=horizon,
        policy=policy,
    )
    pending = recorded_at < horizon.end_at or source_frozen_at < horizon.end_at
    halt = next(
        (
            item
            for item in series.market_status_intervals
            if item.status == OutcomeMarketStatusKind.HALTED.value
            and item.start < horizon.end_at
            and item.end > decision.decision_at
        ),
        None,
    )
    action = next(
        (
            item
            for item in series.corporate_actions
            if decision.decision_at < item.effective_at <= horizon.end_at
        ),
        None,
    )
    action_unresolved = (
        series.coverage_receipt.status
        is IntradayCoverageStatus.CORPORATE_ACTION_UNRESOLVED
    )
    horizon_halt_gaps = tuple(
        (start, end)
        for start, end in series.coverage_receipt.missing_intervals
        if start < horizon.end_at and end > decision.decision_at
    )
    halt_receipt = (
        series.coverage_receipt.status is IntradayCoverageStatus.KNOWN_HALT_GAPS
        and bool(horizon_halt_gaps)
    )
    hard_unavailable = series.coverage_receipt.status in {
        IntradayCoverageStatus.NO_DATA,
        IntradayCoverageStatus.ENTITLEMENT_DENIED,
        IntradayCoverageStatus.SOURCE_CONFLICT,
        IntradayCoverageStatus.HASH_MISMATCH,
        IntradayCoverageStatus.FUTURE_DATA_REJECTED,
        IntradayCoverageStatus.DATA_INELIGIBLE,
    }
    adjustment_bases = {item.bar.price_adjustment_basis for item in observations}
    adjustment_unsupported = (
        PriceAdjustmentBasis.UNKNOWN in adjustment_bases or len(adjustment_bases) > 1
    )
    resolution_observations = observations
    reasons: list[str] = []
    if hard_unavailable:
        completeness = OutcomeCompleteness.UNAVAILABLE
        entry_status = OutcomeEntryStatus.UNSUPPORTED
        path_status = OutcomePathStatus.UNSUPPORTED_EVIDENCE
        reasons.append(f"coverage_{series.coverage_receipt.status.value.lower()}")
    elif adjustment_unsupported:
        completeness = OutcomeCompleteness.UNAVAILABLE
        entry_status = OutcomeEntryStatus.UNSUPPORTED
        path_status = OutcomePathStatus.UNSUPPORTED_EVIDENCE
        reasons.append("unknown_or_mixed_price_adjustment_basis")
    elif action_unresolved:
        completeness = OutcomeCompleteness.CENSORED
        entry_status = OutcomeEntryStatus.UNSUPPORTED
        path_status = OutcomePathStatus.CORPORATE_ACTION_CENSORED
        reasons.append("corporate_action_status_unresolved")
    elif action is not None or halt is not None or halt_receipt:
        censor_facts: list[tuple[datetime, OutcomePathStatus, str]] = []
        if action is not None:
            censor_facts.append(
                (
                    action.effective_at,
                    OutcomePathStatus.CORPORATE_ACTION_CENSORED,
                    "corporate_action_inside_horizon",
                )
            )
        if halt is not None:
            censor_facts.append(
                (halt.start, OutcomePathStatus.HALT_CENSORED, "market_halt_inside_horizon")
            )
        if halt_receipt:
            cutoff = min(start for start, _end in horizon_halt_gaps)
            censor_facts.append(
                (
                    cutoff,
                    OutcomePathStatus.HALT_CENSORED,
                    "coverage_receipt_reports_known_halt_gaps",
                )
            )
        censor_at, censor_path, censor_reason = min(censor_facts, key=lambda item: item[0])
        resolution_observations = _continuous_prefix(
            series,
            observations,
            policy,
            through_at=censor_at,
        )
        resolved_entry, resolved_path = _resolve_trade_path(decision, resolution_observations)
        if resolved_path in {OutcomePathStatus.TARGET_FIRST, OutcomePathStatus.STOP_FIRST}:
            completeness = OutcomeCompleteness.PARTIAL
            entry_status = resolved_entry
            path_status = resolved_path
            reasons.append(f"terminal_path_resolved_before_{censor_reason}")
        else:
            completeness = OutcomeCompleteness.CENSORED
            entry_status = OutcomeEntryStatus.UNSUPPORTED
            path_status = censor_path
            reasons.append(censor_reason)
    elif pending:
        resolution_observations = _continuous_prefix(series, observations, policy)
        partial_entry, partial_path = _resolve_trade_path(decision, resolution_observations)
        if partial_path in {
            OutcomePathStatus.TARGET_FIRST,
            OutcomePathStatus.STOP_FIRST,
        }:
            completeness = OutcomeCompleteness.PARTIAL
            entry_status = partial_entry
            path_status = partial_path
            reasons.append("terminal_path_resolved_before_horizon_end")
        else:
            completeness = OutcomeCompleteness.PENDING
            entry_status = OutcomeEntryStatus.PENDING
            path_status = OutcomePathStatus.PENDING_HORIZON
            reasons.append("horizon_not_yet_observable")
    elif not complete:
        resolution_observations = _continuous_prefix(series, observations, policy)
        partial_entry, partial_path = _resolve_trade_path(decision, resolution_observations)
        if partial_path in {OutcomePathStatus.TARGET_FIRST, OutcomePathStatus.STOP_FIRST}:
            completeness = OutcomeCompleteness.PARTIAL
            entry_status = partial_entry
            path_status = partial_path
            reasons.append("terminal_path_resolved_before_missing_horizon_tail")
        else:
            completeness = OutcomeCompleteness.CENSORED
            entry_status = OutcomeEntryStatus.UNSUPPORTED
            path_status = OutcomePathStatus.MISSING_BARS
            reasons.append(coverage_reason)
    else:
        completeness = OutcomeCompleteness.COMPLETE
        entry_status, path_status = _resolve_trade_path(decision, observations)
        if path_status in {
            OutcomePathStatus.ENTRY_BAR_AMBIGUOUS,
            OutcomePathStatus.SAME_BAR_AMBIGUOUS,
            OutcomePathStatus.GAP_THROUGH_AMBIGUOUS,
        }:
            completeness = OutcomeCompleteness.CENSORED
            reasons.append(path_status.value)

    reference_supported = (
        complete
        and observations
        and not hard_unavailable
        and action is None
        and not action_unresolved
        and halt is None
        and not halt_receipt
        and not adjustment_unsupported
    )
    close_observation = observations[-1] if reference_supported else None
    return _RecordResolution(
        observations=observations,
        reference=reference,
        resolution_observations=resolution_observations,
        completeness=completeness,
        entry_status=entry_status,
        path_status=path_status,
        reasons=tuple(reasons),
        close_observation=close_observation,
    )


@dataclass(frozen=True)
class _PathDetails:
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None
    entry_observation: OutcomeBarEvidence | None = None
    exit_observation: OutcomeBarEvidence | None = None
    target_observation: OutcomeBarEvidence | None = None
    stop_observation: OutcomeBarEvidence | None = None
    entry_index: int | None = None
    exit_index: int | None = None


def _horizon_is_complete(
    *,
    series: OutcomeObservationSeries,
    observations: tuple[OutcomeBarEvidence, ...],
    horizon: OutcomeHorizon,
    policy: OutcomeLabelPolicy,
) -> tuple[bool, str]:
    if not observations:
        return False, "no_post_decision_bars_inside_horizon"
    expected = timedelta(seconds=policy.expected_bar_interval_seconds)
    if observations[0].interval_start_at != series.first_expected_interval_start_at:
        return False, "missing_initial_post_decision_interval"
    if observations[-1].interval_end_at != horizon.end_at:
        return False, "bars_do_not_reach_horizon_end"
    previous_end = observations[0].interval_start_at
    for observation in observations:
        if observation.interval_end_at - observation.interval_start_at != expected:
            return False, "unexpected_bar_interval"
        if observation.interval_start_at != previous_end:
            return False, "missing_interval_inside_horizon"
        previous_end = observation.interval_end_at
    receipt = series.coverage_receipt
    if receipt.observed_end is None or receipt.observed_end < horizon.end_at:
        return False, "coverage_receipt_does_not_reach_horizon_end"
    covered_start = observations[0].interval_start_at
    if any(
        start < horizon.end_at and end > covered_start
        for start, end in receipt.missing_intervals
    ):
        return False, "coverage_receipt_reports_missing_interval_inside_horizon"
    return True, ""


def _continuous_prefix(
    series: OutcomeObservationSeries,
    observations: tuple[OutcomeBarEvidence, ...],
    policy: OutcomeLabelPolicy,
    *,
    through_at: datetime | None = None,
) -> tuple[OutcomeBarEvidence, ...]:
    if not observations or (
        observations[0].interval_start_at != series.first_expected_interval_start_at
    ):
        return ()
    expected = timedelta(seconds=policy.expected_bar_interval_seconds)
    previous_end = series.first_expected_interval_start_at
    prefix: list[OutcomeBarEvidence] = []
    for observation in observations:
        if through_at is not None and observation.interval_end_at > through_at:
            break
        if observation.interval_start_at != previous_end or (
            observation.interval_end_at - observation.interval_start_at != expected
        ):
            break
        if any(
            start < observation.interval_end_at and end > observation.interval_start_at
            for start, end in series.coverage_receipt.missing_intervals
        ):
            break
        prefix.append(observation)
        previous_end = observation.interval_end_at
    return tuple(prefix)


def _resolve_trade_path(
    decision: TradeDecision,
    observations: tuple[OutcomeBarEvidence, ...],
) -> tuple[OutcomeEntryStatus, OutcomePathStatus]:
    evaluation = decision.evaluation
    if decision.direction not in {StrategyDirection.LONG, StrategyDirection.SHORT}:
        return OutcomeEntryStatus.UNSUPPORTED, OutcomePathStatus.UNSUPPORTED_EVIDENCE
    if (
        evaluation.entry_price is None
        or evaluation.invalidation_price is None
        or evaluation.target_price is None
    ):
        if decision.evaluation.status is not EvaluationStatus.ELIGIBLE:
            return OutcomeEntryStatus.NOT_APPLICABLE, OutcomePathStatus.NOT_APPLICABLE
        return OutcomeEntryStatus.UNATTAINABLE, OutcomePathStatus.UNATTAINABLE_FILL
    entry = evaluation.entry_price
    stop = evaluation.invalidation_price
    target = evaluation.target_price
    valid_geometry = (
        decision.direction is StrategyDirection.LONG and stop < entry < target
    ) or (decision.direction is StrategyDirection.SHORT and target < entry < stop)
    if not valid_geometry:
        return OutcomeEntryStatus.UNATTAINABLE, OutcomePathStatus.UNATTAINABLE_FILL
    for index, observation in enumerate(observations):
        bar = observation.bar
        gap_entry = (
            decision.direction is StrategyDirection.LONG and bar.open_price > entry
        ) or (decision.direction is StrategyDirection.SHORT and bar.open_price < entry)
        if gap_entry:
            return OutcomeEntryStatus.GAP_THROUGH_AMBIGUOUS, OutcomePathStatus.GAP_THROUGH_AMBIGUOUS
        entry_touch = bar.low_price <= entry <= bar.high_price
        if not entry_touch:
            invalidated_before_entry = (
                decision.direction is StrategyDirection.LONG and bar.low_price <= stop
            ) or (decision.direction is StrategyDirection.SHORT and bar.high_price >= stop)
            if invalidated_before_entry:
                return OutcomeEntryStatus.UNATTAINABLE, OutcomePathStatus.UNATTAINABLE_FILL
            continue
        target_touch = bar.low_price <= target <= bar.high_price
        stop_touch = bar.low_price <= stop <= bar.high_price
        if target_touch or stop_touch:
            return OutcomeEntryStatus.ENTRY_BAR_AMBIGUOUS, OutcomePathStatus.ENTRY_BAR_AMBIGUOUS
        if index == len(observations) - 1:
            return OutcomeEntryStatus.ENTRY_BAR_AMBIGUOUS, OutcomePathStatus.ENTRY_BAR_AMBIGUOUS
        for later in observations[index + 1 :]:
            later_bar = later.bar
            gap_target = (
                decision.direction is StrategyDirection.LONG and later_bar.open_price > target
            ) or (decision.direction is StrategyDirection.SHORT and later_bar.open_price < target)
            gap_stop = (
                decision.direction is StrategyDirection.LONG and later_bar.open_price < stop
            ) or (decision.direction is StrategyDirection.SHORT and later_bar.open_price > stop)
            if gap_target or gap_stop:
                return OutcomeEntryStatus.FILLED, OutcomePathStatus.GAP_THROUGH_AMBIGUOUS
            if later_bar.open_price == target:
                return OutcomeEntryStatus.FILLED, OutcomePathStatus.TARGET_FIRST
            if later_bar.open_price == stop:
                return OutcomeEntryStatus.FILLED, OutcomePathStatus.STOP_FIRST
            target_touch = later_bar.low_price <= target <= later_bar.high_price
            stop_touch = later_bar.low_price <= stop <= later_bar.high_price
            if target_touch and stop_touch:
                return OutcomeEntryStatus.FILLED, OutcomePathStatus.SAME_BAR_AMBIGUOUS
            if target_touch:
                return OutcomeEntryStatus.FILLED, OutcomePathStatus.TARGET_FIRST
            if stop_touch:
                return OutcomeEntryStatus.FILLED, OutcomePathStatus.STOP_FIRST
        return OutcomeEntryStatus.FILLED, OutcomePathStatus.HORIZON_EXIT
    return OutcomeEntryStatus.NO_ENTRY, OutcomePathStatus.NO_ENTRY


def _path_details(
    decision: TradeDecision,
    observations: tuple[OutcomeBarEvidence, ...],
    entry_status: OutcomeEntryStatus,
    path_status: OutcomePathStatus,
) -> _PathDetails:
    if entry_status is not OutcomeEntryStatus.FILLED:
        return _PathDetails()
    evaluation = decision.evaluation
    assert evaluation.entry_price is not None
    assert evaluation.invalidation_price is not None
    assert evaluation.target_price is not None
    entry_index = next(
        index
        for index, observation in enumerate(observations)
        if observation.bar.low_price <= evaluation.entry_price <= observation.bar.high_price
    )
    entry_observation = observations[entry_index]
    if path_status is OutcomePathStatus.TARGET_FIRST:
        exit_index = next(
            index
            for index in range(entry_index + 1, len(observations))
            if observations[index].bar.low_price
            <= evaluation.target_price
            <= observations[index].bar.high_price
        )
        target_observation = observations[exit_index]
        return _PathDetails(
            evaluation.entry_price,
            evaluation.target_price,
            entry_observation,
            target_observation,
            target_observation,
            None,
            entry_index,
            exit_index,
        )
    if path_status is OutcomePathStatus.STOP_FIRST:
        exit_index = next(
            index
            for index in range(entry_index + 1, len(observations))
            if observations[index].bar.low_price
            <= evaluation.invalidation_price
            <= observations[index].bar.high_price
        )
        stop_observation = observations[exit_index]
        return _PathDetails(
            evaluation.entry_price,
            evaluation.invalidation_price,
            entry_observation,
            stop_observation,
            None,
            stop_observation,
            entry_index,
            exit_index,
        )
    if path_status is OutcomePathStatus.HORIZON_EXIT:
        return _PathDetails(
            evaluation.entry_price,
            observations[-1].bar.close_price,
            entry_observation,
            observations[-1],
            None,
            None,
            entry_index,
            len(observations) - 1,
        )
    return _PathDetails(
        entry_price=evaluation.entry_price,
        entry_observation=entry_observation,
        entry_index=entry_index,
    )


def _build_metrics(
    *,
    decision: TradeDecision,
    risk: ExecutionRiskEvidence | None,
    reference: OutcomeBarEvidence | None,
    close_observation: OutcomeBarEvidence | None,
    observations: tuple[OutcomeBarEvidence, ...],
    completeness: OutcomeCompleteness,
    entry_status: OutcomeEntryStatus,
    path_status: OutcomePathStatus,
    path: _PathDetails,
) -> tuple[OutcomeNumericEvidence, ...]:
    values: dict[OutcomeMetric, OutcomeNumericEvidence] = {}
    sign = _direction_sign(decision.direction)
    if (
        reference is not None
        and close_observation is not None
        and sign is not None
    ):
        values[OutcomeMetric.REFERENCE_HORIZON_RETURN] = _derived_metric(
            OutcomeMetric.REFERENCE_HORIZON_RETURN,
            sign * (close_observation.bar.close_price - reference.bar.open_price)
            / reference.bar.open_price,
            (reference, close_observation),
            "directional_first_post_decision_open_to_horizon_close",
        )
    else:
        values[OutcomeMetric.REFERENCE_HORIZON_RETURN] = _unavailable_metric(
            OutcomeMetric.REFERENCE_HORIZON_RETURN,
            "complete_reference_and_horizon_close_required",
        )

    resolved = (
        completeness in {OutcomeCompleteness.COMPLETE, OutcomeCompleteness.PARTIAL}
        and entry_status is OutcomeEntryStatus.FILLED
        and path_status
        in {
            OutcomePathStatus.TARGET_FIRST,
            OutcomePathStatus.STOP_FIRST,
            OutcomePathStatus.HORIZON_EXIT,
        }
        and path.entry_price is not None
        and path.exit_price is not None
        and path.entry_index is not None
        and path.exit_index is not None
        and sign is not None
        and decision.evaluation.invalidation_price is not None
    )
    if resolved:
        assert path.entry_price is not None
        assert path.exit_price is not None
        assert path.entry_index is not None
        assert path.exit_index is not None
        assert sign is not None
        assert decision.evaluation.invalidation_price is not None
        stop_distance = abs(path.entry_price - decision.evaluation.invalidation_price)
        post_entry_observations = observations[path.entry_index + 1 : path.exit_index + 1]
        intervening_observations = observations[path.entry_index + 1 : path.exit_index]
        gross_r = sign * (path.exit_price - path.entry_price) / stop_distance
        favorable_sources: tuple[OutcomeBarEvidence | None, ...]
        adverse_sources: tuple[OutcomeBarEvidence | None, ...]
        if path_status is OutcomePathStatus.TARGET_FIRST:
            favorable = sign * (path.exit_price - path.entry_price)
            favorable_sources = (path.entry_observation, path.exit_observation)
            adverse = min(
                [
                    Decimal("0"),
                    *(
                    (
                        item.bar.low_price - path.entry_price
                        if sign == 1
                        else path.entry_price - item.bar.high_price
                    )
                    for item in intervening_observations
                    ),
                ]
            )
            adverse_sources = (
                path.entry_observation,
                *intervening_observations,
                path.exit_observation,
            )
        elif path_status is OutcomePathStatus.STOP_FIRST:
            favorable = max(
                [
                    Decimal("0"),
                    *(
                    (
                        item.bar.high_price - path.entry_price
                        if sign == 1
                        else path.entry_price - item.bar.low_price
                    )
                    for item in intervening_observations
                    ),
                ]
            )
            favorable_sources = (
                path.entry_observation,
                *intervening_observations,
                path.exit_observation,
            )
            adverse = -stop_distance
            adverse_sources = (path.entry_observation, path.exit_observation)
        else:
            favorable = max(
                Decimal("0"),
                max(
                    item.bar.high_price - path.entry_price
                    if sign == 1
                    else path.entry_price - item.bar.low_price
                    for item in post_entry_observations
                ),
            )
            adverse = min(
                Decimal("0"),
                min(
                    item.bar.low_price - path.entry_price
                    if sign == 1
                    else path.entry_price - item.bar.high_price
                    for item in post_entry_observations
                ),
            )
            favorable_sources = post_entry_observations
            adverse_sources = post_entry_observations
        values[OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R] = _derived_metric(
            OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R,
            favorable / stop_distance,
            favorable_sources,
            "bar_extreme_maximum_favorable_excursion_over_stop_distance",
        )
        values[OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R] = _derived_metric(
            OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R,
            adverse / stop_distance,
            adverse_sources,
            "bar_extreme_maximum_adverse_excursion_over_stop_distance",
        )
        values[OutcomeMetric.SIMULATED_GROSS_R] = _derived_metric(
            OutcomeMetric.SIMULATED_GROSS_R,
            gross_r,
            (path.entry_observation, path.exit_observation),
            "modeled_directional_exit_delta_over_stop_distance",
        )
        after_cost = _after_cost_value(
            risk=risk,
            modeled_entry=path.entry_price,
            modeled_stop=decision.evaluation.invalidation_price,
            modeled_exit=path.exit_price,
            sign=sign,
        )
        if after_cost is None:
            values[OutcomeMetric.SIMULATED_AFTER_COST_R] = _unavailable_metric(
                OutcomeMetric.SIMULATED_AFTER_COST_R,
                "risk_geometry_or_cost_inputs_do_not_exactly_match_modeled_path",
            )
        else:
            values[OutcomeMetric.SIMULATED_AFTER_COST_R] = _derived_metric(
                OutcomeMetric.SIMULATED_AFTER_COST_R,
                after_cost,
                (path.entry_observation, path.exit_observation),
                "exact_risk_quantity_and_round_trip_cost_adjusted_modeled_return",
            )
    else:
        for metric in (
            OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R,
            OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R,
            OutcomeMetric.SIMULATED_GROSS_R,
            OutcomeMetric.SIMULATED_AFTER_COST_R,
        ):
            values[metric] = _unavailable_metric(metric, "resolved_filled_path_required")

    _add_touch_time_metrics(
        values,
        lower_metric=OutcomeMetric.TIME_TO_TARGET_LOWER_BOUND,
        upper_metric=OutcomeMetric.TIME_TO_TARGET_UPPER_BOUND,
        entry=path.entry_observation,
        touch=path.target_observation,
        label="target",
    )
    _add_touch_time_metrics(
        values,
        lower_metric=OutcomeMetric.TIME_TO_STOP_LOWER_BOUND,
        upper_metric=OutcomeMetric.TIME_TO_STOP_UPPER_BOUND,
        entry=path.entry_observation,
        touch=path.stop_observation,
        label="stop",
    )
    return tuple(values[metric] for metric in CANONICAL_OUTCOME_METRICS)


def _add_touch_time_metrics(
    values: dict[OutcomeMetric, OutcomeNumericEvidence],
    *,
    lower_metric: OutcomeMetric,
    upper_metric: OutcomeMetric,
    entry: OutcomeBarEvidence | None,
    touch: OutcomeBarEvidence | None,
    label: str,
) -> None:
    if entry is None or touch is None:
        values[lower_metric] = _unavailable_metric(lower_metric, f"{label}_not_observed")
        values[upper_metric] = _unavailable_metric(upper_metric, f"{label}_not_observed")
        return
    lower = max(
        Decimal("0"),
        _timedelta_decimal_seconds(touch.interval_start_at - entry.interval_end_at),
    )
    upper = _timedelta_decimal_seconds(touch.interval_end_at - entry.interval_start_at)
    source = (entry, touch)
    values[lower_metric] = _derived_metric(
        lower_metric,
        lower,
        source,
        f"conservative_{label}_touch_interval_lower_bound",
    )
    values[upper_metric] = _derived_metric(
        upper_metric,
        upper,
        source,
        f"conservative_{label}_touch_interval_upper_bound",
    )


def _derived_metric(
    metric: OutcomeMetric,
    value: Decimal,
    observations: tuple[OutcomeBarEvidence | None, ...],
    method: str,
) -> OutcomeNumericEvidence:
    source = tuple(item for item in observations if item is not None)
    return OutcomeNumericEvidence(
        metric=metric,
        unit=_METRIC_UNITS[metric],
        value=value,
        status=OutcomeValueStatus.DERIVED,
        observed_at=max(item.available_at for item in source),
        source_observation_ids=tuple(dict.fromkeys(item.observation_id for item in source)),
        method=method,
    )


def _unavailable_metric(metric: OutcomeMetric, reason: str) -> OutcomeNumericEvidence:
    return OutcomeNumericEvidence(
        metric=metric,
        unit=_METRIC_UNITS[metric],
        value=None,
        status=OutcomeValueStatus.UNAVAILABLE,
        observed_at=None,
        source_observation_ids=(),
        method="outcome_metric_unavailable",
        reason=reason,
    )


def _touch_interval(observation: OutcomeBarEvidence | None) -> OutcomeTouchInterval | None:
    if observation is None:
        return None
    return OutcomeTouchInterval(
        observation_id=observation.observation_id,
        observation_content_hash_sha256=observation.content_hash(),
        interval_start_at=observation.interval_start_at,
        interval_end_at=observation.interval_end_at,
    )
