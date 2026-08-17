"""Pure, research-only execution-risk evidence for opportunity evaluations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from intraday_scanner.v2.opportunity.capabilities import (
    CapabilityState,
    ProviderCapabilityReceipt,
)
from intraday_scanner.v2.opportunity.models import (
    EvidenceKind,
    OpportunityContract,
    StrategyDirection,
    StrategyEvaluation,
    stable_identity,
)
from intraday_scanner.v2.opportunity.universe import SafetyStatus


class RiskValueStatus(str, Enum):
    """How a numeric risk value was established, independent of evidence quality."""

    OBSERVED = "observed"
    PROVISIONAL = "provisional"
    DERIVED = "derived"
    UNAVAILABLE = "unavailable"


class RiskUnit(str, Enum):
    USD_PER_SHARE = "usd_per_share"
    SHARES = "shares"
    BPS = "bps"
    USD = "usd"
    FRACTION = "fraction"
    RATIO = "ratio"
    SECONDS = "seconds"


class RiskMetric(str, Enum):
    ENTRY_PRICE = "entry_price"
    STOP_PRICE = "stop_price"
    TARGET_PRICE = "target_price"
    STOP_DISTANCE = "stop_distance"
    GROSS_REWARD = "gross_reward"
    GROSS_REWARD_RISK = "gross_reward_risk"
    SPREAD_BPS = "spread_bps"
    ENTRY_SLIPPAGE_BPS = "entry_slippage_bps"
    EXIT_SLIPPAGE_BPS = "exit_slippage_bps"
    ROUND_TRIP_FEE_PER_SHARE = "round_trip_fee_per_share"
    PER_SHARE_COST = "per_share_cost"
    QUANTITY = "quantity"
    TOTAL_ROUND_TRIP_COST = "total_round_trip_cost"
    PLANNED_LOSS = "planned_loss"
    AFTER_COST_REWARD_RISK = "after_cost_reward_risk"
    ACCOUNT_EQUITY = "account_equity"
    RISK_FRACTION = "risk_fraction"
    RISK_CAP = "risk_cap"
    AGGREGATE_CONCENTRATION = "aggregate_concentration"
    MAX_AGGREGATE_CONCENTRATION = "max_aggregate_concentration"
    QUOTE_AGE = "quote_age"
    MAX_QUOTE_AGE = "max_quote_age"
    MIN_AFTER_COST_REWARD_RISK = "min_after_cost_reward_risk"


class QuoteEvidenceScope(str, Enum):
    NBBO = "nbbo"
    NONCONSOLIDATED = "nonconsolidated"
    PROVISIONAL = "provisional"
    UNAVAILABLE = "unavailable"


class RiskSafetyCategory(str, Enum):
    HALT = "halt"
    CORPORATE_ACTION = "corporate_action"


_EXPECTED_UNITS = {
    RiskMetric.ENTRY_PRICE: RiskUnit.USD_PER_SHARE,
    RiskMetric.STOP_PRICE: RiskUnit.USD_PER_SHARE,
    RiskMetric.TARGET_PRICE: RiskUnit.USD_PER_SHARE,
    RiskMetric.STOP_DISTANCE: RiskUnit.USD_PER_SHARE,
    RiskMetric.GROSS_REWARD: RiskUnit.USD_PER_SHARE,
    RiskMetric.GROSS_REWARD_RISK: RiskUnit.RATIO,
    RiskMetric.SPREAD_BPS: RiskUnit.BPS,
    RiskMetric.ENTRY_SLIPPAGE_BPS: RiskUnit.BPS,
    RiskMetric.EXIT_SLIPPAGE_BPS: RiskUnit.BPS,
    RiskMetric.ROUND_TRIP_FEE_PER_SHARE: RiskUnit.USD_PER_SHARE,
    RiskMetric.PER_SHARE_COST: RiskUnit.USD_PER_SHARE,
    RiskMetric.QUANTITY: RiskUnit.SHARES,
    RiskMetric.TOTAL_ROUND_TRIP_COST: RiskUnit.USD,
    RiskMetric.PLANNED_LOSS: RiskUnit.USD,
    RiskMetric.AFTER_COST_REWARD_RISK: RiskUnit.RATIO,
    RiskMetric.ACCOUNT_EQUITY: RiskUnit.USD,
    RiskMetric.RISK_FRACTION: RiskUnit.FRACTION,
    RiskMetric.RISK_CAP: RiskUnit.USD,
    RiskMetric.AGGREGATE_CONCENTRATION: RiskUnit.FRACTION,
    RiskMetric.MAX_AGGREGATE_CONCENTRATION: RiskUnit.FRACTION,
    RiskMetric.QUOTE_AGE: RiskUnit.SECONDS,
    RiskMetric.MAX_QUOTE_AGE: RiskUnit.SECONDS,
    RiskMetric.MIN_AFTER_COST_REWARD_RISK: RiskUnit.RATIO,
}

_BASE_METRICS = (
    RiskMetric.ENTRY_PRICE,
    RiskMetric.STOP_PRICE,
    RiskMetric.TARGET_PRICE,
    RiskMetric.SPREAD_BPS,
    RiskMetric.ENTRY_SLIPPAGE_BPS,
    RiskMetric.EXIT_SLIPPAGE_BPS,
    RiskMetric.ROUND_TRIP_FEE_PER_SHARE,
    RiskMetric.QUANTITY,
    RiskMetric.ACCOUNT_EQUITY,
    RiskMetric.RISK_FRACTION,
    RiskMetric.AGGREGATE_CONCENTRATION,
    RiskMetric.MAX_AGGREGATE_CONCENTRATION,
    RiskMetric.MAX_QUOTE_AGE,
    RiskMetric.MIN_AFTER_COST_REWARD_RISK,
)

_DERIVED_INPUTS = {
    RiskMetric.QUOTE_AGE: (RiskMetric.SPREAD_BPS,),
    RiskMetric.STOP_DISTANCE: (RiskMetric.ENTRY_PRICE, RiskMetric.STOP_PRICE),
    RiskMetric.GROSS_REWARD: (RiskMetric.TARGET_PRICE, RiskMetric.ENTRY_PRICE),
    RiskMetric.GROSS_REWARD_RISK: (
        RiskMetric.GROSS_REWARD,
        RiskMetric.STOP_DISTANCE,
    ),
    RiskMetric.PER_SHARE_COST: (
        RiskMetric.ENTRY_PRICE,
        RiskMetric.SPREAD_BPS,
        RiskMetric.ENTRY_SLIPPAGE_BPS,
        RiskMetric.EXIT_SLIPPAGE_BPS,
        RiskMetric.ROUND_TRIP_FEE_PER_SHARE,
    ),
    RiskMetric.TOTAL_ROUND_TRIP_COST: (
        RiskMetric.QUANTITY,
        RiskMetric.PER_SHARE_COST,
    ),
    RiskMetric.PLANNED_LOSS: (
        RiskMetric.QUANTITY,
        RiskMetric.STOP_DISTANCE,
        RiskMetric.TOTAL_ROUND_TRIP_COST,
    ),
    RiskMetric.AFTER_COST_REWARD_RISK: (
        RiskMetric.QUANTITY,
        RiskMetric.GROSS_REWARD,
        RiskMetric.TOTAL_ROUND_TRIP_COST,
        RiskMetric.STOP_DISTANCE,
    ),
    RiskMetric.RISK_CAP: (RiskMetric.ACCOUNT_EQUITY, RiskMetric.RISK_FRACTION),
}

_FORMULAS = {
    RiskMetric.QUOTE_AGE: "decision_at-spread_observed_at",
    RiskMetric.STOP_DISTANCE: "abs(entry_price-stop_price)",
    RiskMetric.GROSS_REWARD: "abs(target_price-entry_price)",
    RiskMetric.GROSS_REWARD_RISK: "gross_reward/stop_distance",
    RiskMetric.PER_SHARE_COST: (
        "entry_price*(spread_bps+entry_slippage_bps+exit_slippage_bps)/10000"
        "+round_trip_fee_per_share"
    ),
    RiskMetric.TOTAL_ROUND_TRIP_COST: "quantity*per_share_cost",
    RiskMetric.PLANNED_LOSS: "quantity*stop_distance+total_round_trip_cost",
    RiskMetric.AFTER_COST_REWARD_RISK: (
        "(quantity*gross_reward-total_round_trip_cost)/"
        "(quantity*stop_distance+total_round_trip_cost)"
    ),
    RiskMetric.RISK_CAP: "account_equity*risk_fraction",
}

_DERIVED_SOURCE = "opportunity-risk-formula-v1"


@dataclass(frozen=True)
class RiskNumericEvidence(OpportunityContract):
    evidence_id: str
    metric: RiskMetric
    value: Decimal | None
    unit: RiskUnit
    status: RiskValueStatus
    capability_state: CapabilityState
    evidence_kind: EvidenceKind
    observed_at: datetime
    source_identity: str
    method: str
    reason: str | None = None
    input_evidence_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.risk_numeric_evidence.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_text(self.evidence_id, "evidence_id")
        _require_sanitized_text(self.source_identity, "source_identity")
        _require_sanitized_text(self.method, "method")
        if self.unit is not _EXPECTED_UNITS[self.metric]:
            expected_unit = _EXPECTED_UNITS[self.metric].value
            raise ValueError(f"{self.metric.value} requires unit {expected_unit}")
        if self.capability_state is CapabilityState.AVAILABLE:
            if self.status is RiskValueStatus.UNAVAILABLE:
                raise ValueError("available capability cannot have unavailable value status")
        elif self.status is not RiskValueStatus.UNAVAILABLE:
            raise ValueError("non-available capability requires unavailable value status")
        if self.status is RiskValueStatus.UNAVAILABLE:
            if self.value is not None:
                raise ValueError("unavailable risk evidence cannot carry a value")
            _require_text(self.reason, "reason")
        else:
            if self.value is None:
                raise ValueError(f"{self.status.value} risk evidence requires a value")
            _validate_metric_value(self.metric, self.value)
        if self.reason is not None:
            _require_sanitized_text(self.reason, "reason")
        if self.status is RiskValueStatus.PROVISIONAL and (
            self.evidence_kind is not EvidenceKind.HEURISTIC
        ):
            raise ValueError("provisional risk evidence must be heuristic")
        if self.metric in _DERIVED_INPUTS:
            if self.status in {RiskValueStatus.OBSERVED, RiskValueStatus.PROVISIONAL}:
                raise ValueError("derived metric cannot claim observed or provisional status")
            if not self.input_evidence_ids:
                raise ValueError("derived risk evidence requires causal input evidence")
        elif self.status is RiskValueStatus.DERIVED or self.input_evidence_ids:
            raise ValueError("non-derived metric cannot claim derived provenance")
        _require_unique(self.input_evidence_ids, "input evidence ID")
        _require_unique(self.limitations, "risk limitation")
        for limitation in self.limitations:
            _require_sanitized_text(limitation, "limitation")
        expected = stable_identity("risk-numeric", _numeric_identity_payload(self))
        if self.evidence_id != expected:
            raise ValueError("risk numeric evidence identity does not match content")


@dataclass(frozen=True)
class RiskSafetyEvidence(OpportunityContract):
    safety_evidence_id: str
    category: RiskSafetyCategory
    symbol: str
    status: SafetyStatus
    observed_at: datetime
    source_identity: str
    method: str
    capability_receipt_id: str
    reason: str | None = None
    limitations: tuple[str, ...] = ()
    schema_version: str = "v2.opportunity.risk_safety_evidence.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_text(self.safety_evidence_id, "safety_evidence_id")
        _require_text(self.symbol, "symbol")
        _require_text(self.capability_receipt_id, "capability_receipt_id")
        _require_sanitized_text(self.source_identity, "source_identity")
        _require_sanitized_text(self.method, "method")
        if self.status is not SafetyStatus.CLEAR:
            _require_text(self.reason, "reason")
        if self.reason is not None:
            _require_sanitized_text(self.reason, "reason")
        _require_unique(self.limitations, "safety limitation")
        for limitation in self.limitations:
            _require_sanitized_text(limitation, "limitation")
        expected = stable_identity("risk-safety", _safety_identity_payload(self))
        if self.safety_evidence_id != expected:
            raise ValueError("risk safety evidence identity does not match content")


@dataclass(frozen=True)
class ExecutionRiskEvidence(OpportunityContract):
    execution_risk_evidence_id: str
    evaluation_id: str
    evaluation_content_hash: str
    symbol: str
    strategy_id: str
    strategy_version: str
    direction: StrategyDirection
    decision_at: datetime
    evaluation: StrategyEvaluation
    capability_receipts: tuple[ProviderCapabilityReceipt, ...]
    quote_capability_receipt_id: str | None
    quote_scope: QuoteEvidenceScope
    halt_evidence: RiskSafetyEvidence
    corporate_action_evidence: RiskSafetyEvidence
    account_identity: str
    risk_cap_identity: str
    concentration_identity: str
    metrics: tuple[RiskNumericEvidence, ...]
    vetoes: tuple[str, ...]
    limitations: tuple[str, ...]
    research_only: bool = True
    schema_version: str = "v2.opportunity.execution_risk_evidence.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        for value, name in (
            (self.execution_risk_evidence_id, "execution_risk_evidence_id"),
            (self.evaluation_id, "evaluation_id"),
            (self.evaluation_content_hash, "evaluation_content_hash"),
            (self.symbol, "symbol"),
            (self.strategy_id, "strategy_id"),
            (self.strategy_version, "strategy_version"),
            (self.account_identity, "account_identity"),
            (self.risk_cap_identity, "risk_cap_identity"),
            (self.concentration_identity, "concentration_identity"),
        ):
            _require_sanitized_text(value, name)
        if not self.research_only:
            raise ValueError("execution risk evidence must remain research_only")
        _validate_evaluation_binding(self)
        receipt_ids = tuple(item.capability_receipt_id for item in self.capability_receipts)
        _require_unique(receipt_ids, "capability receipt ID")
        if receipt_ids != tuple(sorted(receipt_ids)):
            raise ValueError("capability receipts must use canonical identity order")
        if any(item.decision_at != self.decision_at for item in self.capability_receipts):
            raise ValueError("capability receipt decision_at must match risk decision_at")
        if any(item.observed_at > self.decision_at for item in self.capability_receipts):
            raise ValueError("capability receipt cannot be observed after risk decision_at")
        metric_map = _validated_metric_map(self.metrics, self.decision_at)
        _validate_quote_binding(self, metric_map)
        _validate_safety_binding(self)
        _validate_evaluation_prices(self.evaluation, metric_map)
        _validate_source_identity_bindings(self, metric_map)
        _validate_derived_metrics(metric_map, self.decision_at)
        _require_unique(self.vetoes, "risk veto")
        expected_vetoes = _risk_vetoes(
            direction=self.direction,
            quote_scope=self.quote_scope,
            halt_evidence=self.halt_evidence,
            corporate_action_evidence=self.corporate_action_evidence,
            capability_receipts=self.capability_receipts,
            metrics=metric_map,
        )
        if self.vetoes != expected_vetoes:
            raise ValueError("risk vetoes do not match evidence")
        _require_unique(self.limitations, "execution risk limitation")
        for limitation in self.limitations:
            _require_sanitized_text(limitation, "limitation")
        expected = stable_identity("execution-risk", _execution_identity_payload(self))
        if self.execution_risk_evidence_id != expected:
            raise ValueError("execution risk evidence identity does not match content")

    def metric(self, metric: RiskMetric) -> RiskNumericEvidence:
        return next(item for item in self.metrics if item.metric is metric)


def build_risk_numeric_evidence(
    *,
    metric: RiskMetric,
    value: Decimal | None,
    status: RiskValueStatus,
    capability_state: CapabilityState,
    evidence_kind: EvidenceKind,
    observed_at: datetime,
    source_identity: str,
    method: str,
    reason: str | None = None,
    limitations: tuple[str, ...] = (),
) -> RiskNumericEvidence:
    """Build one content-bound base numeric without inventing unavailable values."""

    if metric in _DERIVED_INPUTS:
        raise ValueError("derived metrics are created by build_execution_risk_evidence")
    values = {
        "metric": metric,
        "value": value,
        "unit": _EXPECTED_UNITS[metric],
        "status": status,
        "capability_state": capability_state,
        "evidence_kind": evidence_kind,
        "observed_at": observed_at,
        "source_identity": source_identity,
        "method": method,
        "reason": reason,
        "input_evidence_ids": (),
        "limitations": limitations,
        "schema_version": "v2.opportunity.risk_numeric_evidence.v1",
    }
    return RiskNumericEvidence(
        evidence_id=stable_identity("risk-numeric", values),
        metric=metric,
        value=value,
        unit=_EXPECTED_UNITS[metric],
        status=status,
        capability_state=capability_state,
        evidence_kind=evidence_kind,
        observed_at=observed_at,
        source_identity=source_identity,
        method=method,
        reason=reason,
        limitations=limitations,
    )


def build_risk_safety_evidence(
    *,
    category: RiskSafetyCategory,
    symbol: str,
    status: SafetyStatus,
    observed_at: datetime,
    source_identity: str,
    method: str,
    capability_receipt_id: str,
    reason: str | None = None,
    limitations: tuple[str, ...] = (),
) -> RiskSafetyEvidence:
    """Build causal categorical halt or corporate-action evidence."""

    values = {
        "category": category,
        "symbol": symbol,
        "status": status,
        "observed_at": observed_at,
        "source_identity": source_identity,
        "method": method,
        "capability_receipt_id": capability_receipt_id,
        "reason": reason,
        "limitations": limitations,
        "schema_version": "v2.opportunity.risk_safety_evidence.v1",
    }
    return RiskSafetyEvidence(
        safety_evidence_id=stable_identity("risk-safety", values),
        category=category,
        symbol=symbol,
        status=status,
        observed_at=observed_at,
        source_identity=source_identity,
        method=method,
        capability_receipt_id=capability_receipt_id,
        reason=reason,
        limitations=limitations,
    )


def build_execution_risk_evidence(
    *,
    evaluation: StrategyEvaluation,
    capability_receipts: tuple[ProviderCapabilityReceipt, ...],
    quote_capability_receipt_id: str | None,
    quote_scope: QuoteEvidenceScope,
    halt_evidence: RiskSafetyEvidence,
    corporate_action_evidence: RiskSafetyEvidence,
    account_identity: str,
    risk_cap_identity: str,
    concentration_identity: str,
    base_metrics: tuple[RiskNumericEvidence, ...],
    limitations: tuple[str, ...] = (),
) -> ExecutionRiskEvidence:
    """Adapt one strategy evaluation into exact, fail-closed execution-risk evidence."""

    base_map = _validated_base_metric_map(base_metrics, evaluation.decision_at)
    metric_map = dict(base_map)
    for metric in _DERIVED_INPUTS:
        metric_map[metric] = _derive_metric(metric, metric_map, evaluation.decision_at)
    metrics = tuple(metric_map[metric] for metric in RiskMetric)
    ordered_receipts = tuple(
        sorted(capability_receipts, key=lambda item: item.capability_receipt_id)
    )
    vetoes = _risk_vetoes(
        direction=evaluation.direction,
        quote_scope=quote_scope,
        halt_evidence=halt_evidence,
        corporate_action_evidence=corporate_action_evidence,
        capability_receipts=ordered_receipts,
        metrics=metric_map,
    )
    values = {
        "evaluation_id": evaluation.evaluation_id,
        "evaluation_content_hash": evaluation.content_hash(),
        "symbol": evaluation.symbol,
        "strategy_id": evaluation.strategy_id,
        "strategy_version": evaluation.strategy_version,
        "direction": evaluation.direction,
        "decision_at": evaluation.decision_at,
        "evaluation": evaluation,
        "capability_receipts": ordered_receipts,
        "quote_capability_receipt_id": quote_capability_receipt_id,
        "quote_scope": quote_scope,
        "halt_evidence": halt_evidence,
        "corporate_action_evidence": corporate_action_evidence,
        "account_identity": account_identity,
        "risk_cap_identity": risk_cap_identity,
        "concentration_identity": concentration_identity,
        "metrics": metrics,
        "vetoes": vetoes,
        "limitations": limitations,
        "research_only": True,
        "schema_version": "v2.opportunity.execution_risk_evidence.v1",
    }
    return ExecutionRiskEvidence(
        execution_risk_evidence_id=stable_identity("execution-risk", values),
        evaluation_id=evaluation.evaluation_id,
        evaluation_content_hash=evaluation.content_hash(),
        symbol=evaluation.symbol,
        strategy_id=evaluation.strategy_id,
        strategy_version=evaluation.strategy_version,
        direction=evaluation.direction,
        decision_at=evaluation.decision_at,
        evaluation=evaluation,
        capability_receipts=ordered_receipts,
        quote_capability_receipt_id=quote_capability_receipt_id,
        quote_scope=quote_scope,
        halt_evidence=halt_evidence,
        corporate_action_evidence=corporate_action_evidence,
        account_identity=account_identity,
        risk_cap_identity=risk_cap_identity,
        concentration_identity=concentration_identity,
        metrics=metrics,
        vetoes=vetoes,
        limitations=limitations,
    )


def _derive_metric(
    metric: RiskMetric,
    metrics: dict[RiskMetric, RiskNumericEvidence],
    decision_at: datetime,
) -> RiskNumericEvidence:
    inputs = tuple(metrics[name] for name in _DERIVED_INPUTS[metric])
    values = tuple(item.value for item in inputs)
    unavailable_inputs = tuple(item for item in inputs if item.value is None)
    unavailable = tuple(item.metric.value for item in unavailable_inputs)
    if unavailable:
        value = None
    elif metric is RiskMetric.QUOTE_AGE:
        age = decision_at - inputs[0].observed_at
        value = (
            Decimal(age.days) * Decimal("86400")
            + Decimal(age.seconds)
            + Decimal(age.microseconds) / Decimal("1000000")
        )
    else:
        value = _calculate(metric, values)
    status = RiskValueStatus.UNAVAILABLE if unavailable else RiskValueStatus.DERIVED
    capability_state = _strongest_missing_capability(unavailable_inputs)
    evidence_kind = (
        EvidenceKind.HEURISTIC
        if any(
            item.status is RiskValueStatus.PROVISIONAL
            or item.evidence_kind is EvidenceKind.HEURISTIC
            for item in inputs
        )
        else EvidenceKind.EMPIRICAL
    )
    observed_at = (
        decision_at
        if metric is RiskMetric.QUOTE_AGE
        else max((item.observed_at for item in inputs), default=decision_at)
    )
    reason = (
        f"missing_inputs:{','.join(unavailable)}"
        if unavailable
        else None
    )
    input_ids = tuple(item.evidence_id for item in inputs)
    values_for_identity = {
        "metric": metric,
        "value": value,
        "unit": _EXPECTED_UNITS[metric],
        "status": status,
        "capability_state": capability_state,
        "evidence_kind": evidence_kind,
        "observed_at": observed_at,
        "source_identity": _DERIVED_SOURCE,
        "method": _FORMULAS[metric],
        "reason": reason,
        "input_evidence_ids": input_ids,
        "limitations": (),
        "schema_version": "v2.opportunity.risk_numeric_evidence.v1",
    }
    return RiskNumericEvidence(
        evidence_id=stable_identity("risk-numeric", values_for_identity),
        metric=metric,
        value=value,
        unit=_EXPECTED_UNITS[metric],
        status=status,
        capability_state=capability_state,
        evidence_kind=evidence_kind,
        observed_at=observed_at,
        source_identity=_DERIVED_SOURCE,
        method=_FORMULAS[metric],
        reason=reason,
        input_evidence_ids=input_ids,
    )


def _calculate(metric: RiskMetric, values: tuple[Decimal | None, ...]) -> Decimal:
    if any(value is None for value in values):
        raise AssertionError("calculation received unavailable input")
    concrete = tuple(value for value in values if value is not None)
    if metric is RiskMetric.STOP_DISTANCE:
        return abs(concrete[0] - concrete[1])
    if metric is RiskMetric.GROSS_REWARD:
        return abs(concrete[0] - concrete[1])
    if metric is RiskMetric.GROSS_REWARD_RISK:
        return concrete[0] / concrete[1]
    if metric is RiskMetric.PER_SHARE_COST:
        return concrete[0] * sum(concrete[1:4], Decimal("0")) / Decimal("10000") + concrete[4]
    if metric is RiskMetric.TOTAL_ROUND_TRIP_COST:
        return concrete[0] * concrete[1]
    if metric is RiskMetric.PLANNED_LOSS:
        return concrete[0] * concrete[1] + concrete[2]
    if metric is RiskMetric.AFTER_COST_REWARD_RISK:
        return (concrete[0] * concrete[1] - concrete[2]) / (
            concrete[0] * concrete[3] + concrete[2]
        )
    if metric is RiskMetric.RISK_CAP:
        return concrete[0] * concrete[1]
    raise AssertionError(f"no formula for {metric.value}")


def _validated_base_metric_map(
    metrics: tuple[RiskNumericEvidence, ...],
    decision_at: datetime,
) -> dict[RiskMetric, RiskNumericEvidence]:
    metric_map = {item.metric: item for item in metrics}
    if len(metric_map) != len(metrics):
        raise ValueError("duplicate risk metric")
    if set(metric_map) != set(_BASE_METRICS):
        raise ValueError("base metrics must contain every required non-derived risk metric")
    if any(item.observed_at > decision_at for item in metrics):
        raise ValueError("risk numeric evidence cannot be observed after decision_at")
    if any(item.status is RiskValueStatus.DERIVED for item in metrics):
        raise ValueError("base risk metrics cannot be marked derived")
    return metric_map


def _validated_metric_map(
    metrics: tuple[RiskNumericEvidence, ...],
    decision_at: datetime,
) -> dict[RiskMetric, RiskNumericEvidence]:
    metric_map = {item.metric: item for item in metrics}
    if len(metric_map) != len(metrics):
        raise ValueError("duplicate risk metric")
    if set(metric_map) != set(RiskMetric):
        raise ValueError("execution risk evidence must contain every required risk metric")
    if any(item.observed_at > decision_at for item in metrics):
        raise ValueError("risk numeric evidence cannot be observed after decision_at")
    if any(metric_map[metric].status is RiskValueStatus.DERIVED for metric in _BASE_METRICS):
        raise ValueError("base risk metrics cannot be marked derived")
    return metric_map


def _validate_evaluation_binding(receipt: ExecutionRiskEvidence) -> None:
    evaluation = receipt.evaluation
    expected = (
        evaluation.evaluation_id,
        evaluation.content_hash(),
        evaluation.symbol,
        evaluation.strategy_id,
        evaluation.strategy_version,
        evaluation.direction,
        evaluation.decision_at,
    )
    actual = (
        receipt.evaluation_id,
        receipt.evaluation_content_hash,
        receipt.symbol,
        receipt.strategy_id,
        receipt.strategy_version,
        receipt.direction,
        receipt.decision_at,
    )
    if actual != expected:
        raise ValueError("execution risk metadata does not match StrategyEvaluation")


def _validate_evaluation_prices(
    evaluation: StrategyEvaluation,
    metrics: dict[RiskMetric, RiskNumericEvidence],
) -> None:
    for metric, expected in (
        (RiskMetric.ENTRY_PRICE, evaluation.entry_price),
        (RiskMetric.STOP_PRICE, evaluation.invalidation_price),
        (RiskMetric.TARGET_PRICE, evaluation.target_price),
    ):
        evidence = metrics[metric]
        if evidence.value != expected:
            raise ValueError(f"{metric.value} does not match StrategyEvaluation")


def _validate_source_identity_bindings(
    receipt: ExecutionRiskEvidence,
    metrics: dict[RiskMetric, RiskNumericEvidence],
) -> None:
    expected_sources = {
        RiskMetric.ACCOUNT_EQUITY: receipt.account_identity,
        RiskMetric.RISK_FRACTION: receipt.risk_cap_identity,
        RiskMetric.AGGREGATE_CONCENTRATION: receipt.concentration_identity,
        RiskMetric.MAX_AGGREGATE_CONCENTRATION: receipt.concentration_identity,
    }
    for metric, expected_source in expected_sources.items():
        if metrics[metric].source_identity != expected_source:
            raise ValueError(f"{metric.value} source does not match bound identity")


def _validate_quote_binding(
    receipt: ExecutionRiskEvidence,
    metrics: dict[RiskMetric, RiskNumericEvidence],
) -> None:
    spread = metrics[RiskMetric.SPREAD_BPS]
    referenced = next(
        (
            item
            for item in receipt.capability_receipts
            if item.capability_receipt_id == receipt.quote_capability_receipt_id
        ),
        None,
    )
    if receipt.quote_scope in {QuoteEvidenceScope.PROVISIONAL, QuoteEvidenceScope.UNAVAILABLE}:
        if receipt.quote_capability_receipt_id is not None:
            raise ValueError("provisional or unavailable quote scope cannot claim a receipt")
    if receipt.quote_scope in {QuoteEvidenceScope.NBBO, QuoteEvidenceScope.NONCONSOLIDATED}:
        if referenced is None:
            raise ValueError("observed quote scope requires its capability receipt")
        if referenced.quotes is not CapabilityState.AVAILABLE:
            raise ValueError("observed spread requires available quote capability")
        if spread.status is not RiskValueStatus.OBSERVED:
            raise ValueError("observed quote scope requires observed spread")
    if receipt.quote_scope is QuoteEvidenceScope.NBBO:
        if referenced is None or referenced.consolidated_nbbo is not CapabilityState.AVAILABLE:
            raise ValueError("NBBO scope requires available consolidated/NBBO capability")
    elif receipt.quote_scope is QuoteEvidenceScope.PROVISIONAL:
        if spread.status is not RiskValueStatus.PROVISIONAL:
            raise ValueError("provisional quote scope requires provisional spread")
    elif receipt.quote_scope is QuoteEvidenceScope.UNAVAILABLE:
        if spread.status is not RiskValueStatus.UNAVAILABLE:
            raise ValueError("unavailable quote scope requires unavailable spread")


def _validate_safety_binding(receipt: ExecutionRiskEvidence) -> None:
    receipt_map = {
        item.capability_receipt_id: item for item in receipt.capability_receipts
    }
    for evidence, category in (
        (receipt.halt_evidence, RiskSafetyCategory.HALT),
        (receipt.corporate_action_evidence, RiskSafetyCategory.CORPORATE_ACTION),
    ):
        if evidence.category is not category:
            raise ValueError(f"{category.value} safety evidence category mismatch")
        if evidence.symbol != receipt.symbol:
            raise ValueError(f"{category.value} safety evidence symbol mismatch")
        if evidence.observed_at > receipt.decision_at:
            raise ValueError(
                f"{category.value} safety evidence cannot be observed after decision_at"
            )
        capability = receipt_map.get(evidence.capability_receipt_id)
        if capability is None:
            raise ValueError(f"{category.value} safety evidence receipt is not bound")
        capability_state = (
            capability.halts
            if category is RiskSafetyCategory.HALT
            else capability.corporate_actions
        )
        if evidence.status is not SafetyStatus.UNKNOWN and (
            capability_state is not CapabilityState.AVAILABLE
        ):
            raise ValueError(f"{category.value} status requires available capability")


def _validate_derived_metrics(
    metrics: dict[RiskMetric, RiskNumericEvidence],
    decision_at: datetime,
) -> None:
    working = {metric: metrics[metric] for metric in _BASE_METRICS}
    for metric in _DERIVED_INPUTS:
        expected = _derive_metric(metric, working, decision_at)
        actual = metrics[metric]
        if actual != expected:
            raise ValueError(f"derived {metric.value} does not match formula or lineage")
        working[metric] = actual


def _risk_vetoes(
    *,
    direction: StrategyDirection,
    quote_scope: QuoteEvidenceScope,
    halt_evidence: RiskSafetyEvidence,
    corporate_action_evidence: RiskSafetyEvidence,
    capability_receipts: tuple[ProviderCapabilityReceipt, ...],
    metrics: dict[RiskMetric, RiskNumericEvidence],
) -> tuple[str, ...]:
    vetoes: list[str] = []

    entry = metrics[RiskMetric.ENTRY_PRICE].value
    stop = metrics[RiskMetric.STOP_PRICE].value
    target = metrics[RiskMetric.TARGET_PRICE].value
    if direction is StrategyDirection.BOTH:
        vetoes.append("direction_unknown")
    if entry is None or stop is None or target is None:
        vetoes.append("entry_stop_target_unavailable")
    elif direction is StrategyDirection.LONG and not stop < entry < target:
        vetoes.append("long_geometry_invalid")
    elif direction is StrategyDirection.SHORT and not target < entry < stop:
        vetoes.append("short_geometry_invalid")

    spread = metrics[RiskMetric.SPREAD_BPS]
    if spread.value is None:
        vetoes.append("spread_unavailable")
    if quote_scope is QuoteEvidenceScope.UNAVAILABLE:
        vetoes.append("quote_capability_unavailable")
    elif quote_scope is QuoteEvidenceScope.PROVISIONAL:
        vetoes.append("observed_quote_unavailable")
    elif quote_scope is QuoteEvidenceScope.NONCONSOLIDATED:
        vetoes.append("consolidated_nbbo_unavailable")

    receipt_map = {
        item.capability_receipt_id: item for item in capability_receipts
    }
    halt_receipt = receipt_map.get(halt_evidence.capability_receipt_id)
    action_receipt = receipt_map.get(corporate_action_evidence.capability_receipt_id)
    halt_capability = (
        CapabilityState.UNKNOWN if halt_receipt is None else halt_receipt.halts
    )
    action_capability = (
        CapabilityState.UNKNOWN
        if action_receipt is None
        else action_receipt.corporate_actions
    )
    if halt_capability is not CapabilityState.AVAILABLE:
        vetoes.append("halt_capability_unavailable")
    if action_capability is not CapabilityState.AVAILABLE:
        vetoes.append("corporate_action_capability_unavailable")

    quote_age = metrics[RiskMetric.QUOTE_AGE].value
    max_quote_age = metrics[RiskMetric.MAX_QUOTE_AGE].value
    if quote_age is None or max_quote_age is None:
        vetoes.append("quote_staleness_unavailable")
    elif quote_age > max_quote_age:
        vetoes.append("stale_quote")

    if halt_evidence.status is SafetyStatus.UNKNOWN:
        vetoes.append("halt_status_unknown")
    elif halt_evidence.status is SafetyStatus.BLOCKED:
        vetoes.append("halt_status_blocked")
    if corporate_action_evidence.status is SafetyStatus.UNKNOWN:
        vetoes.append("corporate_action_status_unknown")
    elif corporate_action_evidence.status is SafetyStatus.BLOCKED:
        vetoes.append("corporate_action_status_blocked")

    for metric, veto in (
        (RiskMetric.QUANTITY, "quantity_unavailable"),
        (RiskMetric.ACCOUNT_EQUITY, "account_equity_unavailable"),
        (RiskMetric.RISK_FRACTION, "risk_fraction_unavailable"),
        (RiskMetric.RISK_CAP, "risk_cap_unavailable"),
        (RiskMetric.PLANNED_LOSS, "planned_loss_unavailable"),
        (RiskMetric.AGGREGATE_CONCENTRATION, "concentration_unavailable"),
        (RiskMetric.MAX_AGGREGATE_CONCENTRATION, "concentration_limit_unavailable"),
        (RiskMetric.AFTER_COST_REWARD_RISK, "after_cost_reward_risk_unavailable"),
        (RiskMetric.MIN_AFTER_COST_REWARD_RISK, "minimum_after_cost_r_unavailable"),
    ):
        if metrics[metric].value is None:
            vetoes.append(veto)

    planned_loss = metrics[RiskMetric.PLANNED_LOSS].value
    risk_cap = metrics[RiskMetric.RISK_CAP].value
    if planned_loss is not None and risk_cap is not None and planned_loss > risk_cap:
        vetoes.append("planned_loss_cap_breached")
    concentration = metrics[RiskMetric.AGGREGATE_CONCENTRATION].value
    concentration_cap = metrics[RiskMetric.MAX_AGGREGATE_CONCENTRATION].value
    if (
        concentration is not None
        and concentration_cap is not None
        and concentration > concentration_cap
    ):
        vetoes.append("concentration_cap_breached")
    after_cost = metrics[RiskMetric.AFTER_COST_REWARD_RISK].value
    minimum_after_cost = metrics[RiskMetric.MIN_AFTER_COST_REWARD_RISK].value
    if (
        after_cost is not None
        and minimum_after_cost is not None
        and after_cost < minimum_after_cost
    ):
        vetoes.append("after_cost_reward_risk_below_minimum")
    return tuple(vetoes)


def _validate_metric_value(metric: RiskMetric, value: Decimal) -> None:
    if not value.is_finite():
        raise ValueError(f"{metric.value} must be finite")
    strictly_positive = {
        RiskMetric.ENTRY_PRICE,
        RiskMetric.STOP_PRICE,
        RiskMetric.TARGET_PRICE,
        RiskMetric.STOP_DISTANCE,
        RiskMetric.GROSS_REWARD,
        RiskMetric.GROSS_REWARD_RISK,
        RiskMetric.QUANTITY,
        RiskMetric.PLANNED_LOSS,
        RiskMetric.ACCOUNT_EQUITY,
        RiskMetric.RISK_FRACTION,
        RiskMetric.RISK_CAP,
        RiskMetric.MAX_AGGREGATE_CONCENTRATION,
        RiskMetric.MAX_QUOTE_AGE,
        RiskMetric.MIN_AFTER_COST_REWARD_RISK,
    }
    if metric in strictly_positive and value <= 0:
        raise ValueError(f"{metric.value} must be positive")
    if metric not in strictly_positive and metric is not RiskMetric.AFTER_COST_REWARD_RISK:
        if value < 0:
            raise ValueError(f"{metric.value} cannot be negative")
    if metric is RiskMetric.QUANTITY and value != value.to_integral_value():
        raise ValueError("quantity must be integral")
    if metric in {
        RiskMetric.RISK_FRACTION,
        RiskMetric.AGGREGATE_CONCENTRATION,
        RiskMetric.MAX_AGGREGATE_CONCENTRATION,
    } and value > 1:
        raise ValueError(f"{metric.value} cannot exceed one")


def _numeric_identity_payload(evidence: RiskNumericEvidence) -> dict[str, object]:
    return {
        name: value for name, value in evidence.__dict__.items() if name != "evidence_id"
    }


def _safety_identity_payload(evidence: RiskSafetyEvidence) -> dict[str, object]:
    return {
        name: value
        for name, value in evidence.__dict__.items()
        if name != "safety_evidence_id"
    }


def _strongest_missing_capability(
    evidence: tuple[RiskNumericEvidence, ...],
) -> CapabilityState:
    if not evidence:
        return CapabilityState.AVAILABLE
    priority = {
        CapabilityState.UNKNOWN: 1,
        CapabilityState.UNAVAILABLE: 2,
        CapabilityState.UNSUPPORTED: 3,
        CapabilityState.AVAILABLE: 0,
    }
    return max((item.capability_state for item in evidence), key=priority.__getitem__)


def _execution_identity_payload(receipt: ExecutionRiskEvidence) -> dict[str, object]:
    return {
        name: value
        for name, value in receipt.__dict__.items()
        if name != "execution_risk_evidence_id"
    }


def _require_text(value: str | None, field_name: str) -> None:
    if value is None or not value.strip():
        raise ValueError(f"{field_name} cannot be blank")


def _require_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")


_PRIVATE_VALUE = re.compile(
    r"(?i)(?:api[_-]?key|secret|access[_-]?token|token|password|authorization)"
    r"\s*[:=]\s*\S+|\bbearer\s+\S+|https?://[^\s]*(?:@|api[_-]?key=|token=|secret=)"
    r"|(?:[A-Za-z]:[\\/]|\\\\|/Users/|/home/)"
    r"|(?:localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)"
)


def _require_sanitized_text(value: str, field_name: str) -> None:
    _require_text(value, field_name)
    if _PRIVATE_VALUE.search(value):
        raise ValueError(f"{field_name} contains a private or secret value")


__all__ = [
    "ExecutionRiskEvidence",
    "QuoteEvidenceScope",
    "RiskMetric",
    "RiskNumericEvidence",
    "RiskSafetyCategory",
    "RiskSafetyEvidence",
    "RiskUnit",
    "RiskValueStatus",
    "build_execution_risk_evidence",
    "build_risk_numeric_evidence",
    "build_risk_safety_evidence",
]
