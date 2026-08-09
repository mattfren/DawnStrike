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


def _decision() -> dict[str, object]:
    return {
        "decision_id": "d1",
        "market_date": "2026-08-03",
        "action": "SHADOW_TRACK",
    }
