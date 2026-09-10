from __future__ import annotations

from intraday_scanner.alpha.edge_calibrator import calibrate_edge
from intraday_scanner.alpha.outcome_semantics import account_equity_drawdown, realized_return
from intraday_scanner.alpha.performance_truth import build_truth_report
from intraday_scanner.alpha.setup_memory import build_setup_memory
from intraday_scanner.alpha.v6.contracts import point_in_time_valid
from intraday_scanner.alpha.v6.validation import expanding_purged_splits


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
