"""Versioned experimental DS strategy registry and deterministic pair evaluation."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from intraday_scanner.alpha.v5_policy import (
    DEFAULT_V5_POLICY,
    AlphaOpsV5Policy,
    evaluate_v5_official_paper,
)
from intraday_scanner.v2.opportunity.models import (
    AnomalyType,
    Availability,
    DataQuality,
    EvaluationStatus,
    EvidenceKind,
    ExpectancyEvidence,
    FeatureSnapshot,
    LifecycleActorType,
    MarketRegime,
    OpportunityCandidate,
    RegimeState,
    SecurityRegime,
    StrategyDefinition,
    StrategyDirection,
    StrategyEvaluation,
    StrategyLifecycleTransition,
    StrategyParameter,
    StrategyValidationState,
    stable_identity,
    validate_lifecycle_transition_rules,
)
from intraday_scanner.v2.strategies.alphaops_intraday import (
    IntradayDecisionPoint,
    build_alphaops_intraday_strategy,
    evaluate_alphaops_intraday,
)
from intraday_scanner.v2.strategies.models import StrategySpec

STRATEGY_VERSION = "1.0.0"
THRESHOLD_VERSION = "ds-heuristic-thresholds-v1"

Evaluator = Callable[
    [
        StrategyDefinition,
        OpportunityCandidate,
        FeatureSnapshot,
        MarketRegime,
        SecurityRegime,
        ExpectancyEvidence | None,
    ],
    StrategyEvaluation,
]


@dataclass(frozen=True)
class StrategyRegistry:
    definitions: tuple[StrategyDefinition, ...]
    experimental_adapters: tuple[StrategySpec, ...] = ()

    def __post_init__(self) -> None:
        identities = [(item.strategy_id, item.version) for item in self.definitions]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate strategy ID/version")
        evaluator_ids = set(_EVALUATORS)
        missing = {
            item.evaluator_id
            for item in self.definitions
            if item.lifecycle is not StrategyValidationState.DISABLED
            and item.evaluator_id not in evaluator_ids
        }
        if missing:
            raise ValueError(f"unregistered evaluator identity: {sorted(missing)}")
        for definition in self.definitions:
            if definition.lifecycle is StrategyValidationState.DISABLED:
                continue
            if definition.evaluator_code_hash != evaluator_behavior_hash(definition.evaluator_id):
                raise ValueError("strategy evaluator code hash does not match implementation")
        adapter_identities = tuple(
            (item.strategy_id, item.version) for item in self.experimental_adapters
        )
        if len(adapter_identities) != len(set(adapter_identities)):
            raise ValueError("duplicate experimental strategy adapter ID/version")
        for adapter in self.experimental_adapters:
            if not adapter.status.startswith("research_only"):
                raise ValueError("experimental strategy adapters must remain research-only")
            if adapter.parameters.get("broker_execution_enabled") is not False:
                raise ValueError("experimental strategy adapters cannot enable broker execution")
            if adapter.parameters.get("promotion_authority") is not False:
                raise ValueError("experimental strategy adapters cannot hold promotion authority")
            if adapter.parameters.get("take_authority") is not False:
                raise ValueError("experimental strategy adapters cannot emit TAKE authority")

    def get(self, strategy_id: str, version: str = STRATEGY_VERSION) -> StrategyDefinition:
        for definition in self.definitions:
            if definition.strategy_id == strategy_id and definition.version == version:
                return definition
        raise KeyError(f"unknown strategy {strategy_id}:{version}")

    def evaluate_all(
        self,
        candidate: OpportunityCandidate,
        snapshot: FeatureSnapshot,
        market_regime: MarketRegime,
        security_regime: SecurityRegime,
        expectancy_by_strategy: dict[str, ExpectancyEvidence] | None = None,
    ) -> tuple[StrategyEvaluation, ...]:
        evidence = expectancy_by_strategy or {}
        return tuple(
            evaluate_strategy(
                definition,
                candidate,
                snapshot,
                market_regime,
                security_regime,
                expectancy=evidence.get(definition.strategy_id),
            )
            for definition in self.definitions
        )


def build_default_registry(
    *,
    alphaops_v5_candidates: dict[str, dict[str, Any]] | None = None,
) -> StrategyRegistry:
    """Register all required DS families with heuristic and disabled truth labels."""

    trend_regimes = (
        RegimeState.TREND_UP,
        RegimeState.VOLATILITY_EXPANSION,
        RegimeState.BREAKOUT,
        RegimeState.UNKNOWN,
    )
    reversal_regimes = (
        RegimeState.MEAN_REVERTING,
        RegimeState.EXHAUSTION,
        RegimeState.CHOP,
        RegimeState.UNKNOWN,
    )
    definitions = (
        _definition(
            "DS-MOM-001",
            "High relative-volume momentum continuation",
            StrategyDirection.LONG,
            ("close_price", "atr_prior", "relative_volume", "return_short", "range_position"),
            trend_regimes,
            "evaluate_mom_001",
            ("crowded_extension", "late_session_chase", "false_volume_spike"),
            (
                ("minimum_relative_volume", Decimal("1.5")),
                ("minimum_return_short", Decimal("0.005")),
                ("minimum_range_position", Decimal("0.6")),
            ),
        ),
        _definition(
            "DS-MOM-002",
            "VWAP-proxy pullback continuation",
            StrategyDirection.LONG,
            (
                "close_price",
                "atr_prior",
                "vwap_proxy_displacement",
                "vwap_proxy_reclaim",
                "return_short",
            ),
            trend_regimes,
            "evaluate_mom_002",
            ("proxy_differs_from_trade_vwap", "failed_reclaim"),
            (
                ("minimum_vwap_reclaim", Decimal("0.5")),
                ("minimum_return_short", Decimal("0")),
            ),
        ),
        _definition(
            "DS-MOM-003",
            "Opening-range expansion",
            StrategyDirection.BOTH,
            (
                "close_price",
                "atr_prior",
                "minutes_since_open",
                "relative_volume",
                "breakout_signal",
                "breakdown_signal",
            ),
            tuple(dict.fromkeys((RegimeState.BREAKOUT, RegimeState.BREAKDOWN, *trend_regimes))),
            "evaluate_mom_003",
            ("opening_whipsaw", "late_breakout"),
            (
                ("signal_threshold", Decimal("0.5")),
                ("minimum_minutes_since_open", Decimal("0")),
                ("maximum_minutes_since_open", Decimal("90")),
                ("minimum_relative_volume", Decimal("1.25")),
            ),
        ),
        _definition(
            "DS-MR-001",
            "Extreme VWAP-proxy displacement mean reversion",
            StrategyDirection.BOTH,
            ("close_price", "atr_prior", "vwap_proxy_displacement", "range_position"),
            reversal_regimes,
            "evaluate_mr_001",
            ("trend_day_continuation", "proxy_differs_from_trade_vwap"),
            (("minimum_absolute_vwap_displacement", Decimal("0.015")),),
        ),
        _definition(
            "DS-REV-001",
            "Failed extension or exhaustion reversal",
            StrategyDirection.BOTH,
            (
                "close_price",
                "atr_prior",
                "failed_extension_signal",
                "exhaustion_signal",
                "range_position",
            ),
            reversal_regimes,
            "evaluate_rev_001",
            ("extension_resumes", "halt_or_gap_path"),
            (
                ("signal_threshold", Decimal("0.5")),
                ("range_midpoint", Decimal("0.5")),
            ),
        ),
        _definition(
            "DS-REV-002",
            "Failed breakout or breakdown",
            StrategyDirection.BOTH,
            (
                "close_price",
                "atr_prior",
                "failed_breakout_signal",
                "failed_breakdown_signal",
            ),
            reversal_regimes,
            "evaluate_rev_002",
            ("level_rebreak", "thin_liquidity"),
            (("signal_threshold", Decimal("0.5")),),
        ),
        _definition(
            "DS-RS-001",
            "Market-relative strength continuation",
            StrategyDirection.LONG,
            ("close_price", "atr_prior", "market_relative_strength", "return_short"),
            trend_regimes,
            "evaluate_rs_001",
            ("benchmark_misalignment", "market_beta_reversal"),
            (
                ("minimum_market_relative_strength", Decimal("0.015")),
                ("minimum_return_short", Decimal("0")),
            ),
        ),
        _order_flow_definition("DS-OF-001", "True CVD divergence reversal"),
        _order_flow_definition("DS-OF-002", "Aggressor imbalance continuation"),
    )
    return StrategyRegistry(
        definitions=definitions,
        experimental_adapters=(
            build_alphaops_v5_adapter(alphaops_v5_candidates or {}),
        ),
    )


def build_alphaops_v5_adapter(
    candidates: dict[str, dict[str, Any]],
    *,
    policy: AlphaOpsV5Policy = DEFAULT_V5_POLICY,
) -> StrategySpec:
    """Expose the frozen V5 delegating adapter beside the DS registry."""

    return build_alphaops_intraday_strategy(candidates, policy=policy)


def alphaops_v5_adapter_parity(
    point: IntradayDecisionPoint,
    *,
    policy: AlphaOpsV5Policy = DEFAULT_V5_POLICY,
) -> bool:
    """Prove adapter semantics are byte-equivalent to the frozen V5 policy."""

    observation = dict(point.observation)
    observation.setdefault("requested_at", point.decision_at.isoformat())
    observation.setdefault("observed_at", point.decision_at.isoformat())
    observation.setdefault("is_usable", True)
    direct = evaluate_v5_official_paper(
        point.signal,
        observation,
        decision_time=point.decision_at.isoformat(),
        policy=policy,
    )
    delegated = evaluate_alphaops_intraday(point, policy=policy).decision
    return direct.to_dict() == delegated.to_dict()


def evaluate_strategy(
    definition: StrategyDefinition,
    candidate: OpportunityCandidate,
    snapshot: FeatureSnapshot,
    market_regime: MarketRegime,
    security_regime: SecurityRegime,
    *,
    expectancy: ExpectancyEvidence | None = None,
) -> StrategyEvaluation:
    if candidate.symbol != snapshot.symbol or candidate.decision_at != snapshot.decision_at:
        raise ValueError("candidate and feature snapshot identity/time are inconsistent")
    if candidate.feature_snapshot_id != snapshot.snapshot_id:
        raise ValueError("candidate feature snapshot identity is inconsistent")
    if market_regime.decision_at != candidate.decision_at:
        raise ValueError("market regime decision time is inconsistent")
    if (
        security_regime.symbol != candidate.symbol
        or security_regime.decision_at != candidate.decision_at
    ):
        raise ValueError("security regime identity/time is inconsistent")
    if definition.lifecycle is StrategyValidationState.DISABLED:
        return _result(
            definition,
            candidate,
            snapshot,
            status=EvaluationStatus.DISABLED,
            direction=definition.direction,
            reasons=(definition.disabled_reason or "strategy_disabled",),
            market_regime=market_regime,
            security_regime=security_regime,
            expectancy=expectancy,
        )
    if definition.evaluator_code_hash != evaluator_behavior_hash(definition.evaluator_id):
        raise ValueError("strategy evaluator code hash does not match implementation")
    missing = tuple(
        name
        for name in definition.required_features
        if (feature := snapshot.numeric(name)) is None
        or feature.availability is not Availability.AVAILABLE
        or feature.value is None
    )
    if missing:
        return _result(
            definition,
            candidate,
            snapshot,
            status=EvaluationStatus.INSUFFICIENT_DATA,
            direction=definition.direction,
            reasons=tuple(f"missing_required_feature:{name}" for name in missing),
            market_regime=market_regime,
            security_regime=security_regime,
            expectancy=expectancy,
        )
    if market_regime.state is RegimeState.INSUFFICIENT_DATA or (
        security_regime.state is RegimeState.INSUFFICIENT_DATA
    ):
        return _result(
            definition,
            candidate,
            snapshot,
            status=EvaluationStatus.INSUFFICIENT_DATA,
            direction=definition.direction,
            reasons=("regime_evidence_insufficient",),
            market_regime=market_regime,
            security_regime=security_regime,
            expectancy=expectancy,
        )
    if (
        market_regime.state not in definition.compatible_market_regimes
        or security_regime.state not in definition.compatible_security_regimes
    ):
        return _result(
            definition,
            candidate,
            snapshot,
            status=EvaluationStatus.REJECTED,
            direction=definition.direction,
            reasons=("regime_incompatible",),
            market_regime=market_regime,
            security_regime=security_regime,
            expectancy=expectancy,
        )
    evaluator = _EVALUATORS[definition.evaluator_id]
    return evaluator(
        definition,
        candidate,
        snapshot,
        market_regime,
        security_regime,
        expectancy,
    )


def validate_lifecycle_transition(
    current: StrategyValidationState,
    target: StrategyValidationState,
    *,
    strategy_id: str,
    strategy_version: str,
    requested_at: datetime,
    effective_at: datetime,
    actor_type: LifecycleActorType,
    validation_evidence_ids: tuple[str, ...],
    run_evidence_ids: tuple[str, ...],
    reason: str,
    policy_version: str,
) -> StrategyLifecycleTransition:
    validate_lifecycle_transition_rules(
        current,
        target,
        actor_type=actor_type,
        validation_evidence_ids=validation_evidence_ids,
        run_evidence_ids=run_evidence_ids,
    )
    values = {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "from_state": current,
        "to_state": target,
        "requested_at": requested_at,
        "effective_at": effective_at,
        "actor_type": actor_type,
        "validation_evidence_ids": validation_evidence_ids,
        "run_evidence_ids": run_evidence_ids,
        "reason": reason,
        "policy_version": policy_version,
        "schema_version": "v2.opportunity.strategy_lifecycle_transition.v1",
    }
    return StrategyLifecycleTransition(
        transition_id=stable_identity("lifecycle-transition", values),
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        from_state=current,
        to_state=target,
        requested_at=requested_at,
        effective_at=effective_at,
        actor_type=actor_type,
        validation_evidence_ids=validation_evidence_ids,
        run_evidence_ids=run_evidence_ids,
        reason=reason,
        policy_version=policy_version,
    )


def _definition(
    strategy_id: str,
    name: str,
    direction: StrategyDirection,
    required_features: tuple[str, ...],
    compatible_regimes: tuple[RegimeState, ...],
    evaluator_id: str,
    failure_modes: tuple[str, ...],
    thresholds: tuple[tuple[str, Decimal], ...],
) -> StrategyDefinition:
    evaluator_code_hash = evaluator_behavior_hash(evaluator_id)
    return StrategyDefinition(
        strategy_id=strategy_id,
        version=STRATEGY_VERSION,
        name=name,
        description=f"Research-only deterministic heuristic: {name}.",
        direction=direction,
        lifecycle=StrategyValidationState.EXPERIMENTAL,
        required_features=required_features,
        compatible_market_regimes=compatible_regimes,
        compatible_security_regimes=compatible_regimes,
        evaluator_id=evaluator_id,
        evaluator_code_hash=evaluator_code_hash,
        parameters=tuple(
            StrategyParameter(
                name=name,
                value=value,
                evidence_kind=EvidenceKind.HEURISTIC,
                source=THRESHOLD_VERSION,
            )
            for name, value in ((*thresholds, ("reward_multiple", Decimal("2"))))
        ),
        evidence_kind=EvidenceKind.HEURISTIC,
        thresholds_version=THRESHOLD_VERSION,
        failure_modes=failure_modes,
    )


def _order_flow_definition(strategy_id: str, name: str) -> StrategyDefinition:
    return StrategyDefinition(
        strategy_id=strategy_id,
        version=STRATEGY_VERSION,
        name=name,
        description="Disabled until aggressor-side trade evidence is available.",
        direction=StrategyDirection.BOTH,
        lifecycle=StrategyValidationState.DISABLED,
        required_features=("true_cvd", "aggressor_imbalance"),
        compatible_market_regimes=tuple(RegimeState),
        compatible_security_regimes=tuple(RegimeState),
        evaluator_id="disabled_order_flow",
        evaluator_code_hash=hashlib.sha256(b"disabled_order_flow").hexdigest(),
        parameters=(),
        evidence_kind=EvidenceKind.HEURISTIC,
        thresholds_version=THRESHOLD_VERSION,
        failure_modes=("aggressor_classification_unavailable",),
        disabled_reason=(
            "requires_aggressor_side_trade_evidence;OHLCV_must_not_approximate_true_CVD"
        ),
    )


def _evaluate_mom_001(*args: object) -> StrategyEvaluation:
    definition, candidate, snapshot, market, security, expectancy = _typed_args(args)
    eligible = (
        _has_anomaly(candidate, AnomalyType.RELATIVE_VOLUME)
        and _number(snapshot, "relative_volume")
        >= _parameter(definition, "minimum_relative_volume")
        and _number(snapshot, "return_short") >= _parameter(definition, "minimum_return_short")
        and _number(snapshot, "range_position") >= _parameter(definition, "minimum_range_position")
    )
    return _evaluated(
        definition,
        candidate,
        snapshot,
        market,
        security,
        expectancy,
        eligible=eligible,
        direction=StrategyDirection.LONG,
        reject_reason="momentum_continuation_conditions_not_met",
    )


def _evaluate_mom_002(*args: object) -> StrategyEvaluation:
    definition, candidate, snapshot, market, security, expectancy = _typed_args(args)
    eligible = (
        _has_anomaly(candidate, AnomalyType.VWAP_PROXY_RECLAIM)
        and _number(snapshot, "vwap_proxy_reclaim")
        >= _parameter(definition, "minimum_vwap_reclaim")
        and _number(snapshot, "return_short") > _parameter(definition, "minimum_return_short")
    )
    return _evaluated(
        definition,
        candidate,
        snapshot,
        market,
        security,
        expectancy,
        eligible=eligible,
        direction=StrategyDirection.LONG,
        reject_reason="vwap_proxy_pullback_reclaim_conditions_not_met",
    )


def _evaluate_mom_003(*args: object) -> StrategyEvaluation:
    definition, candidate, snapshot, market, security, expectancy = _typed_args(args)
    signal_threshold = _parameter(definition, "signal_threshold")
    breakout = _number(snapshot, "breakout_signal") >= signal_threshold
    breakdown = _number(snapshot, "breakdown_signal") >= signal_threshold
    opening = (
        _parameter(definition, "minimum_minutes_since_open")
        <= _number(snapshot, "minutes_since_open")
        <= _parameter(definition, "maximum_minutes_since_open")
    )
    liquid = _number(snapshot, "relative_volume") >= _parameter(
        definition, "minimum_relative_volume"
    )
    eligible = (breakout or breakdown) and opening and liquid
    direction = StrategyDirection.LONG if breakout else StrategyDirection.SHORT
    return _evaluated(
        definition,
        candidate,
        snapshot,
        market,
        security,
        expectancy,
        eligible=eligible,
        direction=direction,
        reject_reason="opening_range_expansion_conditions_not_met",
    )


def _evaluate_mr_001(*args: object) -> StrategyEvaluation:
    definition, candidate, snapshot, market, security, expectancy = _typed_args(args)
    displacement = _number(snapshot, "vwap_proxy_displacement")
    eligible = _has_anomaly(candidate, AnomalyType.VWAP_PROXY_DISPLACEMENT) and abs(
        displacement
    ) >= _parameter(definition, "minimum_absolute_vwap_displacement")
    direction = StrategyDirection.SHORT if displacement > 0 else StrategyDirection.LONG
    return _evaluated(
        definition,
        candidate,
        snapshot,
        market,
        security,
        expectancy,
        eligible=eligible,
        direction=direction,
        reject_reason="extreme_vwap_proxy_displacement_not_present",
    )


def _evaluate_rev_001(*args: object) -> StrategyEvaluation:
    definition, candidate, snapshot, market, security, expectancy = _typed_args(args)
    signal_threshold = _parameter(definition, "signal_threshold")
    eligible = (
        _number(snapshot, "failed_extension_signal") >= signal_threshold
        or _number(snapshot, "exhaustion_signal") >= signal_threshold
    )
    direction = (
        StrategyDirection.SHORT
        if _number(snapshot, "range_position") <= _parameter(definition, "range_midpoint")
        else StrategyDirection.LONG
    )
    return _evaluated(
        definition,
        candidate,
        snapshot,
        market,
        security,
        expectancy,
        eligible=eligible,
        direction=direction,
        reject_reason="failed_extension_or_exhaustion_not_present",
    )


def _evaluate_rev_002(*args: object) -> StrategyEvaluation:
    definition, candidate, snapshot, market, security, expectancy = _typed_args(args)
    signal_threshold = _parameter(definition, "signal_threshold")
    failed_breakout = _number(snapshot, "failed_breakout_signal") >= signal_threshold
    failed_breakdown = _number(snapshot, "failed_breakdown_signal") >= signal_threshold
    eligible = failed_breakout or failed_breakdown
    direction = StrategyDirection.SHORT if failed_breakout else StrategyDirection.LONG
    return _evaluated(
        definition,
        candidate,
        snapshot,
        market,
        security,
        expectancy,
        eligible=eligible,
        direction=direction,
        reject_reason="failed_breakout_or_breakdown_not_present",
    )


def _evaluate_rs_001(*args: object) -> StrategyEvaluation:
    definition, candidate, snapshot, market, security, expectancy = _typed_args(args)
    eligible = (
        _has_anomaly(candidate, AnomalyType.MARKET_RELATIVE_STRENGTH)
        and _number(snapshot, "market_relative_strength")
        >= _parameter(definition, "minimum_market_relative_strength")
        and _number(snapshot, "return_short") > _parameter(definition, "minimum_return_short")
    )
    return _evaluated(
        definition,
        candidate,
        snapshot,
        market,
        security,
        expectancy,
        eligible=eligible,
        direction=StrategyDirection.LONG,
        reject_reason="market_relative_strength_conditions_not_met",
    )


def _evaluated(
    definition: StrategyDefinition,
    candidate: OpportunityCandidate,
    snapshot: FeatureSnapshot,
    market: MarketRegime,
    security: SecurityRegime,
    expectancy: ExpectancyEvidence | None,
    *,
    eligible: bool,
    direction: StrategyDirection,
    reject_reason: str,
) -> StrategyEvaluation:
    return _result(
        definition,
        candidate,
        snapshot,
        status=EvaluationStatus.ELIGIBLE if eligible else EvaluationStatus.REJECTED,
        direction=direction,
        reasons=("heuristic_conditions_met",) if eligible else (reject_reason,),
        market_regime=market,
        security_regime=security,
        expectancy=expectancy,
    )


def _result(
    definition: StrategyDefinition,
    candidate: OpportunityCandidate,
    snapshot: FeatureSnapshot,
    *,
    status: EvaluationStatus,
    direction: StrategyDirection,
    reasons: tuple[str, ...],
    market_regime: MarketRegime,
    security_regime: SecurityRegime,
    expectancy: ExpectancyEvidence | None,
) -> StrategyEvaluation:
    effective_reasons = (
        reasons
        if expectancy is not None
        else (*reasons, "expectancy_evidence_unavailable")
    )
    entry = _optional_number(snapshot, "close_price")
    atr = _optional_number(snapshot, "atr_prior")
    stop: Decimal | None = None
    target: Decimal | None = None
    if status is EvaluationStatus.ELIGIBLE and entry is not None and atr is not None:
        reward_multiple = _parameter(definition, "reward_multiple")
        if direction is StrategyDirection.LONG:
            stop = entry - atr
            target = entry + reward_multiple * atr
        elif direction is StrategyDirection.SHORT:
            stop = entry + atr
            target = entry - reward_multiple * atr
    anomaly_strength = max(
        (item.strength for item in candidate.anomalies if item.strength is not None),
        default=None,
    )
    data_quality_score = {
        DataQuality.HIGH: Decimal("1"),
        DataQuality.MEDIUM: Decimal("0.7"),
        DataQuality.LOW: Decimal("0.4"),
        DataQuality.INSUFFICIENT_DATA: None,
    }[snapshot.data_quality]
    liquidity_score = _optional_number(snapshot, "cross_section_liquidity_percentile")
    payload = {
        "candidate_id": candidate.candidate_id,
        "feature_snapshot_id": snapshot.snapshot_id,
        "strategy_id": definition.strategy_id,
        "strategy_version": definition.version,
        "strategy_definition_hash": definition.content_hash(),
        "evaluator_id": definition.evaluator_id,
        "evaluator_code_hash": definition.evaluator_code_hash,
        "status": status,
        "direction": direction,
        "reasons": effective_reasons,
    }
    return StrategyEvaluation(
        evaluation_id=stable_identity("evaluation", payload),
        candidate_id=candidate.candidate_id,
        feature_snapshot_id=snapshot.snapshot_id,
        symbol=candidate.symbol,
        decision_at=candidate.decision_at,
        strategy_id=definition.strategy_id,
        strategy_version=definition.version,
        strategy_definition_hash=definition.content_hash(),
        evaluator_id=definition.evaluator_id,
        evaluator_code_hash=definition.evaluator_code_hash,
        lifecycle=definition.lifecycle,
        direction=direction,
        status=status,
        reasons=effective_reasons,
        entry_price=entry if status is EvaluationStatus.ELIGIBLE else None,
        invalidation_price=stop,
        target_price=target,
        after_cost_reward_risk=None,
        anomaly_strength=anomaly_strength,
        regime_fit=(
            Decimal("1")
            if market_regime.state in definition.compatible_market_regimes
            and security_regime.state in definition.compatible_security_regimes
            else Decimal("0")
        ),
        data_quality_score=data_quality_score,
        liquidity_score=liquidity_score,
        expectancy=expectancy,
    )


def _typed_args(
    args: tuple[object, ...],
) -> tuple[
    StrategyDefinition,
    OpportunityCandidate,
    FeatureSnapshot,
    MarketRegime,
    SecurityRegime,
    ExpectancyEvidence | None,
]:
    definition, candidate, snapshot, market, security, expectancy = args
    if not isinstance(definition, StrategyDefinition):
        raise TypeError("invalid strategy definition")
    if not isinstance(candidate, OpportunityCandidate):
        raise TypeError("invalid candidate")
    if not isinstance(snapshot, FeatureSnapshot):
        raise TypeError("invalid feature snapshot")
    if not isinstance(market, MarketRegime):
        raise TypeError("invalid market regime")
    if not isinstance(security, SecurityRegime):
        raise TypeError("invalid security regime")
    if expectancy is not None and not isinstance(expectancy, ExpectancyEvidence):
        raise TypeError("invalid expectancy evidence")
    return definition, candidate, snapshot, market, security, expectancy


def _number(snapshot: FeatureSnapshot, name: str) -> Decimal:
    value = _optional_number(snapshot, name)
    if value is None:
        raise ValueError(f"required feature {name} is unavailable")
    return value


def _parameter(definition: StrategyDefinition, name: str) -> Decimal:
    matches = [item.value for item in definition.parameters if item.name == name]
    if len(matches) != 1:
        raise ValueError(
            f"strategy {definition.strategy_id}:{definition.version} requires parameter {name}"
        )
    return matches[0]


def evaluator_behavior_hash(evaluator_id: str) -> str:
    """Hash the evaluator and every shared helper that can alter its pair result."""

    evaluator = _EVALUATORS.get(evaluator_id)
    if evaluator is None:
        raise ValueError(f"unregistered evaluator identity: {evaluator_id}")
    components = (
        ("evaluator", evaluator),
        ("evaluate_strategy", evaluate_strategy),
        ("evaluated", _evaluated),
        ("result", _result),
        ("typed_args", _typed_args),
        ("parameter", _parameter),
        ("number", _number),
        ("optional_number", _optional_number),
        ("has_anomaly", _has_anomaly),
    )
    source_bundle = "\n".join(
        f"[{name}]\n{inspect.getsource(function)}" for name, function in components
    )
    return hashlib.sha256(source_bundle.encode("utf-8")).hexdigest()


def _optional_number(snapshot: FeatureSnapshot, name: str) -> Decimal | None:
    feature = snapshot.numeric(name)
    return feature.value if feature is not None else None


def _has_anomaly(candidate: OpportunityCandidate, anomaly_type: AnomalyType) -> bool:
    return any(item.anomaly_type is anomaly_type and item.triggered for item in candidate.anomalies)


_EVALUATORS: dict[str, Evaluator] = {
    "evaluate_mom_001": _evaluate_mom_001,
    "evaluate_mom_002": _evaluate_mom_002,
    "evaluate_mom_003": _evaluate_mom_003,
    "evaluate_mr_001": _evaluate_mr_001,
    "evaluate_rev_001": _evaluate_rev_001,
    "evaluate_rev_002": _evaluate_rev_002,
    "evaluate_rs_001": _evaluate_rs_001,
}


__all__ = [
    "STRATEGY_VERSION",
    "StrategyRegistry",
    "alphaops_v5_adapter_parity",
    "build_alphaops_v5_adapter",
    "build_default_registry",
    "evaluator_behavior_hash",
    "evaluate_strategy",
    "validate_lifecycle_transition",
]
