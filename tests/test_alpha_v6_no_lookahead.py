from __future__ import annotations

from intraday_scanner.alpha.v6.validation import (
    build_catalyst_ablation_views,
    catalyst_ablation_plan,
    expanding_purged_splits,
)
from intraday_scanner.v2.backtest.intraday_engine import (
    build_expanding_walk_forward_folds,
)


def test_v6_purged_expanding_splits_never_train_on_test_or_embargo_date() -> None:
    rows = [{"market_date": f"2026-01-{day:02d}"} for day in range(1, 28)]
    folds = expanding_purged_splits(rows, embargo_dates=1, minimum_train_dates=20)

    assert folds
    assert all(fold["no_lookahead"] is True for fold in folds)
    assert all(
        set(fold["training_dates"]).isdisjoint(fold["test_dates"] + fold["embargoed_dates"])
        for fold in folds
    )


def test_intraday_walk_forward_folds_keep_purge_and_embargo_out_of_training() -> None:
    rows = [f"2026-02-{day:02d}" for day in range(1, 28)]
    folds = build_expanding_walk_forward_folds(
        rows,
        minimum_training_sessions=10,
        validation_sessions=3,
        holdout_sessions=3,
        purge_sessions=1,
        embargo_sessions=1,
    )

    assert folds
    assert all(fold.no_lookahead for fold in folds)
    assert all(
        set(fold.training_dates).isdisjoint(
            set(fold.validation_dates)
            | set(fold.holdout_dates)
            | set(fold.purged_dates)
            | set(fold.embargoed_dates)
        )
        for fold in folds
    )


def test_catalyst_ablations_are_explicit_and_do_not_claim_dominance() -> None:
    rows = [
        {
            "decision_id": "d1",
            "catalyst_bucket": "sourced",
            "catalyst_feature_block": {"availability_status": "available"},
        },
        {"decision_id": "d2", "catalyst_bucket": "missing"},
    ]
    plan = catalyst_ablation_plan(rows)
    views = build_catalyst_ablation_views(rows)

    assert plan["dominant_catalyst_claim_allowed"] is False
    assert set(views) == {
        "full",
        "no_catalyst",
        "catalyst_only",
        "shuffled_negative_control",
    }
    assert views["no_catalyst"][0]["catalyst_bucket"] == "ablation_removed"
