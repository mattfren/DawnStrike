from __future__ import annotations

from datetime import date, timedelta

from intraday_scanner.alpha.v6.training import (
    predict_from_frozen_model_run,
    train_shadow_challengers,
    walk_forward_challenger_predictions,
)
from intraday_scanner.alpha.v6.validation import evaluate_return_predictions


def _dataset() -> dict[str, object]:
    rows = []
    activation_rows = []
    start = date(2026, 1, 2)
    for day_index in range(30):
        market_date = (start + timedelta(days=day_index)).isoformat()
        for row_index in range(5):
            decision_id = f"d-{day_index:02d}-{row_index}"
            signal = float(day_index + row_index)
            activation = float((day_index + row_index) % 2)
            realized = 0.08 * signal - (3.5 if (day_index + row_index) % 13 == 0 else 0.4)
            common = {
                "decision_id": decision_id,
                "market_date": market_date,
                "setup_key": "breakout" if row_index % 2 else "reversal",
                "regime_key": "RISK_ON" if day_index % 2 else "RISK_OFF",
                "source_key": "licensed-primary",
                "liquidity_bucket": "5m_to_20m",
                "catalyst_bucket": "sourced",
                "feature_vector": {
                    "feature_json": {
                        "signal_strength": signal,
                        "spread_pct": 0.4 + row_index / 100.0,
                        "rank": 999 - row_index,
                        "future_high": 10_000 + signal,
                        "selected": row_index == 0,
                    }
                },
                "estimated_round_trip_cost_bps": 25.0,
                "inverse_probability_weight": 1.0,
            }
            activation_rows.append({**common, "activation_label": activation})
            rows.append(
                {
                    **common,
                    "target_net_excess_return_pct": realized,
                    "activation_label": activation,
                    "tail_loss_label": float(realized <= -3.0),
                }
            )
    return {
        "dataset_id": "v6ds-test",
        "dataset_hash_sha256": "a" * 64,
        "feature_schema_version": "v6-features",
        "training_cutoff": "2026-01-31",
        "rows": rows,
        "activation_rows": activation_rows,
    }


def test_training_fits_real_models_and_excludes_prohibited_features() -> None:
    receipt = train_shadow_challengers(_dataset(), code_sha="c" * 40)

    assert receipt["status"] == "TRAINED_RESEARCH_BASELINES"
    assert receipt["artifact"]["fitted"] is True
    names = receipt["artifact"]["feature_names"]
    assert any("signal_strength" in name for name in names)
    assert all("rank" not in name for name in names)
    assert all("future" not in name for name in names)
    assert all("selected" not in name for name in names)
    assert receipt["artifact"]["models"]["conditional_return_model"]["fitted"] is True


def test_walk_forward_predictions_never_see_same_or_future_date() -> None:
    predictions = walk_forward_challenger_predictions(
        _dataset(), model_run_id="v6m-test"
    )

    assert predictions
    assert all(row["no_lookahead"] is True for row in predictions)
    assert all(
        row["training_max_market_date"] < row["market_date"] for row in predictions
    )
    assert all(row["embargoed_dates"] for row in predictions)


def test_frozen_artifact_scores_only_later_matching_schema_decisions() -> None:
    receipt = train_shadow_challengers(_dataset(), code_sha="c" * 40)
    decision = {
        "market_date": "2026-02-02",
        "feature_schema_version": "v6-features",
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
