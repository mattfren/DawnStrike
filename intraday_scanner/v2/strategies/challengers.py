"""Additive, research-only strategy challengers.

This module deliberately sits beside the frozen v1 catalog.  Challengers only
add point-in-time eligibility gates; they never alter a v1 specification or
route a signal to an execution surface.  Every gate is recorded so a rejected
candidate remains explainable rather than becoming an unexplained ``None``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from statistics import mean
from typing import Any

from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.indicators import (
    atr,
    donchian_high,
    rate_of_change,
    rolling_volatility,
    sma,
)
from intraday_scanner.v2.strategies.catalog import build_strategy_catalog as build_legacy_catalog
from intraday_scanner.v2.strategies.models import StrategySignal, StrategySpec

PASS = "PASS"
FAIL = "FAIL"
UNAVAILABLE_REQUIRED_DATA = "UNAVAILABLE_REQUIRED_DATA"
CHALLENGER_VERSION = "v1.1-challenger-20260821"
_GATE_POLICIES = {
    "ts_momentum_sma_atr": "trend_regime,extension_guard,volatility_regime",
    "donchian_breakout_20_10": "breakout_quality,extension_guard,participation,volatility_regime",
    "cross_sectional_relative_strength": "rank_membership,rank_margin,sector_concentration",
    "pullback_reclaim_uptrend": "trend_slope,waterfall_guard",
    "volatility_contraction_breakout": "participation,dead_liquidity,regime_guard",
    "failed_breakout_reversal_short": "rejection_quality,squeeze_guard,borrow_evidence",
    "bullish_fvg_continuation": "daily_ohlc_proxy,gap_quality,participation,trend_quality",
    "gap_up_continuation": (
        "gap_threshold,close_location,trend,participation,data_quality,corporate_action_basis"
    ),
    "gap_up_continuation_atr": (
        "gap_threshold,close_location,trend,participation,data_quality,corporate_action_basis"
    ),
}


def challenger_version_for(strategy_id: str) -> str:
    """Return a stable, strategy-specific challenger semantic version."""

    return f"{CHALLENGER_VERSION}-{strategy_id}"


@dataclass(frozen=True)
class GateResult:
    name: str
    status: str
    reason: str
    value: float | str | bool | None = None

    @property
    def passed(self) -> bool:
        return self.status == PASS


@dataclass(frozen=True)
class GateEvaluation:
    strategy_id: str
    candidate_version: str
    eligible: bool
    gates: tuple[GateResult, ...]

    @property
    def failures(self) -> tuple[GateResult, ...]:
        return tuple(gate for gate in self.gates if not gate.passed)

    @property
    def first_failure(self) -> GateResult | None:
        return self.failures[0] if self.failures else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "candidate_version": self.candidate_version,
            "eligible": self.eligible,
            "first_failure": self.first_failure.name if self.first_failure else None,
            "all_failures": [gate.name for gate in self.failures],
            "gates": [
                {
                    "name": gate.name,
                    "status": gate.status,
                    "reason": gate.reason,
                    "value": gate.value,
                }
                for gate in self.gates
            ],
        }

    def evidence(self) -> tuple[str, ...]:
        failures = ",".join(gate.name for gate in self.failures) or "none"
        return tuple(
            [f"gate:{gate.name}:{gate.status}:{gate.reason}" for gate in self.gates]
            + [f"gate:first_failure:{self.first_failure.name if self.first_failure else 'none'}"]
            + [f"gate:all_failures:{failures}"]
        )


def _gate(
    name: str, passed: bool, reason: str, value: float | str | bool | None = None
) -> GateResult:
    return GateResult(name, PASS if passed else FAIL, reason, value)


def _unavailable(name: str, reason: str) -> GateResult:
    return GateResult(name, UNAVAILABLE_REQUIRED_DATA, reason)


def _finite(*values: object) -> bool:
    return all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in values)


def _point_in_time_dataset(dataset: MarketDataset, index: int) -> MarketDataset:
    """Prevent a full dataset passed by a caller from leaking future bars."""

    return replace(
        dataset,
        bars_by_symbol={
            symbol: bars[: index + 1] for symbol, bars in dataset.bars_by_symbol.items()
        },
    )


def _bars_ok(bars: tuple[MarketBar, ...], index: int) -> bool:
    if index < 0 or index >= len(bars):
        return False
    bar = bars[index]
    return _finite(bar.open, bar.high, bar.low, bar.close, bar.volume) and all(
        bar.open > 0 and bar.high > 0 and bar.low > 0 and bar.close > 0 and bar.volume >= 0
        for bar in (bar,)
    )


def _base_gates(bars: tuple[MarketBar, ...], index: int) -> list[GateResult]:
    if not _bars_ok(bars, index):
        return [
            _unavailable("required_ohlcv", "current bar is missing, non-finite, or non-positive")
        ]
    return [_gate("required_ohlcv", True, "current OHLCV bar is usable")]


def _indicator_values(
    bars: tuple[MarketBar, ...], index: int
) -> tuple[list[float], list[float | None], list[float | None]]:
    closes = [bar.close for bar in bars]
    return closes, sma(closes, 50), atr(bars, 14)


def _evaluate_ts(bars: tuple[MarketBar, ...], index: int) -> list[GateResult]:
    gates = _base_gates(bars, index)
    try:
        closes, sma50, atr14 = _indicator_values(bars, index)
        roc20 = rate_of_change(closes, 20)[index]
        trend = sma50[index]
        volatility = atr14[index]
    except (IndexError, ValueError, ZeroDivisionError):
        return gates + [
            _unavailable("trend_regime", "insufficient point-in-time indicator history")
        ]
    if trend is None or volatility is None or roc20 is None:
        return gates + [_unavailable("trend_regime", "SMA50, ROC20, and ATR14 are required")]
    close = closes[index]
    return gates + [
        _gate(
            "trend_regime",
            close > trend and roc20 > 0,
            "close and momentum must remain positive",
            close - trend,
        ),
        _gate(
            "extension_guard",
            close / trend <= 1.12,
            "reject entries more than 12% above SMA50",
            close / trend,
        ),
        _gate(
            "volatility_regime",
            volatility / close <= 0.08,
            "reject extreme ATR/close regimes",
            volatility / close,
        ),
    ]


def _evaluate_donchian(bars: tuple[MarketBar, ...], index: int) -> list[GateResult]:
    gates = _base_gates(bars, index)
    if index < 20 or index < 14:
        return gates + [
            _unavailable("breakout_quality", "prior high, ATR14, and volume history are required")
        ]
    prior_high = donchian_high(bars, index, 20)
    atr14 = atr(bars, 14)[index]
    prior_vol = [bar.volume for bar in bars[index - 20 : index]]
    if prior_high is None or atr14 is None or not prior_vol or not _finite(prior_high, atr14):
        return gates + [
            _unavailable("breakout_quality", "prior high, ATR14, and volume history are required")
        ]
    close = bars[index].close
    prior_volume_mean = mean(prior_vol)
    return gates + [
        _gate(
            "breakout_quality",
            close / prior_high - 1 >= 0.0025,
            "breakout must clear the prior high by 0.25%",
            close / prior_high - 1,
        ),
        _gate(
            "extension_guard",
            close / prior_high - 1 <= 0.08,
            "reject an overextended breakout",
            close / prior_high - 1,
        ),
        _gate(
            "participation",
            prior_volume_mean > 0 and bars[index].volume / prior_volume_mean >= 1.10,
            "volume must be at least 1.10x prior-20 mean",
            bars[index].volume / prior_volume_mean if prior_volume_mean else None,
        ),
        _gate(
            "volatility_regime",
            atr14 / close <= 0.10,
            "reject extreme ATR/close regimes",
            atr14 / close,
        ),
    ]


def _evaluate_cross_sectional(
    dataset: MarketDataset, symbol: str, bars: tuple[MarketBar, ...], index: int
) -> list[GateResult]:
    gates = _base_gates(bars, index)
    scores: list[tuple[float, str]] = []
    for other_symbol, other_bars in _point_in_time_dataset(dataset, index).bars_by_symbol.items():
        if len(other_bars) <= index:
            continue
        closes = [bar.close for bar in other_bars]
        momentum = rate_of_change(closes, 60)[index]
        volatility = rolling_volatility(closes, 20)[index]
        if momentum is not None and volatility is not None and volatility > 0:
            scores.append((momentum / volatility, other_symbol))
    ranked = sorted(scores, reverse=True)
    own = next((score for score, candidate in ranked if candidate == symbol), None)
    own_rank = next(
        (rank for rank, (_, candidate) in enumerate(ranked, start=1) if candidate == symbol),
        None,
    )
    # Compare the candidate with the next lower rank. Comparing rank two to
    # rank one would make a top-two candidate's margin negative by definition.
    next_lower_score = (
        ranked[own_rank][0]
        if own_rank is not None and own_rank < len(ranked)
        else None
    )
    if own is None or next_lower_score is None or own_rank is None:
        gates.append(
            _unavailable("rank_margin", "synchronized cross-sectional history is required")
        )
    else:
        gates.append(
            _gate(
                "rank_membership",
                own_rank <= 2,
                "candidate must remain in the top two ranks",
                own_rank,
            )
        )
        gates.append(
            _gate(
                "rank_margin",
                own - next_lower_score >= 0.05,
                "candidate must clear the next lower rank by 0.05",
                own - next_lower_score,
            )
        )
    gates.append(
        _unavailable(
            "sector_concentration",
            "sector metadata is not retained; concentration cannot be proven",
        )
    )
    return gates


def _evaluate_pullback(bars: tuple[MarketBar, ...], index: int) -> list[GateResult]:
    gates = _base_gates(bars, index)
    if index < 52:
        return gates + [_unavailable("trend_slope", "SMA50 slope requires point-in-time history")]
    closes = [bar.close for bar in bars]
    sma50 = sma(closes, 50)
    current, previous = sma50[index], sma50[index - 1]
    if current is None or previous is None:
        return gates + [_unavailable("trend_slope", "SMA50 slope is unavailable")]
    waterfall = closes[index] >= closes[index - 2] if index >= 2 else False
    return gates + [
        _gate("trend_slope", current > previous, "SMA50 must slope upward", current - previous),
        _gate(
            "waterfall_guard",
            waterfall,
            "reject a three-bar waterfall decline",
            closes[index] - closes[index - 2] if index >= 2 else None,
        ),
    ]


def _evaluate_volatility(bars: tuple[MarketBar, ...], index: int) -> list[GateResult]:
    gates = _base_gates(bars, index)
    if index < 20 or index < 50:
        return gates + [
            _unavailable("participation", "prior-20 volume and regime history are required")
        ]
    atr14 = atr(bars, 14)[index]
    if atr14 is None:
        return gates + [_unavailable("regime_guard", "ATR14 unavailable")]
    prior_vol = mean(bar.volume for bar in bars[index - 20 : index])
    closes = [bar.close for bar in bars]
    trend = sma(closes, 50)[index]
    return gates + [
        _gate(
            "participation",
            prior_vol > 0 and bars[index].volume / prior_vol >= 1.20,
            "breakout volume must be at least 1.20x prior mean",
            bars[index].volume / prior_vol if prior_vol else None,
        ),
        _gate(
            "dead_liquidity",
            bars[index].volume > 0 and prior_vol > 0,
            "zero-volume compression is not actionable",
            bars[index].volume,
        ),
        _gate(
            "regime_guard",
            trend is not None and closes[index] > trend and atr14 / closes[index] <= 0.10,
            "require positive trend and bounded volatility",
            atr14 / closes[index],
        ),
    ]


def _evaluate_failed_short(bars: tuple[MarketBar, ...], index: int) -> list[GateResult]:
    gates = _base_gates(bars, index)
    if index < 20:
        return gates + [_unavailable("rejection_quality", "prior-20 high is required")]
    prior_high = donchian_high(bars, index, 20)
    atr14 = atr(bars, 14)[index]
    if prior_high is None or atr14 is None:
        return gates + [_unavailable("rejection_quality", "prior-20 high and ATR14 are required")]
    bar = bars[index]
    gates.extend(
        [
            _gate(
                "rejection_quality",
                bar.high > prior_high and bar.close < prior_high,
                "sweep must reject back below the prior high",
            ),
            _gate(
                "squeeze_guard",
                atr14 / bar.close <= 0.12,
                "reject disorderly high-volatility squeezes",
                atr14 / bar.close,
            ),
            _unavailable(
                "borrow_evidence",
                "borrow/locate evidence is not retained; short candidate rejected",
            ),
        ]
    )
    return gates


def _evaluate_fvg(bars: tuple[MarketBar, ...], index: int) -> list[GateResult]:
    gates = _base_gates(bars, index)
    if index < 20:
        return gates + [
            _unavailable("gap_quality", "three-candle gap, ATR14, and volume history are required")
        ]
    atr14 = atr(bars, 14)[index]
    if atr14 is None or atr14 <= 0:
        return gates + [_unavailable("gap_quality", "ATR14 unavailable")]
    gap = bars[index - 1].low - bars[index - 3].high
    prior_vol = mean(bar.volume for bar in bars[index - 20 : index])
    closes = [bar.close for bar in bars]
    sma20 = sma(closes, 20)
    current_sma = sma20[index]
    previous_sma = sma20[index - 1]
    trend_slope = (
        current_sma is not None
        and previous_sma is not None
        and current_sma > previous_sma
    )
    gates.extend(
        [
            _gate("daily_ohlc_proxy", True, "daily OHLC gap is a proxy, not order-flow evidence"),
            _gate("gap_quality", gap / atr14 >= 0.50, "gap must be at least 0.50 ATR", gap / atr14),
            _gate(
                "participation",
                prior_vol > 0 and bars[index].volume / prior_vol >= 1.20,
                "volume must be at least 1.20x prior mean",
                bars[index].volume / prior_vol if prior_vol else None,
            ),
            _gate("trend_quality", trend_slope, "SMA20 must slope upward"),
        ]
    )
    return gates


def _known_price_basis(bar: MarketBar) -> bool:
    return bool(
        bar.price_adjustment_basis
        and bar.price_adjustment_basis.strip().lower() not in {"unknown", "raw", "unadjusted"}
    )


def _evaluate_gap(
    bars: tuple[MarketBar, ...], index: int, *, atr_normalized: bool
) -> list[GateResult]:
    gates = _base_gates(bars, index)
    if index < 100 or index < 20 or index < 14:
        return gates + [
            _unavailable("data_quality", "SMA100, ATR14, and prior-20 volume history are required")
        ]
    current, previous = bars[index], bars[index - 1]
    atr_values = atr(bars, 14)
    trend = sma([bar.close for bar in bars], 100)[index]
    prior_atr = atr_values[index - 1]
    prior_vol = mean(bar.volume for bar in bars[index - 20 : index])
    if trend is None or prior_atr is None or prior_atr <= 0:
        return gates + [_unavailable("data_quality", "trend or ATR evidence is unavailable")]
    gap = current.open / previous.close - 1 if previous.close > 0 else float("nan")
    gap_value = (current.open - previous.close) / prior_atr if atr_normalized else gap
    gates.extend(
        [
            _gate(
                "gap_threshold",
                gap_value >= (0.50 if atr_normalized else 0.0075),
                "gap threshold must be met",
                gap_value,
            ),
            _gate(
                "close_location",
                current.high > current.low
                and (current.close - current.low) / (current.high - current.low) >= 0.70,
                "close must finish in the top 30% of range",
            ),
            _gate(
                "trend",
                current.close > trend,
                "close must remain above SMA100",
                current.close - trend,
            ),
            _gate(
                "participation",
                prior_vol > 0 and current.volume >= prior_vol,
                "volume must meet prior-20 mean",
                current.volume / prior_vol if prior_vol else None,
            ),
            (
                _gate("data_quality", True, "OHLCV values pass quality checks")
                if _bars_ok(bars, index - 1)
                else _unavailable(
                    "data_quality", "prior bar is missing, non-finite, or non-positive"
                )
            ),
            _gate(
                "corporate_action_basis",
                _known_price_basis(current) and _known_price_basis(previous),
                "corporate-action adjustment basis must be known",
            ),
        ]
    )
    return gates


def evaluate_challenger_gates(
    strategy_id: str,
    dataset: MarketDataset,
    symbol: str,
    bars: tuple[MarketBar, ...],
    index: int,
    *,
    candidate_version: str = CHALLENGER_VERSION,
) -> GateEvaluation:
    """Evaluate all candidate predicates at one point in time, with no lookahead."""

    # A malformed current bar must never be converted into a normal predicate
    # failure (or a fabricated zero).  Stop at the shared data contract and
    # preserve the explicit unavailable reason for attribution.
    base = _base_gates(bars, index)
    if not base[0].passed:
        return GateEvaluation(strategy_id, candidate_version, False, tuple(base))
    if strategy_id == "ts_momentum_sma_atr":
        gates = _evaluate_ts(bars, index)
    elif strategy_id == "donchian_breakout_20_10":
        gates = _evaluate_donchian(bars, index)
    elif strategy_id == "cross_sectional_relative_strength":
        gates = _evaluate_cross_sectional(dataset, symbol, bars, index)
    elif strategy_id == "pullback_reclaim_uptrend":
        gates = _evaluate_pullback(bars, index)
    elif strategy_id == "volatility_contraction_breakout":
        gates = _evaluate_volatility(bars, index)
    elif strategy_id == "failed_breakout_reversal_short":
        gates = _evaluate_failed_short(bars, index)
    elif strategy_id == "bullish_fvg_continuation":
        gates = _evaluate_fvg(bars, index)
    elif strategy_id == "gap_up_continuation":
        gates = _evaluate_gap(bars, index, atr_normalized=False)
    elif strategy_id == "gap_up_continuation_atr":
        gates = _evaluate_gap(bars, index, atr_normalized=True)
    else:
        gates = [_unavailable("strategy", f"no challenger evaluator for {strategy_id}")]
    return GateEvaluation(
        strategy_id, candidate_version, all(gate.passed for gate in gates), tuple(gates)
    )


def _candidate_signal(
    champion: StrategySpec,
    candidate: StrategySpec,
    dataset: MarketDataset,
    symbol: str,
    bars: tuple[MarketBar, ...],
    index: int,
) -> StrategySignal | None:
    point_in_time = _point_in_time_dataset(dataset, index)
    # The audited catalog indicators are index-addressed rolling calculations;
    # reusing the stable bar tuple lets their feature cache work without
    # changing the value at ``index``. Cross-sectional inputs still receive a
    # prefix-only dataset, and the future-mutation tests enforce this boundary.
    signal = champion.signal(point_in_time, symbol, bars, index)
    if signal is None:
        return None
    evaluation = evaluate_challenger_gates(
        candidate.strategy_id,
        point_in_time,
        symbol,
        bars,
        index,
        candidate_version=candidate.version,
    )
    if not evaluation.eligible:
        return None
    return replace(
        signal,
        strategy_version=candidate.version,
        evidence=signal.evidence + evaluation.evidence(),
        warnings=signal.warnings + ("research-only challenger; never routes to a broker",),
    )


def build_challenger_catalog() -> tuple[StrategySpec, ...]:
    """Build nine additive candidates while returning the v1 catalog untouched."""

    champions = {
        spec.strategy_id: spec for spec in build_legacy_catalog() if spec.status == "experimental"
    }
    ids: tuple[str, ...] = (
        "ts_momentum_sma_atr",
        "donchian_breakout_20_10",
        "cross_sectional_relative_strength",
        "pullback_reclaim_uptrend",
        "volatility_contraction_breakout",
        "failed_breakout_reversal_short",
        "bullish_fvg_continuation",
    )
    # The two gap v1 specs are maintained by the research catalog, so import
    # them here without making the combined catalog depend on this module.
    from intraday_scanner.v2.strategies.research import build_research_strategy_catalog

    champions.update({spec.strategy_id: spec for spec in build_research_strategy_catalog()})
    ids += ("gap_up_continuation", "gap_up_continuation_atr")
    result: list[StrategySpec] = []
    for strategy_id in ids:
        champion = champions[strategy_id]
        candidate_version = challenger_version_for(strategy_id)

        def generate(
            candidate: StrategySpec,
            dataset: MarketDataset,
            symbol: str,
            bars: tuple[MarketBar, ...],
            index: int,
            *,
            _champion: StrategySpec = champion,
        ) -> StrategySignal | None:
            return _candidate_signal(_champion, candidate, dataset, symbol, bars, index)

        parameters = dict(champion.parameters)
        parameters.update(
            {
                "candidate_version": candidate_version,
                "research_only": True,
                "unsupported_data_policy": "fail_closed",
                "challenger_gate_policy": _GATE_POLICIES[strategy_id],
            }
        )
        result.append(
            replace(
                champion,
                version=candidate_version,
                status="experimental",
                description=(
                    f"research-only challenger for {strategy_id}; additive gates and "
                    "complete predicate telemetry."
                ),
                parameters=parameters,
                entry_logic=(
                    f"{champion.entry_logic} Challenger gates add strategy-specific "
                    "quality and fail-closed data checks."
                ),
                known_failure_modes=champion.known_failure_modes
                + ("challenger is research-only and not broker-routable",),
                validation_status="research_only_challenger_unvalidated",
                generate_signal=generate,
            )
        )
    return tuple(result)


__all__ = [
    "CHALLENGER_VERSION",
    "FAIL",
    "GateEvaluation",
    "GateResult",
    "PASS",
    "UNAVAILABLE_REQUIRED_DATA",
    "build_challenger_catalog",
    "challenger_version_for",
    "evaluate_challenger_gates",
]
