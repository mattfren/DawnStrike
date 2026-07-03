"""Latest-bar strategy scan and research-only decision cards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from intraday_scanner.v2.backtest import BacktestResult
from intraday_scanner.v2.data import MarketDataset
from intraday_scanner.v2.risk import RiskDecision, RiskSettings, evaluate_signal_risk
from intraday_scanner.v2.strategies import StrategySignal, StrategySpec


@dataclass(frozen=True)
class ScanCard:
    symbol: str
    timestamp: datetime
    strategy_id: str
    strategy_version: str
    direction: str
    status: str
    setup_score: float
    entry_trigger: str
    stop: float | None
    target: float | None
    risk_per_share: float | None
    reward: float | None
    reward_risk: float | None
    invalidation: str
    evidence: tuple[str, ...]
    historical_summary: str
    warnings: tuple[str, ...]
    data_snapshot_id: str
    run_manifest_id: str
    research_only: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "direction": self.direction,
            "status": self.status,
            "setup_score": round(self.setup_score, 4),
            "entry_trigger": self.entry_trigger,
            "stop": _round_optional(self.stop),
            "target": _round_optional(self.target),
            "risk_per_share": _round_optional(self.risk_per_share),
            "reward": _round_optional(self.reward),
            "reward_risk": _round_optional(self.reward_risk),
            "invalidation": self.invalidation,
            "evidence": list(self.evidence),
            "historical_summary": self.historical_summary,
            "warnings": list(self.warnings),
            "data_snapshot_id": self.data_snapshot_id,
            "run_manifest_id": self.run_manifest_id,
            "research_only": self.research_only,
        }


@dataclass(frozen=True)
class ScanOutput:
    cards: tuple[ScanCard, ...]
    no_setup: tuple[ScanCard, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "cards": [card.to_dict() for card in self.cards],
            "no_setup": [card.to_dict() for card in self.no_setup],
            "warnings": list(self.warnings),
        }


def run_latest_scan(
    dataset: MarketDataset,
    strategies: tuple[StrategySpec, ...],
    backtest_results: dict[str, BacktestResult],
    *,
    risk_settings: RiskSettings,
    data_snapshot_id: str,
    run_manifest_id: str,
) -> ScanOutput:
    cards: list[ScanCard] = []
    no_setup_cards: list[ScanCard] = []
    warnings: list[str] = list(dataset.warnings)
    for strategy in strategies:
        if strategy.status in {"benchmark", "baseline"}:
            continue
        for symbol in dataset.symbols:
            bars = dataset.bars_by_symbol[symbol]
            if not bars:
                continue
            latest_index = len(bars) - 1
            latest_bar = bars[latest_index]
            signal = strategy.signal(dataset, symbol, bars, latest_index)
            historical_result = backtest_results.get(strategy.strategy_id)
            historical_summary = _historical_summary(historical_result)
            historical_warnings = _historical_warnings(historical_result)
            if signal:
                risk = evaluate_signal_risk(
                    signal,
                    entry_price=signal.entry_reference,
                    settings=risk_settings,
                    stale=False,
                )
                cards.append(
                    _card_from_signal(
                        signal,
                        latest_bar.timestamp,
                        risk,
                        "candidate",
                        historical_summary,
                        historical_warnings,
                        data_snapshot_id,
                        run_manifest_id,
                    )
                )
            else:
                no_setup_cards.append(
                    ScanCard(
                        symbol=symbol,
                        timestamp=latest_bar.timestamp,
                        strategy_id=strategy.strategy_id,
                        strategy_version=strategy.version,
                        direction="flat",
                        status="no_setup",
                        setup_score=0.0,
                        entry_trigger="No current mechanical trigger on latest bar.",
                        stop=None,
                        target=None,
                        risk_per_share=None,
                        reward=None,
                        reward_risk=None,
                        invalidation=(
                            "No setup exists unless the strategy entry rule triggers "
                            "on a future bar."
                        ),
                        evidence=(
                            f"{strategy.strategy_id}: latest bar did not satisfy entry rule",
                        ),
                        historical_summary=historical_summary,
                        warnings=("no_current_setup",) + historical_warnings,
                        data_snapshot_id=data_snapshot_id,
                        run_manifest_id=run_manifest_id,
                    )
                )
    return ScanOutput(
        cards=tuple(
            sorted(cards, key=lambda card: (-card.setup_score, card.symbol, card.strategy_id))
        ),
        no_setup=tuple(no_setup_cards),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _card_from_signal(
    signal: StrategySignal,
    timestamp: datetime,
    risk: RiskDecision,
    status: str,
    historical_summary: str,
    historical_warnings: tuple[str, ...],
    data_snapshot_id: str,
    run_manifest_id: str,
) -> ScanCard:
    reward = risk.reward
    reward_risk = risk.reward_risk
    return ScanCard(
        symbol=signal.symbol,
        timestamp=timestamp,
        strategy_id=signal.strategy_id,
        strategy_version=signal.strategy_version,
        direction=signal.direction,
        status=status,
        setup_score=signal.score,
        entry_trigger=(
            f"Signal at close {signal.entry_reference:.2f}; default execution is next bar open."
        ),
        stop=signal.stop,
        target=signal.target,
        risk_per_share=risk.risk_per_unit,
        reward=reward,
        reward_risk=reward_risk,
        invalidation=signal.invalidation,
        evidence=signal.evidence,
        historical_summary=historical_summary,
        warnings=tuple(dict.fromkeys(risk.warnings + signal.warnings + historical_warnings)),
        data_snapshot_id=data_snapshot_id,
        run_manifest_id=run_manifest_id,
    )


def _historical_summary(result: BacktestResult | None) -> str:
    if result is None:
        return "No backtest summary was available for this strategy."
    metrics = result.metrics
    return (
        f"{int(metrics.get('trade_count') or 0)} trades, "
        f"return {float(metrics.get('total_return_pct') or 0.0) * 100:.2f}%, "
        f"max drawdown {float(metrics.get('max_drawdown_pct') or 0.0) * 100:.2f}% "
        "on the selected dataset."
    )


def _historical_warnings(result: BacktestResult | None) -> tuple[str, ...]:
    if result is None:
        return ("missing_historical_summary",)
    metrics = result.metrics
    warnings: list[str] = []
    trade_count = int(metrics.get("trade_count") or 0)
    total_return = float(metrics.get("total_return_pct") or 0.0)
    max_drawdown = float(metrics.get("max_drawdown_pct") or 0.0)
    if trade_count < 5:
        warnings.append("low_historical_trade_count")
    if total_return < 0:
        warnings.append("historically_underperformed")
    if max_drawdown < -0.25:
        warnings.append("severe_historical_drawdown")
    return tuple(warnings)


def _round_optional(value: float | None) -> float | None:
    return None if value is None else round(value, 4)
