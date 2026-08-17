"""Strict downstream contracts for WP005-C robustness evidence.

The contracts in this module are research-only.  They deliberately have no
package-root export and are not part of the real-time opportunity graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from intraday_scanner.v2.opportunity.models import (
    OpportunityContract,
    StrategyDirection,
    stable_identity,
)
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
    METRIC_DECIMAL_PRECISION,
    METRIC_DECIMAL_SCALE,
)


class CalibrationRegionKind(str, Enum):
    TRAIN_RESEARCH = "train_research"
    PRE_CONFIRMATORY_VALIDATION = "pre_confirmatory_validation"


class RobustnessArmKind(str, Enum):
    PARAMETER_PERTURBATION = "parameter_perturbation"
    NEGATIVE_CONTROL = "negative_control"
    SIMPLE_BASELINE = "simple_baseline"


class RobustnessEvidenceStatus(str, Enum):
    AVAILABLE = "available"
    NOT_APPLICABLE = "not_applicable"
    MISSING = "missing"


class RobustnessCheckKind(str, Enum):
    CALIBRATION_PROVENANCE = "calibration_provenance"
    MINIMUM_SAMPLE = "minimum_sample"
    CONFIDENCE_INTERVAL = "confidence_interval"
    PARAMETER_PLATEAU = "parameter_plateau"
    NEGATIVE_CONTROL = "negative_control"
    REGIME_STABILITY = "regime_stability"
    SIMPLE_BASELINE = "simple_baseline"
    TRIAL_LIMIT = "trial_limit"
    COMPLEXITY = "complexity"
    FUTURE_DATA_SENTINEL = "future_data_sentinel"


class RobustnessCheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


class RobustnessVerdict(str, Enum):
    VETO_MISSING_EVIDENCE = "VETO_MISSING_EVIDENCE"
    VETO_POLICY_PROVENANCE = "VETO_POLICY_PROVENANCE"
    VETO_INSUFFICIENT_SAMPLE = "VETO_INSUFFICIENT_SAMPLE"
    VETO_CONFIDENCE_INTERVAL = "VETO_CONFIDENCE_INTERVAL"
    VETO_PARAMETER_FRAGILITY = "VETO_PARAMETER_FRAGILITY"
    VETO_NEGATIVE_CONTROL = "VETO_NEGATIVE_CONTROL"
    VETO_REGIME_INSTABILITY = "VETO_REGIME_INSTABILITY"
    VETO_BASELINE = "VETO_BASELINE"
    VETO_TRIAL_LIMIT = "VETO_TRIAL_LIMIT"
    VETO_COMPLEXITY = "VETO_COMPLEXITY"
    VETO_LEAKAGE = "VETO_LEAKAGE"
    NO_CONTROL_VETO = "NO_CONTROL_VETO"


@dataclass(frozen=True)
class ConfirmatoryUnit(OutcomeContract):
    unit_id: str
    strategy_id: str
    strategy_version: str
    direction: StrategyDirection
    schema_version: str = "v2.opportunity.confirmatory_unit.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.confirmatory_unit.v1")
        _require_identity(self.unit_id, "unit_id")
        _require_identity(self.strategy_id, "strategy_id")
        _require_sanitized_text(self.strategy_version, "strategy_version")
        expected = stable_identity("confirmatory-unit", _identity_payload(self, "unit_id"))
        if self.unit_id != expected:
            raise ValueError("confirmatory unit identity does not match content")


def build_confirmatory_unit(
    *, strategy_id: str, strategy_version: str, direction: StrategyDirection
) -> ConfirmatoryUnit:
    values = {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "direction": direction,
        "schema_version": "v2.opportunity.confirmatory_unit.v1",
    }
    return ConfirmatoryUnit(
        unit_id=stable_identity("confirmatory-unit", values),
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        direction=direction,
    )


@dataclass(frozen=True)
class CalibrationSourceArtifact(OutcomeContract):
    artifact_id: str
    content_hash_sha256: str
    source_identity: str
    source_version: str
    region_id: str
    region_kind: CalibrationRegionKind
    observed_at: datetime
    available_at: datetime
    limitations: tuple[str, ...]
    schema_version: str = "v2.opportunity.robustness_calibration_source.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.robustness_calibration_source.v1")
        _require_identity(self.artifact_id, "artifact_id")
        _require_hash(self.content_hash_sha256, "calibration source content hash")
        for value, name in (
            (self.source_identity, "source_identity"),
            (self.source_version, "source_version"),
            (self.region_id, "region_id"),
        ):
            _require_sanitized_text(value, name)
        _require_utc(self.observed_at, "observed_at")
        _require_utc(self.available_at, "available_at")
        if self.available_at < self.observed_at:
            raise ValueError("calibration source cannot be available before observation")
        _validate_limitations(self.limitations, "calibration source limitation")
        expected = stable_identity(
            "robustness-calibration-source", _identity_payload(self, "artifact_id")
        )
        if self.artifact_id != expected:
            raise ValueError("calibration source identity does not match content")


def build_calibration_source_artifact(
    *,
    content_hash_sha256: str,
    source_identity: str,
    source_version: str,
    region_id: str,
    region_kind: CalibrationRegionKind,
    observed_at: datetime,
    available_at: datetime,
    limitations: tuple[str, ...] = (),
) -> CalibrationSourceArtifact:
    values = {
        "content_hash_sha256": content_hash_sha256,
        "source_identity": source_identity,
        "source_version": source_version,
        "region_id": region_id,
        "region_kind": region_kind,
        "observed_at": observed_at,
        "available_at": available_at,
        "limitations": limitations,
        "schema_version": "v2.opportunity.robustness_calibration_source.v1",
    }
    return CalibrationSourceArtifact(
        artifact_id=stable_identity("robustness-calibration-source", values),
        **values,  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class ParameterPerturbationSpec(OutcomeContract):
    parameter_name: str
    center_value: Decimal
    lower_value: Decimal
    upper_value: Decimal

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_identity(self.parameter_name, "parameter_name")
        for decimal_value, name in (
            (self.center_value, "center_value"),
            (self.lower_value, "lower_value"),
            (self.upper_value, "upper_value"),
        ):
            _require_finite_decimal(decimal_value, name)
        if not self.lower_value < self.center_value < self.upper_value:
            raise ValueError("perturbation values must straddle the center value")


@dataclass(frozen=True)
class RobustnessCalibrationPolicy(OutcomeContract):
    policy_id: str
    policy_version: str
    declared_at: datetime
    calibration_region_id: str
    calibration_region_kind: CalibrationRegionKind
    source_artifacts: tuple[CalibrationSourceArtifact, ...]
    confirmatory_unit_id: str
    confirmatory_unit_content_hash_sha256: str
    confirmatory_unit: ConfirmatoryUnit
    population_eligibility_rule: str
    trial_count: int
    trial_limit: int
    bootstrap_seed: int
    bootstrap_resamples: int
    confidence_lower_quantile: Decimal
    confidence_upper_quantile: Decimal
    minimum_sessions: int
    minimum_confidence_lower_bound_r: Decimal
    maximum_parameter_degradation_r: Decimal
    maximum_negative_control_mean_r: Decimal
    minimum_baseline_excess_r: Decimal
    minimum_regime_sessions: int
    maximum_regime_mean_spread_r: Decimal
    perturbations: tuple[ParameterPerturbationSpec, ...]
    required_negative_control_names: tuple[str, ...]
    simple_baseline_name: str
    required_regime_buckets: tuple[str, ...]
    maximum_feature_count: int
    maximum_parameter_count: int
    maximum_rule_count: int
    decimal_precision: int = METRIC_DECIMAL_PRECISION
    decimal_scale: int = METRIC_DECIMAL_SCALE
    rounding_mode: str = "ROUND_HALF_EVEN"
    limitations: tuple[str, ...] = ()
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.robustness_calibration_policy.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.robustness_calibration_policy.v1")
        _require_identity(self.policy_id, "policy_id")
        _require_sanitized_text(self.policy_version, "policy_version")
        _require_utc(self.declared_at, "declared_at")
        _require_sanitized_text(self.calibration_region_id, "calibration_region_id")
        _require_identity(self.confirmatory_unit_id, "confirmatory_unit_id")
        _require_hash(
            self.confirmatory_unit_content_hash_sha256,
            "confirmatory unit content hash",
        )
        if (
            self.confirmatory_unit_id != self.confirmatory_unit.unit_id
            or self.confirmatory_unit_content_hash_sha256 != self.confirmatory_unit.content_hash()
        ):
            raise ValueError("calibration confirmatory unit binding does not match content")
        if (
            self.population_eligibility_rule
            != "accepted_final_validation_all_sessions_exact_unit_all_rows_cost_3x_complete"
        ):
            raise ValueError("population eligibility rule must be the predeclared v1 rule")
        if not self.source_artifacts:
            raise ValueError("calibration policy requires source artifacts")
        _require_unique(
            [item.artifact_id for item in self.source_artifacts],
            "calibration source artifact",
        )
        if self.source_artifacts != tuple(
            sorted(self.source_artifacts, key=lambda item: item.artifact_id)
        ):
            raise ValueError("calibration source artifacts must use canonical order")
        for artifact in self.source_artifacts:
            if (
                artifact.region_id != self.calibration_region_id
                or artifact.region_kind is not self.calibration_region_kind
            ):
                raise ValueError("calibration source region does not match policy")
            if artifact.available_at > self.declared_at:
                raise ValueError("calibration source was not available before declaration")
        for integer_value, name in (
            (self.trial_count, "trial_count"),
            (self.trial_limit, "trial_limit"),
            (self.bootstrap_resamples, "bootstrap_resamples"),
            (self.minimum_sessions, "minimum_sessions"),
            (self.minimum_regime_sessions, "minimum_regime_sessions"),
            (self.maximum_feature_count, "maximum_feature_count"),
            (self.maximum_parameter_count, "maximum_parameter_count"),
            (self.maximum_rule_count, "maximum_rule_count"),
        ):
            _require_positive_int(integer_value, name)
        if type(self.bootstrap_seed) is not int or self.bootstrap_seed < 0:
            raise ValueError("bootstrap_seed must be a nonnegative integer")
        if self.bootstrap_resamples < 20:
            raise ValueError("bootstrap_resamples must be at least 20")
        for decimal_value, name in (
            (self.confidence_lower_quantile, "confidence_lower_quantile"),
            (self.confidence_upper_quantile, "confidence_upper_quantile"),
            (self.minimum_confidence_lower_bound_r, "minimum_confidence_lower_bound_r"),
            (self.maximum_parameter_degradation_r, "maximum_parameter_degradation_r"),
            (self.maximum_negative_control_mean_r, "maximum_negative_control_mean_r"),
            (self.minimum_baseline_excess_r, "minimum_baseline_excess_r"),
            (self.maximum_regime_mean_spread_r, "maximum_regime_mean_spread_r"),
        ):
            _require_finite_decimal(decimal_value, name)
        if not (
            Decimal("0")
            < self.confidence_lower_quantile
            < self.confidence_upper_quantile
            < Decimal("1")
        ):
            raise ValueError("confidence quantiles are invalid")
        if self.maximum_parameter_degradation_r < 0 or self.maximum_regime_mean_spread_r < 0:
            raise ValueError("stability tolerances must be nonnegative")
        if self.perturbations != tuple(
            sorted(self.perturbations, key=lambda item: item.parameter_name)
        ):
            raise ValueError("perturbation specifications must use canonical order")
        _require_unique(
            [item.parameter_name for item in self.perturbations],
            "perturbation parameter",
        )
        if self.required_negative_control_names != tuple(
            sorted(self.required_negative_control_names)
        ):
            raise ValueError("negative controls must use canonical order")
        if not self.required_negative_control_names:
            raise ValueError("at least one predeclared negative control is required")
        _require_unique(list(self.required_negative_control_names), "required negative control")
        for control_name in self.required_negative_control_names:
            _require_identity(control_name, "required negative control")
        _require_identity(self.simple_baseline_name, "simple_baseline_name")
        if self.required_regime_buckets != tuple(sorted(self.required_regime_buckets)):
            raise ValueError("required regime buckets must use canonical order")
        if not self.required_regime_buckets:
            raise ValueError("at least one predeclared regime bucket is required")
        _require_unique(list(self.required_regime_buckets), "required regime bucket")
        for bucket in self.required_regime_buckets:
            _require_identity(bucket, "required regime bucket")
        if (
            self.decimal_precision != METRIC_DECIMAL_PRECISION
            or self.decimal_scale != METRIC_DECIMAL_SCALE
            or self.rounding_mode != "ROUND_HALF_EVEN"
        ):
            raise ValueError("robustness Decimal policy must use the accepted context")
        _validate_limitations(self.limitations, "calibration policy limitation")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("robustness policy must remain research-only")
        expected = stable_identity(
            "robustness-calibration-policy", _identity_payload(self, "policy_id")
        )
        if self.policy_id != expected:
            raise ValueError("robustness policy identity does not match content")


def build_robustness_calibration_policy(
    *,
    policy_version: str,
    declared_at: datetime,
    calibration_region_id: str,
    calibration_region_kind: CalibrationRegionKind,
    source_artifacts: tuple[CalibrationSourceArtifact, ...],
    confirmatory_unit: ConfirmatoryUnit,
    trial_count: int,
    trial_limit: int,
    bootstrap_seed: int,
    bootstrap_resamples: int,
    confidence_lower_quantile: Decimal,
    confidence_upper_quantile: Decimal,
    minimum_sessions: int,
    minimum_confidence_lower_bound_r: Decimal,
    maximum_parameter_degradation_r: Decimal,
    maximum_negative_control_mean_r: Decimal,
    minimum_baseline_excess_r: Decimal,
    minimum_regime_sessions: int,
    maximum_regime_mean_spread_r: Decimal,
    perturbations: tuple[ParameterPerturbationSpec, ...],
    required_negative_control_names: tuple[str, ...],
    simple_baseline_name: str,
    required_regime_buckets: tuple[str, ...],
    maximum_feature_count: int,
    maximum_parameter_count: int,
    maximum_rule_count: int,
    limitations: tuple[str, ...] = (),
) -> RobustnessCalibrationPolicy:
    artifacts = tuple(sorted(source_artifacts, key=lambda item: item.artifact_id))
    specs = tuple(sorted(perturbations, key=lambda item: item.parameter_name))
    negative_controls = tuple(sorted(required_negative_control_names))
    buckets = tuple(sorted(required_regime_buckets))
    values = {
        "policy_version": policy_version,
        "declared_at": declared_at,
        "calibration_region_id": calibration_region_id,
        "calibration_region_kind": calibration_region_kind,
        "source_artifacts": artifacts,
        "confirmatory_unit_id": confirmatory_unit.unit_id,
        "confirmatory_unit_content_hash_sha256": confirmatory_unit.content_hash(),
        "confirmatory_unit": confirmatory_unit,
        "population_eligibility_rule": (
            "accepted_final_validation_all_sessions_exact_unit_all_rows_cost_3x_complete"
        ),
        "trial_count": trial_count,
        "trial_limit": trial_limit,
        "bootstrap_seed": bootstrap_seed,
        "bootstrap_resamples": bootstrap_resamples,
        "confidence_lower_quantile": confidence_lower_quantile,
        "confidence_upper_quantile": confidence_upper_quantile,
        "minimum_sessions": minimum_sessions,
        "minimum_confidence_lower_bound_r": minimum_confidence_lower_bound_r,
        "maximum_parameter_degradation_r": maximum_parameter_degradation_r,
        "maximum_negative_control_mean_r": maximum_negative_control_mean_r,
        "minimum_baseline_excess_r": minimum_baseline_excess_r,
        "minimum_regime_sessions": minimum_regime_sessions,
        "maximum_regime_mean_spread_r": maximum_regime_mean_spread_r,
        "perturbations": specs,
        "required_negative_control_names": negative_controls,
        "simple_baseline_name": simple_baseline_name,
        "required_regime_buckets": buckets,
        "maximum_feature_count": maximum_feature_count,
        "maximum_parameter_count": maximum_parameter_count,
        "maximum_rule_count": maximum_rule_count,
        "decimal_precision": METRIC_DECIMAL_PRECISION,
        "decimal_scale": METRIC_DECIMAL_SCALE,
        "rounding_mode": "ROUND_HALF_EVEN",
        "limitations": limitations,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.robustness_calibration_policy.v1",
    }
    return RobustnessCalibrationPolicy(
        policy_id=stable_identity("robustness-calibration-policy", values),
        **values,  # type: ignore[arg-type]
    )


def _require_finite_decimal(value: Decimal, name: str) -> None:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")


def _require_positive_int(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _validate_limitations(values: tuple[str, ...], name: str) -> None:
    _require_unique(list(values), name)
    for value in values:
        _require_sanitized_text(value, name)


__all__ = [
    "CalibrationRegionKind",
    "CalibrationSourceArtifact",
    "ConfirmatoryUnit",
    "ParameterPerturbationSpec",
    "RobustnessArmKind",
    "RobustnessCalibrationPolicy",
    "RobustnessCheckKind",
    "RobustnessCheckStatus",
    "RobustnessEvidenceStatus",
    "RobustnessVerdict",
    "build_calibration_source_artifact",
    "build_confirmatory_unit",
    "build_robustness_calibration_policy",
]
