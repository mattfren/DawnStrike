"""TAKE-only execution population and deterministic cost-stress projections."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from intraday_scanner.v2.opportunity.capabilities import CapabilityState
from intraday_scanner.v2.opportunity.models import (
    EvidenceKind,
    OpportunityContract,
    TradeDecisionValue,
    stable_identity,
)
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeCompleteness,
    OutcomeContract,
    OutcomeEntryStatus,
    OutcomeMetric,
    OutcomePathStatus,
    OutcomeValueStatus,
    _direction_sign,
    _identity_payload,
    _require_hash,
    _require_identity,
    _require_sanitized_text,
    _require_schema,
    _require_unique,
)
from intraday_scanner.v2.opportunity.outcome_records import OutcomeRecord
from intraday_scanner.v2.opportunity.risk import (
    ExecutionRiskEvidence,
    QuoteEvidenceScope,
    RiskMetric,
    RiskValueStatus,
)
from intraday_scanner.v2.opportunity.validation_metric_contracts import (
    CANONICAL_EXECUTION_STRESS_SCENARIOS,
    EXECUTION_STRESS_MULTIPLIERS,
    ExecutionCostEvidenceQuality,
    ExecutionStressScenario,
    TradeMetricDisposition,
    ValidationTradingMetricPolicy,
    quantize_validation_metric,
)
from intraday_scanner.v2.opportunity.validation_metric_math import (
    _fresh_decimal_context,
    _metric_decimal_context,
    _timedelta_decimal_seconds,
)

_TERMINAL_PATHS = {
    OutcomePathStatus.TARGET_FIRST,
    OutcomePathStatus.STOP_FIRST,
    OutcomePathStatus.HORIZON_EXIT,
}
_COST_COMPONENTS = (
    RiskMetric.ENTRY_PRICE,
    RiskMetric.STOP_PRICE,
    RiskMetric.STOP_DISTANCE,
    RiskMetric.SPREAD_BPS,
    RiskMetric.ENTRY_SLIPPAGE_BPS,
    RiskMetric.EXIT_SLIPPAGE_BPS,
    RiskMetric.ROUND_TRIP_FEE_PER_SHARE,
    RiskMetric.PER_SHARE_COST,
    RiskMetric.QUANTITY,
    RiskMetric.TOTAL_ROUND_TRIP_COST,
)


@dataclass(frozen=True)
class _ExecutionStressScenarioValue:
    scenario: ExecutionStressScenario
    multiplier: Decimal
    spread_bps: Decimal
    entry_slippage_bps: Decimal
    exit_slippage_bps: Decimal
    round_trip_fee_per_share: Decimal
    per_share_cost: Decimal
    total_round_trip_cost: Decimal
    after_cost_r_unquantized: Decimal
    after_cost_r: Decimal


@dataclass(frozen=True)
class ExecutionStressTradeEvidence(OutcomeContract):
    trade_evidence_id: str
    outcome_id: str
    outcome_content_hash_sha256: str
    outcome: OutcomeRecord
    policy_id: str
    policy_content_hash_sha256: str
    policy: ValidationTradingMetricPolicy
    disposition: TradeMetricDisposition
    cost_evidence_quality: ExecutionCostEvidenceQuality
    gross_r: Decimal | None
    maximum_favorable_excursion_r: Decimal | None
    maximum_adverse_excursion_r: Decimal | None
    holding_seconds_lower_bound: Decimal | None
    holding_seconds_upper_bound: Decimal | None
    stress_scenarios: tuple[_ExecutionStressScenarioValue, ...]
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.execution_stress_trade_evidence.v1"

    @classmethod
    def from_dict(cls, payload: dict[str, object]):
        with _fresh_decimal_context(precision=28):
            decoded = super().from_dict(payload)
        with _metric_decimal_context(decoded.policy):
            return replace(decoded)

    @classmethod
    def from_json(cls, payload: str):
        with _fresh_decimal_context(precision=28):
            return super().from_json(payload)

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version,
            "v2.opportunity.execution_stress_trade_evidence.v1",
        )
        _require_identity(self.trade_evidence_id, "trade_evidence_id")
        _require_identity(self.outcome_id, "outcome_id")
        _require_hash(self.outcome_content_hash_sha256, "outcome content hash")
        _require_identity(self.policy_id, "policy_id")
        _require_hash(self.policy_content_hash_sha256, "policy content hash")
        if (
            self.outcome_id != self.outcome.outcome_id
            or self.outcome_content_hash_sha256 != self.outcome.content_hash()
        ):
            raise ValueError("stress evidence outcome binding does not match content")
        if (
            self.policy_id != self.policy.policy_id
            or self.policy_content_hash_sha256 != self.policy.content_hash()
        ):
            raise ValueError("stress evidence policy binding does not match content")
        expected = _derive_trade_projection(self.outcome, self.policy)
        actual = (
            self.disposition,
            self.cost_evidence_quality,
            self.gross_r,
            self.maximum_favorable_excursion_r,
            self.maximum_adverse_excursion_r,
            self.holding_seconds_lower_bound,
            self.holding_seconds_upper_bound,
            self.stress_scenarios,
            self.limitations,
        )
        if actual != expected:
            raise ValueError("execution stress trade projection does not recompute")
        _require_unique(list(self.limitations), "execution stress limitation")
        for limitation in self.limitations:
            _require_sanitized_text(limitation, "execution stress limitation")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("execution stress evidence must remain research-only")
        identity = stable_identity(
            "execution-stress-trade-evidence",
            _identity_payload(self, "trade_evidence_id"),
        )
        if self.trade_evidence_id != identity:
            raise ValueError("execution stress trade evidence identity does not match content")


def build_execution_stress_trade_evidence(
    outcome: OutcomeRecord,
    *,
    policy: ValidationTradingMetricPolicy,
) -> ExecutionStressTradeEvidence:
    with _metric_decimal_context(policy):
        projection = _derive_trade_projection(outcome, policy)
        values = {
            "outcome_id": outcome.outcome_id,
            "outcome_content_hash_sha256": outcome.content_hash(),
            "outcome": outcome,
            "policy_id": policy.policy_id,
            "policy_content_hash_sha256": policy.content_hash(),
            "policy": policy,
            "disposition": projection[0],
            "cost_evidence_quality": projection[1],
            "gross_r": projection[2],
            "maximum_favorable_excursion_r": projection[3],
            "maximum_adverse_excursion_r": projection[4],
            "holding_seconds_lower_bound": projection[5],
            "holding_seconds_upper_bound": projection[6],
            "stress_scenarios": projection[7],
            "limitations": projection[8],
            "research_only": True,
            "promotion_eligible": False,
            "schema_version": "v2.opportunity.execution_stress_trade_evidence.v1",
        }
        return ExecutionStressTradeEvidence(
            trade_evidence_id=stable_identity("execution-stress-trade-evidence", values),
            outcome_id=outcome.outcome_id,
            outcome_content_hash_sha256=outcome.content_hash(),
            outcome=outcome,
            policy_id=policy.policy_id,
            policy_content_hash_sha256=policy.content_hash(),
            policy=policy,
            disposition=projection[0],
            cost_evidence_quality=projection[1],
            gross_r=projection[2],
            maximum_favorable_excursion_r=projection[3],
            maximum_adverse_excursion_r=projection[4],
            holding_seconds_lower_bound=projection[5],
            holding_seconds_upper_bound=projection[6],
            stress_scenarios=projection[7],
            limitations=projection[8],
        )


def _derive_trade_projection(
    outcome: OutcomeRecord,
    policy: ValidationTradingMetricPolicy,
) -> tuple[
    TradeMetricDisposition,
    ExecutionCostEvidenceQuality,
    Decimal | None,
    Decimal | None,
    Decimal | None,
    Decimal | None,
    Decimal | None,
    tuple[_ExecutionStressScenarioValue, ...],
    tuple[str, ...],
]:
    if outcome.decision_value is not TradeDecisionValue.TAKE:
        return (
            TradeMetricDisposition.NON_TAKE,
            ExecutionCostEvidenceQuality.NOT_APPLICABLE,
            None,
            None,
            None,
            None,
            None,
            (),
            ("non_take_excluded_from_trade_population",),
        )
    if _is_exact_no_fill(outcome):
        return (
            TradeMetricDisposition.EXACT_NO_FILL,
            ExecutionCostEvidenceQuality.NOT_APPLICABLE,
            None,
            None,
            None,
            None,
            None,
            (),
            ("take_definitive_no_fill",),
        )
    if not _is_resolved_fill(outcome):
        return (
            TradeMetricDisposition.UNRESOLVED_TAKE,
            ExecutionCostEvidenceQuality.UNAVAILABLE,
            None,
            None,
            None,
            None,
            None,
            (),
            ("take_execution_path_unresolved",),
        )
    gross = _required_outcome_metric(outcome, OutcomeMetric.SIMULATED_GROSS_R)
    favorable = _required_outcome_metric(
        outcome, OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R
    )
    adverse = _required_outcome_metric(
        outcome, OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R
    )
    lower, upper = _holding_bounds(outcome, policy)
    quality = _cost_quality(outcome.risk_evidence)
    if outcome.risk_evidence is None or not _cost_values_available(outcome.risk_evidence):
        return (
            TradeMetricDisposition.RESOLVED_FILL_COST_UNAVAILABLE,
            ExecutionCostEvidenceQuality.UNAVAILABLE,
            gross,
            favorable,
            adverse,
            lower,
            upper,
            (),
            ("take_fill_cost_evidence_unavailable",),
        )
    if not _stress_scenarios_supported(outcome.risk_evidence):
        return (
            TradeMetricDisposition.RESOLVED_FILL_COST_UNAVAILABLE,
            quality,
            gross,
            favorable,
            adverse,
            lower,
            upper,
            (),
            (_cost_limitation(quality),),
        )
    scenarios = _build_stress_scenarios(outcome, outcome.risk_evidence, policy)
    return (
        TradeMetricDisposition.RESOLVED_FILL_COST_COMPLETE,
        quality,
        gross,
        favorable,
        adverse,
        lower,
        upper,
        scenarios,
        (),
    )


def _is_resolved_fill(outcome: OutcomeRecord) -> bool:
    if outcome.entry_status is not OutcomeEntryStatus.FILLED:
        return False
    if outcome.path_status not in _TERMINAL_PATHS:
        return False
    if outcome.path_status is OutcomePathStatus.HORIZON_EXIT:
        return outcome.completeness is OutcomeCompleteness.COMPLETE
    return outcome.completeness in {
        OutcomeCompleteness.COMPLETE,
        OutcomeCompleteness.PARTIAL,
    }


def _is_exact_no_fill(outcome: OutcomeRecord) -> bool:
    return outcome.completeness is OutcomeCompleteness.COMPLETE and (
        (
            outcome.entry_status is OutcomeEntryStatus.NO_ENTRY
            and outcome.path_status is OutcomePathStatus.NO_ENTRY
        )
        or (
            outcome.entry_status is OutcomeEntryStatus.UNATTAINABLE
            and outcome.path_status is OutcomePathStatus.UNATTAINABLE_FILL
        )
    )


def _required_outcome_metric(outcome: OutcomeRecord, metric: OutcomeMetric) -> Decimal:
    evidence = next(item for item in outcome.metrics if item.metric is metric)
    if evidence.status is not OutcomeValueStatus.DERIVED or evidence.value is None:
        raise ValueError(f"resolved TAKE path requires {metric.value}")
    return evidence.value


def _holding_bounds(
    outcome: OutcomeRecord,
    policy: ValidationTradingMetricPolicy,
) -> tuple[Decimal, Decimal]:
    if outcome.entry_interval is None or outcome.exit_interval is None:
        raise ValueError("resolved TAKE path requires entry and exit intervals")
    lower = max(
        Decimal("0"),
        _timedelta_decimal_seconds(
            outcome.exit_interval.interval_start_at
            - outcome.entry_interval.interval_end_at,
            policy,
        ),
    )
    upper = _timedelta_decimal_seconds(
        outcome.exit_interval.interval_end_at - outcome.entry_interval.interval_start_at,
        policy,
    )
    if upper < lower:
        raise ValueError("resolved TAKE holding interval is invalid")
    return lower, upper


def _cost_values_available(risk: ExecutionRiskEvidence) -> bool:
    return all(
        risk.metric(metric).value is not None
        and risk.metric(metric).capability_state is CapabilityState.AVAILABLE
        and risk.metric(metric).status is not RiskValueStatus.UNAVAILABLE
        for metric in _COST_COMPONENTS
    )


def _cost_quality(
    risk: ExecutionRiskEvidence | None,
) -> ExecutionCostEvidenceQuality:
    if risk is None or not _cost_values_available(risk):
        return ExecutionCostEvidenceQuality.UNAVAILABLE
    metrics = tuple(risk.metric(metric) for metric in _COST_COMPONENTS)
    if risk.quote_scope is QuoteEvidenceScope.NONCONSOLIDATED:
        return ExecutionCostEvidenceQuality.NONCONSOLIDATED
    if (
        risk.quote_scope is QuoteEvidenceScope.NBBO
        and not risk.vetoes
        and all(item.evidence_kind is EvidenceKind.EMPIRICAL for item in metrics)
        and all(item.status is not RiskValueStatus.PROVISIONAL for item in metrics)
    ):
        return ExecutionCostEvidenceQuality.EMPIRICAL
    return ExecutionCostEvidenceQuality.PROVISIONAL


def _stress_scenarios_supported(risk: ExecutionRiskEvidence | None) -> bool:
    return _cost_quality(risk) is ExecutionCostEvidenceQuality.EMPIRICAL


def _cost_limitation(quality: ExecutionCostEvidenceQuality) -> str:
    if quality is ExecutionCostEvidenceQuality.NONCONSOLIDATED:
        return "take_fill_cost_evidence_nonconsolidated"
    if quality is ExecutionCostEvidenceQuality.PROVISIONAL:
        return "take_fill_cost_evidence_provisional"
    return "take_fill_cost_evidence_unavailable"


def _build_stress_scenarios(
    outcome: OutcomeRecord,
    risk: ExecutionRiskEvidence,
    policy: ValidationTradingMetricPolicy,
) -> tuple[_ExecutionStressScenarioValue, ...]:
    entry = _risk_value(risk, RiskMetric.ENTRY_PRICE)
    stop_distance = _risk_value(risk, RiskMetric.STOP_DISTANCE)
    quantity = _risk_value(risk, RiskMetric.QUANTITY)
    spread = _risk_value(risk, RiskMetric.SPREAD_BPS)
    entry_slippage = _risk_value(risk, RiskMetric.ENTRY_SLIPPAGE_BPS)
    exit_slippage = _risk_value(risk, RiskMetric.EXIT_SLIPPAGE_BPS)
    fee = _risk_value(risk, RiskMetric.ROUND_TRIP_FEE_PER_SHARE)
    gross = _required_outcome_metric(outcome, OutcomeMetric.SIMULATED_GROSS_R)
    accepted_after_cost = _required_outcome_metric(
        outcome, OutcomeMetric.SIMULATED_AFTER_COST_R
    )
    direction_sign = _direction_sign(outcome.direction)
    modeled_stop = outcome.decision.evaluation.invalidation_price
    if (
        direction_sign is None
        or outcome.modeled_entry_price is None
        or outcome.modeled_exit_price is None
        or modeled_stop is None
    ):
        raise ValueError("resolved TAKE direction and modeled geometry are required")
    accepted_total_cost = _risk_value(risk, RiskMetric.TOTAL_ROUND_TRIP_COST)
    with _metric_decimal_context(policy):
        canonical_numerator = (
            quantity
            * direction_sign
            * (outcome.modeled_exit_price - outcome.modeled_entry_price)
            - accepted_total_cost
        )
        canonical_denominator = quantity * abs(
            outcome.modeled_entry_price - modeled_stop
        ) + accepted_total_cost
        canonical_base = canonical_numerator / canonical_denominator
        accepted_exponent = accepted_after_cost.as_tuple().exponent
        if type(accepted_exponent) is not int:
            raise ValueError("accepted BASE after-cost R must be finite")
        accepted_quantum = Decimal(1).scaleb(accepted_exponent)
        canonical_at_accepted_scale = canonical_base.quantize(accepted_quantum)
    if canonical_at_accepted_scale != accepted_after_cost:
        raise ValueError("BASE realized-path formula must exactly match accepted outcome")
    values: list[_ExecutionStressScenarioValue] = []
    for scenario in CANONICAL_EXECUTION_STRESS_SCENARIOS:
        with _metric_decimal_context(policy):
            multiplier = EXECUTION_STRESS_MULTIPLIERS[scenario]
            scaled_spread = spread * multiplier
            scaled_entry_slippage = entry_slippage * multiplier
            scaled_exit_slippage = exit_slippage * multiplier
            scaled_fee = fee * multiplier
            per_share_cost = entry * (
                scaled_spread + scaled_entry_slippage + scaled_exit_slippage
            ) / Decimal("10000") + scaled_fee
            total_cost = quantity * per_share_cost
            denominator = quantity * stop_distance + total_cost
            if denominator <= 0:
                raise ValueError("execution stress denominator must be positive")
            calculated_after_cost = (
                quantity * stop_distance * gross - total_cost
            ) / denominator
        unquantized = (
            accepted_after_cost
            if scenario is ExecutionStressScenario.BASE
            else calculated_after_cost
        )
        if scenario is ExecutionStressScenario.BASE:
            if (
                per_share_cost != _risk_value(risk, RiskMetric.PER_SHARE_COST)
                or total_cost != _risk_value(risk, RiskMetric.TOTAL_ROUND_TRIP_COST)
            ):
                raise ValueError("BASE cost components do not match accepted risk evidence")
        values.append(
            _ExecutionStressScenarioValue(
                scenario=scenario,
                multiplier=multiplier,
                spread_bps=scaled_spread,
                entry_slippage_bps=scaled_entry_slippage,
                exit_slippage_bps=scaled_exit_slippage,
                round_trip_fee_per_share=scaled_fee,
                per_share_cost=per_share_cost,
                total_round_trip_cost=total_cost,
                after_cost_r_unquantized=unquantized,
                after_cost_r=quantize_validation_metric(unquantized, policy),
            )
        )
    if not (
        values[2].after_cost_r_unquantized
        <= values[1].after_cost_r_unquantized
        <= values[0].after_cost_r_unquantized
    ):
        raise ValueError("execution cost stress must be monotonically nonincreasing")
    return tuple(values)


def _risk_value(risk: ExecutionRiskEvidence, metric: RiskMetric) -> Decimal:
    value = risk.metric(metric).value
    if value is None:
        raise ValueError(f"risk metric {metric.value} is unavailable")
    return value


__all__ = [
    "ExecutionStressTradeEvidence",
    "build_execution_stress_trade_evidence",
]
