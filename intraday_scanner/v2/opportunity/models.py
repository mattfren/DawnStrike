"""Immutable contracts for the research-only market-first opportunity core."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from intraday_scanner.v2.contracts.serialization import ContractMixin, contract_to_json


class Availability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INSUFFICIENT_DATA = "insufficient_data"
    UNSUPPORTED = "unsupported"


class DataQuality(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT_DATA = "insufficient_data"


class EvidenceKind(str, Enum):
    HEURISTIC = "heuristic"
    EMPIRICAL = "empirical"


class FeatureStage(str, Enum):
    CHEAP = "cheap"
    RICH = "rich"


class SessionSegment(str, Enum):
    PREMARKET = "premarket"
    OPENING = "opening"
    MORNING = "morning"
    LUNCH = "lunch"
    AFTERNOON = "afternoon"
    POWER_HOUR = "power_hour"
    AFTER_HOURS = "after_hours"


class AnomalyType(str, Enum):
    RELATIVE_VOLUME = "relative_volume"
    VOLUME_ACCELERATION = "volume_acceleration"
    PRICE_ACCELERATION = "price_acceleration"
    GAP = "gap"
    RANGE_EXPANSION = "range_expansion"
    VOLATILITY_EXPANSION = "volatility_expansion"
    VOLATILITY_COMPRESSION = "volatility_compression"
    VWAP_PROXY_DISPLACEMENT = "vwap_proxy_displacement"
    VWAP_PROXY_RECLAIM = "vwap_proxy_reclaim"
    VWAP_PROXY_LOSS = "vwap_proxy_loss"
    MARKET_RELATIVE_STRENGTH = "market_relative_strength"
    MARKET_RELATIVE_WEAKNESS = "market_relative_weakness"
    PRICE_VOLUME_DIVERGENCE = "price_volume_divergence"
    BREAKOUT = "breakout"
    BREAKDOWN = "breakdown"
    FAILED_EXTENSION = "failed_extension"
    EXHAUSTION = "exhaustion"
    CATALYST_ABNORMAL_RESPONSE = "catalyst_abnormal_response"
    LIQUIDITY = "liquidity"
    TRUE_ORDER_FLOW = "true_order_flow"


class RegimeState(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    MEAN_REVERTING = "mean_reverting"
    VOLATILITY_EXPANSION = "volatility_expansion"
    VOLATILITY_COMPRESSION = "volatility_compression"
    CHOP = "chop"
    BREAKOUT = "breakout"
    BREAKDOWN = "breakdown"
    EXHAUSTION = "exhaustion"
    UNKNOWN = "unknown"
    INSUFFICIENT_DATA = "insufficient_data"


class StrategyDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    BOTH = "both"


class StrategyValidationState(str, Enum):
    EXPERIMENTAL = "experimental"
    RESEARCH_PASS = "research_pass"
    VALIDATION_PASS = "validation_pass"
    OOS_PASS = "oos_pass"
    PAPER_TRADING = "paper_trading"
    PRODUCTION_ELIGIBLE = "production_eligible"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    REJECTED = "rejected"


class LifecycleActorType(str, Enum):
    HUMAN_REVIEWER = "human_reviewer"
    GOVERNANCE_REVIEWER = "governance_reviewer"
    AUTOMATED_SYSTEM = "automated_system"


class EvaluationStatus(str, Enum):
    ELIGIBLE = "eligible"
    REJECTED = "rejected"
    INSUFFICIENT_DATA = "insufficient_data"
    DISABLED = "disabled"


class TradeDecisionValue(str, Enum):
    TAKE = "take"
    WATCH = "watch"
    PASS = "pass"
    INSUFFICIENT_DATA = "insufficient_data"


RANKED_GATE_CHECK_IDS = (
    "evaluation_eligible",
    "research_watch_lifecycle",
    "absolute_watch_score",
    "data_quality",
    "liquidity",
    "gross_reward_risk",
    "production_lifecycle",
    "absolute_take_score",
    "after_cost_reward_risk",
    "risk_policy_minimum_available",
    "execution_risk_vetoes",
    "execution_risk_empirical",
    "empirical_expectancy_available",
    "expectancy_sample",
    "expectancy_positive",
    "expectancy_uncertainty",
)

NON_RANKABLE_GATE_CHECK_IDS = ("evaluation_non_rankable_status",)

PAIR_TRACE_STAGE_NAMES = (
    "authoritative_universe_snapshot",
    "cheap_features",
    "strategy_independent_discovery",
    "candidate_rich_features",
    "market_and_security_regimes",
    "strategy_pair_evaluation",
    "global_pair_ranking",
    "absolute_quality_gate",
)

class RunStatus(str, Enum):
    CREATED = "created"
    COMPLETED = "completed"
    FAILED = "failed"
    INSUFFICIENT_DATA = "insufficient_data"


class OpportunityContract(ContractMixin):
    """Contract mixin with a canonical SHA-256 content identity."""

    def content_hash(self) -> str:
        return hashlib.sha256(contract_to_json(self).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NumericFeature(OpportunityContract):
    name: str
    value: Decimal | None
    availability: Availability
    method: str
    sample_size: int
    window_id: str
    observed_at: datetime
    source_kind: str
    reason: str | None = None
    schema_version: str = "v2.opportunity.numeric_feature.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_text(self.name, "name")
        _require_text(self.method, "method")
        _require_text(self.window_id, "window_id")
        _require_text(self.source_kind, "source_kind")
        _require_non_negative(self.sample_size, "sample_size")
        _validate_available_value(self.value, self.availability, self.name)


@dataclass(frozen=True)
class CategoricalFeature(OpportunityContract):
    name: str
    value: str | None
    availability: Availability
    method: str
    observed_at: datetime
    reason: str | None = None
    schema_version: str = "v2.opportunity.categorical_feature.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_text(self.name, "name")
        _require_text(self.method, "method")
        if self.availability is Availability.AVAILABLE and not self.value:
            raise ValueError(f"{self.name} must have a value when available")
        if self.availability is not Availability.AVAILABLE and self.value is not None:
            raise ValueError(f"{self.name} must not carry a value when unavailable")


@dataclass(frozen=True)
class FeatureSnapshot(OpportunityContract):
    snapshot_id: str
    symbol: str
    decision_at: datetime
    market_date: str
    universe_id: str
    dataset_id: str
    stage: FeatureStage
    latest_bar_at: datetime
    numerical: tuple[NumericFeature, ...]
    categorical: tuple[CategoricalFeature, ...]
    unavailable_features: tuple[str, ...]
    data_quality: DataQuality
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.feature_snapshot.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_text(self.snapshot_id, "snapshot_id")
        _require_text(self.symbol, "symbol")
        _require_text(self.market_date, "market_date")
        try:
            parsed_market_date = date.fromisoformat(self.market_date)
        except ValueError as exc:
            raise ValueError("market_date must be an ISO calendar date") from exc
        if parsed_market_date.isoformat() != self.market_date:
            raise ValueError("market_date must use canonical ISO calendar date format")
        if self.latest_bar_at > self.decision_at:
            raise ValueError("latest_bar_at cannot be after decision_at")
        names = [feature.name for feature in self.numerical]
        names.extend(feature.name for feature in self.categorical)
        _require_unique(names, "feature name")
        for numeric_feature in self.numerical:
            if numeric_feature.observed_at > self.decision_at:
                raise ValueError(f"feature {numeric_feature.name} was observed after decision_at")
        for categorical_feature in self.categorical:
            if categorical_feature.observed_at > self.decision_at:
                raise ValueError(
                    f"feature {categorical_feature.name} was observed after decision_at"
                )
        unavailable = {
            feature.name
            for feature in self.numerical
            if feature.availability is not Availability.AVAILABLE
        }
        unavailable.update(
            feature.name
            for feature in self.categorical
            if feature.availability is not Availability.AVAILABLE
        )
        if set(self.unavailable_features) != unavailable:
            raise ValueError("unavailable_features must exactly match unavailable feature states")

    def numeric(self, name: str) -> NumericFeature | None:
        return next((feature for feature in self.numerical if feature.name == name), None)

    def category(self, name: str) -> CategoricalFeature | None:
        return next((feature for feature in self.categorical if feature.name == name), None)


@dataclass(frozen=True)
class AnomalyEvidence(OpportunityContract):
    anomaly_type: AnomalyType
    triggered: bool
    strength: Decimal | None
    availability: Availability
    evidence_kind: EvidenceKind
    threshold: Decimal | None
    threshold_source: str
    method: str
    sample_size: int
    feature_names: tuple[str, ...]
    reasons: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.anomaly_evidence.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_text(self.threshold_source, "threshold_source")
        _require_text(self.method, "method")
        _require_non_negative(self.sample_size, "sample_size")
        _validate_available_value(self.strength, self.availability, "strength")
        if self.threshold is not None:
            _require_finite(self.threshold, "threshold")
        if self.availability is Availability.AVAILABLE and self.threshold is None:
            raise ValueError("available heuristic anomaly requires a threshold")
        if self.triggered and self.availability is not Availability.AVAILABLE:
            raise ValueError("an unavailable anomaly cannot be triggered")


@dataclass(frozen=True)
class OpportunityCandidate(OpportunityContract):
    candidate_id: str
    symbol: str
    decision_at: datetime
    feature_snapshot_id: str
    anomalies: tuple[AnomalyEvidence, ...]
    discovery_reasons: tuple[str, ...]
    discovery_rank: int | None = None
    strategy_independent: bool = True
    schema_version: str = "v2.opportunity.candidate.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        if not self.strategy_independent:
            raise ValueError("opportunity discovery must remain strategy independent")
        if not self.anomalies or not any(item.triggered for item in self.anomalies):
            raise ValueError("candidate requires at least one triggered anomaly")
        if len(self.anomalies) != len({item.anomaly_type for item in self.anomalies}):
            raise ValueError("candidate anomaly types must be unique")
        if self.discovery_rank is not None and self.discovery_rank < 1:
            raise ValueError("discovery_rank must be positive")


@dataclass(frozen=True)
class MarketRegime(OpportunityContract):
    regime_id: str
    decision_at: datetime
    benchmark_symbol: str | None
    state: RegimeState
    measurements: tuple[NumericFeature, ...]
    confidence: Decimal | None
    evidence_kind: EvidenceKind
    reasons: tuple[str, ...]
    schema_version: str = "v2.opportunity.market_regime.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _validate_probability(self.confidence, "confidence")
        _require_unique([item.name for item in self.measurements], "regime measurement")
        if any(item.observed_at > self.decision_at for item in self.measurements):
            raise ValueError("market regime measurement cannot be observed after decision_at")


@dataclass(frozen=True)
class SecurityRegime(OpportunityContract):
    regime_id: str
    symbol: str
    decision_at: datetime
    state: RegimeState
    measurements: tuple[NumericFeature, ...]
    confidence: Decimal | None
    evidence_kind: EvidenceKind
    reasons: tuple[str, ...]
    schema_version: str = "v2.opportunity.security_regime.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _validate_probability(self.confidence, "confidence")
        _require_unique([item.name for item in self.measurements], "regime measurement")
        if any(item.observed_at > self.decision_at for item in self.measurements):
            raise ValueError("security regime measurement cannot be observed after decision_at")


@dataclass(frozen=True)
class StrategyParameter(OpportunityContract):
    name: str
    value: Decimal
    evidence_kind: EvidenceKind
    source: str
    schema_version: str = "v2.opportunity.strategy_parameter.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_text(self.name, "name")
        _require_text(self.source, "source")
        _require_finite(self.value, self.name)


@dataclass(frozen=True)
class StrategyDefinition(OpportunityContract):
    strategy_id: str
    version: str
    name: str
    description: str
    direction: StrategyDirection
    lifecycle: StrategyValidationState
    required_features: tuple[str, ...]
    compatible_market_regimes: tuple[RegimeState, ...]
    compatible_security_regimes: tuple[RegimeState, ...]
    evaluator_id: str
    evaluator_code_hash: str
    parameters: tuple[StrategyParameter, ...]
    evidence_kind: EvidenceKind
    thresholds_version: str
    failure_modes: tuple[str, ...]
    disabled_reason: str | None = None
    schema_version: str = "v2.opportunity.strategy_definition.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        for value, name in (
            (self.strategy_id, "strategy_id"),
            (self.version, "version"),
            (self.name, "name"),
            (self.evaluator_id, "evaluator_id"),
            (self.evaluator_code_hash, "evaluator_code_hash"),
            (self.thresholds_version, "thresholds_version"),
        ):
            _require_text(value, name)
        _require_unique(list(self.required_features), "required feature")
        _require_unique([item.name for item in self.parameters], "parameter")
        if len(self.compatible_market_regimes) != len(set(self.compatible_market_regimes)):
            raise ValueError("duplicate compatible market regime")
        if len(self.compatible_security_regimes) != len(set(self.compatible_security_regimes)):
            raise ValueError("duplicate compatible security regime")
        if self.lifecycle is StrategyValidationState.DISABLED and not self.disabled_reason:
            raise ValueError("disabled strategies require disabled_reason")


_ALLOWED_LIFECYCLE_TRANSITIONS = {
    StrategyValidationState.EXPERIMENTAL: {
        StrategyValidationState.RESEARCH_PASS,
        StrategyValidationState.DISABLED,
        StrategyValidationState.REJECTED,
    },
    StrategyValidationState.RESEARCH_PASS: {
        StrategyValidationState.VALIDATION_PASS,
        StrategyValidationState.DISABLED,
        StrategyValidationState.REJECTED,
    },
    StrategyValidationState.VALIDATION_PASS: {
        StrategyValidationState.OOS_PASS,
        StrategyValidationState.DISABLED,
        StrategyValidationState.REJECTED,
    },
    StrategyValidationState.OOS_PASS: {
        StrategyValidationState.PAPER_TRADING,
        StrategyValidationState.DISABLED,
        StrategyValidationState.REJECTED,
    },
    StrategyValidationState.PAPER_TRADING: {
        StrategyValidationState.PRODUCTION_ELIGIBLE,
        StrategyValidationState.DEGRADED,
        StrategyValidationState.DISABLED,
        StrategyValidationState.REJECTED,
    },
    StrategyValidationState.PRODUCTION_ELIGIBLE: {
        StrategyValidationState.DEGRADED,
        StrategyValidationState.DISABLED,
    },
    StrategyValidationState.DEGRADED: {
        StrategyValidationState.PAPER_TRADING,
        StrategyValidationState.DISABLED,
        StrategyValidationState.REJECTED,
    },
    StrategyValidationState.DISABLED: {StrategyValidationState.EXPERIMENTAL},
    StrategyValidationState.REJECTED: set(),
}

_LIFECYCLE_PROMOTION_EDGES = {
    (StrategyValidationState.EXPERIMENTAL, StrategyValidationState.RESEARCH_PASS),
    (StrategyValidationState.RESEARCH_PASS, StrategyValidationState.VALIDATION_PASS),
    (StrategyValidationState.VALIDATION_PASS, StrategyValidationState.OOS_PASS),
    (StrategyValidationState.OOS_PASS, StrategyValidationState.PAPER_TRADING),
    (StrategyValidationState.PAPER_TRADING, StrategyValidationState.PRODUCTION_ELIGIBLE),
    (StrategyValidationState.DEGRADED, StrategyValidationState.PAPER_TRADING),
    (StrategyValidationState.DISABLED, StrategyValidationState.EXPERIMENTAL),
}


def validate_lifecycle_transition_rules(
    current: StrategyValidationState,
    target: StrategyValidationState,
    *,
    actor_type: LifecycleActorType,
    validation_evidence_ids: tuple[str, ...],
    run_evidence_ids: tuple[str, ...],
) -> None:
    """Enforce the transition graph for builders and direct contract construction."""

    if target not in _ALLOWED_LIFECYCLE_TRANSITIONS[current]:
        raise ValueError(f"invalid lifecycle transition: {current.value}->{target.value}")
    is_promotion = (current, target) in _LIFECYCLE_PROMOTION_EDGES
    if is_promotion and actor_type is LifecycleActorType.AUTOMATED_SYSTEM:
        raise ValueError("automated lifecycle promotion is prohibited")
    if is_promotion and (not validation_evidence_ids or not run_evidence_ids):
        raise ValueError("lifecycle promotion requires validation and run evidence")


@dataclass(frozen=True)
class StrategyLifecycleTransition(OpportunityContract):
    transition_id: str
    strategy_id: str
    strategy_version: str
    from_state: StrategyValidationState
    to_state: StrategyValidationState
    requested_at: datetime
    effective_at: datetime
    actor_type: LifecycleActorType
    validation_evidence_ids: tuple[str, ...]
    run_evidence_ids: tuple[str, ...]
    reason: str
    policy_version: str
    schema_version: str = "v2.opportunity.strategy_lifecycle_transition.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        for value, name in (
            (self.transition_id, "transition_id"),
            (self.strategy_id, "strategy_id"),
            (self.strategy_version, "strategy_version"),
            (self.reason, "reason"),
            (self.policy_version, "policy_version"),
        ):
            _require_text(value, name)
        if self.effective_at < self.requested_at:
            raise ValueError("lifecycle effective_at cannot precede requested_at")
        _require_unique(list(self.validation_evidence_ids), "validation evidence ID")
        _require_unique(list(self.run_evidence_ids), "run evidence ID")
        for evidence_id in (*self.validation_evidence_ids, *self.run_evidence_ids):
            _require_text(evidence_id, "lifecycle evidence ID")
        validate_lifecycle_transition_rules(
            self.from_state,
            self.to_state,
            actor_type=self.actor_type,
            validation_evidence_ids=self.validation_evidence_ids,
            run_evidence_ids=self.run_evidence_ids,
        )
        expected = stable_identity("lifecycle-transition", _lifecycle_identity_payload(self))
        if self.transition_id != expected:
            raise ValueError("lifecycle transition identity does not match content")


@dataclass(frozen=True)
class ExpectancyEvidence(OpportunityContract):
    evidence_id: str
    cohort_id: str
    availability: Availability
    evidence_kind: EvidenceKind
    sample_size: int
    effective_sample_size: Decimal | None
    win_probability: Decimal | None
    average_winner_r: Decimal | None
    average_loser_r: Decimal | None
    expectancy_r: Decimal | None
    profit_factor: Decimal | None
    average_mfe_r: Decimal | None
    average_mae_r: Decimal | None
    average_holding_minutes: Decimal | None
    confidence_interval_low_r: Decimal | None
    confidence_interval_high_r: Decimal | None
    uncertainty_half_width_r: Decimal | None
    stability_score: Decimal | None
    regime: RegimeState | None
    limitations: tuple[str, ...]
    schema_version: str = "v2.opportunity.expectancy_evidence.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_non_negative(self.sample_size, "sample_size")
        for name in (
            "effective_sample_size",
            "win_probability",
            "average_winner_r",
            "average_loser_r",
            "expectancy_r",
            "profit_factor",
            "average_mfe_r",
            "average_mae_r",
            "average_holding_minutes",
            "confidence_interval_low_r",
            "confidence_interval_high_r",
            "uncertainty_half_width_r",
            "stability_score",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_finite(value, name)
        _validate_probability(self.win_probability, "win_probability")
        _validate_probability(self.stability_score, "stability_score")
        metric_values = (
            self.effective_sample_size,
            self.win_probability,
            self.average_winner_r,
            self.average_loser_r,
            self.expectancy_r,
            self.profit_factor,
            self.average_mfe_r,
            self.average_mae_r,
            self.average_holding_minutes,
            self.confidence_interval_low_r,
            self.confidence_interval_high_r,
            self.uncertainty_half_width_r,
            self.stability_score,
        )
        if self.availability is not Availability.AVAILABLE and any(
            value is not None for value in metric_values
        ):
            raise ValueError("unavailable expectancy evidence cannot carry metric values")
        if self.availability is Availability.AVAILABLE:
            core_values = (
                self.effective_sample_size,
                self.win_probability,
                self.average_winner_r,
                self.average_loser_r,
                self.expectancy_r,
            )
            if self.sample_size < 1 or any(value is None for value in core_values):
                raise ValueError("available expectancy requires sample and core R metrics")


@dataclass(frozen=True)
class StrategyExpectancyBinding(OpportunityContract):
    binding_id: str
    decision_at: datetime
    strategy_id: str
    strategy_version: str
    strategy_definition_hash: str
    observed_at: datetime
    source_identity: str
    method: str
    evidence_id: str
    evidence_content_hash: str
    evidence: ExpectancyEvidence
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.strategy_expectancy_binding.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        for value, name in (
            (self.binding_id, "binding_id"),
            (self.strategy_id, "strategy_id"),
            (self.strategy_version, "strategy_version"),
            (self.strategy_definition_hash, "strategy_definition_hash"),
            (self.evidence_id, "evidence_id"),
            (self.evidence_content_hash, "evidence_content_hash"),
        ):
            _require_text(value, name)
        _require_sanitized_lineage(self.source_identity, "source_identity")
        _require_sanitized_lineage(self.method, "method")
        if self.observed_at > self.decision_at:
            raise ValueError("expectancy evidence cannot be observed after decision_at")
        if self.evidence_id != self.evidence.evidence_id:
            raise ValueError("expectancy evidence identity does not match embedded evidence")
        if self.evidence_content_hash != self.evidence.content_hash():
            raise ValueError("expectancy evidence hash does not match embedded evidence")
        _require_unique(list(self.limitations), "expectancy binding limitation")
        for limitation in self.limitations:
            _require_sanitized_lineage(limitation, "expectancy binding limitation")
        expected = stable_identity(
            "strategy-expectancy",
            _strategy_expectancy_binding_payload(self),
        )
        if self.binding_id != expected:
            raise ValueError("strategy expectancy binding identity does not match content")


@dataclass(frozen=True)
class StrategyEvaluation(OpportunityContract):
    evaluation_id: str
    candidate_id: str
    feature_snapshot_id: str
    symbol: str
    decision_at: datetime
    strategy_id: str
    strategy_version: str
    strategy_definition_hash: str
    evaluator_id: str
    evaluator_code_hash: str
    lifecycle: StrategyValidationState
    direction: StrategyDirection
    status: EvaluationStatus
    reasons: tuple[str, ...]
    entry_price: Decimal | None
    invalidation_price: Decimal | None
    target_price: Decimal | None
    after_cost_reward_risk: Decimal | None
    anomaly_strength: Decimal | None
    regime_fit: Decimal | None
    data_quality_score: Decimal | None
    liquidity_score: Decimal | None
    expectancy: ExpectancyEvidence | None = None
    schema_version: str = "v2.opportunity.strategy_evaluation.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_text(self.strategy_definition_hash, "strategy_definition_hash")
        _require_text(self.evaluator_id, "evaluator_id")
        _require_text(self.evaluator_code_hash, "evaluator_code_hash")
        for name in (
            "entry_price",
            "invalidation_price",
            "target_price",
            "after_cost_reward_risk",
            "anomaly_strength",
            "regime_fit",
            "data_quality_score",
            "liquidity_score",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_finite(value, name)
        _validate_probability(self.anomaly_strength, "anomaly_strength")
        _validate_probability(self.regime_fit, "regime_fit")
        _validate_probability(self.data_quality_score, "data_quality_score")
        _validate_probability(self.liquidity_score, "liquidity_score")


@dataclass(frozen=True)
class RankComponent(OpportunityContract):
    name: str
    value: Decimal
    weight: Decimal
    contribution: Decimal
    evidence_kind: EvidenceKind
    explanation: str
    schema_version: str = "v2.opportunity.rank_component.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        for name in ("value", "weight", "contribution"):
            _require_finite(getattr(self, name), name)
        if not Decimal("0") <= self.value <= Decimal("1"):
            raise ValueError("rank component value must be between zero and one")
        if self.weight <= 0:
            raise ValueError("rank component weight must be positive")
        expected = self.value * self.weight
        if abs(self.contribution - expected) > Decimal("0.0000000001"):
            raise ValueError("rank component contribution must equal value times weight")


@dataclass(frozen=True)
class RankedOpportunity(OpportunityContract):
    ranked_id: str
    evaluation_id: str
    symbol: str
    strategy_id: str
    strategy_version: str
    direction: StrategyDirection
    relative_rank: int
    base_score: Decimal
    concentration_penalty: Decimal
    final_score: Decimal
    components: tuple[RankComponent, ...]
    concentration_labels: tuple[str, ...]
    limitations: tuple[str, ...]
    schema_version: str = "v2.opportunity.ranked_opportunity.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        if self.relative_rank < 1:
            raise ValueError("relative_rank must be positive")
        for name in ("base_score", "concentration_penalty", "final_score"):
            _require_finite(getattr(self, name), name)
        if self.concentration_penalty < 0:
            raise ValueError("concentration_penalty cannot be negative")
        _require_unique([item.name for item in self.components], "rank component")
        total_weight = sum((item.weight for item in self.components), Decimal("0"))
        total_contribution = sum((item.contribution for item in self.components), Decimal("0"))
        expected_base = total_contribution / total_weight if total_weight > 0 else Decimal("0")
        if abs(self.base_score - expected_base) > Decimal("0.0000000001"):
            raise ValueError("base_score must equal normalized component contribution")
        expected_final = max(Decimal("0"), self.base_score - self.concentration_penalty)
        if abs(self.final_score - expected_final) > Decimal("0.0000000001"):
            raise ValueError("final_score must equal base_score minus concentration penalty")


@dataclass(frozen=True)
class GateCheck(OpportunityContract):
    check_id: str
    passed: bool | None
    mandatory: bool
    reason: str
    schema_version: str = "v2.opportunity.gate_check.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_text(self.check_id, "check_id")
        _require_text(self.reason, "reason")


@dataclass(frozen=True)
class DecisionRunBinding(OpportunityContract):
    evaluation_id: str
    evaluation_content_hash: str
    evaluation_status: EvaluationStatus
    lifecycle: StrategyValidationState
    ranked_id: str | None
    ranked_content_hash: str | None
    risk_evidence_id: str | None
    risk_evidence_content_hash: str | None
    schema_version: str = "v2.opportunity.decision_run_binding.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_text(self.evaluation_id, "evaluation_id")
        _require_text(self.evaluation_content_hash, "evaluation_content_hash")
        if (self.ranked_id is None) is not (self.ranked_content_hash is None):
            raise ValueError("ranked identity and content hash must be present together")
        if (self.risk_evidence_id is None) is not (
            self.risk_evidence_content_hash is None
        ):
            raise ValueError("risk identity and content hash must be present together")
        if self.ranked_id is not None:
            assert self.ranked_content_hash is not None
            _require_text(self.ranked_id, "ranked_id")
            _require_text(self.ranked_content_hash, "ranked_content_hash")
        if self.risk_evidence_id is not None:
            assert self.risk_evidence_content_hash is not None
            _require_text(self.risk_evidence_id, "risk_evidence_id")
            _require_text(self.risk_evidence_content_hash, "risk_evidence_content_hash")
        if (self.evaluation_status is EvaluationStatus.ELIGIBLE) is not (
            self.ranked_id is not None
        ):
            raise ValueError("decision run ranks must exist exactly for eligible evaluations")
        if (
            self.evaluation_status is not EvaluationStatus.ELIGIBLE
            and self.risk_evidence_id is not None
        ):
            raise ValueError("noneligible decision run binding cannot carry risk evidence")


@dataclass(frozen=True)
class DecisionRunContext(OpportunityContract):
    decision_run_id: str
    decision_at: datetime
    bindings: tuple[DecisionRunBinding, ...]
    gate_config_identity: str
    gate_config_version: str
    research_only: bool = True
    schema_version: str = "v2.opportunity.decision_run_context.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_text(self.decision_run_id, "decision_run_id")
        if re.fullmatch(r"opportunity-decision-run:[0-9a-f]{24}", self.decision_run_id) is None:
            raise ValueError("decision_run_id must be a sanitized content identity")
        _require_text(self.gate_config_identity, "gate_config_identity")
        _require_text(self.gate_config_version, "gate_config_version")
        if not self.research_only:
            raise ValueError("decision run context must remain research_only")
        if not self.bindings:
            raise ValueError("decision run context requires at least one binding")
        evaluation_ids = [item.evaluation_id for item in self.bindings]
        ranked_ids = [item.ranked_id for item in self.bindings if item.ranked_id is not None]
        risk_ids = [
            item.risk_evidence_id
            for item in self.bindings
            if item.risk_evidence_id is not None
        ]
        _require_unique(evaluation_ids, "decision run evaluation")
        _require_unique(ranked_ids, "decision run rank")
        _require_unique(risk_ids, "decision run risk evidence")
        expected_id = stable_identity("opportunity-decision-run", _decision_run_payload(self))
        if self.decision_run_id != expected_id:
            raise ValueError("decision run identity does not match content")


@dataclass(frozen=True)
class TradeDecision(OpportunityContract):
    decision_id: str
    decision_run_id: str
    evaluation_id: str
    evaluation_content_hash: str
    ranked_id: str | None
    non_rankable_reason: str | None
    risk_evidence_id: str | None
    risk_evidence_content_hash: str | None
    symbol: str
    strategy_id: str
    strategy_version: str
    direction: StrategyDirection
    lifecycle: StrategyValidationState
    decision_at: datetime
    decision: TradeDecisionValue
    decision_context: DecisionRunContext
    evaluation: StrategyEvaluation
    ranked: RankedOpportunity | None
    gate_checks: tuple[GateCheck, ...]
    vetoes: tuple[str, ...]
    rationale: tuple[str, ...]
    limitations: tuple[str, ...]
    research_only: bool = True
    schema_version: str = "v2.opportunity.trade_decision.v2"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        if not self.research_only:
            raise ValueError("opportunity decisions must remain research_only")
        for value, name in (
            (self.decision_id, "decision_id"),
            (self.decision_run_id, "decision_run_id"),
            (self.evaluation_id, "evaluation_id"),
            (self.evaluation_content_hash, "evaluation_content_hash"),
            (self.symbol, "symbol"),
            (self.strategy_id, "strategy_id"),
            (self.strategy_version, "strategy_version"),
        ):
            _require_text(value, name)
        if self.decision_context.decision_run_id != self.decision_run_id:
            raise ValueError("decision_run_id does not match embedded decision context")
        if self.decision_context.decision_at != self.decision_at:
            raise ValueError("decision context time does not match decision_at")
        evaluation_metadata = (
            self.evaluation.evaluation_id,
            self.evaluation.content_hash(),
            self.evaluation.symbol,
            self.evaluation.strategy_id,
            self.evaluation.strategy_version,
            self.evaluation.direction,
            self.evaluation.lifecycle,
            self.evaluation.decision_at,
        )
        decision_metadata = (
            self.evaluation_id,
            self.evaluation_content_hash,
            self.symbol,
            self.strategy_id,
            self.strategy_version,
            self.direction,
            self.lifecycle,
            self.decision_at,
        )
        if decision_metadata != evaluation_metadata:
            raise ValueError("decision metadata does not match StrategyEvaluation")
        binding = next(
            (
                item
                for item in self.decision_context.bindings
                if item.evaluation_id == self.evaluation_id
            ),
            None,
        )
        if binding is None:
            raise ValueError("decision evaluation is absent from decision run context")
        if (
            binding.evaluation_content_hash != self.evaluation_content_hash
            or binding.evaluation_status is not self.evaluation.status
            or binding.lifecycle is not self.lifecycle
        ):
            raise ValueError("decision evaluation binding does not match run context")
        if (self.ranked_id is None) is not (self.ranked is None):
            raise ValueError("ranked identity and embedded rank must be present together")
        if (self.ranked_id is None) is not (self.non_rankable_reason is not None):
            raise ValueError("unranked decision requires an explicit non_rankable_reason")
        if (self.risk_evidence_id is None) is not (
            self.risk_evidence_content_hash is None
        ):
            raise ValueError("risk identity and content hash must be present together")
        if self.non_rankable_reason is not None:
            _require_text(self.non_rankable_reason, "non_rankable_reason")
        if self.ranked is not None:
            rank_metadata = (
                self.ranked.ranked_id,
                self.ranked.evaluation_id,
                self.ranked.symbol,
                self.ranked.strategy_id,
                self.ranked.strategy_version,
                self.ranked.direction,
            )
            expected_rank_metadata = (
                self.ranked_id,
                self.evaluation_id,
                self.symbol,
                self.strategy_id,
                self.strategy_version,
                self.direction,
            )
            if rank_metadata != expected_rank_metadata:
                raise ValueError("decision rank does not match evaluation pair metadata")
            if (
                binding.ranked_id != self.ranked_id
                or binding.ranked_content_hash != self.ranked.content_hash()
            ):
                raise ValueError("decision rank does not match run context binding")
        elif self.decision not in {
            TradeDecisionValue.PASS,
            TradeDecisionValue.INSUFFICIENT_DATA,
        }:
            raise ValueError("non-rankable evaluation cannot TAKE or WATCH")
        if self.ranked is None:
            expected_reason = f"evaluation_status_{self.evaluation.status.value}"
            expected_decision = (
                TradeDecisionValue.INSUFFICIENT_DATA
                if self.evaluation.status is EvaluationStatus.INSUFFICIENT_DATA
                else TradeDecisionValue.PASS
            )
            if self.non_rankable_reason != expected_reason:
                raise ValueError("non_rankable_reason does not match evaluation status")
            if self.decision is not expected_decision:
                raise ValueError("non-rankable decision does not match evaluation status")
        if (
            binding.risk_evidence_id != self.risk_evidence_id
            or binding.risk_evidence_content_hash != self.risk_evidence_content_hash
        ):
            raise ValueError("decision risk identity does not match run context binding")
        _require_unique([check.check_id for check in self.gate_checks], "gate check")
        expected_check_ids = (
            RANKED_GATE_CHECK_IDS if self.ranked is not None else NON_RANKABLE_GATE_CHECK_IDS
        )
        actual_check_ids = tuple(check.check_id for check in self.gate_checks)
        if actual_check_ids != expected_check_ids:
            raise ValueError("trade decision gate checks do not match canonical schema")
        if any(not check.mandatory for check in self.gate_checks):
            raise ValueError("trade decision canonical gate checks must remain mandatory")
        _require_unique(list(self.vetoes), "decision veto")
        _require_unique(list(self.rationale), "decision rationale")
        _require_unique(list(self.limitations), "decision limitation")
        for values, label in (
            (self.vetoes, "decision veto"),
            (self.rationale, "decision rationale"),
            (self.limitations, "decision limitation"),
        ):
            for value in values:
                _require_text(value, label)
        if (
            self.decision is TradeDecisionValue.TAKE
            and self.lifecycle is not StrategyValidationState.PRODUCTION_ELIGIBLE
        ):
            raise ValueError("TAKE requires PRODUCTION_ELIGIBLE lifecycle")
        if self.decision is TradeDecisionValue.TAKE:
            if self.risk_evidence_id is None:
                raise ValueError("TAKE requires ExecutionRiskEvidence")
            mandatory = [check for check in self.gate_checks if check.mandatory]
            if not mandatory or any(check.passed is not True for check in mandatory):
                raise ValueError("TAKE requires every mandatory gate check to pass")
            if self.vetoes:
                raise ValueError("TAKE cannot carry vetoes")
        expected_id = stable_identity("decision", _trade_decision_identity_payload(self))
        if self.decision_id != expected_id:
            raise ValueError("trade decision identity does not match content")


@dataclass(frozen=True)
class BacktestRun(OpportunityContract):
    run_id: str
    created_at: datetime
    dataset_id: str
    code_hash: str
    strategy_hashes: tuple[str, ...]
    research_start: datetime
    research_end: datetime
    assumptions: tuple[str, ...]
    status: RunStatus
    limitations: tuple[str, ...]
    schema_version: str = "v2.opportunity.backtest_run.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        if self.research_start > self.research_end:
            raise ValueError("backtest research interval is reversed")


@dataclass(frozen=True)
class ValidationRun(OpportunityContract):
    run_id: str
    created_at: datetime
    backtest_run_id: str
    validation_start: datetime
    validation_end: datetime
    locked_oos: bool
    configuration_hash: str
    status: RunStatus
    limitations: tuple[str, ...]
    schema_version: str = "v2.opportunity.validation_run.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        if self.validation_start > self.validation_end:
            raise ValueError("validation interval is reversed")


@dataclass(frozen=True)
class StageTraceEntry(OpportunityContract):
    ordinal: int
    stage_name: str
    input_ids: tuple[str, ...]
    output_ids: tuple[str, ...]
    input_count: int
    output_count: int
    reasons: tuple[str, ...]
    score_components: tuple[RankComponent, ...] = ()
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.stage_trace_entry.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        if self.ordinal < 1:
            raise ValueError("trace ordinal must be positive")
        if self.input_count != len(self.input_ids):
            raise ValueError("input_count must match input_ids")
        if self.output_count != len(self.output_ids):
            raise ValueError("output_count must match output_ids")


@dataclass(frozen=True)
class DecisionTrace(OpportunityContract):
    trace_id: str
    universe_snapshot_id: str
    universe_snapshot_content_hash: str
    universe_provider_receipt_ids: tuple[str, ...]
    universe_requested_member_ids: tuple[str, ...]
    universe_included_member_ids: tuple[str, ...]
    universe_excluded_member_ids: tuple[str, ...]
    universe_requested_count: int
    universe_included_count: int
    universe_excluded_count: int
    universe_member_id: str
    evaluation_id: str
    evaluation_content_hash: str
    symbol: str
    strategy_id: str
    strategy_version: str
    direction: StrategyDirection
    decision_at: datetime
    candidate_id: str
    ranked_id: str | None
    ranked_content_hash: str | None
    risk_evidence_id: str | None
    risk_evidence_content_hash: str | None
    decision_run_id: str
    global_rank_input_ids: tuple[str, ...]
    global_ranked_ids: tuple[str, ...]
    evaluation: StrategyEvaluation
    ranked: RankedOpportunity | None
    stages: tuple[StageTraceEntry, ...]
    final_decision_id: str
    final_decision_content_hash: str
    final_decision: TradeDecisionValue
    final_trade_decision: TradeDecision
    limitations: tuple[str, ...]
    schema_version: str = "v2.opportunity.decision_trace.v2"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        for value, name in (
            (self.trace_id, "trace_id"),
            (self.universe_snapshot_id, "universe_snapshot_id"),
            (self.universe_snapshot_content_hash, "universe_snapshot_content_hash"),
            (self.universe_member_id, "universe_member_id"),
            (self.evaluation_id, "evaluation_id"),
            (self.evaluation_content_hash, "evaluation_content_hash"),
            (self.symbol, "symbol"),
            (self.strategy_id, "strategy_id"),
            (self.strategy_version, "strategy_version"),
            (self.candidate_id, "candidate_id"),
            (self.decision_run_id, "decision_run_id"),
            (self.final_decision_id, "final_decision_id"),
            (self.final_decision_content_hash, "final_decision_content_hash"),
        ):
            _require_text(value, name)
        _require_unique(list(self.universe_provider_receipt_ids), "universe receipt ID")
        if self.universe_provider_receipt_ids != tuple(
            sorted(self.universe_provider_receipt_ids)
        ):
            raise ValueError("universe receipt IDs must use canonical order")
        for values, label in (
            (self.universe_requested_member_ids, "requested member ID"),
            (self.universe_included_member_ids, "included member ID"),
            (self.universe_excluded_member_ids, "excluded member ID"),
        ):
            _require_unique(list(values), label)
        if set(self.universe_included_member_ids) & set(self.universe_excluded_member_ids):
            raise ValueError("included and excluded universe member IDs must be disjoint")
        if set(self.universe_requested_member_ids) != set(
            (*self.universe_included_member_ids, *self.universe_excluded_member_ids)
        ):
            raise ValueError("universe trace member IDs do not reconcile")
        if self.universe_requested_count != len(self.universe_requested_member_ids):
            raise ValueError("universe requested count does not reconcile")
        if self.universe_included_count != len(self.universe_included_member_ids):
            raise ValueError("universe included count does not reconcile")
        if self.universe_excluded_count != len(self.universe_excluded_member_ids):
            raise ValueError("universe excluded count does not reconcile")
        if self.universe_member_id not in self.universe_included_member_ids:
            raise ValueError("pair trace member must be included in the universe")
        evaluation_metadata = (
            self.evaluation.evaluation_id,
            self.evaluation.content_hash(),
            self.evaluation.symbol,
            self.evaluation.strategy_id,
            self.evaluation.strategy_version,
            self.evaluation.direction,
            self.evaluation.decision_at,
            self.evaluation.candidate_id,
        )
        trace_metadata = (
            self.evaluation_id,
            self.evaluation_content_hash,
            self.symbol,
            self.strategy_id,
            self.strategy_version,
            self.direction,
            self.decision_at,
            self.candidate_id,
        )
        if trace_metadata != evaluation_metadata:
            raise ValueError("pair trace metadata does not match embedded evaluation")
        if (self.ranked_id is None) is not (self.ranked is None):
            raise ValueError("trace rank identity and embedded rank must be present together")
        if (self.ranked_id is None) is not (self.ranked_content_hash is None):
            raise ValueError("trace rank identity and hash must be present together")
        if self.ranked is not None:
            if (
                self.ranked_id != self.ranked.ranked_id
                or self.ranked_content_hash != self.ranked.content_hash()
                or self.ranked.evaluation_id != self.evaluation_id
                or self.ranked.symbol != self.symbol
                or self.ranked.strategy_id != self.strategy_id
                or self.ranked.strategy_version != self.strategy_version
                or self.ranked.direction is not self.direction
            ):
                raise ValueError("pair trace rank does not match evaluation")
        if (self.risk_evidence_id is None) is not (
            self.risk_evidence_content_hash is None
        ):
            raise ValueError("trace risk identity and hash must be present together")
        decision = self.final_trade_decision
        if (
            decision.decision_id != self.final_decision_id
            or decision.content_hash() != self.final_decision_content_hash
            or decision.decision is not self.final_decision
            or decision.decision_run_id != self.decision_run_id
            or decision.evaluation_id != self.evaluation_id
            or decision.evaluation_content_hash != self.evaluation_content_hash
            or decision.ranked_id != self.ranked_id
            or decision.risk_evidence_id != self.risk_evidence_id
            or decision.risk_evidence_content_hash != self.risk_evidence_content_hash
            or decision.evaluation != self.evaluation
            or decision.ranked != self.ranked
        ):
            raise ValueError("pair trace final decision does not match bound pair")
        _require_unique(list(self.global_rank_input_ids), "global rank input")
        _require_unique(list(self.global_ranked_ids), "global ranked identity")
        ordinals = [stage.ordinal for stage in self.stages]
        if ordinals != list(range(1, len(self.stages) + 1)):
            raise ValueError("trace stages must be consecutively ordered")
        if tuple(stage.stage_name for stage in self.stages) != PAIR_TRACE_STAGE_NAMES:
            raise ValueError("pair trace stages do not match canonical schema")
        universe_stage = self.stages[0]
        if universe_stage.input_ids != (
            self.universe_snapshot_id,
            self.universe_snapshot_content_hash,
            *self.universe_provider_receipt_ids,
            *self.universe_requested_member_ids,
        ):
            raise ValueError("universe trace stage inputs do not match bound snapshot")
        if universe_stage.output_ids != self.universe_included_member_ids:
            raise ValueError("universe trace stage outputs do not match included members")
        if self.stages[5].output_ids != (self.evaluation_id,):
            raise ValueError("evaluation trace stage must output bound evaluation")
        if (
            self.stages[6].input_ids != self.global_rank_input_ids
            or self.stages[6].output_ids != self.global_ranked_ids
        ):
            raise ValueError("ranking trace stage does not match global ranking payload")
        expected_gate_inputs = (
            self.evaluation_id,
            self.decision_run_id,
            *((self.ranked_id,) if self.ranked_id is not None else ()),
            *((self.risk_evidence_id,) if self.risk_evidence_id is not None else ()),
        )
        if self.stages[7].input_ids != expected_gate_inputs:
            raise ValueError("quality-gate trace inputs do not match pair evidence")
        if self.stages[7].output_ids != (self.final_decision_id,):
            raise ValueError("quality-gate trace must output final decision")
        _require_unique(list(self.limitations), "trace limitation")
        expected = stable_identity("decision-trace", _decision_trace_identity_payload(self))
        if self.trace_id != expected:
            raise ValueError("decision trace identity does not match content")


def stable_identity(prefix: str, payload: Any) -> str:
    """Return a deterministic bounded identity for canonical contract content."""

    digest = hashlib.sha256(contract_to_json(payload).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:24]}"


def _lifecycle_identity_payload(
    transition: StrategyLifecycleTransition,
) -> dict[str, object]:
    return {
        name: value
        for name, value in transition.__dict__.items()
        if name != "transition_id"
    }


def _trade_decision_identity_payload(decision: TradeDecision) -> dict[str, object]:
    return {
        name: value for name, value in decision.__dict__.items() if name != "decision_id"
    }


def _decision_run_payload(context: DecisionRunContext) -> dict[str, object]:
    return {
        name: value
        for name, value in context.__dict__.items()
        if name != "decision_run_id"
    }


def _strategy_expectancy_binding_payload(
    binding: StrategyExpectancyBinding,
) -> dict[str, object]:
    return {
        name: value for name, value in binding.__dict__.items() if name != "binding_id"
    }


def _decision_trace_identity_payload(trace: DecisionTrace) -> dict[str, object]:
    return {name: value for name, value in trace.__dict__.items() if name != "trace_id"}


def _validate_available_value(
    value: Decimal | None,
    availability: Availability,
    field_name: str,
) -> None:
    if availability is Availability.AVAILABLE:
        if value is None:
            raise ValueError(f"{field_name} requires a value when available")
        _require_finite(value, field_name)
    elif value is not None:
        raise ValueError(f"{field_name} must not carry a value when unavailable")


def _require_finite(value: Decimal, field_name: str) -> None:
    if not value.is_finite() or not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")


def _validate_probability(value: Decimal | None, field_name: str) -> None:
    if value is None:
        return
    _require_finite(value, field_name)
    if not Decimal("0") <= value <= Decimal("1"):
        raise ValueError(f"{field_name} must be between zero and one")


def _require_non_negative(value: int, field_name: str) -> None:
    if value < 0:
        raise ValueError(f"{field_name} cannot be negative")


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be blank")


_PRIVATE_LINEAGE_VALUE = re.compile(
    r"(?i)(?:api[_-]?key|secret|access[_-]?token|token|password|authorization)"
    r"\s*[:=]\s*\S+|\bbearer\s+\S+|https?://[^\s]*(?:@|api[_-]?key=|token=|secret=)"
    r"|(?:[A-Za-z]:[\\/]|\\\\|/Users/|/home/)"
    r"|(?:localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)"
)


def _require_sanitized_lineage(value: str, field_name: str) -> None:
    _require_text(value, field_name)
    if _PRIVATE_LINEAGE_VALUE.search(value):
        raise ValueError(f"{field_name} contains a private or secret value")


def _require_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")


__all__ = [
    "AnomalyEvidence",
    "AnomalyType",
    "Availability",
    "BacktestRun",
    "CategoricalFeature",
    "DataQuality",
    "DecisionTrace",
    "DecisionRunBinding",
    "DecisionRunContext",
    "EvaluationStatus",
    "EvidenceKind",
    "ExpectancyEvidence",
    "PAIR_TRACE_STAGE_NAMES",
    "FeatureSnapshot",
    "FeatureStage",
    "GateCheck",
    "LifecycleActorType",
    "MarketRegime",
    "NumericFeature",
    "OpportunityCandidate",
    "RankComponent",
    "RankedOpportunity",
    "RegimeState",
    "RunStatus",
    "SecurityRegime",
    "SessionSegment",
    "StageTraceEntry",
    "StrategyDefinition",
    "StrategyDirection",
    "StrategyEvaluation",
    "StrategyExpectancyBinding",
    "StrategyLifecycleTransition",
    "StrategyParameter",
    "StrategyValidationState",
    "TradeDecision",
    "TradeDecisionValue",
    "ValidationRun",
    "stable_identity",
    "validate_lifecycle_transition_rules",
]
