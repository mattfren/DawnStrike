from __future__ import annotations

from datetime import date, timedelta

import pytest

from intraday_scanner.alpha.v6 import dataset_builder as dataset_builder_module
from intraday_scanner.alpha.v6 import models as models_module
from intraday_scanner.alpha.v6 import training as training_module
from intraday_scanner.alpha.v6.dataset_builder import build_return_dataset
from intraday_scanner.alpha.v6.training import (
    predict_from_frozen_model_run,
    train_shadow_challengers,
    walk_forward_challenger_predictions,
)
from intraday_scanner.alpha.v6.validation import evaluate_return_predictions
from tests._alpha_path_truth import canonical_v6_decision, canonical_v6_label


def _synthetic_research_fill_truth(value: object) -> bool:
    """Authenticate only the explicit unit-test research fixture marker."""

    return isinstance(value, dict) and value.get("synthetic_research_fixture") is True


@pytest.fixture
def synthetic_research_fill_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep synthetic model-integrity data isolated from production FillTruth."""

    monkeypatch.setattr(
        dataset_builder_module,
        "has_authenticated_committed_fill_truth",
        _synthetic_research_fill_truth,
    )
    monkeypatch.setattr(
        models_module,
        "has_authenticated_committed_fill_truth",
        _synthetic_research_fill_truth,
    )


def _dataset(
    *,
    day_count: int = 30,
    rows_per_day: int = 5,
) -> dict[str, object]:
    decisions = []
    labels = []
    start = date(2026, 1, 2)
    for day_index in range(day_count):
        market_date = (start + timedelta(days=day_index)).isoformat()
        for row_index in range(rows_per_day):
            decision_id = f"d-{day_index:02d}-{row_index}"
            signal = float(day_index + row_index)
            activation = float((day_index + row_index) % 2)
            realized = 0.08 * signal - (3.5 if (day_index + row_index) % 13 == 0 else 0.4)
            decision = {
                **canonical_v6_decision(decision_id, market_date=market_date),
                "setup_key": "breakout" if row_index % 2 else "reversal",
                "regime_key": "RISK_ON" if day_index % 2 else "RISK_OFF",
                "feature_vector": {
                    "feature_json": {
                        "signal_strength": signal,
                        "spread_pct": 0.4 + row_index / 100.0,
                        "liquidity_execution": {
                            "premarket_dollar_volume": 10_000_000.0,
                        },
                        "catalyst": {
                            "confirmed": True,
                            "event_type": "EARNINGS",
                            "evidence_hashes": ["b" * 64],
                        },
                        "rank": 999 - row_index,
                        "future_high": 10_000 + signal,
                        "selected": row_index == 0,
                    }
                },
                "estimated_round_trip_cost_bps": 25.0,
                "inverse_probability_weight": 1.0,
            }
            decisions.append(decision)
            labels.extend(
                {
                    **label,
                    "synthetic_research_fixture": True,
                }
                for label in (
                    canonical_v6_label(decision, value=realized),
                    canonical_v6_label(
                        decision,
                        family="activation",
                        value=activation,
                    ),
                    canonical_v6_label(
                        decision,
                        family="tail_loss_event",
                        value=float(realized <= -3.0),
                    ),
                )
            )
    return build_return_dataset(decisions=decisions, labels=labels)


def test_training_fits_real_models_and_excludes_prohibited_features(
    synthetic_research_fill_truth,
) -> None:
    receipt = train_shadow_challengers(_dataset(), code_sha="c" * 40)

    assert receipt["status"] == "TRAINED_RESEARCH_BASELINES"
    assert receipt["artifact"]["fitted"] is True
    names = receipt["artifact"]["feature_names"]
    assert any("signal_strength" in name for name in names)
    assert all("rank" not in name for name in names)
    assert all("future" not in name for name in names)
    assert all("selected" not in name for name in names)
    assert receipt["artifact"]["models"]["conditional_return_model"]["fitted"] is True


def test_walk_forward_predictions_never_see_same_or_future_date(
    synthetic_research_fill_truth,
) -> None:
    predictions = walk_forward_challenger_predictions(
        _dataset(), model_run_id="v6m-test"
    )

    assert predictions
    assert all(row["no_lookahead"] is True for row in predictions)
    assert all(
        row["training_max_market_date"] < row["market_date"] for row in predictions
    )
    assert all(row["embargoed_dates"] for row in predictions)


def test_walk_forward_evaluates_permitted_gradient_on_its_own_exact_fold(
    monkeypatch,
    synthetic_research_fill_truth,
) -> None:
    dataset = _dataset(day_count=61, rows_per_day=9)
    rows = dataset["rows"]
    assert isinstance(rows, list)
    training_dates = sorted({str(row["market_date"]) for row in rows})[:60]
    test_date = (date(2026, 1, 2) + timedelta(days=60)).isoformat()
    monkeypatch.setattr(
        training_module,
        "expanding_purged_splits",
        lambda _: [
            {
                "fold_id": "gradient-fold",
                "training_dates": training_dates,
                "test_dates": [test_date],
                "embargoed_dates": [test_date],
                "no_lookahead": True,
            }
        ],
    )

    predictions = walk_forward_challenger_predictions(
        dataset,
        model_run_id="v6m-gradient-test",
    )

    assert predictions
    assert all(
        "controlled_gradient_boosting" in row["permitted_families"]
        for row in predictions
    )
    assert all(
        row["prediction"]["selected_family"] == "regularized_baselines"
        for row in predictions
    )
    assert all(
        "controlled_gradient_boosting" in row["prediction"]["family_predictions"]
        for row in predictions
    )


def test_frozen_artifact_scores_only_later_matching_schema_decisions(
    synthetic_research_fill_truth,
) -> None:
    receipt = train_shadow_challengers(_dataset(), code_sha="c" * 40)
    decision = {
        "market_date": "2026-02-02",
        "feature_schema_version": receipt["feature_schema_version"],
        "feature_vector": {
            "feature_json": {"signal_strength": 12.0, "spread_pct": 0.44}
        },
        "setup_key": "breakout",
        "regime_key": "RISK_ON",
        "source_key": "licensed-primary",
        "liquidity_bucket": "5m_to_20m",
        "catalyst_bucket": "sourced",
        "estimated_round_trip_cost_bps": 25.0,
        "safety_vetoes": [],
    }

    prediction = predict_from_frozen_model_run(receipt, decision)

    assert prediction is not None
    assert prediction["model_run_id"] == receipt["model_run_id"]
    assert prediction["training_cutoff"] < decision["market_date"]
    assert prediction["activation_probability"] is not None
    assert prediction["conditional_net_excess_return_pct"] is not None
    assert prediction["utility_lcb_pct"] is not None
    assert (
        predict_from_frozen_model_run(
            receipt,
            {**decision, "market_date": str(receipt["training_cutoff"])},
        )
        is None
    )
    assert (
        predict_from_frozen_model_run(
            receipt,
            {**decision, "feature_schema_version": "incompatible"},
        )
        is None
    )


def test_validation_applies_ipw_slippage_bootstrap_and_no_lookahead() -> None:
    rows = [
        {
            "market_date": f"2026-02-{index + 1:02d}",
            "training_max_market_date": f"2026-01-{index + 1:02d}",
            "no_lookahead": True,
            "utility_lcb_pct": float(index),
            "realized_net_excess_return_pct": 2.0 if index else -1.0,
            "inverse_probability_weight": 5.0 if index == 0 else 1.0,
            "estimated_round_trip_cost_bps": 20.0,
            "regime_key": "RISK_ON",
            "source_key": "primary",
            "liquidity_bucket": "5m_to_20m",
            "catalyst_bucket": "sourced",
        }
        for index in range(8)
    ]

    metrics = evaluate_return_predictions(rows, bootstrap_samples=200)

    assert metrics["selection_bias_correction"]["sampled_row_count"] == 1
    assert metrics["after_cost_expectancy_pct"] < 1.0
    assert metrics["slippage_stress"]["one_point_five_x_expectancy_pct"] is not None
    assert metrics["bootstrap_expectancy_95_ci_pct"]["lower"] is not None
    assert metrics["no_lookahead_audit_passed"] is True
