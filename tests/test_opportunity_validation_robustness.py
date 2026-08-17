from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal, Inexact, Rounded, localcontext
from pathlib import Path

import pytest

from intraday_scanner.v2.contracts import contract_to_json
from intraday_scanner.v2.opportunity.models import stable_identity
from intraday_scanner.v2.opportunity.validation_robustness import (
    CalibrationRegionKind,
    CausalControlArm,
    ParameterPerturbationSpec,
    RobustnessArmKind,
    RobustnessCheckKind,
    RobustnessCheckStatus,
    RobustnessEvidenceStatus,
    RobustnessVerdict,
    ValidationRobustnessReport,
    build_calibration_source_artifact,
    build_causal_control_arm,
    build_complexity_evidence,
    build_confirmatory_population,
    build_confirmatory_unit,
    build_control_observations,
    build_future_data_sentinel_evidence,
    build_regime_stability_evidence,
    build_robustness_calibration_policy,
    build_session_clustered_confidence_interval,
    build_unavailable_control_arm,
    build_validation_robustness_report,
)
from tests.test_opportunity_validation_metrics import _bounded_preparation, _report


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def population():
    monkeypatch = pytest.MonkeyPatch()
    try:
        metric_report = _report(_bounded_preparation(monkeypatch))
        scope = next(item for item in metric_report.scopes if item.kind.value == "final_validation")
        row = next(item for item in metric_report.bound_rows if item.row_id in scope.row_ids)
        evaluation = row.outcome.decision.evaluation
        unit = build_confirmatory_unit(
            strategy_id=evaluation.strategy_id,
            strategy_version=evaluation.strategy_version,
            direction=evaluation.direction,
        )
        yield build_confirmatory_population(metric_report, unit=unit)
    finally:
        monkeypatch.undo()


def _policy(population, **overrides):
    source = build_calibration_source_artifact(
        content_hash_sha256=_hash("frozen calibration source"),
        source_identity="wp005-c-calibration-source",
        source_version="v1",
        region_id="train-research-2026q2",
        region_kind=CalibrationRegionKind.TRAIN_RESEARCH,
        observed_at=population.first_confirmatory_at - timedelta(days=3),
        available_at=population.first_confirmatory_at - timedelta(days=2),
        limitations=("synthetic_test_source_not_empirical_edge_evidence",),
    )
    values = {
        "policy_version": "wp005-c-v1",
        "declared_at": population.first_confirmatory_at - timedelta(days=1),
        "calibration_region_id": "train-research-2026q2",
        "calibration_region_kind": CalibrationRegionKind.TRAIN_RESEARCH,
        "source_artifacts": (source,),
        "confirmatory_unit": population.unit,
        "trial_count": 4,
        "trial_limit": 10,
        "bootstrap_seed": 90210,
        "bootstrap_resamples": 101,
        "confidence_lower_quantile": Decimal("0.025"),
        "confidence_upper_quantile": Decimal("0.975"),
        "minimum_sessions": 2,
        "minimum_confidence_lower_bound_r": Decimal("-0.01"),
        "maximum_parameter_degradation_r": Decimal("0.20"),
        "maximum_negative_control_mean_r": Decimal("0"),
        "minimum_baseline_excess_r": Decimal("0.05"),
        "minimum_regime_sessions": 1,
        "maximum_regime_mean_spread_r": Decimal("0.20"),
        "perturbations": (
            ParameterPerturbationSpec(
                parameter_name="threshold",
                center_value=Decimal("1"),
                lower_value=Decimal("0.9"),
                upper_value=Decimal("1.1"),
            ),
        ),
        "required_negative_control_names": ("permuted-signal-placebo",),
        "simple_baseline_name": "always-pass-baseline",
        "required_regime_buckets": ("bear", "bull"),
        "maximum_feature_count": 4,
        "maximum_parameter_count": 3,
        "maximum_rule_count": 4,
        "limitations": ("synthetic_controls_prove_software_invariants_only",),
    }
    values.update(overrides)
    return build_robustness_calibration_policy(**values)


def _observations(population, values: tuple[Decimal, ...], prefix: str):
    artifact_ids = tuple(
        (stable_identity("causal-output", {"prefix": prefix, "session": index}),)
        for index in range(population.session_count)
    )
    hashes = tuple((_hash(f"{prefix}:{index}"),) for index in range(population.session_count))
    return build_control_observations(
        population,
        session_values_r=values,
        output_artifact_ids=artifact_ids,
        output_artifact_content_hashes=hashes,
    )


def _arm(population, *, kind, name, values, parameter_name=None, parameter_value=None):
    return build_causal_control_arm(
        population,
        kind=kind,
        control_name=name,
        observations=_observations(population, values, name),
        parameter_name=parameter_name,
        parameter_value=parameter_value,
    )


def _complete_evidence(population):
    zeros = tuple(Decimal("0") for _ in population.observations)
    negative = tuple(Decimal("-0.1") for _ in population.observations)
    arms = (
        _arm(
            population,
            kind=RobustnessArmKind.PARAMETER_PERTURBATION,
            name="threshold-lower",
            values=zeros,
            parameter_name="threshold",
            parameter_value=Decimal("0.9"),
        ),
        _arm(
            population,
            kind=RobustnessArmKind.PARAMETER_PERTURBATION,
            name="threshold-upper",
            values=zeros,
            parameter_name="threshold",
            parameter_value=Decimal("1.1"),
        ),
        _arm(
            population,
            kind=RobustnessArmKind.NEGATIVE_CONTROL,
            name="permuted-signal-placebo",
            values=negative,
        ),
        _arm(
            population,
            kind=RobustnessArmKind.SIMPLE_BASELINE,
            name="always-pass-baseline",
            values=negative,
        ),
    )
    regimes = build_regime_stability_evidence(population, bucket_assignments=("bull", "bear"))
    complexity = build_complexity_evidence(
        population,
        feature_names=("price", "volume"),
        parameter_names=("threshold",),
        rule_names=("entry", "risk"),
    )
    report_rows = {item.row_id: item for item in population.metric_report.bound_rows}
    originals = tuple(
        report_rows[row_id].outcome.decision.content_hash()
        for observation in population.observations
        for row_id in observation.source_row_ids
    )
    sentinel = build_future_data_sentinel_evidence(
        population, future_mutated_decision_hashes=originals
    )
    return arms, regimes, complexity, sentinel


def _robustness_report(
    population, *, policy=None, arms=None, regimes=None, complexity=None, sentinel=None
):
    complete = _complete_evidence(population)
    return build_validation_robustness_report(
        population,
        policy=policy or _policy(population),
        recorded_at=population.metric_report.recorded_at + timedelta(seconds=1),
        control_arms=complete[0] if arms is None else arms,
        regime_evidence=complete[1] if regimes is None else regimes,
        complexity_evidence=complete[2] if complexity is None else complexity,
        future_data_sentinel=complete[3] if sentinel is None else sentinel,
    )


def test_exact_confirmatory_unit_and_cost_3x_session_population(population) -> None:
    assert population.endpoint == "cost_3x_mean_after_cost_session_r"
    assert population.scenario.value == "cost_3x"
    assert population.session_count == 2
    assert population.source_row_count == 4
    assert all(item.source_row_ids for item in population.observations)
    assert all(item.cost_3x_session_r == 0 for item in population.observations)
    assert (
        population.unit.strategy_id,
        population.unit.strategy_version,
        population.unit.direction,
    ) == ("DS-MOM-001", "1.0.0", population.unit.direction.LONG)


def test_complete_evidence_yields_exact_non_promotional_no_control_veto(population) -> None:
    report = _robustness_report(population)
    assert report.verdict is RobustnessVerdict.NO_CONTROL_VETO
    assert report.no_control_veto_is_non_promotional
    assert not report.promotion_eligible
    assert not report.take_authorization
    assert report.lifecycle_mutation_count == 0
    assert ValidationRobustnessReport.from_json(report.to_json()) == report


def test_missing_required_causal_evidence_yields_exact_missing_evidence_veto(population) -> None:
    arms, regimes, complexity, sentinel = _complete_evidence(population)
    report = _robustness_report(
        population,
        arms=tuple(item for item in arms if item.kind is not RobustnessArmKind.NEGATIVE_CONTROL),
        regimes=regimes,
        complexity=complexity,
        sentinel=sentinel,
    )
    assert report.verdict is RobustnessVerdict.VETO_MISSING_EVIDENCE
    check = next(
        item for item in report.checks if item.kind is RobustnessCheckKind.NEGATIVE_CONTROL
    )
    assert check.status is RobustnessCheckStatus.MISSING


def test_control_population_missing_injected_duplicate_reordered_and_filtered_fail(
    population,
) -> None:
    observations = _observations(
        population,
        tuple(Decimal("0") for _ in population.observations),
        "population-attacks",
    )
    attacks = [
        observations[:-1],
        (*observations, observations[0]),
        tuple(reversed(observations)),
    ]
    first = observations[0]
    attacks.append(
        (
            replace(
                first,
                source_row_ids=first.source_row_ids[:-1],
                source_row_content_hashes=first.source_row_content_hashes[:-1],
            ),
            *observations[1:],
        )
    )
    for attack in attacks:
        with pytest.raises(ValueError, match="exact ordered confirmatory population"):
            build_causal_control_arm(
                population,
                kind=RobustnessArmKind.NEGATIVE_CONTROL,
                control_name="population-attack",
                observations=attack,
            )
    with pytest.raises(ValueError, match="duplicate control source row"):
        replace(
            first,
            source_row_ids=(*first.source_row_ids, first.source_row_ids[0]),
            source_row_content_hashes=(
                *first.source_row_content_hashes,
                first.source_row_content_hashes[0],
            ),
        )


def test_future_sentinel_invalidates_only_leakage_and_is_never_an_arm(population) -> None:
    clean = _robustness_report(population)
    arms, regimes, complexity, clean_sentinel = _complete_evidence(population)
    changed = (_hash("future changed"), *clean_sentinel.original_decision_hashes[1:])
    sentinel = build_future_data_sentinel_evidence(
        population, future_mutated_decision_hashes=changed
    )
    report = _robustness_report(
        population,
        arms=arms,
        regimes=regimes,
        complexity=complexity,
        sentinel=sentinel,
    )
    assert report.verdict is RobustnessVerdict.VETO_LEAKAGE
    assert report.confidence_interval == clean.confidence_interval
    assert report.control_arms == clean.control_arms
    assert sentinel.statistical_observation_count == 0
    assert all("sentinel" not in arm.kind.value for arm in report.control_arms)


@contextmanager
def _hostile_decimal_context(precision: int):
    with localcontext() as context:
        context.prec = precision
        context.rounding = ROUND_DOWN
        context.Emin = -9
        context.Emax = 9
        context.clamp = 1
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        yield


def test_session_clustered_interval_is_order_and_hostile_decimal_invariant(population) -> None:
    policy = _policy(population)
    observations = (
        ("session-a", Decimal("0.1234567890123456789")),
        ("session-b", Decimal("-0.0234567890123456789")),
        ("session-c", Decimal("0.3333333333333333333")),
    )
    outputs = []
    for precision in (6, 28, 64):
        with _hostile_decimal_context(precision):
            outputs.append(
                build_session_clustered_confidence_interval(
                    tuple(reversed(observations)), policy=policy
                ).to_json()
            )
    assert len(set(outputs)) == 1
    assert (
        build_session_clustered_confidence_interval(observations, policy=policy).to_json()
        == outputs[0]
    )


def test_calibration_provenance_rejects_locked_late_and_post_confirmatory_sources(
    population,
) -> None:
    policy = _policy(population)
    payload = policy.to_dict()
    payload["calibration_region_kind"] = "locked_oos"
    with pytest.raises(ValueError):
        type(policy).from_dict(payload)
    late_source = build_calibration_source_artifact(
        content_hash_sha256=_hash("late source"),
        source_identity="late-source",
        source_version="v1",
        region_id="train-research-2026q2",
        region_kind=CalibrationRegionKind.TRAIN_RESEARCH,
        observed_at=population.first_confirmatory_at,
        available_at=population.first_confirmatory_at + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="not available before declaration"):
        _policy(population, source_artifacts=(late_source,))
    late_policy = _policy(
        population,
        declared_at=population.first_confirmatory_at + timedelta(seconds=2),
        source_artifacts=(
            build_calibration_source_artifact(
                content_hash_sha256=_hash("pre late policy source"),
                source_identity="pre-late-policy-source",
                source_version="v1",
                region_id="train-research-2026q2",
                region_kind=CalibrationRegionKind.TRAIN_RESEARCH,
                observed_at=population.first_confirmatory_at - timedelta(days=1),
                available_at=population.first_confirmatory_at - timedelta(hours=1),
            ),
        ),
    )
    with pytest.raises(ValueError, match="must predate"):
        _robustness_report(population, policy=late_policy)
    other_unit = build_confirmatory_unit(
        strategy_id=population.unit.strategy_id,
        strategy_version=population.unit.strategy_version,
        direction=population.unit.direction.SHORT,
    )
    with pytest.raises(ValueError, match="exact confirmatory unit"):
        _robustness_report(population, policy=_policy(population, confirmatory_unit=other_unit))


def test_direct_and_json_contracts_reject_strict_boundary_attacks(population) -> None:
    report = _robustness_report(population)
    payload = report.to_dict()
    payload["unknown_field"] = "attack"
    with pytest.raises(ValueError, match="unknown field"):
        ValidationRobustnessReport.from_dict(payload)
    duplicated = report.to_json()[:-1] + f',"report_id":"{report.report_id}"}}'
    with pytest.raises(ValueError, match="duplicate JSON key"):
        ValidationRobustnessReport.from_json(duplicated)
    policy_payload = report.policy.to_dict()
    policy_payload["confidence_lower_quantile"] = 0.025
    with pytest.raises(ValueError, match="exact Decimal"):
        type(report.policy).from_dict(policy_payload)
    policy_payload = report.policy.to_dict()
    policy_payload["declared_at"] = report.policy.declared_at.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        type(report.policy).from_dict(policy_payload)
    unit_payload = population.unit.to_dict()
    unit_payload["strategy_id"] = "api_key=secret-value"
    with pytest.raises(ValueError):
        type(population.unit).from_dict(unit_payload)
    payload = report.to_dict()
    payload["schema_version"] = "v2.opportunity.validation_robustness_report.v999"
    with pytest.raises(ValueError, match="unsupported schema_version"):
        ValidationRobustnessReport.from_dict(payload)


def test_consistently_rehashed_derived_report_and_arm_tamper_reject(population) -> None:
    report = _robustness_report(population)
    payload = report.to_dict()
    payload["checks"][1]["observed_count"] = 999
    payload["report_id"] = stable_identity(
        "validation-robustness-report",
        {key: value for key, value in payload.items() if key != "report_id"},
    )
    with pytest.raises(ValueError, match="checks do not recompute"):
        ValidationRobustnessReport.from_dict(payload)
    arm = report.control_arms[0]
    arm_payload = arm.to_dict()
    arm_payload["mean_session_r"] = "999"
    arm_payload["arm_id"] = stable_identity(
        "causal-robustness-control-arm",
        {key: value for key, value in arm_payload.items() if key != "arm_id"},
    )
    with pytest.raises(ValueError, match="mean does not recompute"):
        CausalControlArm.from_dict(arm_payload)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("confidence", RobustnessVerdict.VETO_CONFIDENCE_INTERVAL),
        ("parameter", RobustnessVerdict.VETO_PARAMETER_FRAGILITY),
        ("negative", RobustnessVerdict.VETO_NEGATIVE_CONTROL),
        ("regime", RobustnessVerdict.VETO_REGIME_INSTABILITY),
        ("baseline", RobustnessVerdict.VETO_BASELINE),
        ("trial", RobustnessVerdict.VETO_TRIAL_LIMIT),
        ("complexity", RobustnessVerdict.VETO_COMPLEXITY),
    ),
)
def test_each_predeclared_stability_control_has_an_explicit_veto(
    population, mutation, expected
) -> None:
    policy = _policy(population)
    arms, regimes, complexity, sentinel = _complete_evidence(population)
    if mutation == "confidence":
        policy = _policy(population, minimum_confidence_lower_bound_r=Decimal("0.01"))
    elif mutation == "parameter":
        bad = _arm(
            population,
            kind=RobustnessArmKind.PARAMETER_PERTURBATION,
            name="threshold-lower",
            values=tuple(Decimal("-1") for _ in population.observations),
            parameter_name="threshold",
            parameter_value=Decimal("0.9"),
        )
        arms = tuple(bad if item.control_name == "threshold-lower" else item for item in arms)
    elif mutation == "negative":
        bad = _arm(
            population,
            kind=RobustnessArmKind.NEGATIVE_CONTROL,
            name="permuted-signal-placebo",
            values=tuple(Decimal("0.1") for _ in population.observations),
        )
        arms = tuple(
            bad if item.kind is RobustnessArmKind.NEGATIVE_CONTROL else item for item in arms
        )
    elif mutation == "regime":
        policy = _policy(population, minimum_regime_sessions=2)
    elif mutation == "baseline":
        bad = _arm(
            population,
            kind=RobustnessArmKind.SIMPLE_BASELINE,
            name="always-pass-baseline",
            values=tuple(Decimal("0") for _ in population.observations),
        )
        arms = tuple(
            bad if item.kind is RobustnessArmKind.SIMPLE_BASELINE else item for item in arms
        )
    elif mutation == "trial":
        policy = _policy(population, trial_count=11, trial_limit=10)
    else:
        complexity = build_complexity_evidence(
            population,
            feature_names=("f1", "f2", "f3", "f4", "f5"),
            parameter_names=("threshold",),
            rule_names=("entry",),
        )
    report = _robustness_report(
        population,
        policy=policy,
        arms=arms,
        regimes=regimes,
        complexity=complexity,
        sentinel=sentinel,
    )
    assert report.verdict is expected


def test_parameter_free_unit_requires_explicit_non_applicability(population) -> None:
    policy = _policy(population, perturbations=())
    arms, regimes, _complexity, sentinel = _complete_evidence(population)
    arms = tuple(item for item in arms if item.kind is not RobustnessArmKind.PARAMETER_PERTURBATION)
    disclosure = build_unavailable_control_arm(
        population,
        kind=RobustnessArmKind.PARAMETER_PERTURBATION,
        control_name="parameter_free",
        status=RobustnessEvidenceStatus.NOT_APPLICABLE,
        reason="unit_has_no_tunable_parameters",
    )
    complexity = build_complexity_evidence(
        population,
        feature_names=("price",),
        parameter_names=(),
        rule_names=("entry",),
    )
    report = _robustness_report(
        population,
        policy=policy,
        arms=(*arms, disclosure),
        regimes=regimes,
        complexity=complexity,
        sentinel=sentinel,
    )
    assert report.verdict is RobustnessVerdict.NO_CONTROL_VETO
    check = next(
        item for item in report.checks if item.kind is RobustnessCheckKind.PARAMETER_PLATEAU
    )
    assert check.status is RobustnessCheckStatus.NOT_APPLICABLE


def test_real_time_and_core_import_firewall_is_one_way() -> None:
    root = Path(__file__).resolve().parents[1]
    core_files = tuple(
        root / "intraday_scanner" / "v2" / "opportunity" / name
        for name in (
            "discovery.py",
            "features.py",
            "regimes.py",
            "registry.py",
            "ranking.py",
            "risk.py",
            "quality_gate.py",
            "pipeline.py",
        )
    ) + (root / "app.py",)
    for path in core_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = tuple(
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ) + tuple(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any("validation_robustness" in item for item in imports), path
    code = """
import sys
import intraday_scanner.v2.opportunity
import intraday_scanner.v2.opportunity.models
import intraday_scanner.v2.opportunity.features
import intraday_scanner.v2.opportunity.discovery
import intraday_scanner.v2.opportunity.regimes
import intraday_scanner.v2.opportunity.registry
import intraday_scanner.v2.opportunity.ranking
import intraday_scanner.v2.opportunity.risk
import intraday_scanner.v2.opportunity.quality_gate
import intraday_scanner.v2.opportunity.pipeline
import intraday_scanner.storage.opportunity_store
import intraday_scanner.storage.opportunity_outcome_store
import intraday_scanner.storage.opportunity_miss_store
import intraday_scanner.storage.opportunity_metric_store
assert not any('validation_robustness' in name for name in sys.modules)
assert not hasattr(intraday_scanner.v2.opportunity, 'ValidationRobustnessReport')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=root, text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    forbidden_dependencies = (
        "alpha.v6",
        "backtest",
        "app",
        "broker",
        "network",
        "scheduler",
        "streamlit",
        "storage",
    )
    for path in (root / "intraday_scanner/v2/opportunity").glob("validation_robustness*.py"):
        assert path.stat().st_size < 40_000
        imports = tuple(
            node.module or ""
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom)
        )
        assert not any(token in module for token in forbidden_dependencies for module in imports)


def test_future_sentinel_json_body_is_not_a_statistical_arm(population) -> None:
    report = _robustness_report(population)
    payload = json.loads(contract_to_json(report))
    assert payload["future_data_sentinel"]["statistical_observation_count"] == 0
    assert {item["kind"] for item in payload["control_arms"]} == {
        "parameter_perturbation",
        "negative_control",
        "simple_baseline",
    }
