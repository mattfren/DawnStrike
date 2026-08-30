from __future__ import annotations

import pytest

from intraday_scanner.alpha.v6.experiment_ledger import build_trial_receipt
from intraday_scanner.alpha.v6.validation import (
    evaluate_return_predictions,
    purged_expanding_splits,
)
from intraday_scanner.errors import StorageError
from intraday_scanner.performance.account_contract import (
    evaluate_expected_account_sessions,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _experiment() -> dict[str, object]:
    return {
        "experiment_id": "v6x-governance",
        "configuration_hash_sha256": "a" * 64,
        "status": "REGISTERED_NOT_APPLIED",
        "frozen_windows": {
            "training": {
                "start": "2026-01-01",
                "end": "2026-01-20",
                "cutoff": "2026-01-20",
                "market_dates": [],
            },
            "validation": {"start": "2026-02-01", "end": "2026-02-10", "market_dates": []},
            "untouched_holdout": {"start": "2026-03-01", "end": "2026-03-10", "market_dates": []},
        },
    }


def test_trial_ledger_assigns_global_and_experiment_counts(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "trials.sqlite")
    experiment = _experiment()
    for index in range(2):
        receipt = build_trial_receipt(
            attempt_id=f"weekly-2026-08-30-{index}",
            experiment=experiment,
            arm_id="candidate",
            strategy_id="alphaops_v6",
            strategy_version="v6",
            configuration_hash_sha256="a" * 64,
            feature_set_hash_sha256="b" * 64,
            cost_model_version="cost-v1",
            validation_window=experiment["frozen_windows"],
            code_sha="c" * 40,
            source_hash_sha256="d" * 64,
        )
        assert store.persist_alpha_v6_trial(receipt) is True
    counts = store.alpha_v6_trial_counts(experiment_id="v6x-governance")
    assert counts == {"global_attempt_count": 2, "experiment_attempt_count": 2}
    assert [row["trial_number"] for row in store.load_alpha_v6_trials()] == [2, 1]


def test_trial_retry_is_idempotent_but_distinct_attempt_identity_is_counted(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "trials.sqlite")
    experiment = _experiment()
    common = {
        "experiment": experiment,
        "arm_id": "candidate",
        "strategy_id": "alphaops_v6",
        "strategy_version": "v6",
        "configuration_hash_sha256": "a" * 64,
        "feature_set_hash_sha256": "b" * 64,
        "cost_model_version": "cost-v1",
        "validation_window": experiment["frozen_windows"],
        "code_sha": "c" * 40,
        "source_hash_sha256": "d" * 64,
    }
    first = build_trial_receipt(attempt_id="run-001", **common)
    second = build_trial_receipt(attempt_id="run-002", **common)

    assert store.persist_alpha_v6_trial(first) is True
    assert store.persist_alpha_v6_trial(dict(first)) is False
    assert store.persist_alpha_v6_trial(second) is True
    assert store.alpha_v6_trial_counts(experiment_id="v6x-governance") == {
        "global_attempt_count": 2,
        "experiment_attempt_count": 2,
    }


def test_trial_store_rejects_caller_ordinal_and_tampering(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "trials.sqlite")
    experiment = _experiment()
    receipt = build_trial_receipt(
        attempt_id="run-001",
        experiment=experiment,
        arm_id="candidate",
        strategy_id="alphaops_v6",
        strategy_version="v6",
        configuration_hash_sha256="a" * 64,
        feature_set_hash_sha256="b" * 64,
        cost_model_version="cost-v1",
        validation_window=experiment["frozen_windows"],
        code_sha="c" * 40,
        source_hash_sha256="d" * 64,
    )
    forged_ordinal = {**receipt, "trial_number": 99}
    tampered = {**receipt, "strategy_version": "forged"}

    with pytest.raises(StorageError, match="caller_assigned_trial_number_forbidden"):
        store.persist_alpha_v6_trial(forged_ordinal)
    with pytest.raises(StorageError, match="trial_identity_mismatch"):
        store.persist_alpha_v6_trial(tampered)


def test_trial_count_missing_does_not_become_one() -> None:
    metrics = evaluate_return_predictions(
        [
            {
                "market_date": "2026-01-01",
                "utility_lcb_pct": 1.0,
                "realized_net_excess_return_pct": 1.0,
            }
        ],
        require_durable_trial_count=True,
        bootstrap_samples=100,
    )
    assert metrics["status"] == "NOT_EVALUABLE_TRIAL_COUNT_MISSING"
    assert metrics["multiple_testing"]["trial_count"] is None


def test_interval_purge_removes_overlapping_label_rows() -> None:
    rows = []
    for index in range(21):
        day = f"2026-01-{index + 1:02d}"
        rows.append(
            {
                "decision_id": f"d{index}",
                "market_date": day,
                "entry_at": f"{day}T14:00:00+00:00",
                "exit_at": f"{day}T20:00:00+00:00",
            }
        )
    rows.append(
        {
            "decision_id": "overlap",
            "market_date": "2026-01-19",
            "entry_at": "2026-01-19T19:00:00+00:00",
            "exit_at": "2026-01-21T15:00:00+00:00",
        }
    )
    folds = purged_expanding_splits(rows, minimum_train_dates=5, max_holding_horizon_minutes=390)
    assert folds
    assert any("overlap" in fold["purged_training_decision_ids"] for fold in folds)


def test_expected_calendar_missing_and_authoritative_no_trade_block() -> None:
    expected = [
        {"session_id": "XNYS:2026-01-01", "market_date": "2026-01-01"},
        {"session_id": "XNYS:2026-01-02", "market_date": "2026-01-02"},
    ]
    result = evaluate_expected_account_sessions(
        expected_sessions=expected,
        observed_sessions=[
            {
                "account_id": "paper",
                "market_date": "2026-01-01",
                "status": "NO_TRADE",
                "no_trade": True,
                "authoritative_receipt": {"receipt_id": "r1"},
            }
        ],
        account_id="paper",
    )
    assert result["status"] == "NOT_EVALUABLE_ACCOUNT_SESSION_COMPLETENESS"
    assert result["rows"][0]["net_return"] == "0"
    assert result["rows"][1]["net_return"] is None
