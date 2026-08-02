from __future__ import annotations

from copy import deepcopy

from intraday_scanner.services.alpha_v6_learning_service import (
    MODEL_COMPETITION_CONTRACT,
    select_research_model_family,
)


def _family_metric(
    *,
    objective: float,
    rank: float,
    drawdown: float,
    cvar: float,
    profit_factor: float,
    turnover: float,
    concentration: float,
    capacity: float,
    top_decile_lift: float,
    adjusted_sharpe: float,
    segment_expectancy: float,
    brier: float,
    ece: float,
    coverage: float,
) -> dict[str, object]:
    segments = {
        key: [
            {
                "segment": "verified",
                "sample_size": 30,
                "after_cost_expectancy_pct": segment_expectancy,
                "positive": segment_expectancy > 0,
            }
        ]
        for key in ("regime_key", "source_key", "liquidity_bucket", "catalyst_bucket")
    }
    return {
        "status": "EVALUABLE",
        "sample_size": 30,
        "no_lookahead_audit_passed": True,
        "bootstrap_expectancy_95_ci_pct": {"lower": objective, "upper": objective + 0.5},
        "maximum_drawdown_pct": drawdown,
        "conditional_value_at_risk_95_pct": cvar,
        "profit_factor": profit_factor,
        "turnover_observations_per_session": turnover,
        "gain_loss_concentration_pct": concentration,
        "capacity": {"status": "EVALUABLE", "median_capacity_dollars": capacity},
        "top_decile_lift_pct": top_decile_lift,
        "rank_correlation": rank,
        "multiple_testing_adjusted_sharpe": adjusted_sharpe,
        "slippage_stress": {
            "one_point_five_x_expectancy_pct": 0.4,
            "two_x_expectancy_pct": 0.2,
        },
        "segmented_performance": segments,
    }


def _comparison() -> dict[str, object]:
    baseline = _family_metric(
        objective=0.4,
        rank=0.20,
        drawdown=-5.0,
        cvar=-3.0,
        profit_factor=1.20,
        turnover=2.0,
        concentration=30.0,
        capacity=10_000.0,
        top_decile_lift=0.4,
        adjusted_sharpe=0.3,
        segment_expectancy=0.4,
        brier=0.15,
        ece=0.10,
        coverage=80.0,
    )
    challenger = _family_metric(
        objective=0.8,
        rank=0.30,
        drawdown=-4.0,
        cvar=-2.0,
        profit_factor=1.30,
        turnover=1.8,
        concentration=28.0,
        capacity=11_000.0,
        top_decile_lift=0.6,
        adjusted_sharpe=0.4,
        segment_expectancy=0.5,
        brier=0.10,
        ece=0.05,
        coverage=90.0,
    )
    return {
        "comparison_status": "EVALUABLE_EXACT_COMMON_FOLDS",
        "families_evaluated": [
            "regularized_baselines",
            "controlled_gradient_boosting",
        ],
        "regularized_baselines": {
            "exact_common_fold_oof": baseline,
            "exact_common_fold_calibration": {
                "status": "EVALUABLE",
                "brier_score": 0.15,
                "expected_calibration_error": 0.10,
            },
            "exact_common_fold_interval_coverage": {
                "status": "EVALUABLE",
                "coverage_pct": 80.0,
            },
        },
        "controlled_gradient_boosting": {
            "exact_common_fold_oof": challenger,
            "exact_common_fold_calibration": {
                "status": "EVALUABLE",
                "brier_score": 0.10,
                "expected_calibration_error": 0.05,
            },
            "exact_common_fold_interval_coverage": {
                "status": "EVALUABLE",
                "coverage_pct": 90.0,
            },
        },
    }


def test_challenger_can_win_only_as_a_frozen_research_receipt() -> None:
    result = select_research_model_family(_comparison())

    assert result["selected_research_family"] == "controlled_gradient_boosting"
    assert result["selection_status"] == "CHALLENGER_RESEARCH_WINNER_NOT_PROMOTED"
    assert result["candidates"]["controlled_gradient_boosting"]["eligible_for_research_win"]
    assert result["automatic_policy_change"] is False
    assert result["automatic_model_serving_change"] is False
    assert result["contract"] == MODEL_COMPETITION_CONTRACT


def test_competition_rejects_marginal_or_less_reliable_challengers() -> None:
    comparison = _comparison()
    challenger = comparison["controlled_gradient_boosting"]
    assert isinstance(challenger, dict)
    exact = challenger["exact_common_fold_oof"]
    assert isinstance(exact, dict)
    weakened = deepcopy(exact)
    weakened["bootstrap_expectancy_95_ci_pct"] = {"lower": 0.6, "upper": 1.0}
    weakened["rank_correlation"] = 0.10
    weakened["slippage_stress"] = {
        "one_point_five_x_expectancy_pct": 0.4,
        "two_x_expectancy_pct": 0.0,
    }
    challenger["exact_common_fold_oof"] = weakened

    result = select_research_model_family(comparison)

    candidate = result["candidates"]["controlled_gradient_boosting"]
    assert result["selected_research_family"] == "regularized_baselines"
    assert result["selection_status"] == "BASELINE_RETAINED_RESEARCH_ONLY"
    assert candidate["eligible_for_research_win"] is False
    assert {
        "primary_objective_not_materially_better",
        "two_x_slippage_expectancy_not_positive",
        "rank_correlation_worsened",
    } <= set(candidate["rejection_reasons"])


def test_competition_waits_when_the_common_fold_evidence_is_absent() -> None:
    result = select_research_model_family(
        {
            "comparison_status": "NOT_EVALUABLE_NO_OOF_PREDICTIONS",
            "families_evaluated": [],
        }
    )

    assert result["selected_research_family"] == "regularized_baselines"
    assert result["selection_status"] == "WAITING_FOR_FORWARD_EVIDENCE"
    assert result["candidates"] == {}
