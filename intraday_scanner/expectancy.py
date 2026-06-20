"""Expected paper-return model for scanned candidates.

This is an empirical research model, not a trading recommendation. Confidence is
intentionally capped when paper-audit sample sizes are small.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

MODEL_VERSION = "expectancy-v1.0"


@dataclass(frozen=True)
class ExpectancyEstimate:
    ticker: str
    expected_return_pct: float
    confidence_pct: float
    lower_return_pct: float
    upper_return_pct: float
    win_probability_pct: float
    risk_adjusted_return_pct: float
    uncertainty_width_pct: float
    downside_risk_pct: float
    sample_size: int
    effective_sample_size: float
    target_exit: str
    model_basis: str
    confidence_tier: str
    next_confidence_step: str
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "expected_return_pct": self.expected_return_pct,
            "confidence_pct": self.confidence_pct,
            "lower_return_pct": self.lower_return_pct,
            "upper_return_pct": self.upper_return_pct,
            "win_probability_pct": self.win_probability_pct,
            "risk_adjusted_return_pct": self.risk_adjusted_return_pct,
            "uncertainty_width_pct": self.uncertainty_width_pct,
            "downside_risk_pct": self.downside_risk_pct,
            "sample_size": self.sample_size,
            "effective_sample_size": self.effective_sample_size,
            "target_exit": self.target_exit,
            "model_basis": self.model_basis,
            "confidence_tier": self.confidence_tier,
            "next_confidence_step": self.next_confidence_step,
            "explanation": self.explanation,
        }


def estimate_expectancy(
    candidate_rows: list[dict[str, Any]],
    audit_rows: list[dict[str, Any]],
) -> list[ExpectancyEstimate]:
    candidate_by_ticker = {str(row.get("ticker", "")).upper(): row for row in candidate_rows}
    estimates = [
        _estimate_one(candidate, audit_rows, candidate_by_ticker)
        for candidate in candidate_rows
        if not str(candidate.get("avoid_reasons", "")).strip()
    ]
    return sorted(estimates, key=lambda item: item.expected_return_pct, reverse=True)


def _estimate_one(
    candidate: dict[str, Any],
    audit_rows: list[dict[str, Any]],
    candidate_by_ticker: dict[str, dict[str, Any]],
) -> ExpectancyEstimate:
    ticker = str(candidate.get("ticker", "")).upper()
    target_exit = _target_exit(candidate)
    prior_mean = _prior_return(candidate)
    prior_weight = 4.0
    weighted_returns: list[tuple[float, float]] = [(prior_mean, prior_weight)]
    empirical_weights: list[float] = []
    empirical_count = 0

    for audit in audit_rows:
        audit_ticker = str(audit.get("ticker", "")).upper()
        audit_candidate = candidate_by_ticker.get(audit_ticker)
        if audit_candidate is None:
            continue
        realized_return = _target_return(audit, target_exit)
        weight = _similarity_weight(candidate, audit_candidate)
        if audit_ticker == ticker:
            weight += 6.0
        weighted_returns.append((realized_return, weight))
        empirical_weights.append(weight)
        empirical_count += 1

    expected = _weighted_mean(weighted_returns)
    deviation = max(_weighted_std(weighted_returns, expected), 6.0)
    total_effective_n = _effective_n([weight for _, weight in weighted_returns])
    empirical_effective_n = _effective_n(empirical_weights)
    standard_error = deviation / math.sqrt(max(total_effective_n, 1.0))
    margin = (1.645 * standard_error) + _sparse_sample_margin(empirical_count)
    lower = expected - margin
    upper = expected + margin
    confidence = _confidence(candidate, deviation, empirical_count, empirical_effective_n)
    win_probability = _win_probability(weighted_returns)
    basis = _basis(empirical_count)
    uncertainty_width = upper - lower
    downside_risk = abs(min(lower, 0.0))
    risk_adjusted_return = expected * (confidence / 100.0)
    confidence_tier = _confidence_tier(confidence)
    next_step = _next_confidence_step(empirical_count, empirical_effective_n, uncertainty_width)
    explanation = _explanation(
        candidate,
        expected,
        confidence,
        lower,
        upper,
        basis,
        confidence_tier,
        next_step,
    )

    return ExpectancyEstimate(
        ticker=ticker,
        expected_return_pct=round(expected, 2),
        confidence_pct=round(confidence, 1),
        lower_return_pct=round(lower, 2),
        upper_return_pct=round(upper, 2),
        win_probability_pct=round(win_probability, 1),
        risk_adjusted_return_pct=round(risk_adjusted_return, 2),
        uncertainty_width_pct=round(uncertainty_width, 2),
        downside_risk_pct=round(downside_risk, 2),
        sample_size=empirical_count,
        effective_sample_size=round(empirical_effective_n, 2),
        target_exit=target_exit,
        model_basis=basis,
        confidence_tier=confidence_tier,
        next_confidence_step=next_step,
        explanation=explanation,
    )


def _target_exit(candidate: dict[str, Any]) -> str:
    bias = str(candidate.get("best_exit_bias", "")).lower()
    if bias == "trail_to_close":
        return "close"
    if bias == "scale_lunch_then_close":
        return "blend"
    if bias == "lunch":
        return "lunch"
    return "blend"


def _target_return(audit: dict[str, Any], target_exit: str) -> float:
    lunch = _number(audit.get("lunch_return_pct"))
    close = _number(audit.get("close_return_pct"))
    if target_exit == "close":
        return close
    if target_exit == "lunch":
        return lunch
    return (lunch + close) / 2


def _prior_return(candidate: dict[str, Any]) -> float:
    score = _number(candidate.get("score"))
    range_position = _number(candidate.get("range_position_pct"))
    data_quality = _number(candidate.get("data_quality_score"))
    score_edge = (score - 50.0) * 0.08
    range_edge = (range_position - 50.0) * 0.025
    data_edge = (data_quality - 6.0) * 0.20
    liquidity_edge = {
        "high_liquidity": 0.8,
        "watchable_liquidity": 0.25,
        "thin_liquidity": -0.75,
        "illiquid": -1.5,
    }.get(str(candidate.get("liquidity_tier", "")).lower(), 0.0)
    risk_penalty = 0.75 * len(_tokens(candidate.get("risk_flags")))
    avoid_penalty = 2.5 if str(candidate.get("avoid_reasons", "")).strip() else 0.0
    raw_prior = score_edge + range_edge + data_edge + liquidity_edge - risk_penalty - avoid_penalty
    return _clamp(raw_prior, -8, 8)


def _similarity_weight(candidate: dict[str, Any], audited: dict[str, Any]) -> float:
    score_gap = abs(_number(candidate.get("score")) - _number(audited.get("score")))
    gap_gap = abs(_number(candidate.get("gap_pct")) - _number(audited.get("gap_pct")))
    rotation_gap = abs(
        _number(candidate.get("float_rotation_pct")) - _number(audited.get("float_rotation_pct"))
    )
    score_similarity = math.exp(-score_gap / 28.0)
    gap_similarity = math.exp(-gap_gap / 130.0)
    rotation_similarity = math.exp(-rotation_gap / 18.0)
    signature_boost = (
        1.35
        if candidate.get("volatility_signature") == audited.get("volatility_signature")
        else 0.85
    )
    liquidity_boost = (
        1.15 if candidate.get("liquidity_tier") == audited.get("liquidity_tier") else 0.95
    )
    combined = (
        score_similarity
        * gap_similarity
        * rotation_similarity
        * signature_boost
        * liquidity_boost
    )
    return max(combined, 0.05)


def _confidence(
    candidate: dict[str, Any],
    deviation: float,
    empirical_count: int,
    empirical_effective_n: float,
) -> float:
    sample_confidence = 1.0 - math.exp(-empirical_effective_n / 18.0)
    data_confidence = _clamp(_number(candidate.get("data_quality_score")) / 8.0, 0.0, 1.0)
    dispersion_confidence = 1.0 / (1.0 + (deviation / 14.0))
    score_confidence = _clamp(_number(candidate.get("score")) / 100.0, 0.0, 1.0)
    raw = (
        0.50 * sample_confidence
        + 0.20 * data_confidence
        + 0.20 * dispersion_confidence
        + 0.10 * score_confidence
    ) * 100
    return min(raw, _sample_size_cap(empirical_count))


def _sample_size_cap(empirical_count: int) -> float:
    if empirical_count == 0:
        return 18.0
    if empirical_count < 5:
        return 38.0
    if empirical_count < 20:
        return 58.0
    if empirical_count < 50:
        return 74.0
    return 88.0


def _win_probability(weighted_returns: list[tuple[float, float]]) -> float:
    total_weight = sum(weight for _, weight in weighted_returns)
    if total_weight <= 0:
        return 0.0
    positive_weight = sum(weight for value, weight in weighted_returns if value > 0)
    return (positive_weight / total_weight) * 100


def _basis(empirical_count: int) -> str:
    if empirical_count == 0:
        return "score prior only"
    if empirical_count < 5:
        return "sparse paper audit"
    if empirical_count < 20:
        return "early calibration"
    return "empirical calibration"


def _confidence_tier(confidence: float) -> str:
    if confidence < 25:
        return "exploratory"
    if confidence < 40:
        return "sparse but usable"
    if confidence < 60:
        return "early calibration"
    if confidence < 75:
        return "moderate confidence"
    return "high confidence"


def _next_confidence_step(
    empirical_count: int,
    empirical_effective_n: float,
    uncertainty_width: float,
) -> str:
    if empirical_count == 0:
        return "Run paper audits for this scanner output."
    if empirical_count < 5:
        return f"Add {5 - empirical_count} more audited setups to leave sparse-sample mode."
    if empirical_count < 20:
        return f"Add {20 - empirical_count} more audited setups for early calibration."
    if empirical_count < 50:
        return f"Add {50 - empirical_count} more audited setups for stronger calibration."
    if empirical_effective_n < 20:
        return "Add more setups similar to today's candidate profile."
    if uncertainty_width > 18:
        return "Reduce range width by collecting more same-signature outcomes."
    return "Keep auditing; the model is now mainly improved by broader market regimes."


def _explanation(
    candidate: dict[str, Any],
    expected: float,
    confidence: float,
    lower: float,
    upper: float,
    basis: str,
    confidence_tier: str,
    next_step: str,
) -> str:
    ticker = str(candidate.get("ticker", "This name")).upper()
    return (
        f"{ticker} expectancy is {expected:.2f}% with {confidence:.1f}% model confidence. "
        f"The likely paper-return band is {lower:.2f}% to {upper:.2f}%. "
        f"Basis: {basis}; tier: {confidence_tier}. {next_step}"
    )


def _weighted_mean(values: list[tuple[float, float]]) -> float:
    total_weight = sum(weight for _, weight in values)
    if total_weight <= 0:
        return 0.0
    return sum(value * weight for value, weight in values) / total_weight


def _weighted_std(values: list[tuple[float, float]], mean: float) -> float:
    total_weight = sum(weight for _, weight in values)
    if total_weight <= 0:
        return 0.0
    variance = sum(weight * ((value - mean) ** 2) for value, weight in values) / total_weight
    return math.sqrt(max(variance, 0.0))


def _effective_n(weights: list[float]) -> float:
    if not weights:
        return 0.0
    squared_sum = sum(weights) ** 2
    sum_squares = sum(weight**2 for weight in weights)
    if sum_squares <= 0:
        return 0.0
    return squared_sum / sum_squares


def _sparse_sample_margin(empirical_count: int) -> float:
    return 10.0 / math.sqrt(max(empirical_count, 1))


def _tokens(value: Any) -> list[str]:
    return [token.strip() for token in str(value or "").split(";") if token.strip()]


def _number(value: Any) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(str(value).replace("$", "").replace(",", ""))
    except ValueError:
        return 0.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
