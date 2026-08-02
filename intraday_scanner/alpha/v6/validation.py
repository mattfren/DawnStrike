"""Purged walk-forward validation and conservative research diagnostics."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from statistics import mean, pstdev
from typing import Any


def expanding_purged_splits(
    rows: list[dict[str, Any]], *, embargo_dates: int = 1, minimum_train_dates: int = 20
) -> list[dict[str, Any]]:
    """Create expanding folds whose training dates precede their test date."""

    if embargo_dates < 0 or minimum_train_dates < 1:
        raise ValueError("embargo_dates must be >= 0 and minimum_train_dates must be >= 1")
    dates = sorted(
        {str(row.get("market_date") or "")[:10] for row in rows if row.get("market_date")}
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


def evaluate_return_predictions(
    rows: list[dict[str, Any]], *, bootstrap_samples: int = 1_000
) -> dict[str, Any]:
    """Evaluate a frozen prediction set with selection-bias and leakage controls."""

    if bootstrap_samples < 100:
        raise ValueError("bootstrap_samples must be at least 100")
    usable = [
        row
        for row in rows
        if _number(row.get("utility_lcb_pct")) is not None
        and _number(row.get("realized_net_excess_return_pct")) is not None
    ]
    if not usable:
        return _empty()
    pairs = [
        (
            float(row["utility_lcb_pct"]),
            float(row["realized_net_excess_return_pct"]),
        )
        for row in usable
    ]
    realized = [pair[1] for pair in pairs]
    weights = [_weight(row) for row in usable]
    weighted_expectancy = _weighted_mean(realized, weights)
    wins = [(value, weight) for value, weight in zip(realized, weights, strict=True) if value > 0]
    losses = [(value, weight) for value, weight in zip(realized, weights, strict=True) if value < 0]
    gross_win = sum(value * weight for value, weight in wins)
    gross_loss = abs(sum(value * weight for value, weight in losses))
    drawdowns = _drawdowns(realized)
    concentration = _concentration(realized, weights)
    conditional_value_at_risk = _conditional_value_at_risk(realized, weights)
    top_count = max(1, len(pairs) // 10)
    top_indices = sorted(range(len(pairs)), key=lambda index: pairs[index][0], reverse=True)[
        :top_count
    ]
    top_mean = _weighted_mean(
        [realized[index] for index in top_indices],
        [weights[index] for index in top_indices],
    )
    stress_15 = _slippage_stress(usable, multiplier=1.5)
    stress_20 = _slippage_stress(usable, multiplier=2.0)
    bootstrap = _cluster_bootstrap_expectancy(usable, sample_count=bootstrap_samples, seed=6_001)
    sharpe = _annualized_observation_sharpe(realized)
    adjusted_sharpe = _multiple_testing_adjusted_sharpe(
        sharpe,
        sample_size=len(realized),
        trial_count=max(1, max(_int(row.get("experiment_trial_count"), 1) for row in usable)),
    )
    no_lookahead = all(
        row.get("no_lookahead") is True
        and (
            not row.get("training_max_market_date")
            or str(row["training_max_market_date"]) < str(row.get("market_date") or "")
        )
        for row in usable
    )
    return {
        "status": "EVALUABLE",
        "sample_size": len(pairs),
        "market_date_count": len({str(row.get("market_date") or "") for row in usable}),
        "after_cost_expectancy_pct": round(weighted_expectancy, 6),
        "benchmark_excess_return_pct": round(weighted_expectancy, 6),
        "profit_factor": round(gross_win / gross_loss, 6) if gross_loss else None,
        "maximum_drawdown_pct": round(min(drawdowns), 6),
        "conditional_value_at_risk_95_pct": conditional_value_at_risk,
        "downside_deviation_pct": _downside_deviation(realized, weights),
        "gain_loss_concentration_pct": round(concentration, 6),
        "turnover_observations_per_session": round(
            len(usable) / max(1, len({str(row.get("market_date") or "") for row in usable})),
            6,
        ),
        "capacity": _capacity_report(usable),
        "top_decile_lift_pct": round(top_mean - weighted_expectancy, 6),
        "rank_correlation": _rank_correlation(pairs),
        "slippage_stress": {
            "one_point_five_x_expectancy_pct": stress_15,
            "two_x_expectancy_pct": stress_20,
        },
        "bootstrap_expectancy_95_ci_pct": bootstrap,
        "annualized_observation_sharpe": _round(sharpe),
        "multiple_testing_adjusted_sharpe": _round(adjusted_sharpe),
        "shuffled_label_rank_correlation": _shuffled_negative_control(pairs),
        "segmented_performance": {
            key: _segments(usable, key)
            for key in ("regime_key", "source_key", "liquidity_bucket", "catalyst_bucket")
        },
        "selection_bias_correction": {
            "method": "inverse_probability_weighting",
            "weight_cap": 10.0,
            "sampled_row_count": sum(1 for weight in weights if weight > 1.0),
        },
        "no_lookahead_audit_passed": no_lookahead,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _empty() -> dict[str, Any]:
    return {
        "status": "NOT_EVALUABLE",
        "sample_size": 0,
        "market_date_count": 0,
        "after_cost_expectancy_pct": None,
        "benchmark_excess_return_pct": None,
        "profit_factor": None,
        "maximum_drawdown_pct": None,
        "conditional_value_at_risk_95_pct": None,
        "downside_deviation_pct": None,
        "gain_loss_concentration_pct": None,
        "turnover_observations_per_session": None,
        "capacity": {"status": "NOT_EVALUABLE", "median_capacity_dollars": None},
        "top_decile_lift_pct": None,
        "rank_correlation": None,
        "slippage_stress": {
            "one_point_five_x_expectancy_pct": None,
            "two_x_expectancy_pct": None,
        },
        "bootstrap_expectancy_95_ci_pct": {"lower": None, "upper": None},
        "annualized_observation_sharpe": None,
        "multiple_testing_adjusted_sharpe": None,
        "shuffled_label_rank_correlation": None,
        "segmented_performance": {},
        "selection_bias_correction": {
            "method": "inverse_probability_weighting",
            "weight_cap": 10.0,
            "sampled_row_count": 0,
        },
        "no_lookahead_audit_passed": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _drawdowns(values: list[float]) -> list[float]:
    equity = 1.0
    high = equity
    output: list[float] = []
    for value in values:
        equity *= max(0.0, 1.0 + value / 100.0)
        high = max(high, equity)
        output.append((equity / high - 1.0) * 100.0 if high else -100.0)
    return output


def _downside_deviation(values: list[float], weights: list[float]) -> float | None:
    denominator = sum(weights)
    if not denominator:
        return None
    value = sum(weight * min(0.0, item) ** 2 for item, weight in zip(values, weights, strict=True))
    return round((value / denominator) ** 0.5, 6)


def _concentration(values: list[float], weights: list[float]) -> float:
    contributions = [abs(value * weight) for value, weight in zip(values, weights, strict=True)]
    denominator = sum(contributions)
    return 100.0 * max(contributions) / denominator if denominator else 100.0


def _conditional_value_at_risk(values: list[float], weights: list[float]) -> float | None:
    """Return the inverse-probability weighted expected shortfall of the worst 5%.

    This is an observation-level research diagnostic, never a paper-account
    CVaR.  The return is null when the input is incomplete rather than silently
    assigning a zero tail loss.
    """

    if not values or len(values) != len(weights):
        return None
    tail_count = max(1, math.ceil(len(values) * 0.05))
    tail_indexes = sorted(range(len(values)), key=lambda index: values[index])[:tail_count]
    tail_values = [values[index] for index in tail_indexes]
    tail_weights = [weights[index] for index in tail_indexes]
    return round(_weighted_mean(tail_values, tail_weights), 6)


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
    return _rank_correlation(list(zip([pair[0] for pair in pairs], shuffled, strict=True)))


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        average_rank = (cursor + 1 + end) / 2.0
        for index in order[cursor:end]:
            result[index] = average_rank
        cursor = end
    return result


def _cluster_bootstrap_expectancy(
    rows: list[dict[str, Any]], *, sample_count: int, seed: int
) -> dict[str, float | None]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[str(row.get("market_date") or "unknown")].append(row)
    dates = sorted(by_date)
    if len(dates) < 2:
        return {"lower": None, "upper": None}
    generator = random.Random(seed)
    samples: list[float] = []
    for _ in range(sample_count):
        sampled = [generator.choice(dates) for _ in dates]
        values = [
            float(row["realized_net_excess_return_pct"])
            for market_date in sampled
            for row in by_date[market_date]
        ]
        weights = [_weight(row) for market_date in sampled for row in by_date[market_date]]
        samples.append(_weighted_mean(values, weights))
    samples.sort()
    return {
        "lower": round(_quantile(samples, 0.025), 6),
        "upper": round(_quantile(samples, 0.975), 6),
    }


def _slippage_stress(rows: list[dict[str, Any]], *, multiplier: float) -> float | None:
    stressed: list[float] = []
    weights: list[float] = []
    for row in rows:
        cost_bps = _number(row.get("estimated_round_trip_cost_bps"))
        actual = _number(row.get("realized_net_excess_return_pct"))
        if cost_bps is None or actual is None:
            continue
        stressed.append(actual - ((multiplier - 1.0) * cost_bps / 100.0))
        weights.append(_weight(row))
    return round(_weighted_mean(stressed, weights), 6) if stressed else None


def _segments(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "unknown")].append(row)
    output = []
    for value, group in sorted(groups.items()):
        returns = [float(row["realized_net_excess_return_pct"]) for row in group]
        weights = [_weight(row) for row in group]
        output.append(
            {
                "segment": value,
                "sample_size": len(group),
                "after_cost_expectancy_pct": round(_weighted_mean(returns, weights), 6),
                "positive": _weighted_mean(returns, weights) > 0,
            }
        )
    return output


def _capacity_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = sorted(
        value
        for row in rows
        if (value := _number(row.get("estimated_capacity_dollars"))) is not None
    )
    if not values:
        return {
            "status": "MISSING_CAPACITY_TRUTH",
            "sample_size": 0,
            "median_capacity_dollars": None,
        }
    return {
        "status": "EVALUABLE",
        "sample_size": len(values),
        "median_capacity_dollars": round(_quantile(values, 0.5), 2),
    }


def _annualized_observation_sharpe(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    deviation = pstdev(values)
    return mean(values) / deviation * math.sqrt(252.0) if deviation else None


def _multiple_testing_adjusted_sharpe(
    sharpe: float | None, *, sample_size: int, trial_count: int
) -> float | None:
    if sharpe is None or sample_size < 2:
        return None
    penalty = math.sqrt(2.0 * math.log(max(1, trial_count))) / math.sqrt(sample_size)
    return sharpe - penalty


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    denominator = sum(weights)
    return sum(value * weight for value, weight in zip(values, weights, strict=True)) / denominator


def _weight(row: dict[str, Any]) -> float:
    return min(10.0, max(1.0, _number(row.get("inverse_probability_weight")) or 1.0))


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("quantile requires values")
    index = probability * (len(values) - 1)
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return values[lower]
    fraction = index - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def _number(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _round(value: float | None) -> float | None:
    return round(value, 6) if value is not None and math.isfinite(value) else None


__all__ = ["evaluate_return_predictions", "expanding_purged_splits"]
