from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from intraday_scanner.v2.contracts.serialization import ContractValidationError
from intraday_scanner.v2.opportunity.models import (
    AnomalyEvidence,
    AnomalyType,
    Availability,
    BacktestRun,
    DataQuality,
    EvaluationStatus,
    EvidenceKind,
    ExpectancyEvidence,
    FeatureSnapshot,
    FeatureStage,
    GateCheck,
    LifecycleActorType,
    MarketRegime,
    NumericFeature,
    RankComponent,
    RankedOpportunity,
    RegimeState,
    RunStatus,
    StrategyDirection,
    StrategyEvaluation,
    StrategyValidationState,
    TradeDecision,
    TradeDecisionValue,
    ValidationRun,
)
from intraday_scanner.v2.opportunity.quality_gate import (
    apply_quality_gate,
    build_decision_run_context,
)
from intraday_scanner.v2.opportunity.ranking import rank_opportunities
from intraday_scanner.v2.opportunity.registry import (
    StrategyRegistry,
    build_default_registry,
    validate_lifecycle_transition,
)

NOW = datetime(2026, 8, 11, 14, 35, tzinfo=UTC)


def _feature(name: str, value: str = "1", *, observed_at: datetime = NOW) -> NumericFeature:
    return NumericFeature(
        name=name,
        value=Decimal(value),
        availability=Availability.AVAILABLE,
        method="fixture",
        sample_size=5,
        window_id="fixture:5",
        observed_at=observed_at,
        source_kind="OHLCV_BAR",
    )


def _decision_fixture() -> TradeDecision:
    evaluation = StrategyEvaluation(
        evaluation_id="decision-evaluation",
        candidate_id="decision-candidate",
        feature_snapshot_id="decision-features",
        symbol="ABC",
        decision_at=NOW,
        strategy_id="DS-MOM-001",
        strategy_version="1.0.0",
        strategy_definition_hash="definition-hash",
        evaluator_id="fixture-evaluator",
        evaluator_code_hash="evaluator-hash",
        lifecycle=StrategyValidationState.EXPERIMENTAL,
        direction=StrategyDirection.LONG,
        status=EvaluationStatus.ELIGIBLE,
        reasons=("fixture",),
        entry_price=Decimal("100"),
        invalidation_price=Decimal("98"),
        target_price=Decimal("106"),
        after_cost_reward_risk=None,
        anomaly_strength=Decimal("0.8"),
        regime_fit=Decimal("0.8"),
        data_quality_score=Decimal("1"),
        liquidity_score=Decimal("1"),
    )
    ranked = rank_opportunities((evaluation,))[0]
    context = build_decision_run_context(
        (evaluation,),
        (ranked,),
        risk_by_evaluation={},
    )
    return apply_quality_gate(ranked, evaluation, decision_context=context)


def test_contracts_reject_naive_timestamps_and_unknown_enums() -> None:
    with pytest.raises(ContractValidationError, match="timezone-aware"):
        _feature("bad", observed_at=datetime(2026, 8, 11, 10, 0))
    with pytest.raises(ValueError):
        StrategyValidationState("invented")
    with pytest.raises(TypeError):
        FeatureSnapshot(
            snapshot_id="snapshot",
            symbol="ABC",
            decision_at=NOW,
            market_date="2026-08-11",
            universe_id="u",
            dataset_id="d",
            stage="rich",  # type: ignore[arg-type]
            latest_bar_at=NOW,
            numerical=(),
            categorical=(),
            unavailable_features=(),
            data_quality=DataQuality.HIGH,
        )


def test_serialization_and_content_hash_are_deterministic() -> None:
    left = _feature("relative_volume", "2.5")
    right = _feature("relative_volume", "2.5")
    assert left.to_json() == right.to_json()
    assert left.content_hash() == right.content_hash()
    assert '"value":"2.5"' in left.to_json()
    snapshot = FeatureSnapshot(
        snapshot_id="snapshot",
        symbol="ABC",
        decision_at=NOW,
        market_date="2026-08-11",
        universe_id="u",
        dataset_id="d",
        stage=FeatureStage.RICH,
        latest_bar_at=NOW,
        numerical=(left,),
        categorical=(),
        unavailable_features=(),
        data_quality=DataQuality.HIGH,
    )
    assert FeatureSnapshot.from_json(snapshot.to_json()) == snapshot
    with pytest.raises(ValueError, match="ISO calendar date"):
        FeatureSnapshot.from_dict({**snapshot.to_dict(), "market_date": "not-a-date"})


def test_anomaly_threshold_must_be_finite_and_present_when_available() -> None:
    kwargs = {
        "anomaly_type": AnomalyType.GAP,
        "triggered": False,
        "strength": Decimal("0.2"),
        "availability": Availability.AVAILABLE,
        "evidence_kind": EvidenceKind.HEURISTIC,
        "threshold_source": "fixture",
        "method": "fixture",
        "sample_size": 5,
        "feature_names": ("gap_return",),
    }
    with pytest.raises(ValueError, match="threshold must be finite"):
        AnomalyEvidence(threshold=Decimal("NaN"), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires a threshold"):
        AnomalyEvidence(threshold=None, **kwargs)  # type: ignore[arg-type]


def test_regime_rejects_measurement_observed_after_decision() -> None:
    with pytest.raises(ValueError, match="after decision_at"):
        MarketRegime(
            regime_id="market",
            decision_at=NOW,
            benchmark_symbol="SPY",
            state=RegimeState.TREND_UP,
            measurements=(_feature("return_long", observed_at=NOW + timedelta(seconds=1)),),
            confidence=Decimal("0.7"),
            evidence_kind=EvidenceKind.HEURISTIC,
            reasons=("fixture",),
        )


def test_expectancy_availability_cannot_hide_or_invent_metrics() -> None:
    with pytest.raises(ValueError, match="cannot carry metric values"):
        ExpectancyEvidence(
            evidence_id="ev",
            cohort_id="cohort",
            availability=Availability.INSUFFICIENT_DATA,
            evidence_kind=EvidenceKind.EMPIRICAL,
            sample_size=2,
            effective_sample_size=None,
            win_probability=None,
            average_winner_r=None,
            average_loser_r=None,
            expectancy_r=Decimal("0"),
            profit_factor=None,
            average_mfe_r=None,
            average_mae_r=None,
            average_holding_minutes=None,
            confidence_interval_low_r=None,
            confidence_interval_high_r=None,
            uncertainty_half_width_r=None,
            stability_score=None,
            regime=None,
            limitations=("small",),
        )


def test_run_contracts_validate_chronology() -> None:
    with pytest.raises(ValueError, match="interval is reversed"):
        BacktestRun(
            run_id="bt",
            created_at=NOW,
            dataset_id="d",
            code_hash="hash",
            strategy_hashes=(),
            research_start=NOW,
            research_end=NOW - timedelta(days=1),
            assumptions=(),
            status=RunStatus.CREATED,
            limitations=(),
        )
    with pytest.raises(ValueError, match="interval is reversed"):
        ValidationRun(
            run_id="v",
            created_at=NOW,
            backtest_run_id="bt",
            validation_start=NOW,
            validation_end=NOW - timedelta(days=1),
            locked_oos=True,
            configuration_hash="hash",
            status=RunStatus.CREATED,
            limitations=(),
        )


def test_rank_arithmetic_and_take_receipts_fail_closed() -> None:
    component = RankComponent(
        name="anomaly",
        value=Decimal("0.8"),
        weight=Decimal("0.5"),
        contribution=Decimal("0.4"),
        evidence_kind=EvidenceKind.HEURISTIC,
        explanation="fixture",
    )
    with pytest.raises(ValueError, match="base_score"):
        RankedOpportunity(
            ranked_id="ranked",
            evaluation_id="evaluation",
            symbol="ABC",
            strategy_id="DS-MOM-001",
            strategy_version="1.0.0",
            direction=StrategyDirection.LONG,
            relative_rank=1,
            base_score=Decimal("0.7"),
            concentration_penalty=Decimal("0"),
            final_score=Decimal("0.7"),
            components=(component,),
            concentration_labels=(),
            limitations=(),
        )
    failed = GateCheck("data", False, True, "data must pass")
    valid = _decision_fixture()
    production_evaluation = replace(
        valid.evaluation,
        lifecycle=StrategyValidationState.PRODUCTION_ELIGIBLE,
    )
    assert valid.ranked is not None
    production_context = build_decision_run_context(
        (production_evaluation,),
        (valid.ranked,),
        risk_by_evaluation={},
    )
    with pytest.raises(ValueError, match="canonical schema"):
        replace(
            valid,
            decision_id="invalid",
            decision_run_id=production_context.decision_run_id,
            decision_context=production_context,
            evaluation_content_hash=production_evaluation.content_hash(),
            lifecycle=StrategyValidationState.PRODUCTION_ELIGIBLE,
            evaluation=production_evaluation,
            decision=TradeDecisionValue.TAKE,
            gate_checks=(failed,),
            vetoes=(),
        )
    duplicate = GateCheck("data", True, True, "data must pass")
    with pytest.raises(ValueError, match="duplicate gate check"):
        replace(
            valid,
            decision_id="invalid",
            gate_checks=(duplicate, duplicate),
        )


def test_registry_identity_and_lifecycle_fail_closed() -> None:
    registry = build_default_registry()
    assert len(registry.definitions) == 9
    with pytest.raises(ValueError, match="duplicate strategy"):
        StrategyRegistry((registry.definitions[0], registry.definitions[0]))
    transition = validate_lifecycle_transition(
        StrategyValidationState.EXPERIMENTAL,
        StrategyValidationState.RESEARCH_PASS,
        strategy_id="DS-MOM-001",
        strategy_version="1.0.0",
        requested_at=NOW,
        effective_at=NOW + timedelta(minutes=1),
        actor_type=LifecycleActorType.HUMAN_REVIEWER,
        validation_evidence_ids=("validation-fixture",),
        run_evidence_ids=("run-fixture",),
        reason="controlled research review",
        policy_version="lifecycle-policy-v1",
    )
    assert transition.from_state is StrategyValidationState.EXPERIMENTAL
    with pytest.raises(ValueError, match="invalid lifecycle transition"):
        validate_lifecycle_transition(
            StrategyValidationState.EXPERIMENTAL,
            StrategyValidationState.PRODUCTION_ELIGIBLE,
            strategy_id="DS-MOM-001",
            strategy_version="1.0.0",
            requested_at=NOW,
            effective_at=NOW,
            actor_type=LifecycleActorType.HUMAN_REVIEWER,
            validation_evidence_ids=("validation-fixture",),
            run_evidence_ids=("run-fixture",),
            reason="invalid skipped transition",
            policy_version="lifecycle-policy-v1",
        )


def test_core_import_boundaries_and_future_label_isolation() -> None:
    root = Path("intraday_scanner/v2/opportunity")
    forbidden_modules = {"sqlite3", "streamlit", "requests", "httpx", "argparse"}
    realtime = {
        "features.py",
        "discovery.py",
        "regimes.py",
        "registry.py",
        "ranking.py",
        "quality_gate.py",
        "pipeline.py",
    }
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert not imports & forbidden_modules
        if path.name in realtime:
            assert "OutcomeRecord" not in path.read_text(encoding="utf-8")
            assert "MissedOpportunityRecord" not in path.read_text(encoding="utf-8")
    discovery_text = (root / "discovery.py").read_text(encoding="utf-8")
    assert ".registry" not in discovery_text
    assert ".strateg" not in discovery_text
