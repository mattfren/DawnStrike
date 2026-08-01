"""Purged date-grouped walk-forward contracts with negative controls."""

from __future__ import annotations

import random
from statistics import mean
from typing import Any


def expanding_purged_splits(
    rows: list[dict[str, Any]], *, embargo_dates: int = 1, minimum_train_dates: int = 20
) -> list[dict[str, Any]]:
    """Create expanding folds whose training dates precede their test date.

    An embargo is expressed in observed market dates, not calendar days.  This
    avoids accidental same-day or adjacent-session target leakage.
    """

    if embargo_dates < 0 or minimum_train_dates < 1:
        raise ValueError("embargo_dates must be >= 0 and minimum_train_dates must be >= 1")
    dates = sorted(
        {
            str(row.get("market_date") or "")[:10]
            for row in rows
            if row.get("market_date")
        }
    )
    folds: list[dict[str, Any]] = []
    for test_index in range(minimum_train_dates + embargo_dates, len(dates)):
        test_date = dates[test_index]
        training_dates = dates[: test_index - embargo_dates]
        if len(training_dates) < minimum_train_dates:
            continue
        folds.append(
            {
                "fold_id": f"v6-fold-{test_date}",
                "training_dates": training_dates,
                "test_dates": [test_date],
                "embargoed_dates": dates[test_index - embargo_dates : test_index],
                "no_lookahead": max(training_dates) < test_date,
            }
        )
    return folds


def evaluate_return_predictions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return auditable, after-cost metrics for an already-frozen prediction set."""

    pairs = [
        (float(row["utility_lcb_pct"]), float(row["realized_net_excess_return_pct"]))
        for row in rows
        if _number(row.get("utility_lcb_pct")) is not None
        and _number(row.get("realized_net_excess_return_pct")) is not None
    ]
    if not pairs:
        return _empty()
    realized = [actual for _, actual in pairs]
    wins = [value for value in realized if value > 0]
    losses = [value for value in realized if value < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    equity = 1.0
    high = equity
    drawdowns: list[float] = []
    for value in realized:
        equity *= 1.0 + value / 100.0
        high = max(high, equity)
        drawdowns.append((equity / high - 1.0) * 100.0)
    concentration = max(abs(value) for value in realized) / sum(
        abs(value) for value in realized
    ) * 100.0
    top_count = max(1, len(pairs) // 10)
    top = sorted(pairs, key=lambda pair: pair[0], reverse=True)[:top_count]
    top_mean = mean(pair[1] for pair in top)
    overall_mean = mean(realized)
    return {
        "status": "EVALUABLE",
        "sample_size": len(pairs),
        "after_cost_expectancy_pct": round(overall_mean, 6),
        "profit_factor": round(gross_win / gross_loss, 6) if gross_loss else None,
        "maximum_drawdown_pct": round(min(drawdowns), 6),
        "downside_deviation_pct": _downside_deviation(realized),
        "gain_loss_concentration_pct": round(concentration, 6),
        "top_decile_lift_pct": round(top_mean - overall_mean, 6),
        "rank_correlation": _rank_correlation(pairs),
        "shuffled_label_rank_correlation": _shuffled_negative_control(pairs),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _empty() -> dict[str, Any]:
    return {
        "status": "NOT_EVALUABLE",
        "sample_size": 0,
        "after_cost_expectancy_pct": None,
        "profit_factor": None,
        "maximum_drawdown_pct": None,
        "downside_deviation_pct": None,
        "gain_loss_concentration_pct": None,
        "top_decile_lift_pct": None,
        "rank_correlation": None,
        "shuffled_label_rank_correlation": None,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _downside_deviation(values: list[float]) -> float | None:
    downside = [min(0.0, value) for value in values]
    if not downside:
        return None
    return round((sum(value * value for value in downside) / len(downside)) ** 0.5, 6)


def _rank_correlation(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    predicted_rank = _ranks([pair[0] for pair in pairs])
    actual_rank = _ranks([pair[1] for pair in pairs])
    mean_predicted = mean(predicted_rank)
    mean_actual = mean(actual_rank)
    numerator = sum(
        (predicted - mean_predicted) * (actual - mean_actual)
        for predicted, actual in zip(predicted_rank, actual_rank, strict=True)
    )
    left = sum((value - mean_predicted) ** 2 for value in predicted_rank) ** 0.5
    right = sum((value - mean_actual) ** 2 for value in actual_rank) ** 0.5
    return round(numerator / (left * right), 6) if left and right else None


def _shuffled_negative_control(pairs: list[tuple[float, float]]) -> float | None:
    shuffled = [pair[1] for pair in pairs]
    random.Random(0).shuffle(shuffled)
    return _rank_correlation(
        list(zip([pair[0] for pair in pairs], shuffled, strict=True))
    )


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    for rank, index in enumerate(order, 1):
        result[index] = float(rank)
    return result


def _number(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


__all__ = ["evaluate_return_predictions", "expanding_purged_splits"]
