from __future__ import annotations

from intraday_scanner.alpha.v6.models import model_eligibility
from intraday_scanner.alpha.v6.training import train_shadow_challengers
from intraday_scanner.services.alpha_v6_learning_service import run_alpha_v6_learning
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


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


def test_v6_full_learning_service_persists_untrained_evidence_without_fake_returns(
    tmp_path,
) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    result = run_alpha_v6_learning(store, code_sha="c" * 40)

    assert result["status"] == "NOT_TRAINED_INSUFFICIENT_LABELS"
    assert result["dataset"]["row_count"] == 0
    assert result["promotion_review"]["approved"] is False
    assert result["performance_status"] == "WAITING_FOR_FORWARD_EVIDENCE"
