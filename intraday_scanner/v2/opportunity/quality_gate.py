"""Pure absolute quality gate, intentionally separate from pair ranking."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from intraday_scanner.v2.opportunity.models import (
    NON_RANKABLE_GATE_CHECK_IDS,
    RANKED_GATE_CHECK_IDS,
    Availability,
    DecisionRunBinding,
    DecisionRunContext,
    EvaluationStatus,
    EvidenceKind,
    GateCheck,
    RankedOpportunity,
    StrategyEvaluation,
    StrategyValidationState,
    TradeDecision,
    TradeDecisionValue,
    stable_identity,
)
from intraday_scanner.v2.opportunity.risk import (
    ExecutionRiskEvidence,
    RiskMetric,
    RiskValueStatus,
)


@dataclass(frozen=True)
class QualityGateConfig:
    minimum_watch_score: Decimal = Decimal("0.55")
    minimum_take_score: Decimal = Decimal("0.70")
    minimum_data_quality: Decimal = Decimal("0.50")
    minimum_liquidity: Decimal = Decimal("0.20")
    minimum_gross_reward_risk: Decimal = Decimal("1.50")
    minimum_after_cost_reward_risk: Decimal = Decimal("1.50")
    minimum_empirical_expectancy_r: Decimal = Decimal("0")
    minimum_expectancy_sample: int = 100
    maximum_uncertainty_half_width_r: Decimal = Decimal("0.50")
    config_version: str = "opportunity-absolute-quality-gate-v1"

    def __post_init__(self) -> None:
        for name in (
            "minimum_watch_score",
            "minimum_take_score",
            "minimum_data_quality",
            "minimum_liquidity",
            "minimum_gross_reward_risk",
            "minimum_after_cost_reward_risk",
            "minimum_empirical_expectancy_r",
            "maximum_uncertainty_half_width_r",
        ):
            value = getattr(self, name)
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
        if self.minimum_expectancy_sample < 1:
            raise ValueError("minimum_expectancy_sample must be positive")
        if self.minimum_take_score < self.minimum_watch_score:
            raise ValueError("minimum_take_score cannot be below minimum_watch_score")
        for name in (
            "minimum_watch_score",
            "minimum_take_score",
            "minimum_data_quality",
            "minimum_liquidity",
        ):
            value = getattr(self, name)
            if not Decimal("0") <= value <= Decimal("1"):
                raise ValueError(f"{name} must be between zero and one")
        for name in (
            "minimum_gross_reward_risk",
            "minimum_after_cost_reward_risk",
            "minimum_empirical_expectancy_r",
            "maximum_uncertainty_half_width_r",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if not self.config_version.strip():
            raise ValueError("config_version cannot be blank")


DEFAULT_QUALITY_GATE_CONFIG = QualityGateConfig()


def build_decision_run_context(
    evaluations: tuple[StrategyEvaluation, ...],
    ranked: tuple[RankedOpportunity, ...],
    *,
    risk_by_evaluation: Mapping[str, ExecutionRiskEvidence],
    config: QualityGateConfig = DEFAULT_QUALITY_GATE_CONFIG,
) -> DecisionRunContext:
    """Bind the ordered decision inputs without depending on final decision IDs."""

    evaluation_by_id = _validated_evaluations(evaluations)
    if not evaluations:
        raise ValueError("decision run context requires at least one evaluation")
    ranked_by_evaluation = _validated_ranks(ranked, evaluation_by_id)
    risk_map = _validated_risk_mapping(risk_by_evaluation, evaluation_by_id)
    eligible_ids = {
        item.evaluation_id
        for item in evaluations
        if item.status is EvaluationStatus.ELIGIBLE
    }
    if set(ranked_by_evaluation) != eligible_ids:
        raise ValueError("eligible evaluations and ranked opportunities do not reconcile")
    decision_times = {item.decision_at for item in evaluations}
    if len(decision_times) > 1:
        raise ValueError("decision run evaluations must share decision_at")
    decision_at = next(iter(decision_times))
    bindings = tuple(
        DecisionRunBinding(
            evaluation_id=evaluation.evaluation_id,
            evaluation_content_hash=evaluation.content_hash(),
            evaluation_status=evaluation.status,
            lifecycle=evaluation.lifecycle,
            ranked_id=(
                ranked_by_evaluation[evaluation.evaluation_id].ranked_id
                if evaluation.evaluation_id in ranked_by_evaluation
                else None
            ),
            ranked_content_hash=(
                ranked_by_evaluation[evaluation.evaluation_id].content_hash()
                if evaluation.evaluation_id in ranked_by_evaluation
                else None
            ),
            risk_evidence_id=(
                risk_map[evaluation.evaluation_id].execution_risk_evidence_id
                if evaluation.evaluation_id in risk_map
                else None
            ),
            risk_evidence_content_hash=(
                risk_map[evaluation.evaluation_id].content_hash()
                if evaluation.evaluation_id in risk_map
                else None
            ),
        )
        for evaluation in evaluations
    )
    config_identity = stable_identity("quality-gate-config", config)
    values = {
        "decision_at": decision_at,
        "bindings": bindings,
        "gate_config_identity": config_identity,
        "gate_config_version": config.config_version,
        "research_only": True,
        "schema_version": "v2.opportunity.decision_run_context.v1",
    }
    return DecisionRunContext(
        decision_run_id=stable_identity("opportunity-decision-run", values),
        decision_at=decision_at,
        bindings=bindings,
        gate_config_identity=config_identity,
        gate_config_version=config.config_version,
    )


def apply_quality_gate(
    ranked: RankedOpportunity,
    evaluation: StrategyEvaluation,
    *,
    decision_context: DecisionRunContext,
    risk_evidence: ExecutionRiskEvidence | None = None,
    config: QualityGateConfig = DEFAULT_QUALITY_GATE_CONFIG,
) -> TradeDecision:
    """Return TAKE/WATCH/PASS/INSUFFICIENT_DATA from absolute evidence checks."""

    _validate_rank_binding(ranked, evaluation)
    _validate_decision_context(
        decision_context,
        evaluation,
        ranked,
        risk_evidence,
        config,
    )
    if evaluation.status is not EvaluationStatus.ELIGIBLE:
        raise ValueError("absolute quality gate requires an eligible evaluation")
    declared_minimum: Decimal | None = None
    if risk_evidence is not None:
        _validate_risk_binding(risk_evidence, evaluation)
        declared_minimum = risk_evidence.metric(
            RiskMetric.MIN_AFTER_COST_REWARD_RISK
        ).value
        if (
            declared_minimum is not None
            and declared_minimum != config.minimum_after_cost_reward_risk
        ):
            raise ValueError("risk minimum after-cost R does not match gate config")
    gross_reward_risk = _risk_value(risk_evidence, RiskMetric.GROSS_REWARD_RISK)
    after_cost_reward_risk = _risk_value(
        risk_evidence,
        RiskMetric.AFTER_COST_REWARD_RISK,
    )
    expectancy = evaluation.expectancy
    checks = (
        _check(
            "evaluation_eligible",
            evaluation.status is EvaluationStatus.ELIGIBLE,
            "strategy evaluation must be eligible",
        ),
        _check(
            "research_watch_lifecycle",
            evaluation.lifecycle
            not in {StrategyValidationState.DISABLED, StrategyValidationState.REJECTED},
            "disabled or rejected lifecycle cannot WATCH",
        ),
        _check(
            "absolute_watch_score",
            ranked.final_score >= config.minimum_watch_score,
            f"final score must be >= {config.minimum_watch_score}",
        ),
        _check_optional_minimum(
            "data_quality",
            evaluation.data_quality_score,
            config.minimum_data_quality,
        ),
        _check_optional_minimum(
            "liquidity",
            evaluation.liquidity_score,
            config.minimum_liquidity,
        ),
        _check_optional_minimum(
            "gross_reward_risk",
            gross_reward_risk,
            config.minimum_gross_reward_risk,
        ),
        _check(
            "production_lifecycle",
            evaluation.lifecycle is StrategyValidationState.PRODUCTION_ELIGIBLE,
            "TAKE requires PRODUCTION_ELIGIBLE lifecycle",
        ),
        _check(
            "absolute_take_score",
            ranked.final_score >= config.minimum_take_score,
            f"TAKE score must be >= {config.minimum_take_score}",
        ),
        _check_optional_minimum(
            "after_cost_reward_risk",
            after_cost_reward_risk,
            config.minimum_after_cost_reward_risk,
        ),
        _check(
            "risk_policy_minimum_available",
            declared_minimum is not None,
            "TAKE requires an available minimum after-cost R policy threshold",
            unknown=declared_minimum is None,
        ),
        _check(
            "execution_risk_vetoes",
            risk_evidence is not None and not risk_evidence.vetoes,
            "TAKE requires ExecutionRiskEvidence with no vetoes",
            unknown=risk_evidence is None,
        ),
        _check(
            "execution_risk_empirical",
            (
                risk_evidence is not None
                and all(
                    item.status
                    in {RiskValueStatus.OBSERVED, RiskValueStatus.DERIVED}
                    and item.evidence_kind is EvidenceKind.EMPIRICAL
                    for item in risk_evidence.metrics
                )
            ),
            "TAKE requires non-provisional empirical numeric risk evidence",
            unknown=risk_evidence is None,
        ),
        _check(
            "empirical_expectancy_available",
            (
                expectancy is not None
                and expectancy.availability is Availability.AVAILABLE
                and expectancy.evidence_kind is EvidenceKind.EMPIRICAL
                and expectancy.expectancy_r is not None
            ),
            "TAKE requires available empirical expectancy",
            unknown=expectancy is None or expectancy.availability is not Availability.AVAILABLE,
        ),
        _check(
            "expectancy_sample",
            expectancy is not None and expectancy.sample_size >= config.minimum_expectancy_sample,
            f"expectancy sample must be >= {config.minimum_expectancy_sample}",
            unknown=(
                expectancy is None
                or expectancy.availability is not Availability.AVAILABLE
            ),
        ),
        _check(
            "expectancy_positive",
            expectancy is not None
            and expectancy.expectancy_r is not None
            and expectancy.expectancy_r > config.minimum_empirical_expectancy_r,
            f"expectancy R must be > {config.minimum_empirical_expectancy_r}",
            unknown=expectancy is None or expectancy.expectancy_r is None,
        ),
        _check(
            "expectancy_uncertainty",
            expectancy is not None
            and expectancy.uncertainty_half_width_r is not None
            and expectancy.uncertainty_half_width_r <= config.maximum_uncertainty_half_width_r,
            (
                "expectancy uncertainty half-width must be <= "
                f"{config.maximum_uncertainty_half_width_r}"
            ),
            unknown=expectancy is None or expectancy.uncertainty_half_width_r is None,
        ),
    )
    if tuple(check.check_id for check in checks) != RANKED_GATE_CHECK_IDS:
        raise AssertionError("quality gate checks diverged from canonical schema")
    watch_checks = checks[:6]
    take_checks = checks[6:]
    decision: TradeDecisionValue
    rationale: tuple[str, ...]
    if any(check.passed is False for check in watch_checks):
        decision = TradeDecisionValue.PASS
        rationale = ("absolute_watch_criteria_failed",)
    elif any(check.passed is None for check in watch_checks):
        decision = TradeDecisionValue.INSUFFICIENT_DATA
        rationale = ("mandatory_watch_evidence_unavailable",)
    elif evaluation.lifecycle is not StrategyValidationState.PRODUCTION_ELIGIBLE:
        decision = TradeDecisionValue.WATCH
        rationale = ("strong_research_candidate_non_production_lifecycle",)
    elif any(check.passed is None for check in take_checks):
        decision = TradeDecisionValue.INSUFFICIENT_DATA
        rationale = ("mandatory_take_evidence_unavailable",)
    elif any(check.passed is False for check in take_checks):
        decision = TradeDecisionValue.PASS
        rationale = ("absolute_take_criteria_failed",)
    else:
        decision = TradeDecisionValue.TAKE
        rationale = ("all_absolute_take_criteria_satisfied",)

    vetoes = tuple(
        dict.fromkeys(
            (
                *(check.check_id for check in checks if check.passed is not True),
                *(risk_evidence.vetoes if risk_evidence is not None else ()),
            )
        )
    )
    limitations = _decision_limitations(ranked, risk_evidence, checks)
    return _build_decision(
        evaluation=evaluation,
        decision_context=decision_context,
        ranked=ranked,
        risk_evidence=risk_evidence,
        non_rankable_reason=None,
        decision=decision,
        checks=checks,
        vetoes=vetoes,
        rationale=rationale,
        limitations=limitations,
    )


def reconcile_trade_decisions(
    evaluations: tuple[StrategyEvaluation, ...],
    ranked: tuple[RankedOpportunity, ...],
    *,
    risk_by_evaluation: Mapping[str, ExecutionRiskEvidence],
    config: QualityGateConfig = DEFAULT_QUALITY_GATE_CONFIG,
) -> tuple[TradeDecision, ...]:
    """Return exactly one deterministic decision for every supplied evaluation."""

    evaluation_by_id = _validated_evaluations(evaluations)
    ranked_by_evaluation = _validated_ranks(ranked, evaluation_by_id)
    risk_map = _validated_risk_mapping(risk_by_evaluation, evaluation_by_id)
    if not evaluations:
        if ranked or risk_map:
            raise ValueError("empty evaluations cannot have rank or risk inputs")
        return ()
    eligible_ids = {
        item.evaluation_id
        for item in evaluations
        if item.status is EvaluationStatus.ELIGIBLE
    }
    if set(ranked_by_evaluation) != eligible_ids:
        raise ValueError("eligible evaluations and ranked opportunities do not reconcile")
    if set(risk_map) != eligible_ids:
        raise ValueError("risk evidence must exist exactly for eligible evaluations")
    decision_context = build_decision_run_context(
        evaluations,
        ranked,
        risk_by_evaluation=risk_map,
        config=config,
    )

    decisions: list[TradeDecision] = []
    for evaluation in evaluations:
        risk_evidence = risk_map.get(evaluation.evaluation_id)
        if evaluation.status is EvaluationStatus.ELIGIBLE:
            decisions.append(
                apply_quality_gate(
                    ranked_by_evaluation[evaluation.evaluation_id],
                    evaluation,
                    decision_context=decision_context,
                    risk_evidence=risk_evidence,
                    config=config,
                )
            )
        else:
            decisions.append(
                _non_rankable_decision(
                    evaluation,
                    risk_evidence,
                    decision_context=decision_context,
                )
            )
    if tuple(item.evaluation_id for item in decisions) != tuple(
        item.evaluation_id for item in evaluations
    ):
        raise AssertionError("decision reconciliation lost or reordered an evaluation")
    if len({item.decision_id for item in decisions}) != len(decisions):
        raise AssertionError("decision reconciliation produced duplicate identities")
    return tuple(decisions)


def _validated_evaluations(
    evaluations: tuple[StrategyEvaluation, ...],
) -> dict[str, StrategyEvaluation]:
    evaluation_by_id = {item.evaluation_id: item for item in evaluations}
    if len(evaluation_by_id) != len(evaluations):
        raise ValueError("duplicate evaluation identity")
    pairs = [(item.symbol, item.strategy_id, item.strategy_version) for item in evaluations]
    if len(set(pairs)) != len(pairs):
        raise ValueError("duplicate symbol-strategy-version evaluation")
    versions_by_pair: dict[tuple[str, str], set[str]] = {}
    for item in evaluations:
        versions_by_pair.setdefault((item.symbol, item.strategy_id), set()).add(
            item.strategy_version
        )
    if any(len(versions) > 1 for versions in versions_by_pair.values()):
        raise ValueError("strategy version ambiguity within decision reconciliation")
    return evaluation_by_id


def _validated_ranks(
    ranked: tuple[RankedOpportunity, ...],
    evaluation_by_id: dict[str, StrategyEvaluation],
) -> dict[str, RankedOpportunity]:
    if len({item.ranked_id for item in ranked}) != len(ranked):
        raise ValueError("duplicate ranked opportunity identity")
    ranked_by_evaluation = {item.evaluation_id: item for item in ranked}
    if len(ranked_by_evaluation) != len(ranked):
        raise ValueError("duplicate rank for evaluation")
    if len({item.relative_rank for item in ranked}) != len(ranked):
        raise ValueError("duplicate relative rank")
    if ranked and set(item.relative_rank for item in ranked) != set(
        range(1, len(ranked) + 1)
    ):
        raise ValueError("relative ranks must be contiguous")
    for evaluation_id, item in ranked_by_evaluation.items():
        evaluation = evaluation_by_id.get(evaluation_id)
        if evaluation is None:
            raise ValueError("rank references unknown evaluation")
        if evaluation.status is not EvaluationStatus.ELIGIBLE:
            raise ValueError("only eligible evaluations may be ranked")
        _validate_rank_binding(item, evaluation)
    return ranked_by_evaluation


def _validated_risk_mapping(
    risk_by_evaluation: Mapping[str, ExecutionRiskEvidence],
    evaluation_by_id: dict[str, StrategyEvaluation],
) -> dict[str, ExecutionRiskEvidence]:
    risk_map = dict(risk_by_evaluation)
    if len({item.evaluation_id for item in risk_map.values()}) != len(risk_map):
        raise ValueError("duplicate risk evidence evaluation identity")
    for key, risk_evidence in risk_map.items():
        evaluation = evaluation_by_id.get(key)
        if evaluation is None:
            raise ValueError("risk mapping references unknown evaluation")
        if evaluation.status is not EvaluationStatus.ELIGIBLE:
            raise ValueError("risk mapping may contain only eligible evaluations")
        if risk_evidence.evaluation_id != key:
            raise ValueError("risk mapping key does not match risk evaluation identity")
        _validate_risk_binding(risk_evidence, evaluation)
    return risk_map


def _validate_rank_binding(
    ranked: RankedOpportunity,
    evaluation: StrategyEvaluation,
) -> None:
    if ranked.evaluation_id != evaluation.evaluation_id:
        raise ValueError("ranked opportunity and evaluation identities differ")
    if (
        ranked.symbol != evaluation.symbol
        or ranked.strategy_id != evaluation.strategy_id
        or ranked.strategy_version != evaluation.strategy_version
        or ranked.direction is not evaluation.direction
    ):
        raise ValueError("ranked opportunity and evaluation pair metadata differ")


def _validate_risk_binding(
    risk_evidence: ExecutionRiskEvidence,
    evaluation: StrategyEvaluation,
) -> None:
    risk_metadata = (
        risk_evidence.evaluation_id,
        risk_evidence.evaluation_content_hash,
        risk_evidence.symbol,
        risk_evidence.strategy_id,
        risk_evidence.strategy_version,
        risk_evidence.direction,
        risk_evidence.decision_at,
    )
    expected = (
        evaluation.evaluation_id,
        evaluation.content_hash(),
        evaluation.symbol,
        evaluation.strategy_id,
        evaluation.strategy_version,
        evaluation.direction,
        evaluation.decision_at,
    )
    if risk_metadata != expected:
        raise ValueError("ExecutionRiskEvidence does not match StrategyEvaluation")


def _validate_decision_context(
    context: DecisionRunContext,
    evaluation: StrategyEvaluation,
    ranked: RankedOpportunity,
    risk_evidence: ExecutionRiskEvidence | None,
    config: QualityGateConfig,
) -> None:
    if context.decision_at != evaluation.decision_at:
        raise ValueError("decision context time does not match evaluation")
    expected_config_identity = stable_identity("quality-gate-config", config)
    if (
        context.gate_config_identity != expected_config_identity
        or context.gate_config_version != config.config_version
    ):
        raise ValueError("decision context gate config does not match quality gate")
    binding = next(
        (
            item
            for item in context.bindings
            if item.evaluation_id == evaluation.evaluation_id
        ),
        None,
    )
    if binding is None:
        raise ValueError("evaluation is absent from decision context")
    expected = (
        evaluation.content_hash(),
        evaluation.status,
        evaluation.lifecycle,
        ranked.ranked_id,
        ranked.content_hash(),
        (
            risk_evidence.execution_risk_evidence_id
            if risk_evidence is not None
            else None
        ),
        risk_evidence.content_hash() if risk_evidence is not None else None,
    )
    actual = (
        binding.evaluation_content_hash,
        binding.evaluation_status,
        binding.lifecycle,
        binding.ranked_id,
        binding.ranked_content_hash,
        binding.risk_evidence_id,
        binding.risk_evidence_content_hash,
    )
    if actual != expected:
        raise ValueError("decision context binding does not match gate inputs")


def _risk_value(
    risk_evidence: ExecutionRiskEvidence | None,
    metric: RiskMetric,
) -> Decimal | None:
    if risk_evidence is None:
        return None
    evidence = risk_evidence.metric(metric)
    if evidence.status is RiskValueStatus.UNAVAILABLE:
        return None
    return evidence.value


def _decision_limitations(
    ranked: RankedOpportunity,
    risk_evidence: ExecutionRiskEvidence | None,
    checks: tuple[GateCheck, ...],
) -> tuple[str, ...]:
    limitations: list[str] = list(ranked.limitations)
    if risk_evidence is None:
        limitations.append("execution_risk_evidence_unavailable")
    else:
        limitations.extend(risk_evidence.limitations)
        for evidence in risk_evidence.metrics:
            limitations.extend(evidence.limitations)
            if evidence.status in {
                RiskValueStatus.PROVISIONAL,
                RiskValueStatus.UNAVAILABLE,
            }:
                limitations.append(
                    f"risk_{evidence.status.value}:{evidence.metric.value}"
                )
            if evidence.reason is not None:
                limitations.append(f"risk_reason:{evidence.metric.value}:{evidence.reason}")
    limitations.extend(check.reason for check in checks if check.passed is None)
    return tuple(dict.fromkeys(limitations))


def _non_rankable_decision(
    evaluation: StrategyEvaluation,
    risk_evidence: ExecutionRiskEvidence | None,
    *,
    decision_context: DecisionRunContext,
) -> TradeDecision:
    if evaluation.status is EvaluationStatus.ELIGIBLE:
        raise ValueError("eligible evaluation requires ranked quality gate")
    decision = (
        TradeDecisionValue.INSUFFICIENT_DATA
        if evaluation.status is EvaluationStatus.INSUFFICIENT_DATA
        else TradeDecisionValue.PASS
    )
    reason = f"evaluation_status_{evaluation.status.value}"
    checks = (
        GateCheck(
            check_id=NON_RANKABLE_GATE_CHECK_IDS[0],
            passed=True,
            mandatory=True,
            reason=reason,
        ),
    )
    limitations = tuple(
        dict.fromkeys(
            (
                *evaluation.reasons,
                *(
                    risk_evidence.limitations
                    if risk_evidence is not None
                    else ("execution_risk_evidence_not_required_for_non_rankable_pair",)
                ),
            )
        )
    )
    return _build_decision(
        evaluation=evaluation,
        decision_context=decision_context,
        ranked=None,
        risk_evidence=risk_evidence,
        non_rankable_reason=reason,
        decision=decision,
        checks=checks,
        vetoes=(reason,),
        rationale=(reason,),
        limitations=limitations,
    )


def _build_decision(
    *,
    evaluation: StrategyEvaluation,
    decision_context: DecisionRunContext,
    ranked: RankedOpportunity | None,
    risk_evidence: ExecutionRiskEvidence | None,
    non_rankable_reason: str | None,
    decision: TradeDecisionValue,
    checks: tuple[GateCheck, ...],
    vetoes: tuple[str, ...],
    rationale: tuple[str, ...],
    limitations: tuple[str, ...],
) -> TradeDecision:
    values = {
        "decision_run_id": decision_context.decision_run_id,
        "evaluation_id": evaluation.evaluation_id,
        "evaluation_content_hash": evaluation.content_hash(),
        "ranked_id": ranked.ranked_id if ranked is not None else None,
        "non_rankable_reason": non_rankable_reason,
        "risk_evidence_id": (
            risk_evidence.execution_risk_evidence_id
            if risk_evidence is not None
            else None
        ),
        "risk_evidence_content_hash": (
            risk_evidence.content_hash() if risk_evidence is not None else None
        ),
        "symbol": evaluation.symbol,
        "strategy_id": evaluation.strategy_id,
        "strategy_version": evaluation.strategy_version,
        "direction": evaluation.direction,
        "lifecycle": evaluation.lifecycle,
        "decision_at": evaluation.decision_at,
        "decision": decision,
        "decision_context": decision_context,
        "evaluation": evaluation,
        "ranked": ranked,
        "gate_checks": checks,
        "vetoes": vetoes,
        "rationale": rationale,
        "limitations": limitations,
        "research_only": True,
        "schema_version": "v2.opportunity.trade_decision.v2",
    }
    return TradeDecision(
        decision_id=stable_identity("decision", values),
        **values,  # type: ignore[arg-type]
    )


def _check(
    check_id: str,
    passed: bool,
    reason: str,
    *,
    unknown: bool = False,
) -> GateCheck:
    return GateCheck(
        check_id=check_id,
        passed=None if unknown else passed,
        mandatory=True,
        reason=reason,
    )


def _check_optional_minimum(
    check_id: str,
    value: Decimal | None,
    threshold: Decimal,
) -> GateCheck:
    return _check(
        check_id,
        value is not None and value >= threshold,
        f"{check_id} must be available and >= {threshold}",
        unknown=value is None,
    )


__all__ = [
    "DEFAULT_QUALITY_GATE_CONFIG",
    "QualityGateConfig",
    "apply_quality_gate",
    "build_decision_run_context",
    "reconcile_trade_decisions",
]
