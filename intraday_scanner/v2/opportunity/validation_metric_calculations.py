"""Deterministic Decimal calculations for validation metric populations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from intraday_scanner.v2.opportunity.validation_metric_contracts import (
    CANONICAL_EXECUTION_STRESS_SCENARIOS,
    CANONICAL_VALIDATION_TRADING_METRICS,
    VALIDATION_TRADING_METRIC_UNITS,
    ExecutionCostEvidenceQuality,
    ExecutionStressScenario,
    TradeMetricDisposition,
    ValidationMetricReportStatus,
    ValidationMetricValueStatus,
    ValidationTradingMetric,
    ValidationTradingMetricPolicy,
    ValidationTradingMetricUnit,
    quantize_validation_metric,
)
from intraday_scanner.v2.opportunity.validation_metric_math import (
    _decimal_mean,
    _decimal_ratio,
    _decimal_sum,
    _downside_deviation,
    _metric_decimal_context,
    _population_standard_deviation,
    _session_drawdown,
)
from intraday_scanner.v2.opportunity.validation_metric_population import (
    ExecutionStressTradeEvidence,
)


@dataclass(frozen=True)
class _MetricCalculationInput:
    row_id: str
    row_content_hash_sha256: str
    session_source_id: str
    trade_evidence: ExecutionStressTradeEvidence


@dataclass(frozen=True)
class _MetricUnitInventory:
    ids: tuple[str, ...]
    content_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.ids) != len(self.content_hashes):
            raise ValueError("metric unit IDs and content hashes must align")
        if len(self.ids) != len(set(self.ids)):
            raise ValueError("metric unit IDs must be unique and canonical")


@dataclass(frozen=True)
class _ValidationTradingMetricValue:
    scenario: ExecutionStressScenario
    metric: ValidationTradingMetric
    unit: ValidationTradingMetricUnit
    status: ValidationMetricValueStatus
    value: Decimal | None
    numerator_count: int | None
    denominator_count: int | None
    numerator_unit_ids: tuple[str, ...]
    numerator_unit_content_hashes: tuple[str, ...]
    denominator_unit_ids: tuple[str, ...]
    denominator_unit_content_hashes: tuple[str, ...]
    source_row_ids: tuple[str, ...]
    source_row_content_hashes: tuple[str, ...]
    source_trade_evidence_ids: tuple[str, ...]
    source_trade_evidence_content_hashes: tuple[str, ...]
    reason: str | None

    def __post_init__(self) -> None:
        numerator = _MetricUnitInventory(
            self.numerator_unit_ids,
            self.numerator_unit_content_hashes,
        )
        denom = _MetricUnitInventory(
            self.denominator_unit_ids,
            self.denominator_unit_content_hashes,
        )
        if type(self.numerator_count) is not int or self.numerator_count != len(numerator.ids):
            raise ValueError("metric numerator count must equal its exact unit inventory")
        if type(self.denominator_count) is not int or self.denominator_count != len(denom.ids):
            raise ValueError("metric denominator count must equal its exact unit inventory")


_PATH_METRICS = {
    ValidationTradingMetric.TOTAL_TRADES,
    ValidationTradingMetric.MAXIMUM_FAVORABLE_EXCURSION_R,
    ValidationTradingMetric.MAXIMUM_ADVERSE_EXCURSION_R,
    ValidationTradingMetric.AVERAGE_HOLDING_LOWER_SECONDS,
    ValidationTradingMetric.AVERAGE_HOLDING_UPPER_SECONDS,
}
_ALWAYS_UNAVAILABLE = {
    ValidationTradingMetric.CAPITAL_MAX_DRAWDOWN,
    ValidationTradingMetric.ANNUALIZED_RETURN,
    ValidationTradingMetric.BENCHMARK_EXCESS_RETURN,
}
_SESSION_METRICS = {
    ValidationTradingMetric.MEAN_SESSION_R,
    ValidationTradingMetric.SESSION_R_SHARPE,
    ValidationTradingMetric.SESSION_R_SORTINO,
    ValidationTradingMetric.SESSION_R_MAX_DRAWDOWN,
    ValidationTradingMetric.SESSION_R_MAX_DRAWDOWN_DURATION_SESSIONS,
}
_EMPTY_UNITS = _MetricUnitInventory((), ())


def _calculate_metric_values(
    inputs: tuple[_MetricCalculationInput, ...],
    *,
    session_source_ids: tuple[str, ...],
    session_content_hashes: tuple[str, ...],
    scope_status: ValidationMetricReportStatus,
    policy: ValidationTradingMetricPolicy,
) -> tuple[_ValidationTradingMetricValue, ...]:
    rows = tuple(item.row_id for item in inputs)
    row_hashes = tuple(item.row_content_hash_sha256 for item in inputs)
    trade_ids = tuple(item.trade_evidence.trade_evidence_id for item in inputs)
    trade_hashes = tuple(item.trade_evidence.content_hash() for item in inputs)
    lineage = (rows, row_hashes, trade_ids, trade_hashes)
    all_row_units = _row_units(inputs)
    session_units = _MetricUnitInventory(session_source_ids, session_content_hashes)
    if not inputs:
        return tuple(
            (
                _unsupported_metric(scenario, metric, lineage)
                if metric in _ALWAYS_UNAVAILABLE
                else _metric(
                    scenario,
                    metric,
                    status=ValidationMetricValueStatus.INSUFFICIENT_DATA,
                    value=None,
                    lineage=lineage,
                    numerator_units=_EMPTY_UNITS,
                    denominator_units=_candidate_denominator(
                        metric, all_row_units, session_units
                    ),
                    reason="scope_population_is_empty",
                )
            )
            for scenario in CANONICAL_EXECUTION_STRESS_SCENARIOS
            for metric in CANONICAL_VALIDATION_TRADING_METRICS
        )
    diagnostic_reason = (
        None
        if scope_status is ValidationMetricReportStatus.AVAILABLE
        else f"bounded_diagnostic_parent_scope_{scope_status.value}"
    )
    unresolved = tuple(
        item
        for item in inputs
        if item.trade_evidence.disposition is TradeMetricDisposition.UNRESOLVED_TAKE
    )
    if unresolved:
        return tuple(
            (
                _unsupported_metric(scenario, metric, lineage)
                if metric in _ALWAYS_UNAVAILABLE
                else _metric(
                    scenario,
                    metric,
                    status=ValidationMetricValueStatus.INCOMPLETE,
                    value=None,
                    lineage=lineage,
                    numerator_units=_EMPTY_UNITS,
                    denominator_units=_candidate_denominator(
                        metric, all_row_units, session_units
                    ),
                    reason=(
                        "unresolved_take_execution_truth"
                        if diagnostic_reason is None
                        else f"unresolved_take_execution_truth:{diagnostic_reason}"
                    ),
                )
            )
            for scenario in CANONICAL_EXECUTION_STRESS_SCENARIOS
            for metric in CANONICAL_VALIDATION_TRADING_METRICS
        )
    fills = tuple(
        item
        for item in inputs
        if item.trade_evidence.disposition
        in {
            TradeMetricDisposition.RESOLVED_FILL_COST_COMPLETE,
            TradeMetricDisposition.RESOLVED_FILL_COST_UNAVAILABLE,
        }
    )
    cost_blocked = any(
        item.trade_evidence.disposition
        is TradeMetricDisposition.RESOLVED_FILL_COST_UNAVAILABLE
        for item in fills
    )
    _validate_trade_scenario_membership(fills)
    values: list[_ValidationTradingMetricValue] = []
    for scenario in CANONICAL_EXECUTION_STRESS_SCENARIOS:
        after_cost = tuple(
            _scenario_value(
                item.trade_evidence,
                scenario,
                "after_cost_r_unquantized",
            )
            for item in fills
            if item.trade_evidence.disposition
            is TradeMetricDisposition.RESOLVED_FILL_COST_COMPLETE
        )
        execution_costs = tuple(
            _scenario_value(item.trade_evidence, scenario, "total_round_trip_cost")
            for item in fills
            if item.trade_evidence.disposition
            is TradeMetricDisposition.RESOLVED_FILL_COST_COMPLETE
        )
        for metric in CANONICAL_VALIDATION_TRADING_METRICS:
            values.append(
                _calculate_one(
                    metric,
                    scenario=scenario,
                    inputs=inputs,
                    fills=fills,
                    after_cost=after_cost,
                    execution_costs=execution_costs,
                    session_source_ids=session_source_ids,
                    session_content_hashes=session_content_hashes,
                    cost_blocked=cost_blocked,
                    diagnostic_reason=diagnostic_reason,
                    policy=policy,
                    lineage=lineage,
                )
            )
    return tuple(values)


def _calculate_one(
    metric: ValidationTradingMetric,
    *,
    scenario: ExecutionStressScenario,
    inputs: tuple[_MetricCalculationInput, ...],
    fills: tuple[_MetricCalculationInput, ...],
    after_cost: tuple[Decimal, ...],
    execution_costs: tuple[Decimal, ...],
    session_source_ids: tuple[str, ...],
    session_content_hashes: tuple[str, ...],
    cost_blocked: bool,
    diagnostic_reason: str | None,
    policy: ValidationTradingMetricPolicy,
    lineage: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]],
) -> _ValidationTradingMetricValue:
    output_status = (
        ValidationMetricValueStatus.PROVISIONAL
        if diagnostic_reason is not None
        else ValidationMetricValueStatus.AVAILABLE
    )
    fill_count = len(fills)
    all_row_units = _row_units(inputs)
    fill_units = _row_units(fills)
    session_units = _MetricUnitInventory(session_source_ids, session_content_hashes)
    cost_complete_fills = tuple(
        item
        for item in fills
        if item.trade_evidence.disposition
        is TradeMetricDisposition.RESOLVED_FILL_COST_COMPLETE
    )
    cost_complete_units = _row_units(cost_complete_fills)
    if metric in _ALWAYS_UNAVAILABLE:
        return _unsupported_metric(scenario, metric, lineage)
    if metric is ValidationTradingMetric.TOTAL_TRADES:
        return _metric(
            scenario,
            metric,
            status=output_status,
            value=Decimal(fill_count),
            numerator_units=fill_units,
            denominator_units=all_row_units,
            lineage=lineage,
            reason=diagnostic_reason,
        )
    if metric in _PATH_METRICS:
        field = {
            ValidationTradingMetric.MAXIMUM_FAVORABLE_EXCURSION_R: (
                "maximum_favorable_excursion_r"
            ),
            ValidationTradingMetric.MAXIMUM_ADVERSE_EXCURSION_R: (
                "maximum_adverse_excursion_r"
            ),
            ValidationTradingMetric.AVERAGE_HOLDING_LOWER_SECONDS: (
                "holding_seconds_lower_bound"
            ),
            ValidationTradingMetric.AVERAGE_HOLDING_UPPER_SECONDS: (
                "holding_seconds_upper_bound"
            ),
        }[metric]
        if not fills:
            return _insufficient(
                scenario,
                metric,
                lineage,
                "no_resolved_take_fills",
                denominator_units=fill_units,
            )
        path_values = tuple(_required_trade_decimal(item.trade_evidence, field) for item in fills)
        return _available(
            scenario,
            metric,
            quantize_validation_metric(_decimal_mean(path_values, policy), policy),
            output_status,
            lineage,
            numerator_units=fill_units,
            denominator_units=fill_units,
            reason=diagnostic_reason,
        )
    if cost_blocked:
        return _metric(
            scenario,
            metric,
            status=ValidationMetricValueStatus.INCOMPLETE,
            value=None,
            lineage=lineage,
            numerator_units=_EMPTY_UNITS,
            denominator_units=(
                session_units if metric in _SESSION_METRICS else fill_units
            ),
            reason=_cost_block_reason(fills),
        )
    win_pairs = tuple(
        (item, value)
        for item, value in zip(cost_complete_fills, after_cost, strict=True)
        if value > 0
    )
    loss_pairs = tuple(
        (item, value)
        for item, value in zip(cost_complete_fills, after_cost, strict=True)
        if value < 0
    )
    breakeven_pairs = tuple(
        (item, value)
        for item, value in zip(cost_complete_fills, after_cost, strict=True)
        if value == 0
    )
    wins = tuple(value for _, value in win_pairs)
    losses = tuple(value for _, value in loss_pairs)
    breakevens = tuple(value for _, value in breakeven_pairs)
    winner_units = _row_units(tuple(item for item, _ in win_pairs))
    loser_units = _row_units(tuple(item for item, _ in loss_pairs))
    breakeven_units = _row_units(tuple(item for item, _ in breakeven_pairs))
    if metric in {
        ValidationTradingMetric.WINS,
        ValidationTradingMetric.LOSSES,
        ValidationTradingMetric.BREAKEVENS,
    }:
        count = {
            ValidationTradingMetric.WINS: len(wins),
            ValidationTradingMetric.LOSSES: len(losses),
            ValidationTradingMetric.BREAKEVENS: len(breakevens),
        }[metric]
        numerator_units = {
            ValidationTradingMetric.WINS: winner_units,
            ValidationTradingMetric.LOSSES: loser_units,
            ValidationTradingMetric.BREAKEVENS: breakeven_units,
        }[metric]
        return _available(
            scenario,
            metric,
            Decimal(count),
            output_status,
            lineage,
            numerator_units=numerator_units,
            denominator_units=cost_complete_units,
            reason=diagnostic_reason,
        )
    if metric is ValidationTradingMetric.TOTAL_EXECUTION_COST_USD:
        return _available(
            scenario,
            metric,
            quantize_validation_metric(_decimal_sum(execution_costs, policy), policy),
            output_status,
            lineage,
            numerator_units=cost_complete_units,
            denominator_units=cost_complete_units,
            reason=diagnostic_reason,
        )
    if metric is ValidationTradingMetric.WIN_RATE:
        if not after_cost:
            return _insufficient(
                scenario,
                metric,
                lineage,
                "no_resolved_take_fills",
                denominator_units=cost_complete_units,
            )
        return _available(
            scenario,
            metric,
            _decimal_ratio(Decimal(len(wins)), Decimal(len(after_cost)), policy),
            output_status,
            lineage,
            numerator_units=winner_units,
            denominator_units=cost_complete_units,
            reason=diagnostic_reason,
        )
    if metric is ValidationTradingMetric.EXPECTANCY_R:
        if not after_cost:
            return _insufficient(
                scenario,
                metric,
                lineage,
                "no_resolved_take_fills",
                denominator_units=cost_complete_units,
            )
        return _available(
            scenario,
            metric,
            quantize_validation_metric(_decimal_mean(after_cost, policy), policy),
            output_status,
            lineage,
            numerator_units=cost_complete_units,
            denominator_units=cost_complete_units,
            reason=diagnostic_reason,
        )
    if metric is ValidationTradingMetric.PROFIT_FACTOR:
        denominator = _decimal_sum(losses, policy).copy_abs()
        if not after_cost or denominator == 0:
            return _insufficient(
                scenario,
                metric,
                lineage,
                "loss_denominator_is_zero_or_missing",
                numerator_units=winner_units,
                denominator_units=loser_units,
            )
        return _available(
            scenario,
            metric,
            _decimal_ratio(_decimal_sum(wins, policy), denominator, policy),
            output_status,
            lineage,
            numerator_units=winner_units,
            denominator_units=loser_units,
            reason=diagnostic_reason,
        )
    if metric in {
        ValidationTradingMetric.AVERAGE_WIN_R,
        ValidationTradingMetric.AVERAGE_LOSS_R,
    }:
        population = wins if metric is ValidationTradingMetric.AVERAGE_WIN_R else losses
        population_units = (
            winner_units
            if metric is ValidationTradingMetric.AVERAGE_WIN_R
            else loser_units
        )
        if not population:
            return _insufficient(
                scenario,
                metric,
                lineage,
                "required_win_or_loss_population_is_empty",
                numerator_units=population_units,
                denominator_units=population_units,
            )
        return _available(
            scenario,
            metric,
            quantize_validation_metric(_decimal_mean(population, policy), policy),
            output_status,
            lineage,
            numerator_units=population_units,
            denominator_units=population_units,
            reason=diagnostic_reason,
        )
    session_values = _session_returns(inputs, session_source_ids, scenario, policy)
    if not session_values:
        return _insufficient(
            scenario,
            metric,
            lineage,
            "session_population_is_empty",
            denominator_units=session_units,
        )
    mean = _decimal_mean(session_values, policy)
    if metric is ValidationTradingMetric.MEAN_SESSION_R:
        return _available(
            scenario,
            metric,
            quantize_validation_metric(mean, policy),
            output_status,
            lineage,
            numerator_units=session_units,
            denominator_units=session_units,
            reason=diagnostic_reason,
        )
    if metric is ValidationTradingMetric.SESSION_R_SHARPE:
        deviation = _population_standard_deviation(session_values, policy)
        if deviation == 0:
            return _insufficient(
                scenario,
                metric,
                lineage,
                "session_r_variance_is_zero",
                numerator_units=session_units,
                denominator_units=session_units,
            )
        return _available(
            scenario,
            metric,
            _decimal_ratio(mean, deviation, policy),
            output_status,
            lineage,
            numerator_units=session_units,
            denominator_units=session_units,
            reason=diagnostic_reason,
        )
    if metric is ValidationTradingMetric.SESSION_R_SORTINO:
        downside = _downside_deviation(
            session_values, policy.session_downside_target_r, policy
        )
        if downside == 0:
            return _insufficient(
                scenario,
                metric,
                lineage,
                "downside_deviation_is_zero",
                numerator_units=session_units,
                denominator_units=session_units,
            )
        return _available(
            scenario,
            metric,
            _sortino_ratio(mean, downside, policy),
            output_status,
            lineage,
            numerator_units=session_units,
            denominator_units=session_units,
            reason=diagnostic_reason,
        )
    drawdown, duration = _session_drawdown(session_values, policy)
    if metric is ValidationTradingMetric.SESSION_R_MAX_DRAWDOWN:
        value = drawdown
    elif metric is ValidationTradingMetric.SESSION_R_MAX_DRAWDOWN_DURATION_SESSIONS:
        value = Decimal(duration)
    else:
        raise ValueError(f"unhandled validation trading metric {metric.value}")
    return _available(
        scenario,
        metric,
        quantize_validation_metric(value, policy),
        output_status,
        lineage,
        numerator_units=session_units,
        denominator_units=session_units,
        reason=diagnostic_reason,
    )


def _session_returns(
    inputs: tuple[_MetricCalculationInput, ...],
    session_source_ids: tuple[str, ...],
    scenario: ExecutionStressScenario,
    policy: ValidationTradingMetricPolicy,
) -> tuple[Decimal, ...]:
    values = {session_id: Decimal("0") for session_id in session_source_ids}
    for item in inputs:
        if item.trade_evidence.disposition is TradeMetricDisposition.RESOLVED_FILL_COST_COMPLETE:
            with _metric_decimal_context(policy):
                values[item.session_source_id] += _scenario_value(
                    item.trade_evidence,
                    scenario,
                    "after_cost_r_unquantized",
                )
    return tuple(values[session_id] for session_id in session_source_ids)


def _scenario_value(
    evidence: ExecutionStressTradeEvidence,
    scenario: ExecutionStressScenario,
    field: str,
) -> Decimal:
    item = next(value for value in evidence.stress_scenarios if value.scenario is scenario)
    return getattr(item, field)  # type: ignore[no-any-return]


def _required_trade_decimal(evidence: ExecutionStressTradeEvidence, field: str) -> Decimal:
    value = getattr(evidence, field)
    if type(value) is not Decimal:
        raise ValueError(f"resolved TAKE evidence requires {field}")
    return value


def _cost_block_reason(fills: tuple[_MetricCalculationInput, ...]) -> str:
    qualities = {item.trade_evidence.cost_evidence_quality for item in fills}
    if ExecutionCostEvidenceQuality.UNAVAILABLE in qualities:
        return "resolved_take_cost_truth_unavailable"
    if ExecutionCostEvidenceQuality.NONCONSOLIDATED in qualities:
        return "resolved_take_cost_truth_nonconsolidated"
    return "resolved_take_cost_truth_provisional"


def _sortino_ratio(
    mean: Decimal,
    downside: Decimal,
    policy: ValidationTradingMetricPolicy,
) -> Decimal:
    with _metric_decimal_context(policy):
        numerator = mean - policy.session_downside_target_r
    return _decimal_ratio(numerator, downside, policy)


def _row_units(
    inputs: tuple[_MetricCalculationInput, ...],
) -> _MetricUnitInventory:
    return _MetricUnitInventory(
        tuple(item.row_id for item in inputs),
        tuple(item.row_content_hash_sha256 for item in inputs),
    )


def _candidate_denominator(
    metric: ValidationTradingMetric,
    row_units: _MetricUnitInventory,
    session_units: _MetricUnitInventory,
) -> _MetricUnitInventory:
    if metric in _ALWAYS_UNAVAILABLE:
        return _EMPTY_UNITS
    if metric in _SESSION_METRICS:
        return session_units
    return row_units


def _validate_trade_scenario_membership(
    fills: tuple[_MetricCalculationInput, ...],
) -> None:
    cost_complete = tuple(
        item for item in fills if item.trade_evidence.disposition
        is TradeMetricDisposition.RESOLVED_FILL_COST_COMPLETE
    )
    expected_membership = tuple(
        (
            item.row_id,
            item.row_content_hash_sha256,
            item.trade_evidence.trade_evidence_id,
            item.trade_evidence.content_hash(),
        )
        for item in cost_complete
    )
    for scenario in CANONICAL_EXECUTION_STRESS_SCENARIOS:
        actual_membership = tuple(
            (
                item.row_id,
                item.row_content_hash_sha256,
                item.trade_evidence.trade_evidence_id,
                item.trade_evidence.content_hash(),
            )
            for item in cost_complete
            if any(
                value.scenario is scenario
                for value in item.trade_evidence.stress_scenarios
            )
        )
        if actual_membership != expected_membership:
            raise ValueError("stress trade membership must be identical across scenarios")
    for item in cost_complete:
        evidence = item.trade_evidence
        scenarios = tuple(
            next(
                value
                for value in evidence.stress_scenarios
                if value.scenario is scenario
            )
            for scenario in CANONICAL_EXECUTION_STRESS_SCENARIOS
        )
        if not (
            scenarios[0].after_cost_r_unquantized
            >= scenarios[1].after_cost_r_unquantized
            >= scenarios[2].after_cost_r_unquantized
        ):
            raise ValueError("stress trade after-cost R must be nonincreasing")


def _unsupported_metric(
    scenario: ExecutionStressScenario,
    metric: ValidationTradingMetric,
    lineage: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]],
) -> _ValidationTradingMetricValue:
    reason = (
        "capital_model_not_defined"
        if metric is not ValidationTradingMetric.BENCHMARK_EXCESS_RETURN
        else "future_benchmark_outcomes_not_embedded"
    )
    return _metric(
        scenario,
        metric,
        status=ValidationMetricValueStatus.UNAVAILABLE,
        value=None,
        lineage=lineage,
        numerator_units=_EMPTY_UNITS,
        denominator_units=_EMPTY_UNITS,
        reason=reason,
    )


def _available(
    scenario: ExecutionStressScenario,
    metric: ValidationTradingMetric,
    value: Decimal,
    status: ValidationMetricValueStatus,
    lineage: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]],
    *,
    numerator_units: _MetricUnitInventory,
    denominator_units: _MetricUnitInventory,
    reason: str | None,
) -> _ValidationTradingMetricValue:
    return _metric(
        scenario,
        metric,
        status=status,
        value=value,
        numerator_units=numerator_units,
        denominator_units=denominator_units,
        lineage=lineage,
        reason=reason,
    )


def _insufficient(
    scenario: ExecutionStressScenario,
    metric: ValidationTradingMetric,
    lineage: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]],
    reason: str,
    *,
    numerator_units: _MetricUnitInventory = _EMPTY_UNITS,
    denominator_units: _MetricUnitInventory = _EMPTY_UNITS,
) -> _ValidationTradingMetricValue:
    return _metric(
        scenario,
        metric,
        status=ValidationMetricValueStatus.INSUFFICIENT_DATA,
        value=None,
        lineage=lineage,
        numerator_units=numerator_units,
        denominator_units=denominator_units,
        reason=reason,
    )


def _metric(
    scenario: ExecutionStressScenario,
    metric: ValidationTradingMetric,
    *,
    status: ValidationMetricValueStatus,
    value: Decimal | None,
    lineage: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]],
    numerator_units: _MetricUnitInventory,
    denominator_units: _MetricUnitInventory,
    reason: str | None = None,
) -> _ValidationTradingMetricValue:
    if status is ValidationMetricValueStatus.AVAILABLE:
        if type(value) is not Decimal or reason is not None:
            raise ValueError("available metric requires a Decimal value without reason")
    elif status is ValidationMetricValueStatus.PROVISIONAL:
        if type(value) is not Decimal or reason is None:
            raise ValueError("provisional metric requires a Decimal value and reason")
    elif value is not None or reason is None:
        raise ValueError("unavailable metric requires null value and reason")
    return _ValidationTradingMetricValue(
        scenario=scenario,
        metric=metric,
        unit=VALIDATION_TRADING_METRIC_UNITS[metric],
        status=status,
        value=value,
        numerator_count=len(numerator_units.ids),
        denominator_count=len(denominator_units.ids),
        numerator_unit_ids=numerator_units.ids,
        numerator_unit_content_hashes=numerator_units.content_hashes,
        denominator_unit_ids=denominator_units.ids,
        denominator_unit_content_hashes=denominator_units.content_hashes,
        source_row_ids=lineage[0],
        source_row_content_hashes=lineage[1],
        source_trade_evidence_ids=lineage[2],
        source_trade_evidence_content_hashes=lineage[3],
        reason=reason,
    )
