from __future__ import annotations

import re

import pytest

from intraday_scanner.alpha.v6.dataset_builder import build_return_dataset
from intraday_scanner.errors import StorageError
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from tests._alpha_path_truth import (
    DATASET_SCHEMA_VERSION,
    LABEL_SCHEMA_VERSION,
    canonical_v6_decision,
    canonical_v6_label,
)

LABEL_SCHEMA_V2 = LABEL_SCHEMA_VERSION


def test_v6_dataset_excludes_unverifiable_label_and_retains_reason() -> None:
    dataset = build_return_dataset(
        decisions=[_decision("d1"), _decision("d2")],
        labels=[
            _label("d1", 1.2, True),
            _label("d2", None, False),
        ],
    )

    assert dataset["row_count"] == 0
    assert dataset["exclusion_counts"]["committed_fill_truth_missing"] == 2


def test_v6_dataset_retains_lineage_eligibility_and_catalyst_ablation_contract() -> None:
    dataset = build_return_dataset(
        decisions=[
            {
                **_decision("d3"),
                "feature_vector": {
                    "feature_json": {
                        "catalyst": {
                            "confirmed": True,
                            "event_type": "EARNINGS",
                            "availability_status": "available_before_decision",
                            "evidence_hashes": ["c" * 64],
                        }
                    }
                },
            }
        ],
        labels=[canonical_v6_label(_decision("d3"), prospective=False, value=1.2)],
    )

    assert dataset["row_count"] == 0
    assert dataset["activation_row_count"] == 0
    assert dataset["catalyst_ablation_plan"]["modes"] == [
        "full",
        "no_catalyst",
        "catalyst_only",
        "shuffled_negative_control",
    ]


@pytest.mark.parametrize("legacy_first", (False, True))
def test_v6_dataset_quarantines_legacy_label_and_current_label_wins_grouping(
    legacy_first: bool,
) -> None:
    current = _label("d4", 1.25, True)
    legacy = {
        **current,
        "label_id": "legacy-label",
        "label_schema_version": "dawnstrike-alphaops-v6-label-schema-v1",
        "eligibility_policy_version": "dawnstrike.alphaops-v6-eligibility.v1",
        "path_replay_schema_version": "dawnstrike.path_truth.v1",
        "learning_eligible": True,
        "label_value": 99.0,
    }
    labels = [legacy, current] if legacy_first else [current, legacy]

    dataset = build_return_dataset(decisions=[_decision("d4")], labels=labels)

    assert dataset["schema_version"].endswith("v2")
    assert dataset["row_count"] == 0
    assert dataset["exclusion_counts"]["committed_fill_truth_missing"] == 2
    assert "legacy_or_incomplete_label_quarantined" not in dataset[
        "exclusion_counts"
    ]


@pytest.mark.parametrize("reverse", (False, True))
def test_v6_dataset_rejects_two_conflicting_current_labels_order_independently(
    reverse: bool,
) -> None:
    decision = _decision("d-conflict")
    first = canonical_v6_label(decision, value=1.0)
    second = canonical_v6_label(decision, value=2.0)
    labels = [first, second]
    if reverse:
        labels.reverse()

    dataset = build_return_dataset(decisions=[decision], labels=labels)
    assert dataset["row_count"] == 0
    assert dataset["exclusion_counts"]["committed_fill_truth_missing"] == 2


def test_v6_dataset_identity_and_hash_bind_ordered_label_lineage() -> None:
    decision = _decision("d-identity")
    baseline = build_return_dataset(
        decisions=[decision],
        labels=[canonical_v6_label(decision, value=1.0)],
    )
    changed = build_return_dataset(
        decisions=[decision],
        labels=[canonical_v6_label(decision, value=1.1, case="ordered_stop")],
    )

    assert baseline["schema_version"] == DATASET_SCHEMA_VERSION
    assert baseline["eligibility_policy_version"] == (
        "dawnstrike.alphaops-v6-eligibility.v2"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", baseline["dataset_hash_sha256"])
    assert baseline["dataset_id"] == (
        "v6ds-v2-" + baseline["dataset_hash_sha256"]
    )
    assert baseline["dataset_id"] == changed["dataset_id"]
    assert baseline["dataset_hash_sha256"] == changed["dataset_hash_sha256"]
    assert baseline["ordered_label_ids"] == changed["ordered_label_ids"] == []
    assert baseline["ordered_label_hashes"] == changed["ordered_label_hashes"] == []


def test_v6_dataset_store_is_idempotent_equal_and_conflicts_on_same_id(
    tmp_path,
) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    decision = _decision("dataset-store-conflict")
    dataset = build_return_dataset(
        decisions=[decision],
        labels=[canonical_v6_label(decision, value=1.0)],
    )

    assert store.persist_alpha_v6_dataset(dataset) is True
    assert store.persist_alpha_v6_dataset(dataset) is False
    with pytest.raises(StorageError, match="immutable V6 dataset conflict"):
        store.persist_alpha_v6_dataset(
            {**dataset, "row_count": int(dataset["row_count"]) + 1}
        )

    assert store.load_alpha_v6_datasets() == [dataset]


def test_v6_dataset_identity_binds_causal_experiment_assignment() -> None:
    baseline_decision = {
        **_decision("dataset-context"),
        "experiment_assignment": {
            "experiment_id": "experiment-v2",
            "arm": "baseline",
            "configuration_hash_sha256": "d" * 64,
        },
    }
    candidate_decision = {
        **baseline_decision,
        "experiment_assignment": {
            **baseline_decision["experiment_assignment"],
            "arm": "candidate",
        },
    }
    baseline = build_return_dataset(
        decisions=[baseline_decision],
        labels=[canonical_v6_label(baseline_decision, value=1.0)],
    )
    candidate = build_return_dataset(
        decisions=[candidate_decision],
        labels=[canonical_v6_label(candidate_decision, value=1.0)],
    )

    assert baseline["ordered_label_ids"] == candidate["ordered_label_ids"] == []
    assert baseline["ordered_label_hashes"] == candidate["ordered_label_hashes"] == []
    assert baseline["dataset_id"] == candidate["dataset_id"]
    assert baseline["dataset_hash_sha256"] == candidate["dataset_hash_sha256"]


def test_v6_dataset_rejects_120_forged_boolean_labels() -> None:
    decisions = [_decision(f"legacy-{index}") for index in range(120)]
    forged = [
        {
            "label_id": f"legacy-label-{index}",
            "decision_id": decision["decision_id"],
            "label_family": "benchmark_relative_excess_return",
            "label_value": 2.0,
            "learning_eligible": True,
            "return_label_eligible": True,
        }
        for index, decision in enumerate(decisions)
    ]

    dataset = build_return_dataset(decisions=decisions, labels=forged)

    assert dataset["row_count"] == 0
    assert dataset["exclusion_counts"]["committed_fill_truth_missing"] == 120


def _decision(decision_id: str) -> dict[str, object]:
    return canonical_v6_decision(decision_id)


def _label(decision_id: str, value: float | None, eligible: bool) -> dict[str, object]:
    decision = _decision(decision_id)
    label = canonical_v6_label(decision, prospective=False, value=value or 0.0)
    if not eligible:
        label.update(
            {
                "label_value": value,
                "learning_eligible": False,
                "return_label_eligible": False,
                "return_truth_status": "INCOMPLETE",
                "exclusion_reason": "return_truth_missing_or_ineligible",
            }
        )
    return label
