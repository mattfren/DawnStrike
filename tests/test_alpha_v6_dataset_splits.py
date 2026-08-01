from __future__ import annotations

from intraday_scanner.alpha.v6.dataset_builder import build_return_dataset


def test_v6_dataset_excludes_unverifiable_label_and_retains_reason() -> None:
    dataset = build_return_dataset(
        decisions=[_decision("d1"), _decision("d2")],
        labels=[
            _label("d1", 1.2, True),
            _label("d2", None, False),
        ],
    )

    assert dataset["row_count"] == 1
    assert dataset["rows"][0]["target_net_excess_return_pct"] == 1.2
    assert dataset["exclusion_counts"]["return_truth_missing_or_ineligible"] == 1


def _decision(decision_id: str) -> dict[str, object]:
    return {
        "decision_id": decision_id,
        "market_date": "2026-08-03",
        "decision_at": "2026-08-03T12:00:00+00:00",
        "ticker": "NOVA",
        "input_hash_sha256": "a" * 64,
        "source_lineage_hash_sha256": "b" * 64,
        "point_in_time": {"all_inputs_observed_at_or_before_decision": True},
    }


def _label(decision_id: str, value: float | None, eligible: bool) -> dict[str, object]:
    return {
        "decision_id": decision_id,
        "label_family": "benchmark_relative_excess_return",
        "label_value": value,
        "learning_eligible": eligible,
        "exclusion_reason": "return_truth_missing_or_ineligible",
    }
