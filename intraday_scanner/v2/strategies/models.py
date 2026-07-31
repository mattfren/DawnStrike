"""Typed strategy models used by the v2 Alpha Lab backtester and scanner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from intraday_scanner.v2.data import MarketBar, MarketDataset


class Direction:
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True)
class StrategySignal:
    strategy_id: str
    strategy_version: str
    symbol: str
    signal_index: int
    direction: str
    entry_reference: float
    stop: float
    target: float | None
    score: float
    evidence: tuple[str, ...]
    invalidation: str
    warnings: tuple[str, ...] = ()

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_reference - self.stop)

    @property
    def reward_per_unit(self) -> float | None:
        if self.target is None:
            return None
        return abs(self.target - self.entry_reference)


SignalFunction = Callable[
    ["StrategySpec", MarketDataset, str, tuple[MarketBar, ...], int],
    StrategySignal | None,
]


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    version: str
    status: str
    description: str
    compatible_timeframe: str
    required_data_fields: tuple[str, ...]
    parameters: dict[str, float | int | str | bool]
    indicators: tuple[str, ...]
    entry_logic: str
    exit_logic: str
    stop_logic: str
    target_logic: str
    position_sizing_assumption: str
    known_failure_modes: tuple[str, ...]
    validation_status: str
    generate_signal: SignalFunction

    def signal(
        self,
        dataset: MarketDataset,
        symbol: str,
        bars: tuple[MarketBar, ...],
        index: int,
    ) -> StrategySignal | None:
        return self.generate_signal(self, dataset, symbol, bars, index)
