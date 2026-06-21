"""Versioned Dawnstrike scoring equation.

The formula is a research-ranking model. It is intentionally explainable and does
not submit, recommend, or automate trades.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from intraday_scanner.config import ScannerConfig
from intraday_scanner.models import SnapshotRow

FORMULA_VERSION = "dawnstrike-v2.0"


@dataclass(frozen=True)
class FormulaResult:
    score: float
    gap_pct: float
    dollar_volume: float
    float_rotation_pct: float
    range_position_pct: float
    data_quality_score: float
    liquidity_tier: str
    setup_grade: str
    volatility_signature: str
    risk_flags: list[str]
    avoid_reasons: list[str]
    best_exit_bias: str
    score_breakdown: dict[str, float]


def evaluate_formula(row: SnapshotRow, config: ScannerConfig) -> FormulaResult:
    gap_pct = row.gap_pct if row.previous_close <= 0 and row.gap_pct else _gap_pct(
        row.premarket_price, row.previous_close
    )
    dollar_volume = row.premarket_price * row.premarket_volume
    float_rotation_pct = _float_rotation_pct(row)
    range_position_pct = _range_position_pct(row)
    data_quality_score = _data_quality_score(row)
    risk_flags, avoid_reasons = _risk_flags(row, gap_pct, dollar_volume, config)

    components = {
        "gap_curve": _gap_curve_score(gap_pct, config) * config.score_weight_gap,
        "liquidity_thrust": (
            _liquidity_thrust_score(dollar_volume, row.premarket_volume)
            * config.score_weight_liquidity
        ),
        "float_rotation": (
            _float_rotation_score(float_rotation_pct, row.float_shares)
            * config.score_weight_float_rotation
        ),
        "range_control": (
            _range_control_score(row, range_position_pct) * config.score_weight_range
        ),
        "squeeze_catalyst": _squeeze_catalyst_score(row) * config.score_weight_catalyst,
        "execution_quality": _execution_quality_score(row, config)
        * config.score_weight_execution,
        "data_quality": data_quality_score * config.score_weight_data_quality,
    }
    risk_penalty = (
        _risk_penalty(row, gap_pct, dollar_volume, data_quality_score, config)
        * config.score_weight_risk_penalty
    )
    raw_score = sum(components.values()) - risk_penalty
    score = round(_clamp(raw_score, 0, 100), 2)
    setup_grade = _setup_grade(score, avoid_reasons)
    volatility_signature = _volatility_signature(
        row, gap_pct, range_position_pct, dollar_volume, config
    )
    components["risk_penalty"] = round(risk_penalty, 2)

    return FormulaResult(
        score=score,
        gap_pct=round(gap_pct, 2),
        dollar_volume=round(dollar_volume, 2),
        float_rotation_pct=round(float_rotation_pct, 2),
        range_position_pct=round(range_position_pct, 2),
        data_quality_score=round(data_quality_score, 2),
        liquidity_tier=_liquidity_tier(dollar_volume, row.premarket_volume),
        setup_grade=setup_grade,
        volatility_signature=volatility_signature,
        risk_flags=risk_flags,
        avoid_reasons=avoid_reasons,
        best_exit_bias=_exit_bias(score, gap_pct, risk_flags, avoid_reasons, range_position_pct),
        score_breakdown={key: round(value, 2) for key, value in components.items()},
    )


def _gap_pct(price: float, previous_close: float) -> float:
    if previous_close <= 0:
        return 0.0
    return ((price - previous_close) / previous_close) * 100


def _gap_curve_score(gap_pct: float, config: ScannerConfig) -> float:
    if gap_pct <= 0:
        return 0.0
    if gap_pct < config.min_gap_pct:
        return _clamp((gap_pct / config.min_gap_pct) * 7, 0, 7)
    if gap_pct < config.ideal_gap_low_pct:
        span = config.ideal_gap_low_pct - config.min_gap_pct
        return 7 + ((gap_pct - config.min_gap_pct) / span) * 11
    if gap_pct <= config.ideal_gap_high_pct:
        return 18.0
    if gap_pct <= config.max_credible_gap_pct:
        span = config.max_credible_gap_pct - config.ideal_gap_high_pct
        return 18 - ((gap_pct - config.ideal_gap_high_pct) / span) * 10
    return 4.0


def _liquidity_thrust_score(dollar_volume: float, share_volume: int) -> float:
    dollar_score = 0.0
    share_score = 0.0
    if dollar_volume > 0:
        dollar_score = _clamp((math.log10(dollar_volume) - 5.4) * 6.0, 0, 14)
    if share_volume > 0:
        share_score = _clamp((math.log10(share_volume) - 5.0) * 4.0, 0, 6)
    return dollar_score + share_score


def _float_rotation_pct(row: SnapshotRow) -> float:
    if row.float_shares is None or row.float_shares <= 0:
        return 0.0
    return (row.premarket_volume / row.float_shares) * 100


def _float_rotation_score(float_rotation_pct: float, float_shares: float | None) -> float:
    if float_shares is None or float_shares <= 0:
        return 3.0
    return _clamp((float_rotation_pct / 8.0) * 14, 0, 14)


def _range_position_pct(row: SnapshotRow) -> float:
    price_range = row.premarket_high - row.premarket_low
    if price_range <= 0:
        return 50.0
    return _clamp(((row.premarket_price - row.premarket_low) / price_range) * 100, 0, 100)


def _range_control_score(row: SnapshotRow, range_position_pct: float) -> float:
    position_score = _clamp((range_position_pct / 100) * 11, 0, 11)
    range_width_pct = _range_width_pct(row)
    width_penalty = _clamp((range_width_pct - 45) * 0.18, 0, 4)
    return position_score + (4 - width_penalty)


def _squeeze_catalyst_score(row: SnapshotRow) -> float:
    news_score = 5.0 if row.has_news else 0.0
    short_score = _clamp(((row.short_float_pct or 0) / 30) * 5, 0, 5)
    scarcity_score = 0.0
    if row.float_shares is None or row.float_shares <= 0:
        scarcity_score = 0.75
    elif row.float_shares <= 20_000_000:
        scarcity_score = 3.0
    elif row.float_shares <= 50_000_000:
        scarcity_score = 2.0
    elif row.float_shares <= 100_000_000:
        scarcity_score = 1.0
    return news_score + short_score + scarcity_score


def _execution_quality_score(row: SnapshotRow, config: ScannerConfig) -> float:
    spread_score = _clamp(10 - (row.spread_pct * 1.8), 0, 10)
    price_band_score = 2.0 if config.min_price <= row.premarket_price <= config.max_price else 0.0
    return spread_score + price_band_score


def _data_quality_score(row: SnapshotRow) -> float:
    checks = [
        row.previous_close > 0,
        row.premarket_price > 0,
        row.premarket_high >= row.premarket_low,
        row.premarket_volume > 0,
        row.float_shares is not None and row.float_shares > 0,
        row.market_cap is not None and row.market_cap > 0,
        row.short_float_pct is not None,
        bool(row.as_of_timestamp),
    ]
    return (sum(1 for passed in checks if passed) / len(checks)) * 8


def _risk_flags(
    row: SnapshotRow, gap_pct: float, dollar_volume: float, config: ScannerConfig
) -> tuple[list[str], list[str]]:
    risk_flags: list[str] = []
    avoid_reasons: list[str] = []

    if row.previous_close <= 0:
        risk_flags.append("no_previous_close")
        if row.gap_pct <= 0:
            avoid_reasons.append("no_previous_close")
    if row.premarket_volume <= 0:
        risk_flags.append("zero_volume")
        avoid_reasons.append("zero_volume")
    if row.current_halt:
        risk_flags.append("current_halt")
        avoid_reasons.append("current_halt")
    if row.recent_offering:
        risk_flags.append("recent_offering")
        avoid_reasons.append("recent_offering")
    if row.reverse_split_90d:
        risk_flags.append("reverse_split_90d")
    if row.spread_pct >= config.wide_spread_pct:
        risk_flags.append("wide_spread")
    if row.premarket_price < config.min_price:
        risk_flags.append("sub_min_price")
        avoid_reasons.append("price_below_min")
    if row.premarket_price > config.max_price:
        risk_flags.append("above_max_price")
        avoid_reasons.append("price_above_max")
    if gap_pct < config.min_gap_pct:
        avoid_reasons.append("gap_below_min")
    if dollar_volume < config.min_premarket_dollar_volume:
        avoid_reasons.append("low_dollar_volume")
    if row.premarket_volume < config.min_premarket_share_volume:
        avoid_reasons.append("low_share_volume")
    if gap_pct > config.max_credible_gap_pct:
        risk_flags.append("extreme_gap_above_300_pct")
    return risk_flags, avoid_reasons


def _risk_penalty(
    row: SnapshotRow,
    gap_pct: float,
    dollar_volume: float,
    data_quality_score: float,
    config: ScannerConfig,
) -> float:
    penalty = 0.0
    if row.current_halt:
        penalty += 95
    if row.recent_offering:
        penalty += 28
    if row.reverse_split_90d:
        penalty += 8
    if row.previous_close <= 0 and row.gap_pct <= 0:
        penalty += 35
    if row.premarket_volume <= 0:
        penalty += 30
    if row.spread_pct >= config.wide_spread_pct:
        penalty += min(row.spread_pct * 2.5, 25)
    if row.premarket_price < config.min_price:
        penalty += 18
    if row.premarket_price > config.max_price:
        penalty += 12
    if dollar_volume < config.min_premarket_dollar_volume:
        penalty += 12
    if row.premarket_volume < config.min_premarket_share_volume:
        penalty += 8
    if gap_pct > config.max_credible_gap_pct:
        penalty += 12
    if gap_pct > config.max_credible_gap_pct * 2:
        penalty += 12
    if data_quality_score < 5:
        penalty += 8
    return penalty


def _liquidity_tier(dollar_volume: float, share_volume: int) -> str:
    if dollar_volume >= 25_000_000 and share_volume >= 2_000_000:
        return "institutional_liquidity"
    if dollar_volume >= 7_500_000 and share_volume >= 750_000:
        return "high_liquidity"
    if dollar_volume >= 1_000_000 and share_volume >= 250_000:
        return "watchable_liquidity"
    if dollar_volume >= 500_000 and share_volume >= 100_000:
        return "thin_liquidity"
    return "illiquid"


def _setup_grade(score: float, avoid_reasons: list[str]) -> str:
    if avoid_reasons:
        return "AVOID"
    if score >= 90:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    return "D"


def _volatility_signature(
    row: SnapshotRow,
    gap_pct: float,
    range_position_pct: float,
    dollar_volume: float,
    config: ScannerConfig,
) -> str:
    if row.current_halt:
        return "halted_event_risk"
    if row.spread_pct >= config.wide_spread_pct:
        return "wide_spread_chop"
    if dollar_volume < config.min_premarket_dollar_volume:
        return "thin_liquidity_spike"
    if gap_pct > config.max_credible_gap_pct:
        return "extreme_gap_squeeze"
    if range_position_pct >= 80 and gap_pct >= config.ideal_gap_low_pct:
        return "high_tight_momentum"
    if _range_width_pct(row) >= 45:
        return "wide_range_momentum"
    return "premarket_momentum"


def _exit_bias(
    score: float,
    gap_pct: float,
    risk_flags: list[str],
    avoid_reasons: list[str],
    range_position_pct: float,
) -> str:
    if avoid_reasons or "current_halt" in risk_flags or "recent_offering" in risk_flags:
        return "avoid"
    if gap_pct > 180 or "extreme_gap_above_300_pct" in risk_flags:
        return "lunch"
    if score >= 82 and range_position_pct >= 75:
        return "trail_to_close"
    if score >= 70:
        return "scale_lunch_then_close"
    return "lunch_or_close"


def _range_width_pct(row: SnapshotRow) -> float:
    if row.premarket_price <= 0:
        return 0.0
    return ((row.premarket_high - row.premarket_low) / row.premarket_price) * 100


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
