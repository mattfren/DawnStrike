"""Backtesting engine for Dawnstrike v2 Alpha Lab."""

from intraday_scanner.v2.backtest.engine import (
    BacktestEngine,
    BacktestResult,
    BacktestSettings,
    EquityPoint,
    TradeRecord,
)
from intraday_scanner.v2.backtest.intraday_engine import (
    CausalMarketEvent,
    IntradayBacktestEngine,
    IntradayBacktestResult,
    IntradayBacktestSettings,
    IntradayEquityPoint,
    IntradayReplayTrade,
    WalkForwardFold,
    build_expanding_walk_forward_folds,
    run_intraday_backtest,
)
from intraday_scanner.v2.backtest.intraday_metrics import (
    compare_benchmark,
    compute_intraday_metrics,
)

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "BacktestSettings",
    "CausalMarketEvent",
    "EquityPoint",
    "IntradayBacktestEngine",
    "IntradayBacktestResult",
    "IntradayBacktestSettings",
    "IntradayEquityPoint",
    "IntradayReplayTrade",
    "TradeRecord",
    "WalkForwardFold",
    "build_expanding_walk_forward_folds",
    "compare_benchmark",
    "compute_intraday_metrics",
    "run_intraday_backtest",
]
