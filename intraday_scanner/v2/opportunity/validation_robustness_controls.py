"""Exact-population control evidence for WP005-C."""

from __future__ import annotations

from dataclasses import dataclass
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
)
from intraday_scanner.v2.opportunity.validation_metric_math import (
    _decimal_mean,
    _metric_decimal_context,
    _quantize_metric_decimal,
)
from intraday_scanner.v2.opportunity.validation_robustness_contracts import (
    RobustnessArmKind,
    RobustnessEvidenceStatus,
)
from intraday_scanner.v2.opportunity.validation_robustness_population import (
    ConfirmatoryPopulation,
    _confirmatory_population_content_hash,
)

CANONICAL_PIPELINE_ENTRYPOINT = "intraday_scanner.v2.opportunity.pipeline.run_opportunity_pipeline"


@dataclass(frozen=True)
class CausalSessionObservation(OutcomeContract):
    session_source_id: str
    session_content_hash_sha256: str
    source_row_ids: tuple[str, ...]
    source_row_content_hashes: tuple[str, ...]
    output_artifact_ids: tuple[str, ...]
    output_artifact_content_hashes: tuple[str, ...]
    cost_3x_session_r: Decimal

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_identity(self.session_source_id, "session_source_id")
        _require_hash(self.session_content_hash_sha256, "session content hash")
        if not self.source_row_ids:
            raise ValueError("control observation cannot omit source rows")
        _require_unique(list(self.source_row_ids), "control source row")
        if len(self.source_row_ids) != len(self.source_row_content_hashes):
            raise ValueError("control source row identities and hashes must align")
        if not self.output_artifact_ids or len(self.output_artifact_ids) != len(
            self.output_artifact_content_hashes
        ):
            raise ValueError("control output artifact identities and hashes must align")
        _require_unique(list(self.output_artifact_ids), "control output artifact")
        for value in (*self.source_row_ids, *self.output_artifact_ids):
            _require_identity(value, "control lineage identity")
        for value in (
            *self.source_row_content_hashes,
            *self.output_artifact_content_hashes,
        ):
            _require_hash(value, "control lineage hash")
        if type(self.cost_3x_session_r) is not Decimal or not self.cost_3x_session_r.is_finite():
            raise ValueError("control COST_3X session R must be a finite Decimal")


@dataclass(frozen=True)
class CausalControlArm(OutcomeContract):
    arm_id: str
    population_id: str
    population_content_hash_sha256: str
    kind: RobustnessArmKind
    control_name: str
    status: RobustnessEvidenceStatus
    parameter_name: str | None
    parameter_value: Decimal | None
    pipeline_entrypoint: str | None
    causal_recomputation: bool
    source_outcome_identity_preserved: bool
    observations: tuple[CausalSessionObservation, ...]
    mean_session_r: Decimal | None
    reason: str | None
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.causal_robustness_control_arm.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version,
            "v2.opportunity.causal_robustness_control_arm.v1",
        )
        _require_identity(self.arm_id, "arm_id")
        _require_identity(self.population_id, "population_id")
        _require_hash(self.population_content_hash_sha256, "population hash")
        _require_identity(self.control_name, "control_name")
        if self.parameter_name is not None:
            _require_identity(self.parameter_name, "parameter_name")
        if self.parameter_value is not None and (
            type(self.parameter_value) is not Decimal or not self.parameter_value.is_finite()
        ):
            raise ValueError("parameter_value must be a finite Decimal")
        _validate_text_tuple(self.limitations, "control arm limitation")
        if self.status is RobustnessEvidenceStatus.AVAILABLE:
            if not self.observations or self.mean_session_r is None or self.reason is not None:
                raise ValueError("available control arm requires observations and mean")
            expected_mean = _mean(tuple(item.cost_3x_session_r for item in self.observations))
            if self.mean_session_r != expected_mean:
                raise ValueError("control arm mean does not recompute")
            if self.kind in {
                RobustnessArmKind.PARAMETER_PERTURBATION,
                RobustnessArmKind.NEGATIVE_CONTROL,
            }:
                if (
                    self.pipeline_entrypoint != CANONICAL_PIPELINE_ENTRYPOINT
                    or not self.causal_recomputation
                    or not self.source_outcome_identity_preserved
                ):
                    raise ValueError("causal arm must bind the accepted opportunity pipeline")
            else:
                if (
                    self.pipeline_entrypoint != "deterministic_baseline"
                    or self.causal_recomputation
                    or not self.source_outcome_identity_preserved
                ):
                    raise ValueError("simple baseline must be deterministic and population-bound")
        else:
            if self.observations or self.mean_session_r is not None:
                raise ValueError("unavailable control arm cannot carry observations")
            if self.reason is None:
                raise ValueError("unavailable control arm requires an exact reason")
            _require_sanitized_text(self.reason, "control arm reason")
            if self.pipeline_entrypoint is not None:
                raise ValueError("unavailable control arm cannot claim a pipeline run")
            if self.causal_recomputation or self.source_outcome_identity_preserved:
                raise ValueError("unavailable control arm cannot claim causal evidence")
        if self.kind is RobustnessArmKind.PARAMETER_PERTURBATION:
            if self.status is not RobustnessEvidenceStatus.NOT_APPLICABLE and (
                self.parameter_name is None or self.parameter_value is None
            ):
                raise ValueError("parameter perturbation arm requires name and value")
        elif self.parameter_name is not None or self.parameter_value is not None:
            raise ValueError("non-parameter arm cannot carry a parameter value")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("control arm must remain research-only")
        expected_id = stable_identity(
            "causal-robustness-control-arm", _identity_payload(self, "arm_id")
        )
        if self.arm_id != expected_id:
            raise ValueError("control arm identity does not match content")


def build_control_observations(
    population: ConfirmatoryPopulation,
    *,
    session_values_r: tuple[Decimal, ...],
    output_artifact_ids: tuple[tuple[str, ...], ...],
    output_artifact_content_hashes: tuple[tuple[str, ...], ...],
) -> tuple[CausalSessionObservation, ...]:
    if not (
        len(session_values_r)
        == len(output_artifact_ids)
        == len(output_artifact_content_hashes)
        == len(population.observations)
    ):
        raise ValueError("control observations must cover the exact population")
    return tuple(
        CausalSessionObservation(
            session_source_id=source.session_source_id,
            session_content_hash_sha256=source.session_content_hash_sha256,
            source_row_ids=source.source_row_ids,
            source_row_content_hashes=source.source_row_content_hashes,
            output_artifact_ids=artifact_ids,
            output_artifact_content_hashes=artifact_hashes,
            cost_3x_session_r=value,
        )
        for source, value, artifact_ids, artifact_hashes in zip(
            population.observations,
            session_values_r,
            output_artifact_ids,
            output_artifact_content_hashes,
            strict=True,
        )
    )


def build_causal_control_arm(
    population: ConfirmatoryPopulation,
    *,
    kind: RobustnessArmKind,
    control_name: str,
    observations: tuple[CausalSessionObservation, ...],
    parameter_name: str | None = None,
    parameter_value: Decimal | None = None,
    limitations: tuple[str, ...] = (),
) -> CausalControlArm:
    _require_exact_population(population, observations)
    is_baseline = kind is RobustnessArmKind.SIMPLE_BASELINE
    values = {
        "population_id": population.population_id,
        "population_content_hash_sha256": _confirmatory_population_content_hash(population),
        "kind": kind,
        "control_name": control_name,
        "status": RobustnessEvidenceStatus.AVAILABLE,
        "parameter_name": parameter_name,
        "parameter_value": parameter_value,
        "pipeline_entrypoint": (
            "deterministic_baseline" if is_baseline else CANONICAL_PIPELINE_ENTRYPOINT
        ),
        "causal_recomputation": not is_baseline,
        "source_outcome_identity_preserved": True,
        "observations": observations,
        "mean_session_r": _mean(tuple(item.cost_3x_session_r for item in observations)),
        "reason": None,
        "limitations": limitations,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.causal_robustness_control_arm.v1",
    }
    return CausalControlArm(
        arm_id=stable_identity("causal-robustness-control-arm", values),
        **values,  # type: ignore[arg-type]
    )


def build_unavailable_control_arm(
    population: ConfirmatoryPopulation,
    *,
    kind: RobustnessArmKind,
    control_name: str,
    status: RobustnessEvidenceStatus,
    reason: str,
    parameter_name: str | None = None,
    parameter_value: Decimal | None = None,
    limitations: tuple[str, ...] = (),
) -> CausalControlArm:
    if status is RobustnessEvidenceStatus.AVAILABLE:
        raise ValueError("unavailable arm builder cannot emit available evidence")
    values = {
        "population_id": population.population_id,
        "population_content_hash_sha256": _confirmatory_population_content_hash(population),
        "kind": kind,
        "control_name": control_name,
        "status": status,
        "parameter_name": parameter_name,
        "parameter_value": parameter_value,
        "pipeline_entrypoint": None,
        "causal_recomputation": False,
        "source_outcome_identity_preserved": False,
        "observations": (),
        "mean_session_r": None,
        "reason": reason,
        "limitations": limitations,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.causal_robustness_control_arm.v1",
    }
    return CausalControlArm(
        arm_id=stable_identity("causal-robustness-control-arm", values),
        **values,  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class RegimeBucketSummary(OutcomeContract):
    bucket: str
    session_ids: tuple[str, ...]
    mean_session_r: Decimal

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_identity(self.bucket, "regime bucket")
        if not self.session_ids:
            raise ValueError("regime bucket cannot be empty")
        _require_unique(list(self.session_ids), "regime session")
        for session_id in self.session_ids:
            _require_identity(session_id, "regime session")
        if type(self.mean_session_r) is not Decimal or not self.mean_session_r.is_finite():
            raise ValueError("regime mean must be a finite Decimal")


@dataclass(frozen=True)
class RegimeStabilityEvidence(OutcomeContract):
    evidence_id: str
    population_id: str
    population_content_hash_sha256: str
    session_ids: tuple[str, ...]
    session_content_hashes: tuple[str, ...]
    source_row_ids: tuple[tuple[str, ...], ...]
    source_row_content_hashes: tuple[tuple[str, ...], ...]
    session_values_r: tuple[Decimal, ...]
    bucket_assignments: tuple[str, ...]
    summaries: tuple[RegimeBucketSummary, ...]
    schema_version: str = "v2.opportunity.regime_stability_evidence.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.regime_stability_evidence.v1")
        _require_identity(self.evidence_id, "evidence_id")
        _require_identity(self.population_id, "population_id")
        _require_hash(self.population_content_hash_sha256, "population hash")
        size = len(self.session_ids)
        if not size or not all(
            len(values) == size
            for values in (
                self.session_content_hashes,
                self.source_row_ids,
                self.source_row_content_hashes,
                self.session_values_r,
                self.bucket_assignments,
            )
        ):
            raise ValueError("regime evidence must align to every population session")
        _require_unique(list(self.session_ids), "regime evidence session")
        for bucket in self.bucket_assignments:
            _require_identity(bucket, "regime bucket assignment")
        for value in self.session_values_r:
            if type(value) is not Decimal or not value.is_finite():
                raise ValueError("regime session value must be a finite Decimal")
        expected = _regime_summaries(
            self.session_ids,
            self.bucket_assignments,
            self.session_values_r,
        )
        if self.summaries != expected:
            raise ValueError("regime summaries do not recompute")
        expected_id = stable_identity(
            "regime-stability-evidence", _identity_payload(self, "evidence_id")
        )
        if self.evidence_id != expected_id:
            raise ValueError("regime evidence identity does not match content")


def build_regime_stability_evidence(
    population: ConfirmatoryPopulation,
    *,
    bucket_assignments: tuple[str, ...],
) -> RegimeStabilityEvidence:
    if len(bucket_assignments) != len(population.observations):
        raise ValueError("regime assignments must cover the exact population")
    session_ids = tuple(item.session_source_id for item in population.observations)
    values_r = tuple(item.cost_3x_session_r for item in population.observations)
    summaries = _regime_summaries(session_ids, bucket_assignments, values_r)
    values = {
        "population_id": population.population_id,
        "population_content_hash_sha256": _confirmatory_population_content_hash(population),
        "session_ids": session_ids,
        "session_content_hashes": tuple(
            item.session_content_hash_sha256 for item in population.observations
        ),
        "source_row_ids": tuple(item.source_row_ids for item in population.observations),
        "source_row_content_hashes": tuple(
            item.source_row_content_hashes for item in population.observations
        ),
        "session_values_r": values_r,
        "bucket_assignments": bucket_assignments,
        "summaries": summaries,
        "schema_version": "v2.opportunity.regime_stability_evidence.v1",
    }
    return RegimeStabilityEvidence(
        evidence_id=stable_identity("regime-stability-evidence", values),
        **values,  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class ComplexityEvidence(OutcomeContract):
    evidence_id: str
    population_id: str
    population_content_hash_sha256: str
    feature_names: tuple[str, ...]
    parameter_names: tuple[str, ...]
    rule_names: tuple[str, ...]
    feature_count: int
    parameter_count: int
    rule_count: int
    schema_version: str = "v2.opportunity.robustness_complexity_evidence.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.robustness_complexity_evidence.v1")
        _require_identity(self.evidence_id, "evidence_id")
        _require_identity(self.population_id, "population_id")
        _require_hash(self.population_content_hash_sha256, "population hash")
        for names, label in (
            (self.feature_names, "complexity feature"),
            (self.parameter_names, "complexity parameter"),
            (self.rule_names, "complexity rule"),
        ):
            if names != tuple(sorted(names)):
                raise ValueError(f"{label} inventory must use canonical order")
            _require_unique(list(names), label)
            for name in names:
                _require_identity(name, label)
        if (self.feature_count, self.parameter_count, self.rule_count) != (
            len(self.feature_names),
            len(self.parameter_names),
            len(self.rule_names),
        ):
            raise ValueError("complexity counts do not reconcile")
        expected = stable_identity(
            "robustness-complexity-evidence", _identity_payload(self, "evidence_id")
        )
        if self.evidence_id != expected:
            raise ValueError("complexity evidence identity does not match content")


def build_complexity_evidence(
    population: ConfirmatoryPopulation,
    *,
    feature_names: tuple[str, ...],
    parameter_names: tuple[str, ...],
    rule_names: tuple[str, ...],
) -> ComplexityEvidence:
    features = tuple(sorted(feature_names))
    parameters = tuple(sorted(parameter_names))
    rules = tuple(sorted(rule_names))
    values = {
        "population_id": population.population_id,
        "population_content_hash_sha256": _confirmatory_population_content_hash(population),
        "feature_names": features,
        "parameter_names": parameters,
        "rule_names": rules,
        "feature_count": len(features),
        "parameter_count": len(parameters),
        "rule_count": len(rules),
        "schema_version": "v2.opportunity.robustness_complexity_evidence.v1",
    }
    return ComplexityEvidence(
        evidence_id=stable_identity("robustness-complexity-evidence", values),
        **values,  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class FutureDataSentinelEvidence(OutcomeContract):
    evidence_id: str
    population_id: str
    population_content_hash_sha256: str
    source_row_ids: tuple[str, ...]
    source_row_content_hashes: tuple[str, ...]
    original_decision_hashes: tuple[str, ...]
    future_mutated_decision_hashes: tuple[str, ...]
    leakage_detected: bool
    statistical_observation_count: int = 0
    schema_version: str = "v2.opportunity.future_data_sentinel_evidence.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.future_data_sentinel_evidence.v1")
        _require_identity(self.evidence_id, "evidence_id")
        _require_identity(self.population_id, "population_id")
        _require_hash(self.population_content_hash_sha256, "population hash")
        size = len(self.source_row_ids)
        if not size or not all(
            len(values) == size
            for values in (
                self.source_row_content_hashes,
                self.original_decision_hashes,
                self.future_mutated_decision_hashes,
            )
        ):
            raise ValueError("future sentinel identities and hashes must align")
        _require_unique(list(self.source_row_ids), "future sentinel source row")
        for digest in (
            *self.source_row_content_hashes,
            *self.original_decision_hashes,
            *self.future_mutated_decision_hashes,
        ):
            _require_hash(digest, "future sentinel hash")
        expected_leakage = any(
            original != mutated
            for original, mutated in zip(
                self.original_decision_hashes,
                self.future_mutated_decision_hashes,
                strict=True,
            )
        )
        if self.leakage_detected is not expected_leakage:
            raise ValueError("future sentinel leakage result does not recompute")
        if self.statistical_observation_count != 0:
            raise ValueError("future-data sentinels are never statistical observations")
        expected = stable_identity(
            "future-data-sentinel-evidence", _identity_payload(self, "evidence_id")
        )
        if self.evidence_id != expected:
            raise ValueError("future sentinel identity does not match content")


def build_future_data_sentinel_evidence(
    population: ConfirmatoryPopulation,
    *,
    future_mutated_decision_hashes: tuple[str, ...],
) -> FutureDataSentinelEvidence:
    report_rows = {item.row_id: item for item in population.metric_report.bound_rows}
    source_row_ids = tuple(
        row_id for observation in population.observations for row_id in observation.source_row_ids
    )
    source_hashes = tuple(
        digest
        for observation in population.observations
        for digest in observation.source_row_content_hashes
    )
    originals = tuple(
        report_rows[row_id].outcome.decision.content_hash() for row_id in source_row_ids
    )
    if len(future_mutated_decision_hashes) != len(originals):
        raise ValueError("future sentinel must cover every exact population row")
    leakage = any(
        original != mutated
        for original, mutated in zip(originals, future_mutated_decision_hashes, strict=True)
    )
    values = {
        "population_id": population.population_id,
        "population_content_hash_sha256": _confirmatory_population_content_hash(population),
        "source_row_ids": source_row_ids,
        "source_row_content_hashes": source_hashes,
        "original_decision_hashes": originals,
        "future_mutated_decision_hashes": future_mutated_decision_hashes,
        "leakage_detected": leakage,
        "statistical_observation_count": 0,
        "schema_version": "v2.opportunity.future_data_sentinel_evidence.v1",
    }
    return FutureDataSentinelEvidence(
        evidence_id=stable_identity("future-data-sentinel-evidence", values),
        **values,  # type: ignore[arg-type]
    )


def _require_exact_population(
    population: ConfirmatoryPopulation,
    observations: tuple[CausalSessionObservation, ...],
) -> None:
    expected = tuple(
        (
            item.session_source_id,
            item.session_content_hash_sha256,
            item.source_row_ids,
            item.source_row_content_hashes,
        )
        for item in population.observations
    )
    actual = tuple(
        (
            item.session_source_id,
            item.session_content_hash_sha256,
            item.source_row_ids,
            item.source_row_content_hashes,
        )
        for item in observations
    )
    if actual != expected:
        raise ValueError("control arm must preserve the exact ordered confirmatory population")


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise ValueError("control mean population cannot be empty")
    policy = _ControlDecimalPolicy()
    with _metric_decimal_context(policy):
        return _quantize_metric_decimal(_decimal_mean(values, policy), policy)


@dataclass(frozen=True)
class _ControlDecimalPolicy:
    decimal_precision: int = 64
    decimal_scale: int = 12
    rounding_mode: str = "ROUND_HALF_EVEN"


def _regime_summaries(
    session_ids: tuple[str, ...],
    assignments: tuple[str, ...],
    session_values_r: tuple[Decimal, ...],
) -> tuple[RegimeBucketSummary, ...]:
    if len(session_ids) != len(assignments) or len(session_ids) != len(session_values_r):
        raise ValueError("regime summary inputs must align")
    return tuple(
        RegimeBucketSummary(
            bucket=bucket,
            session_ids=tuple(
                session_id
                for session_id, assigned in zip(session_ids, assignments, strict=True)
                if assigned == bucket
            ),
            mean_session_r=_mean(
                tuple(
                    value
                    for value, assigned in zip(session_values_r, assignments, strict=True)
                    if assigned == bucket
                )
            ),
        )
        for bucket in sorted(set(assignments))
    )


def _validate_text_tuple(values: tuple[str, ...], label: str) -> None:
    _require_unique(list(values), label)
    for value in values:
        _require_sanitized_text(value, label)


__all__ = [
    "CANONICAL_PIPELINE_ENTRYPOINT",
    "CausalControlArm",
    "CausalSessionObservation",
    "ComplexityEvidence",
    "FutureDataSentinelEvidence",
    "RegimeBucketSummary",
    "RegimeStabilityEvidence",
    "build_causal_control_arm",
    "build_complexity_evidence",
    "build_control_observations",
    "build_future_data_sentinel_evidence",
    "build_regime_stability_evidence",
    "build_unavailable_control_arm",
]
