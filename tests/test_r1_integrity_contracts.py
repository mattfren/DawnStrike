from __future__ import annotations

import hashlib
from intraday_scanner.alpha.edge_calibrator import calibrate_edge
from intraday_scanner.alpha.outcome_semantics import (
    account_equity_drawdown,
    chronology_valid,
    realized_return,
)
from intraday_scanner.alpha.commit_bridge import _mint_authenticated_fill_truth
from intraday_scanner.decisioning.contracts import canonical_json
from intraday_scanner.alpha.performance_truth import build_truth_report
from intraday_scanner.alpha.setup_memory import build_setup_memory
from intraday_scanner.alpha.v6.contracts import point_in_time_valid
from intraday_scanner.alpha.v6.dataset_builder import build_return_dataset
from intraday_scanner.alpha.v6.models import current_training_rows
from intraday_scanner.alpha.v6.training import (
    train_shadow_challengers,
    walk_forward_challenger_predictions,
)
from intraday_scanner.alpha.v6.validation import expanding_purged_splits
from tests._alpha_path_truth import canonical_v6_decision, canonical_v6_label


def test_realized_return_does_not_promote_favorable_excursion_or_nonfinite_values() -> None:
    assert realized_return({"high_after_entry_return_pct": 10, "close_return_pct": -5}) == -5
    assert realized_return({"high_after_entry_return_pct": 10}) is None
    assert realized_return({"close_return_pct": 0}) == 0
    assert realized_return({"close_return_pct": float("nan")}) is None
    report = calibrate_edge(
        bucket_rows=[{"high_after_entry_return": 10, "close_return_pct": -5}] * 20,
        global_rows=[{"high_after_entry_return": 10, "close_return_pct": -5}] * 20,
        real_shadow_days=20,
    )
    assert report["expected_return_pct"] == -5
    assert report["hit_rate_pct"] == 0


def test_mae_is_not_account_drawdown_and_equity_series_is_reconciled() -> None:
    assert build_setup_memory(
        [{"setup_key": "A", "close_return_pct": 1, "low_after_entry_drawdown": -20}]
    )["A"]["max_drawdown_pct"] is None
    missing = build_setup_memory([{"setup_key": "M", "high_after_entry_return": 10}])["M"]
    assert missing["avg_return_pct"] is None
    assert missing["win_rate_pct"] is None
    rows = [
        {"rank": 1, "close_return_pct": 2, "max_adverse_excursion": -20, "account_equity": 100},
        {"rank": 2, "close_return_pct": -1, "max_adverse_excursion": -30, "account_equity": 90},
    ]
    assert round(account_equity_drawdown(rows), 6) == -10
    assert round(build_truth_report(rows, real_days_collected=20)["max_drawdown_pct"], 6) == -10


def test_point_in_time_rejects_future_feature_timestamp() -> None:
    decision = {
        "decision_at": "2026-09-09T14:00:00+00:00",
        "feature_timestamp": "2026-09-09T14:01:00+00:00",
        "input_hash_sha256": "a" * 64,
        "source_lineage_hash_sha256": "b" * 64,
        "point_in_time": {"all_inputs_observed_at_or_before_decision": True},
    }
    assert point_in_time_valid(decision) is False


def test_feature_event_availability_ingestion_and_decision_order_is_required() -> None:
    decision = canonical_v6_decision("timing-contract")
    label = canonical_v6_label(decision, value=1.0)
    assert chronology_valid(decision=decision, label=label)
    assert not chronology_valid(
        decision={**decision, "feature_available_at": "2026-08-03T12:11:00+00:00"},
        label=label,
    )
    assert not chronology_valid(
        decision={**decision, "feature_ingested_at": "2026-08-03T12:10:30+00:00"},
        label=label,
    )
    assert not chronology_valid(
        decision={**decision, "feature_available_at": None},
        label=label,
    )
    assert not chronology_valid(
        decision={**decision, "feature_ingested_at": None},
        label=label,
    )


def test_cash_flows_are_unitized_with_explicit_timing_and_currency() -> None:
    deposit = [
        {"account_equity": 100, "cash_flow": 0, "valuation_currency": "USD"},
        {
            "account_equity": 200,
            "cash_flow": 100,
            "cash_flow_timing": "start",
            "cash_flow_currency": "USD",
            "valuation_currency": "USD",
        },
        {"account_equity": 190, "cash_flow": 0, "valuation_currency": "USD"},
    ]
    withdrawal = [
        {"account_equity": 200, "cash_flow": 0, "valuation_currency": "USD"},
        {
            "account_equity": 150,
            "cash_flow": -50,
            "cash_flow_timing": "start",
            "cash_flow_currency": "USD",
            "valuation_currency": "USD",
        },
        {"account_equity": 140, "cash_flow": 0, "valuation_currency": "USD"},
    ]
    assert round(account_equity_drawdown(deposit), 6) == round(-5.0, 6)
    assert round(account_equity_drawdown(withdrawal), 6) == round(-6.6666666667, 6)
    assert account_equity_drawdown(deposit[:2]) == 0.0
    assert account_equity_drawdown(withdrawal[:2]) == 0.0
    assert account_equity_drawdown(
        [{"account_equity": 100}, {"account_equity": 200, "cash_flow": 100}]
    ) is None
    assert account_equity_drawdown(
        [
            {"account_equity": 100, "valuation_currency": "USD"},
            {"account_equity": 200, "cash_flow": float("nan"), "valuation_currency": "USD"},
        ]
    ) is None


def test_authenticated_typed_rows_reach_real_training_boundary() -> None:
    rows = []
    activations = []
    for index in range(140):
        decision = canonical_v6_decision(
            f"auth-{index}", market_date=f"2026-01-{(index % 28) + 1:02d}"
        )
        # Keep event, availability, and ingest clocks distinct and ordered.
        label = canonical_v6_label(decision, value=1.0 if index % 2 else -1.0)
        label = {
            **label,
            "return_units": "pct",
            "holding_horizon_minutes": 390,
            "return_denominator": "entry_notional",
            "return_basis": "net_after_cost",
            "cost_availability_status": "complete",
            "label_available_at": label["observed_at"],
        }
        fill_payload = dict(label)
        fill_payload["receipt_hash_sha256"] = hashlib.sha256(
            canonical_json(dict(label)).encode()
        ).hexdigest()
        authenticated = _mint_authenticated_fill_truth(fill_payload)
        rows.append(
            {
                "decision_id": decision["decision_id"],
                "market_date": decision["market_date"],
                "source_label": label,
                "source_decision": decision,
                "fill_truth": authenticated,
                "target_net_excess_return_pct": label["label_value"],
                "retrospective_research_eligible": True,
                "prospective_promotion_eligible": False,
                "feature_vector": {"feature_json": {"alpha_score": float(index % 10)}},
            }
        )
        activations.append(
            {"decision_id": decision["decision_id"], "activation_label": float(index % 2)}
        )
    accepted = current_training_rows(rows)
    assert len(accepted) == 140
    receipt = train_shadow_challengers(
        {
            "dataset_id": "typed-authenticated",
            "dataset_hash_sha256": "a" * 64,
            "feature_schema_version": "dawnstrike-alphaops-v6-feature-schema-v1",
            "training_cutoff": "2026-12-31",
            "rows": rows,
            "activation_rows": activations,
        },
        code_sha="b" * 40,
    )
    assert receipt["status"] in {"TRAINED_RESEARCH_BASELINES", "TRAINED_CONTROLLED_CHALLENGERS"}
    assert receipt["eligibility"]["eligible_label_count"] == 140
    predictions = walk_forward_challenger_predictions(
        {
            "rows": rows,
            "activation_rows": activations,
        },
        model_run_id="typed-authenticated-run",
    )
    assert predictions
    assert all(item["no_lookahead"] for item in predictions)


def test_dataset_builder_preserves_authenticated_bridge_separately() -> None:
    decision = canonical_v6_decision("dataset-auth")
    label = canonical_v6_label(decision, value=1.0)
    label = {
        **label,
        "return_units": "pct",
        "holding_horizon_minutes": 390,
        "return_denominator": "entry_notional",
        "return_basis": "net_after_cost",
        "cost_availability_status": "complete",
        "label_available_at": label["observed_at"],
    }
    payload = dict(label)
    payload["receipt_hash_sha256"] = hashlib.sha256(canonical_json(dict(label)).encode()).hexdigest()
    label["fill_truth"] = _mint_authenticated_fill_truth(payload)
    dataset = build_return_dataset(decisions=[decision], labels=[label])
    assert dataset["row_count"] == 1
    assert dataset["rows"][0]["source_fill_truth"] is not None


def test_purged_fold_exposes_exact_training_ids() -> None:
    rows = []
    for index in range(5):
        day = f"2026-09-{index + 1:02d}"
        rows.append(
            {
                "decision_id": f"base-{index}",
                "market_date": day,
                "entry_at": f"{day}T14:00:00+00:00",
                "exit_at": f"{day}T14:10:00+00:00",
            }
        )
    rows.append(
        {
            "decision_id": "purged",
            "market_date": "2026-09-01",
            "entry_at": "2026-09-01T14:00:00+00:00",
            "exit_at": "2026-09-03T14:05:00+00:00",
        }
    )
    folds = expanding_purged_splits(
        rows, embargo_dates=0, minimum_train_dates=2, max_holding_horizon_minutes=0
    )
    assert folds
    first = folds[0]
    assert "purged" in first["purged_training_decision_ids"]
    assert "purged" not in first["training_decision_ids"]
