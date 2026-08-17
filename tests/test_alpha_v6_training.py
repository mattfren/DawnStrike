from __future__ import annotations

from datetime import date, timedelta

from intraday_scanner.alpha.v6.models import model_eligibility
from intraday_scanner.alpha.v6.training import train_shadow_challengers
from intraday_scanner.services.alpha_v6_learning_service import run_alpha_v6_learning
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from tests._alpha_path_truth import canonical_v6_decision, canonical_v6_label


def test_v6_return_model_training_is_blocked_below_100_labels() -> None:
    eligibility = model_eligibility([{"market_date": "2026-08-03"}] * 99)
    receipt = train_shadow_challengers(
        {
            "dataset_id": "d1",
            "dataset_hash_sha256": "a" * 64,
            "feature_schema_version": "f1",
            "training_cutoff": "2026-08-03",
            "rows": [],
        },
        code_sha="c" * 40,
    )

    assert eligibility.status == "NOT_TRAINED_INSUFFICIENT_LABELS"
    assert receipt["status"] == "NOT_TRAINED_INSUFFICIENT_LABELS"
    assert receipt["automatic_promotion"] is False


def test_v6_gradient_boosting_needs_500_labels_and_60_dates() -> None:
    rows = [
        {"market_date": f"2026-01-{(index % 28) + 1:02d}"}
        for index in range(500)
    ]

    eligibility = model_eligibility(rows)

    assert "controlled_gradient_boosting" not in eligibility.allowed_families


def test_v6_model_eligibility_does_not_trust_legacy_boolean_rows() -> None:
    forged = [
        {
            "market_date": f"2026-01-{(index % 28) + 1:02d}",
            "learning_eligible": True,
            "retrospective_research_eligible": True,
            "target_net_excess_return_pct": 2.0,
        }
        for index in range(120)
    ]

    eligibility = model_eligibility(forged)

    assert eligibility.status == "NOT_TRAINED_INSUFFICIENT_LABELS"
    assert eligibility.eligible_label_count == 0
    assert "legacy_or_incomplete_contract_rows_quarantined" in eligibility.exact_exclusions


def test_v6_training_independently_rejects_120_forged_dataset_rows() -> None:
    forged = [
        {
            "market_date": f"2026-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}",
            "learning_eligible": True,
            "retrospective_research_eligible": True,
            "prospective_promotion_eligible": True,
            "target_net_excess_return_pct": 2.0,
        }
        for index in range(120)
    ]

    receipt = train_shadow_challengers(
        {
            "schema_version": "dawnstrike-alphaops-v6-dataset-v2",
            "dataset_id": "forged-dataset",
            "dataset_hash_sha256": "a" * 64,
            "feature_schema_version": "dawnstrike-alphaops-v6-feature-schema-v1",
            "training_cutoff": "2026-08-03",
            "rows": forged,
        },
        code_sha="c" * 40,
    )

    assert receipt["status"] == "NOT_TRAINED_INSUFFICIENT_LABELS"
    assert receipt["eligibility"]["eligible_label_count"] == 0
    assert "legacy_or_incomplete_contract_rows_quarantined" in receipt[
        "eligibility"
    ]["exact_exclusions"]


def test_v6_model_eligibility_accepts_120_authentic_current_rows() -> None:
    rows = []
    for index in range(120):
        market_date = (date(2026, 1, 2) + timedelta(days=index % 60)).isoformat()
        decision = canonical_v6_decision(f"current-{index}", market_date=market_date)
        label = canonical_v6_label(decision)
        rows.append(
            {
                **label,
                "source_decision": decision,
                "source_label": label,
                "target_net_excess_return_pct": label["label_value"],
            }
        )

    eligibility = model_eligibility(rows)

    assert eligibility.status == "RESEARCH_BASELINES_ONLY"
    assert eligibility.eligible_label_count == 120
    assert eligibility.forward_date_count == 60


def test_v6_training_receipt_records_exact_lineage_gate_exclusions() -> None:
    receipt = train_shadow_challengers(
        {
            "dataset_id": "d1",
            "dataset_hash_sha256": "a" * 64,
            "feature_schema_version": "f1",
            "training_cutoff": "2026-08-03",
            "rows": [
                {
                    "market_date": "2026-08-03",
                    "source_artifact_hash_sha256": "b" * 64,
                    "path_replay_id": "path-1",
                    "benchmark_hash_sha256": "c" * 64,
                    "evidence_cohort": "cohort-1",
                    "retrospective_research_eligible": True,
                }
            ],
        },
        code_sha="c" * 40,
    )

    assert receipt["status"] == "NOT_TRAINED_INSUFFICIENT_LABELS"
    assert receipt["eligibility"]["exact_exclusions"]
    assert receipt["evidence_lineage"]["path_replay_ids"] == ["path-1"]
    assert receipt["eligibility_dimensions"]["prospective_promotion"]["eligible"] is False


def test_v6_full_learning_service_persists_untrained_evidence_without_fake_returns(
    tmp_path,
) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    result = run_alpha_v6_learning(store, code_sha="c" * 40)

    assert result["status"] == "NOT_TRAINED_INSUFFICIENT_LABELS"
    assert result["dataset"]["row_count"] == 0
    assert result["promotion_review"]["approved"] is False
    assert result["performance_status"] == "WAITING_FOR_FORWARD_EVIDENCE"


def test_v6_weekly_service_rejects_120_persisted_pathless_boolean_labels(
    tmp_path,
) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    decisions = []
    forged_labels = []
    for index in range(120):
        market_date = (date(2026, 1, 2) + timedelta(days=index % 60)).isoformat()
        decision = canonical_v6_decision(f"weekly-forged-{index}", market_date=market_date)
        decisions.append(decision)
        forged_labels.append(
            {
                "label_id": f"forged-label-{index}",
                "decision_id": decision["decision_id"],
                "market_date": market_date,
                "observed_at": f"{market_date}T21:00:00+00:00",
                "label_family": "benchmark_relative_excess_return",
                "label_value": 2.0,
                "learning_eligible": True,
                "retrospective_research_eligible": True,
                "prospective_promotion_eligible": True,
            }
        )
    store.persist_alpha_v6_decisions(decisions)
    store.persist_alpha_v6_labels(forged_labels)

    result = run_alpha_v6_learning(store, code_sha="c" * 40)

    assert result["dataset"]["row_count"] == 0
    assert result["model_run"]["eligibility"]["eligible_label_count"] == 0
    assert result["promotion_review"]["approved"] is False
    assert result["performance_status"] == "WAITING_FOR_FORWARD_EVIDENCE"
