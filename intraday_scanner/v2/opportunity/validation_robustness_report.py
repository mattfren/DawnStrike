"""Self-recomputing WP005-C robustness report and veto semantics."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from intraday_scanner.v2.opportunity.models import OpportunityContract, stable_identity
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeContract,
    _identity_payload,
    _require_hash,
    _require_identity,
    _require_sanitized_text,
    _require_schema,
    _require_unique,
    _require_utc,
)
from intraday_scanner.v2.opportunity.validation_metric_math import (
    _fresh_decimal_context,
    _metric_decimal_context,
    _quantize_metric_decimal,
)
from intraday_scanner.v2.opportunity.validation_robustness_contracts import (
    RobustnessArmKind,
    RobustnessCalibrationPolicy,
    RobustnessCheckKind,
    RobustnessCheckStatus,
    RobustnessEvidenceStatus,
    RobustnessVerdict,
)
from intraday_scanner.v2.opportunity.validation_robustness_controls import (
    CausalControlArm,
    ComplexityEvidence,
    FutureDataSentinelEvidence,
    RegimeStabilityEvidence,
    _require_exact_population,
)
from intraday_scanner.v2.opportunity.validation_robustness_math import (
    SessionClusterConfidenceInterval,
    build_session_clustered_confidence_interval,
)
from intraday_scanner.v2.opportunity.validation_robustness_population import (
    ConfirmatoryPopulation,
    _confirmatory_population_content_hash,
)


@dataclass(frozen=True)
class RobustnessCheck(OutcomeContract):
    kind: RobustnessCheckKind
    status: RobustnessCheckStatus
    observed_decimal: Decimal | None
    threshold_decimal: Decimal | None
    observed_count: int | None
    threshold_count: int | None
    source_evidence_ids: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        for decimal_value in (self.observed_decimal, self.threshold_decimal):
            if decimal_value is not None and (
                type(decimal_value) is not Decimal or not decimal_value.is_finite()
            ):
                raise ValueError("robustness check Decimal values must be finite")
        for count_value in (self.observed_count, self.threshold_count):
            if count_value is not None and (type(count_value) is not int or count_value < 0):
                raise ValueError("robustness check counts must be nonnegative integers")
        _require_unique(list(self.source_evidence_ids), "robustness check source")
        for evidence_id in self.source_evidence_ids:
            _require_identity(evidence_id, "robustness check source")
        _require_sanitized_text(self.reason, "robustness check reason")


@dataclass(frozen=True)
class ValidationRobustnessReport(OutcomeContract):
    report_id: str
    population_id: str
    population_content_hash_sha256: str
    population: ConfirmatoryPopulation
    policy_id: str
    policy_content_hash_sha256: str
    policy: RobustnessCalibrationPolicy
    recorded_at: datetime
    confidence_interval: SessionClusterConfidenceInterval
    control_arms: tuple[CausalControlArm, ...]
    regime_evidence: RegimeStabilityEvidence | None
    complexity_evidence: ComplexityEvidence | None
    future_data_sentinel: FutureDataSentinelEvidence | None
    checks: tuple[RobustnessCheck, ...]
    verdict: RobustnessVerdict
    limitations: tuple[str, ...]
    lifecycle_mutation_count: int = 0
    take_authorization: bool = False
    no_control_veto_is_non_promotional: bool = True
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.validation_robustness_report.v1"

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
        _require_schema(self.schema_version, "v2.opportunity.validation_robustness_report.v1")
        _require_identity(self.report_id, "report_id")
        _require_identity(self.population_id, "population_id")
        _require_hash(self.population_content_hash_sha256, "population hash")
        _require_identity(self.policy_id, "policy_id")
        _require_hash(self.policy_content_hash_sha256, "policy hash")
        _require_utc(self.recorded_at, "recorded_at")
        if (
            self.population_id != self.population.population_id
            or self.population_content_hash_sha256
            != _confirmatory_population_content_hash(self.population)
        ):
            raise ValueError("robustness population binding does not match content")
        if (
            self.policy_id != self.policy.policy_id
            or self.policy_content_hash_sha256 != self.policy.content_hash()
        ):
            raise ValueError("robustness policy binding does not match content")
        _validate_chronology(self.population, self.policy, self.recorded_at)
        with _metric_decimal_context(self.policy):
            expected_interval, expected_checks, expected_verdict, expected_limits = _derive_report(
                self.population,
                self.policy,
                self.control_arms,
                self.regime_evidence,
                self.complexity_evidence,
                self.future_data_sentinel,
            )
        if self.confidence_interval != expected_interval:
            raise ValueError("robustness confidence interval does not recompute")
        if self.checks != expected_checks:
            raise ValueError("robustness checks do not recompute")
        if self.verdict is not expected_verdict:
            raise ValueError("robustness verdict does not recompute")
        if self.limitations != expected_limits:
            raise ValueError("robustness limitations do not recompute")
        if self.lifecycle_mutation_count != 0 or self.take_authorization:
            raise ValueError("robustness report cannot mutate or authorize lifecycle state")
        if (
            not self.no_control_veto_is_non_promotional
            or not self.research_only
            or self.promotion_eligible
        ):
            raise ValueError("robustness report must remain explicitly non-promotional")
        expected_id = stable_identity(
            "validation-robustness-report", _identity_payload(self, "report_id")
        )
        if self.report_id != expected_id:
            raise ValueError("robustness report identity does not match content")


def build_validation_robustness_report(
    population: ConfirmatoryPopulation,
    *,
    policy: RobustnessCalibrationPolicy,
    recorded_at: datetime,
    control_arms: tuple[CausalControlArm, ...],
    regime_evidence: RegimeStabilityEvidence | None,
    complexity_evidence: ComplexityEvidence | None,
    future_data_sentinel: FutureDataSentinelEvidence | None,
) -> ValidationRobustnessReport:
    _validate_chronology(population, policy, recorded_at)
    arms = tuple(
        sorted(
            control_arms,
            key=lambda item: (
                item.kind.value,
                item.control_name,
                item.parameter_name or "",
                str(item.parameter_value) if item.parameter_value is not None else "",
            ),
        )
    )
    with _metric_decimal_context(policy):
        interval, checks, verdict, limitations = _derive_report(
            population,
            policy,
            arms,
            regime_evidence,
            complexity_evidence,
            future_data_sentinel,
        )
        values = {
            "population_id": population.population_id,
            "population_content_hash_sha256": _confirmatory_population_content_hash(population),
            "population": population,
            "policy_id": policy.policy_id,
            "policy_content_hash_sha256": policy.content_hash(),
            "policy": policy,
            "recorded_at": recorded_at,
            "confidence_interval": interval,
            "control_arms": arms,
            "regime_evidence": regime_evidence,
            "complexity_evidence": complexity_evidence,
            "future_data_sentinel": future_data_sentinel,
            "checks": checks,
            "verdict": verdict,
            "limitations": limitations,
            "lifecycle_mutation_count": 0,
            "take_authorization": False,
            "no_control_veto_is_non_promotional": True,
            "research_only": True,
            "promotion_eligible": False,
            "schema_version": "v2.opportunity.validation_robustness_report.v1",
        }
        return ValidationRobustnessReport(
            report_id=stable_identity("validation-robustness-report", values),
            **values,  # type: ignore[arg-type]
        )


def _derive_report(
    population: ConfirmatoryPopulation,
    policy: RobustnessCalibrationPolicy,
    arms: tuple[CausalControlArm, ...],
    regime: RegimeStabilityEvidence | None,
    complexity: ComplexityEvidence | None,
    sentinel: FutureDataSentinelEvidence | None,
) -> tuple[
    SessionClusterConfidenceInterval,
    tuple[RobustnessCheck, ...],
    RobustnessVerdict,
    tuple[str, ...],
]:
    canonical = tuple(
        sorted(
            arms,
            key=lambda item: (
                item.kind.value,
                item.control_name,
                item.parameter_name or "",
                str(item.parameter_value) if item.parameter_value is not None else "",
            ),
        )
    )
    if arms != canonical:
        raise ValueError("control arms must use canonical order")
    _require_unique([item.arm_id for item in arms], "robustness control arm")
    for arm in arms:
        _validate_arm_binding(population, arm)
    _validate_optional_bindings(population, regime, complexity, sentinel)
    interval = build_session_clustered_confidence_interval(
        tuple((item.session_source_id, item.cost_3x_session_r) for item in population.observations),
        policy=policy,
    )
    checks = (
        _pass_check(
            RobustnessCheckKind.CALIBRATION_PROVENANCE,
            "calibration policy predates the exact confirmatory population",
            (policy.policy_id,),
        ),
        _sample_check(population, policy),
        _confidence_check(interval, policy),
        _parameter_check(population, policy, arms),
        _negative_control_check(policy, arms),
        _regime_check(policy, regime),
        _baseline_check(population, policy, arms),
        _trial_check(policy),
        _complexity_check(policy, complexity),
        _sentinel_check(sentinel),
    )
    verdict = _verdict(checks)
    limitations = tuple(
        dict.fromkeys(
            (
                "NO_CONTROL_VETO_is_not_validation_approval_profitability_or_promotion",
                "locked_OOS_was_not_used_for_calibration_tuning_or_repair",
                *policy.limitations,
                *(item for arm in arms for item in arm.limitations),
            )
        )
    )
    return interval, checks, verdict, limitations


def _sample_check(
    population: ConfirmatoryPopulation, policy: RobustnessCalibrationPolicy
) -> RobustnessCheck:
    status = (
        RobustnessCheckStatus.PASS
        if population.session_count >= policy.minimum_sessions
        else RobustnessCheckStatus.FAIL
    )
    return RobustnessCheck(
        kind=RobustnessCheckKind.MINIMUM_SAMPLE,
        status=status,
        observed_decimal=None,
        threshold_decimal=None,
        observed_count=population.session_count,
        threshold_count=policy.minimum_sessions,
        source_evidence_ids=(population.population_id,),
        reason=(
            "minimum session gate passed"
            if status is RobustnessCheckStatus.PASS
            else "minimum session gate failed"
        ),
    )


def _confidence_check(
    interval: SessionClusterConfidenceInterval, policy: RobustnessCalibrationPolicy
) -> RobustnessCheck:
    passed = interval.lower_bound_r >= policy.minimum_confidence_lower_bound_r
    return RobustnessCheck(
        kind=RobustnessCheckKind.CONFIDENCE_INTERVAL,
        status=RobustnessCheckStatus.PASS if passed else RobustnessCheckStatus.FAIL,
        observed_decimal=interval.lower_bound_r,
        threshold_decimal=policy.minimum_confidence_lower_bound_r,
        observed_count=None,
        threshold_count=None,
        source_evidence_ids=(interval.interval_id,),
        reason="confidence lower bound passed" if passed else "confidence lower bound failed",
    )


def _parameter_check(
    population: ConfirmatoryPopulation,
    policy: RobustnessCalibrationPolicy,
    arms: tuple[CausalControlArm, ...],
) -> RobustnessCheck:
    parameter_arms = tuple(
        item for item in arms if item.kind is RobustnessArmKind.PARAMETER_PERTURBATION
    )
    if not policy.perturbations:
        disclosed = (
            len(parameter_arms) == 1
            and parameter_arms[0].status is RobustnessEvidenceStatus.NOT_APPLICABLE
            and parameter_arms[0].control_name == "parameter_free"
        )
        return RobustnessCheck(
            kind=RobustnessCheckKind.PARAMETER_PLATEAU,
            status=(
                RobustnessCheckStatus.NOT_APPLICABLE if disclosed else RobustnessCheckStatus.MISSING
            ),
            observed_decimal=None,
            threshold_decimal=policy.maximum_parameter_degradation_r,
            observed_count=len(parameter_arms),
            threshold_count=1,
            source_evidence_ids=tuple(item.arm_id for item in parameter_arms),
            reason=(
                "parameter-free unit explicitly disclosed"
                if disclosed
                else "parameter-free non-applicability evidence is missing"
            ),
        )
    required = {
        (spec.parameter_name, value)
        for spec in policy.perturbations
        for value in (spec.lower_value, spec.upper_value)
    }
    available = {
        (item.parameter_name, item.parameter_value): item
        for item in parameter_arms
        if item.status is RobustnessEvidenceStatus.AVAILABLE
    }
    if set(available) != required or len(parameter_arms) != len(required):
        return RobustnessCheck(
            kind=RobustnessCheckKind.PARAMETER_PLATEAU,
            status=RobustnessCheckStatus.MISSING,
            observed_decimal=None,
            threshold_decimal=policy.maximum_parameter_degradation_r,
            observed_count=len(available),
            threshold_count=len(required),
            source_evidence_ids=tuple(item.arm_id for item in parameter_arms),
            reason="required causal perturbation evidence is missing",
        )
    base_mean = _population_mean(population, policy)
    degradation = max(base_mean - _required_mean(item) for item in available.values())
    degradation = _quantize_metric_decimal(degradation, policy)
    passed = degradation <= policy.maximum_parameter_degradation_r
    return RobustnessCheck(
        kind=RobustnessCheckKind.PARAMETER_PLATEAU,
        status=RobustnessCheckStatus.PASS if passed else RobustnessCheckStatus.FAIL,
        observed_decimal=degradation,
        threshold_decimal=policy.maximum_parameter_degradation_r,
        observed_count=len(available),
        threshold_count=len(required),
        source_evidence_ids=tuple(item.arm_id for item in parameter_arms),
        reason="parameter plateau passed" if passed else "parameter plateau is fragile",
    )


def _negative_control_check(
    policy: RobustnessCalibrationPolicy, arms: tuple[CausalControlArm, ...]
) -> RobustnessCheck:
    candidates = tuple(item for item in arms if item.kind is RobustnessArmKind.NEGATIVE_CONTROL)
    available = tuple(
        item for item in candidates if item.status is RobustnessEvidenceStatus.AVAILABLE
    )
    if {item.control_name for item in available} != set(
        policy.required_negative_control_names
    ) or len(candidates) != len(policy.required_negative_control_names):
        return _missing_check(
            RobustnessCheckKind.NEGATIVE_CONTROL,
            "required causal negative control evidence is missing",
            tuple(item.arm_id for item in candidates),
        )
    observed = max(_required_mean(item) for item in available)
    passed = observed <= policy.maximum_negative_control_mean_r
    return RobustnessCheck(
        kind=RobustnessCheckKind.NEGATIVE_CONTROL,
        status=RobustnessCheckStatus.PASS if passed else RobustnessCheckStatus.FAIL,
        observed_decimal=observed,
        threshold_decimal=policy.maximum_negative_control_mean_r,
        observed_count=len(available),
        threshold_count=1,
        source_evidence_ids=tuple(item.arm_id for item in available),
        reason="negative control passed" if passed else "negative control is adverse",
    )


def _regime_check(
    policy: RobustnessCalibrationPolicy, regime: RegimeStabilityEvidence | None
) -> RobustnessCheck:
    if regime is None:
        return _missing_check(
            RobustnessCheckKind.REGIME_STABILITY,
            "required regime stability evidence is missing",
            (),
        )
    summaries = {item.bucket: item for item in regime.summaries}
    if set(summaries) != set(policy.required_regime_buckets):
        return _missing_check(
            RobustnessCheckKind.REGIME_STABILITY,
            "predeclared regime bucket evidence is incomplete",
            (regime.evidence_id,),
        )
    minimum = min(len(item.session_ids) for item in summaries.values())
    means = tuple(item.mean_session_r for item in summaries.values())
    spread = _quantize_metric_decimal(max(means) - min(means), policy)
    passed = (
        minimum >= policy.minimum_regime_sessions and spread <= policy.maximum_regime_mean_spread_r
    )
    return RobustnessCheck(
        kind=RobustnessCheckKind.REGIME_STABILITY,
        status=RobustnessCheckStatus.PASS if passed else RobustnessCheckStatus.FAIL,
        observed_decimal=spread,
        threshold_decimal=policy.maximum_regime_mean_spread_r,
        observed_count=minimum,
        threshold_count=policy.minimum_regime_sessions,
        source_evidence_ids=(regime.evidence_id,),
        reason="regime stability passed" if passed else "regime stability failed",
    )


def _baseline_check(
    population: ConfirmatoryPopulation,
    policy: RobustnessCalibrationPolicy,
    arms: tuple[CausalControlArm, ...],
) -> RobustnessCheck:
    candidates = tuple(item for item in arms if item.kind is RobustnessArmKind.SIMPLE_BASELINE)
    available = tuple(
        item for item in candidates if item.status is RobustnessEvidenceStatus.AVAILABLE
    )
    if (
        len(available) != 1
        or len(candidates) != 1
        or available[0].control_name != policy.simple_baseline_name
    ):
        return _missing_check(
            RobustnessCheckKind.SIMPLE_BASELINE,
            "one exact-population deterministic baseline is required",
            tuple(item.arm_id for item in candidates),
        )
    excess = _quantize_metric_decimal(
        _population_mean(population, policy) - _required_mean(available[0]), policy
    )
    passed = excess >= policy.minimum_baseline_excess_r
    return RobustnessCheck(
        kind=RobustnessCheckKind.SIMPLE_BASELINE,
        status=RobustnessCheckStatus.PASS if passed else RobustnessCheckStatus.FAIL,
        observed_decimal=excess,
        threshold_decimal=policy.minimum_baseline_excess_r,
        observed_count=1,
        threshold_count=1,
        source_evidence_ids=(available[0].arm_id,),
        reason="simple baseline comparison passed"
        if passed
        else "simple baseline comparison failed",
    )


def _trial_check(policy: RobustnessCalibrationPolicy) -> RobustnessCheck:
    passed = policy.trial_count <= policy.trial_limit
    return RobustnessCheck(
        kind=RobustnessCheckKind.TRIAL_LIMIT,
        status=RobustnessCheckStatus.PASS if passed else RobustnessCheckStatus.FAIL,
        observed_decimal=None,
        threshold_decimal=None,
        observed_count=policy.trial_count,
        threshold_count=policy.trial_limit,
        source_evidence_ids=(policy.policy_id,),
        reason="trial limit passed" if passed else "trial limit exceeded",
    )


def _complexity_check(
    policy: RobustnessCalibrationPolicy, complexity: ComplexityEvidence | None
) -> RobustnessCheck:
    if complexity is None:
        return _missing_check(
            RobustnessCheckKind.COMPLEXITY,
            "required complexity evidence is missing",
            (),
        )
    excess = max(
        complexity.feature_count - policy.maximum_feature_count,
        complexity.parameter_count - policy.maximum_parameter_count,
        complexity.rule_count - policy.maximum_rule_count,
    )
    passed = excess <= 0
    return RobustnessCheck(
        kind=RobustnessCheckKind.COMPLEXITY,
        status=RobustnessCheckStatus.PASS if passed else RobustnessCheckStatus.FAIL,
        observed_decimal=None,
        threshold_decimal=None,
        observed_count=max(
            complexity.feature_count,
            complexity.parameter_count,
            complexity.rule_count,
        ),
        threshold_count=max(
            policy.maximum_feature_count,
            policy.maximum_parameter_count,
            policy.maximum_rule_count,
        ),
        source_evidence_ids=(complexity.evidence_id,),
        reason="complexity limits passed" if passed else "complexity limit exceeded",
    )


def _sentinel_check(
    sentinel: FutureDataSentinelEvidence | None,
) -> RobustnessCheck:
    if sentinel is None:
        return _missing_check(
            RobustnessCheckKind.FUTURE_DATA_SENTINEL,
            "required future-data sentinel evidence is missing",
            (),
        )
    return RobustnessCheck(
        kind=RobustnessCheckKind.FUTURE_DATA_SENTINEL,
        status=(
            RobustnessCheckStatus.FAIL if sentinel.leakage_detected else RobustnessCheckStatus.PASS
        ),
        observed_decimal=None,
        threshold_decimal=None,
        observed_count=sentinel.statistical_observation_count,
        threshold_count=0,
        source_evidence_ids=(sentinel.evidence_id,),
        reason=(
            "future-data mutation changed decision-time evidence"
            if sentinel.leakage_detected
            else "future-data sentinel passed without statistical observations"
        ),
    )


def _verdict(checks: tuple[RobustnessCheck, ...]) -> RobustnessVerdict:
    if any(item.status is RobustnessCheckStatus.MISSING for item in checks):
        return RobustnessVerdict.VETO_MISSING_EVIDENCE
    mapping = {
        RobustnessCheckKind.CALIBRATION_PROVENANCE: RobustnessVerdict.VETO_POLICY_PROVENANCE,
        RobustnessCheckKind.MINIMUM_SAMPLE: RobustnessVerdict.VETO_INSUFFICIENT_SAMPLE,
        RobustnessCheckKind.CONFIDENCE_INTERVAL: RobustnessVerdict.VETO_CONFIDENCE_INTERVAL,
        RobustnessCheckKind.PARAMETER_PLATEAU: RobustnessVerdict.VETO_PARAMETER_FRAGILITY,
        RobustnessCheckKind.NEGATIVE_CONTROL: RobustnessVerdict.VETO_NEGATIVE_CONTROL,
        RobustnessCheckKind.REGIME_STABILITY: RobustnessVerdict.VETO_REGIME_INSTABILITY,
        RobustnessCheckKind.SIMPLE_BASELINE: RobustnessVerdict.VETO_BASELINE,
        RobustnessCheckKind.TRIAL_LIMIT: RobustnessVerdict.VETO_TRIAL_LIMIT,
        RobustnessCheckKind.COMPLEXITY: RobustnessVerdict.VETO_COMPLEXITY,
        RobustnessCheckKind.FUTURE_DATA_SENTINEL: RobustnessVerdict.VETO_LEAKAGE,
    }
    for check in checks:
        if check.status is RobustnessCheckStatus.FAIL:
            return mapping[check.kind]
    return RobustnessVerdict.NO_CONTROL_VETO


def _validate_chronology(
    population: ConfirmatoryPopulation,
    policy: RobustnessCalibrationPolicy,
    recorded_at: datetime,
) -> None:
    _require_utc(recorded_at, "recorded_at")
    if (
        policy.confirmatory_unit_id != population.unit_id
        or policy.confirmatory_unit_content_hash_sha256 != population.unit_content_hash_sha256
        or policy.confirmatory_unit != population.unit
    ):
        raise ValueError("calibration policy does not bind the exact confirmatory unit")
    if policy.declared_at >= population.first_confirmatory_at:
        raise ValueError("calibration policy must predate the first confirmatory session")
    if recorded_at < population.metric_report.recorded_at:
        raise ValueError("robustness report predates its accepted metric report")


def _validate_arm_binding(population: ConfirmatoryPopulation, arm: CausalControlArm) -> None:
    if (
        arm.population_id != population.population_id
        or arm.population_content_hash_sha256 != _confirmatory_population_content_hash(population)
    ):
        raise ValueError("control arm population binding does not match content")
    if arm.status is RobustnessEvidenceStatus.AVAILABLE:
        _require_exact_population(population, arm.observations)


def _validate_optional_bindings(
    population: ConfirmatoryPopulation,
    regime: RegimeStabilityEvidence | None,
    complexity: ComplexityEvidence | None,
    sentinel: FutureDataSentinelEvidence | None,
) -> None:
    for evidence in (regime, complexity, sentinel):
        if evidence is not None and (
            evidence.population_id != population.population_id
            or evidence.population_content_hash_sha256
            != _confirmatory_population_content_hash(population)
        ):
            raise ValueError("robustness evidence population binding does not match content")
    if regime is not None:
        expected = tuple(
            (
                item.session_source_id,
                item.session_content_hash_sha256,
                item.source_row_ids,
                item.source_row_content_hashes,
                item.cost_3x_session_r,
            )
            for item in population.observations
        )
        actual = tuple(
            zip(
                regime.session_ids,
                regime.session_content_hashes,
                regime.source_row_ids,
                regime.source_row_content_hashes,
                regime.session_values_r,
                strict=True,
            )
        )
        if actual != expected:
            raise ValueError("regime evidence must preserve the exact population")
    if sentinel is not None:
        row_ids = tuple(
            row_id for item in population.observations for row_id in item.source_row_ids
        )
        row_hashes = tuple(
            digest for item in population.observations for digest in item.source_row_content_hashes
        )
        report_rows = {item.row_id: item for item in population.metric_report.bound_rows}
        originals = tuple(report_rows[row_id].outcome.decision.content_hash() for row_id in row_ids)
        if (
            sentinel.source_row_ids != row_ids
            or sentinel.source_row_content_hashes != row_hashes
            or sentinel.original_decision_hashes != originals
        ):
            raise ValueError("future sentinel must preserve the exact population")


def _population_mean(
    population: ConfirmatoryPopulation, policy: RobustnessCalibrationPolicy
) -> Decimal:
    interval = build_session_clustered_confidence_interval(
        tuple((item.session_source_id, item.cost_3x_session_r) for item in population.observations),
        policy=policy,
    )
    return interval.mean_session_r


def _required_mean(arm: CausalControlArm) -> Decimal:
    if arm.mean_session_r is None:
        raise ValueError("available control arm lacks a mean")
    return arm.mean_session_r


def _pass_check(
    kind: RobustnessCheckKind, reason: str, sources: tuple[str, ...]
) -> RobustnessCheck:
    return RobustnessCheck(
        kind=kind,
        status=RobustnessCheckStatus.PASS,
        observed_decimal=None,
        threshold_decimal=None,
        observed_count=None,
        threshold_count=None,
        source_evidence_ids=sources,
        reason=reason,
    )


def _missing_check(
    kind: RobustnessCheckKind, reason: str, sources: tuple[str, ...]
) -> RobustnessCheck:
    return RobustnessCheck(
        kind=kind,
        status=RobustnessCheckStatus.MISSING,
        observed_decimal=None,
        threshold_decimal=None,
        observed_count=0,
        threshold_count=1,
        source_evidence_ids=sources,
        reason=reason,
    )


__all__ = [
    "RobustnessCheck",
    "ValidationRobustnessReport",
    "build_validation_robustness_report",
]
