from __future__ import annotations

from intraday_scanner.services.alpha_outcome_capture_service import (
    _v6_shadow_outcome_targets,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_sampled_rejected_candidate_becomes_a_sourced_counterfactual_target(
    tmp_path,
) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    decision = {
        "decision_id": "v6d-rejected",
        "scan_id": "scan-1",
        "source_signal_id": "candidate-BETA",
        "shadow_signal_id": "v6s-rejected",
        "market_date": "2026-08-03",
        "decision_at": "2026-08-03T12:00:00+00:00",
        "ticker": "BETA",
        "strategy_version": "dawnstrike-alphaops-v6-shadow",
        "model_version": "dawnstrike-alphaops-v6-research-suite-v2",
        "action": "SHADOW_REJECTED_POLICY",
        "decision_state": "REJECTED",
        "setup_key": "breakout",
        "regime_key": "SELECTIVE",
        "feature_schema_version": "dawnstrike-alphaops-v6-feature-schema-v1",
        "feature_hash_sha256": "f" * 64,
        "feature_vector": {},
        "raw_facts": {"source": "fixture", "source_url": "https://example.test"},
        "universe_membership": {
            "universe_id": "v6u-test",
            "status": "ACTIVE",
            "source_lineage_hash_sha256": "u" * 64,
        },
        "source_summary": {"status": "complete"},
        "safety_vetoes": [],
        "estimated_round_trip_cost_bps": 25.0,
        "cost_model_version": "v6-cost",
        "execution_assumptions": {"policy": "open_to_close"},
        "point_in_time": {
            "all_inputs_observed_at_or_before_decision": True,
            "decision_timestamp": "2026-08-03T12:00:00+00:00",
        },
        "prediction": {"status": "NOT_SCORED_POLICY_REJECTED"},
        "score_components": {},
        "uncertainty": {},
        "rejected_sampling": {
            "included": True,
            "inclusion_probability": 0.2,
            "policy_version": "frozen-v1",
        },
        "input_hash_sha256": "i" * 64,
        "source_lineage_hash_sha256": "s" * 64,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    store.persist_alpha_v6_decisions([decision])

    targets = _v6_shadow_outcome_targets(
        store,
        market_date="2026-08-03",
        historical_signals=[],
    )

    assert len(targets) == 1
    assert targets[0]["signal_id"] == "v6s-rejected"
    assert targets[0]["v6_counterfactual_policy"] == "OPEN_TO_CLOSE_V1"
    assert targets[0]["v6_sampling_probability"] == 0.2
