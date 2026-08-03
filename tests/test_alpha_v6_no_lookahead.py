from __future__ import annotations

from intraday_scanner.alpha.v6.validation import expanding_purged_splits


def test_v6_purged_expanding_splits_never_train_on_test_or_embargo_date() -> None:
    rows = [{"market_date": f"2026-01-{day:02d}"} for day in range(1, 28)]
    folds = expanding_purged_splits(rows, embargo_dates=1, minimum_train_dates=20)

    assert folds
    assert all(fold["no_lookahead"] is True for fold in folds)
    assert all(
        set(fold["training_dates"]).isdisjoint(fold["test_dates"] + fold["embargoed_dates"])
        for fold in folds
    )
