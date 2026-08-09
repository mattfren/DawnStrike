from __future__ import annotations

from intraday_scanner.alpha.v6.label_builder import build_label_families


def test_v6_label_builder_keeps_missing_return_truth_null() -> None:
    labels = build_label_families(
        decision=_decision(),
        outcome={
            "outcome_id": "o1",
            "outcome_status": "TERMINAL_MISSING",
            "activation_status": "MISSING",
            "observed_at": "2026-08-04T01:00:00+00:00",
        },
    )
    by_family = {row["label_family"]: row for row in labels}

    assert by_family["net_return_after_cost"]["label_value"] is None
    assert by_family["net_return_after_cost"]["learning_eligible"] is False
    assert by_family["data_quality_failure"]["label_value"] == 1.0
    assert all(row["missing_truth_is_zero"] is False for row in labels)


def test_v6_return_labels_require_explicit_path_replay_when_present() -> None:
    labels = build_label_families(
        decision=_decision(),
        outcome={
            "outcome_id": "o2",
            "outcome_status": "COMPLETE_SOURCED",
            "activation_status": "ACTIVATED",
            "source_bar_hash_sha256": "bars",
            "path_replay_id": "",
            "learning_eligible": True,
            "net_return_pct": 1.0,
        },
    )
    return_label = next(row for row in labels if row["label_family"] == "net_return_after_cost")

    assert return_label["learning_eligible"] is False
    assert return_label["path_replay_id"] is None


def test_v6_return_labels_carry_lineage_and_require_complete_truth_contract() -> None:
    labels = build_label_families(
        decision={
            **_decision(),
            "input_hash_sha256": "i" * 64,
            "source_lineage_hash_sha256": "l" * 64,
            "point_in_time": {
                "all_inputs_observed_at_or_before_decision": True,
            },
        },
        outcome={
            "outcome_id": "o3",
            "outcome_status": "COMPLETE_SOURCED",
            "activation_status": "ACTIVATED",
            "source_bar_hash_sha256": "b" * 64,
            "benchmark_source_bar_hash_sha256": "m" * 64,
            "independent_reconciliation_status": "PASSED",
            "benchmark_independent_reconciliation_status": "PASSED",
            "net_return_pct": 1.0,
            "net_excess_return_pct": 0.5,
            "path_replay_id": "path-3",
            "learning_eligible": True,
            "evidence_cohort": "cohort-1",
            "observed_cost_model_version": "observed-v1",
            "modeled_cost_model_version": "modeled-v1",
        },
    )
    return_label = next(
        row for row in labels if row["label_family"] == "net_return_after_cost"
    )

    assert return_label["learning_eligible"] is True
    assert return_label["source_artifact_hash_sha256"] == "b" * 64
    assert return_label["benchmark_hash_sha256"] == "m" * 64
    assert return_label["path_replay_id"] == "path-3"
    assert return_label["evidence_cohort"] == "cohort-1"
    assert return_label["return_truth_status"] == "COMPLETE"


def _decision() -> dict[str, object]:
    return {
        "decision_id": "d1",
        "market_date": "2026-08-03",
        "action": "SHADOW_TRACK",
    }
