from __future__ import annotations

from intraday_scanner.alpha.fill_truth import has_authenticated_committed_fill_truth
from intraday_scanner.alpha.v6.dataset_builder import build_return_dataset
from intraday_scanner.alpha.v6.label_builder import build_label_families
from intraday_scanner.alpha.v6_shadow import build_v6_outcomes, strict_walk_forward_evaluation
from intraday_scanner.services.learning_service import (
    load_production_alpha_learning_labels,
    run_alpha_learning,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from tests._alpha_path_truth import (
    canonical_return_outcome,
    canonical_v6_decision,
    causal_identity_from,
    replay_binding_from,
)


def test_v5_replay_return_is_quarantined_before_setup_memory(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "alpha.sqlite")
    store.persist_strategy_reconciliation(
        evaluations=[
            {
                "evaluation_id": "evaluation-replay",
                "selection_id": "selection-replay",
                "signal_id": "signal-replay",
                "market_date": "2026-08-20",
                "ticker": "NOVA",
                "strategy_id": "alphaops_v5",
                "strategy_version": "dawnstrike-alphaops-v5.0.0",
                "cohort": "official_telegram",
                "terminal_state": "filled_and_closed",
                "reconciliation_status": "resolved",
                "activated": True,
                "filled": True,
                "closed": True,
                "net_return_pct": 999.0,
                "source_bar_hash_sha256": "a" * 64,
                "execution_policy_version": "alphaops_v5_execution_v1",
                "reconciled_at": "2026-08-20T22:00:00+00:00",
                "delivered": True,
                "delivery_channel": "telegram",
                "delivery_status": "delivered",
                "trade_return_eligible": True,
            }
        ],
        paper_trades=[
            {
                "trade_id": "trade-replay",
                "selection_id": "selection-replay",
                "signal_id": "signal-replay",
                "market_date": "2026-08-20",
                "ticker": "NOVA",
                "strategy_id": "alphaops_v5",
                "strategy_version": "dawnstrike-alphaops-v5.0.0",
                "cohort": "official_telegram",
                "direction": "long",
                "decision_time": "2026-08-20T14:00:00+00:00",
                "entry_time": "2026-08-20T14:01:00+00:00",
                "entry_fill_price": 10.0,
                "exit_time": "2026-08-20T20:00:00+00:00",
                "exit_fill_price": 109.9,
                "exit_reason": "target_1",
                "quantity": 1.0,
                "notional": 10.0,
                "net_pnl": 999.0,
                "net_return_pct": 999.0,
                "r_multiple": 999.0,
                "fees": 0.1,
                "slippage_cost": 0.1,
                "source_bar_hash_sha256": "a" * 64,
                "execution_policy_version": "alphaops_v5_execution_v1",
                "created_at": "2026-08-20T22:00:00+00:00",
                "source": "bar_replay",
            }
        ],
        learning_labels=[
            {
                "label_id": "label-replay",
                "evaluation_id": "evaluation-replay",
                "signal_id": "signal-replay",
                "market_date": "2026-08-20",
                "ticker": "NOVA",
                "strategy_id": "alphaops_v5",
                "strategy_version": "dawnstrike-alphaops-v5.0.0",
                "cohort": "official_telegram",
                "label_family": "trade_return",
                "label_value": 999.0,
                "r_multiple": 999.0,
                "eligible": True,
                "source_bar_hash_sha256": "a" * 64,
                "created_at": "2026-08-20T22:00:00+00:00",
            }
        ],
        scorecards=[],
    )

    result = run_alpha_learning(store)

    assert result["total_return_labels"] == 0
    assert result["return_learning_eligible"] is False
    assert result["return_learning_quarantine"] == {
        "status": "QUARANTINED_MISSING_COMMITTED_FILL_TRUTH",
        "reason": "committed_point_in_time_fill_truth_required",
        "count": 1,
    }
    raw_label = store.load_strategy_learning_labels()[0]
    assert raw_label["label_value"] == 999.0
    assert raw_label["r_multiple"] == 999.0
    assert result["setup_memory_count"] == 0
    assert load_production_alpha_learning_labels(store) == []


def test_v6_replay_return_keeps_diagnostics_but_cannot_train(tmp_path) -> None:
    decision = canonical_v6_decision("decision-replay")
    outcome = {
        **canonical_return_outcome(
            causal_identity=causal_identity_from(
                decision,
                kind="alpha_v6_shadow_decision",
            ),
            replay_binding=replay_binding_from(
                decision,
                kind="alpha_v6_shadow_decision",
            ),
        ),
        "decision_id": decision["decision_id"],
        "market_date": decision["market_date"],
        "signal_id": decision["shadow_signal_id"],
    }

    labels = build_label_families(decision=decision, outcome=outcome)
    by_family = {row["label_family"]: row for row in labels}
    dataset = build_return_dataset(decisions=[decision], labels=labels)
    projected = build_v6_outcomes(
        decisions=[decision],
        sourced_outcomes=[outcome],
        capture_attempts=[],
    )[0]
    walk_forward = strict_walk_forward_evaluation(
        decisions=[decision], outcomes=[projected]
    )

    assert by_family["activation"]["learning_eligible"] is True
    assert by_family["data_quality_failure"]["learning_eligible"] is True
    assert by_family["benchmark_relative_excess_return"]["label_value"] == outcome[
        "net_excess_return_pct"
    ]
    assert by_family["benchmark_relative_excess_return"]["learning_eligible"] is False
    assert (
        by_family["benchmark_relative_excess_return"]["exclusion_reason"]
        == "committed_point_in_time_fill_truth_required"
    )
    assert dataset["row_count"] == 0
    assert dataset["activation_row_count"] == 1
    assert dataset["exclusion_counts"]["committed_fill_truth_missing"] >= 1
    assert projected["learning_eligible"] is False
    assert projected["fill_truth_status"] == "missing_committed_fill_truth"
    assert walk_forward["total_label_count"] == 0
    assert has_authenticated_committed_fill_truth(
        {
            "fill_truth_status": "committed",
            "fill_truth_hash_sha256": "a" * 64,
            "fill_truth_contract_verified": True,
            "fill_truth_receipt": {"status": "committed"},
        }
    ) is False
