"""Minimal honest event-style backtester for v2 Alpha Lab strategies."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from statistics import mean, median, pstdev

from intraday_scanner.v2.data import MarketBar, MarketDataset, timestamp_alignment_issues
from intraday_scanner.v2.risk import RiskSettings, evaluate_signal_risk
from intraday_scanner.v2.strategies import Direction, StrategySignal, StrategySpec


@dataclass(frozen=True)
class BacktestSettings:
    initial_capital: float = 100_000.0
    fee_bps: float = 1.0
    slippage_bps: float = 5.0
    commission_per_trade: float = 0.0
    max_gross_exposure_pct: float = 1.0
    max_concurrent_positions: int = 3
    risk: RiskSettings = RiskSettings()
    holding_timeout_calendar_days: int = 10


@dataclass(frozen=True)
class TradeRecord:
    trade_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    stop: float
    target: float | None
    quantity: int
    gross_pnl: float
    net_pnl: float
    return_pct: float
    r_multiple: float
    exit_reason: str
    holding_bars: int
    fees_paid: float
    slippage_paid: float
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    equity: float
    cash: float
    open_positions: int
    drawdown_pct: float


@dataclass(frozen=True)
class BacktestResult:
    strategy: StrategySpec
    trades: tuple[TradeRecord, ...]
    equity_curve: tuple[EquityPoint, ...]
    metrics: dict[str, float | int | str | None]
    warnings: tuple[str, ...]


@dataclass
class _Position:
    symbol: str
    direction: str
    quantity: int
    entry_price: float
    stop: float
    target: float | None
    entry_time: datetime
    entry_index: int
    entry_notional: float
    entry_fee: float
    entry_slippage: float
    initial_risk: float
    signal: StrategySignal


class BacktestEngine:
    def __init__(self, settings: BacktestSettings | None = None) -> None:
        self.settings = settings or BacktestSettings()
        if not math.isfinite(self.settings.initial_capital) or self.settings.initial_capital <= 0:
            raise ValueError("backtest initial_capital must be finite and positive")
        for name, value in (
            ("fee_bps", self.settings.fee_bps),
            ("slippage_bps", self.settings.slippage_bps),
            ("commission_per_trade", self.settings.commission_per_trade),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"backtest {name} must be finite and non-negative")
        if (
            not math.isfinite(self.settings.max_gross_exposure_pct)
            or self.settings.max_gross_exposure_pct <= 0
        ):
            raise ValueError("backtest max_gross_exposure_pct must be finite and positive")
        if self.settings.max_concurrent_positions < 1:
            raise ValueError("backtest max_concurrent_positions must be at least 1")
        if (
            isinstance(self.settings.holding_timeout_calendar_days, bool)
            or not isinstance(self.settings.holding_timeout_calendar_days, int)
            or self.settings.holding_timeout_calendar_days < 1
        ):
            raise ValueError(
                "backtest holding_timeout_calendar_days must be an integer of at least 1"
            )

    def run(self, strategy: StrategySpec, dataset: MarketDataset) -> BacktestResult:
        alignment_issues = timestamp_alignment_issues(dataset)
        if alignment_issues:
            raise ValueError(
                "multi-symbol backtests require timestamp-aligned bars: "
                + "; ".join(alignment_issues)
            )
        if strategy.strategy_id == "cash_no_trade_baseline":
            return self._cash_result(strategy, dataset)
        if strategy.strategy_id == "benchmark_buy_hold_equal_weight":
            return self._benchmark_result(strategy, dataset)

        cash = self.settings.initial_capital
        positions: dict[str, _Position] = {}
        trades: list[TradeRecord] = []
        equity_curve: list[EquityPoint] = []
        warnings: list[str] = list(dataset.warnings)
        max_bars = max((len(bars) for bars in dataset.bars_by_symbol.values()), default=0)
        peak_equity = self.settings.initial_capital
        exposure_position_days = 0
        eligible_symbol_days = 0

        for index in range(1, max_bars):
            for symbol in dataset.symbols:
                bars = dataset.bars_by_symbol[symbol]
                if len(bars) <= index:
                    continue
                eligible_symbol_days += 1
                if (
                    symbol in positions
                    or len(positions) >= self.settings.max_concurrent_positions
                ):
                    continue
                signal = strategy.signal(dataset, symbol, bars, index - 1)
                if signal is None:
                    continue
                current_equity = self._mark_to_market_at_open(
                    cash,
                    positions,
                    dataset,
                    index,
                )
                gross_exposure = self._gross_exposure_at_open(
                    positions,
                    dataset,
                    index,
                )
                position = self._enter_position(
                    signal,
                    bars[index],
                    index,
                    cash,
                    current_equity=current_equity,
                    current_gross_exposure=gross_exposure,
                )
                if position is None:
                    warnings.append(
                        f"{symbol}: risk engine rejected signal at "
                        f"{bars[index].timestamp.isoformat()}"
                    )
                    continue
                cash = self._apply_entry_cash(cash, position)
                positions[symbol] = position

            for symbol in list(positions):
                bars = dataset.bars_by_symbol[symbol]
                if len(bars) <= index:
                    continue
                position = positions[symbol]
                bar = bars[index]
                exit_price, exit_reason = self._exit_price(position, bar)
                if exit_price is None:
                    continue
                trade, cash = self._close_position(
                    position, bar, index, exit_price, exit_reason, cash
                )
                trades.append(trade)
                del positions[symbol]

            exposure_position_days += len(positions)
            equity = self._mark_to_market(cash, positions, dataset, index)
            peak_equity = max(peak_equity, equity)
            drawdown_pct = (equity / peak_equity - 1.0) if peak_equity else 0.0
            timestamp = self._timestamp_for_index(dataset, index)
            if timestamp is not None:
                equity_curve.append(
                    EquityPoint(
                        timestamp=timestamp,
                        equity=equity,
                        cash=cash,
                        open_positions=len(positions),
                        drawdown_pct=drawdown_pct,
                    )
                )

        final_index = max_bars - 1
        for symbol in list(positions):
            bars = dataset.bars_by_symbol[symbol]
            if not bars:
                continue
            bar = bars[min(final_index, len(bars) - 1)]
            trade, cash = self._close_position(
                positions[symbol],
                bar,
                min(final_index, len(bars) - 1),
                bar.close,
                "end_of_test_liquidation",
                cash,
            )
            trades.append(trade)
            del positions[symbol]

        final_equity = cash
        if equity_curve:
            peak_equity = max(
                point.equity
                for point in equity_curve
                + [EquityPoint(equity_curve[-1].timestamp, final_equity, cash, 0, 0.0)]
            )
            drawdown_pct = (final_equity / peak_equity - 1.0) if peak_equity else 0.0
            equity_curve.append(
                EquityPoint(
                    timestamp=equity_curve[-1].timestamp,
                    equity=final_equity,
                    cash=cash,
                    open_positions=0,
                    drawdown_pct=drawdown_pct,
                )
            )

        metrics = self._metrics(
            tuple(trades),
            tuple(equity_curve),
            exposure_position_days=exposure_position_days,
            eligible_symbol_days=eligible_symbol_days,
        )
        return BacktestResult(
            strategy=strategy,
            trades=tuple(trades),
            equity_curve=tuple(equity_curve),
            metrics=metrics,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _enter_position(
        self,
        signal: StrategySignal,
        bar: MarketBar,
        index: int,
        cash: float,
        *,
        current_equity: float,
        current_gross_exposure: float,
    ) -> _Position | None:
        if current_equity <= 0 or self.settings.max_gross_exposure_pct <= 0:
            return None
        if signal.direction == Direction.LONG:
            if bar.open <= signal.stop:
                return None
            entry_price = bar.open * (1.0 + self._slippage_rate())
            entry_slippage = max(0.0, entry_price - bar.open)
        elif signal.direction == Direction.SHORT:
            if bar.open >= signal.stop:
                return None
            entry_price = bar.open * (1.0 - self._slippage_rate())
            entry_slippage = max(0.0, bar.open - entry_price)
        else:
            return None
        if signal.target is None:
            return None
        if (
            signal.direction == Direction.LONG
            and entry_price >= signal.target
            or signal.direction == Direction.SHORT
            and entry_price <= signal.target
        ):
            return None

        risk_decision = evaluate_signal_risk(
            signal,
            entry_price=entry_price,
            settings=replace(self.settings.risk, account_equity=current_equity),
            stale=False,
        )
        if not risk_decision.allowed or risk_decision.quantity <= 0:
            return None
        stop_fill = self._adverse_exit_fill(signal.direction, signal.stop)
        target_fill = self._adverse_exit_fill(signal.direction, signal.target)
        entry_bps_per_unit = entry_price * self.settings.fee_bps / 10_000.0
        stop_bps_per_unit = stop_fill * self.settings.fee_bps / 10_000.0
        target_bps_per_unit = target_fill * self.settings.fee_bps / 10_000.0
        loss_per_unit = max(
            0.0,
            -self._directional_pnl(signal.direction, entry_price, stop_fill),
        ) + entry_bps_per_unit + stop_bps_per_unit
        reward_per_unit = self._directional_pnl(
            signal.direction,
            entry_price,
            target_fill,
        ) - entry_bps_per_unit - target_bps_per_unit
        if loss_per_unit <= 0 or reward_per_unit <= 0:
            return None
        risk_budget = current_equity * min(
            self.settings.risk.risk_per_trade_pct,
            self.settings.risk.max_risk_per_trade_pct,
        )
        fixed_round_trip_cost = 2.0 * self.settings.commission_per_trade
        quantity_from_after_cost_risk = int(
            max(0.0, risk_budget - fixed_round_trip_cost) // loss_per_unit
        )
        remaining_gross = max(
            0.0,
            current_equity * self.settings.max_gross_exposure_pct
            - current_gross_exposure,
        )
        quantity_from_gross = int(remaining_gross // entry_price)
        quantity = min(
            risk_decision.quantity,
            quantity_from_after_cost_risk,
            quantity_from_gross,
        )
        if signal.direction == Direction.LONG:
            cash_after_commission = max(
                0.0,
                cash - self.settings.commission_per_trade,
            )
            quantity = min(
                quantity,
                int(cash_after_commission // (entry_price + entry_bps_per_unit)),
            )
        if quantity <= 0:
            return None
        modeled_loss = loss_per_unit * quantity + fixed_round_trip_cost
        modeled_reward = reward_per_unit * quantity - fixed_round_trip_cost
        if (
            modeled_reward <= 0
            or modeled_reward / modeled_loss < self.settings.risk.min_reward_risk
        ):
            return None
        notional = quantity * entry_price
        entry_fee = self._fee(notional)
        initial_risk = modeled_loss
        return _Position(
            symbol=signal.symbol,
            direction=signal.direction,
            quantity=quantity,
            entry_price=entry_price,
            stop=signal.stop,
            target=signal.target,
            entry_time=bar.timestamp,
            entry_index=index,
            entry_notional=notional,
            entry_fee=entry_fee,
            entry_slippage=entry_slippage * quantity,
            initial_risk=initial_risk,
            signal=signal,
        )

    def _apply_entry_cash(self, cash: float, position: _Position) -> float:
        if position.direction == Direction.LONG:
            return cash - position.entry_notional - position.entry_fee
        return cash + position.entry_notional - position.entry_fee

    def _close_position(
        self,
        position: _Position,
        bar: MarketBar,
        index: int,
        raw_exit_price: float,
        exit_reason: str,
        cash: float,
    ) -> tuple[TradeRecord, float]:
        if position.direction == Direction.LONG:
            exit_price = raw_exit_price * (1.0 - self._slippage_rate())
            exit_slippage = max(0.0, raw_exit_price - exit_price) * position.quantity
            exit_notional = exit_price * position.quantity
            exit_fee = self._fee(exit_notional)
            new_cash = cash + exit_notional - exit_fee
            gross_pnl = (exit_price - position.entry_price) * position.quantity
        else:
            exit_price = raw_exit_price * (1.0 + self._slippage_rate())
            exit_slippage = max(0.0, exit_price - raw_exit_price) * position.quantity
            exit_notional = exit_price * position.quantity
            exit_fee = self._fee(exit_notional)
            new_cash = cash - exit_notional - exit_fee
            gross_pnl = (position.entry_price - exit_price) * position.quantity

        fees_paid = position.entry_fee + exit_fee
        slippage_paid = position.entry_slippage + exit_slippage
        net_pnl = gross_pnl - fees_paid
        denominator = position.entry_notional if position.entry_notional else 1.0
        r_multiple = net_pnl / position.initial_risk if position.initial_risk else 0.0
        trade = TradeRecord(
            trade_id=f"{position.signal.strategy_id}:{position.symbol}:{position.entry_time.date()}:{index}",
            strategy_id=position.signal.strategy_id,
            strategy_version=position.signal.strategy_version,
            symbol=position.symbol,
            direction=position.direction,
            entry_time=position.entry_time,
            exit_time=bar.timestamp,
            entry_price=position.entry_price,
            exit_price=exit_price,
            stop=position.stop,
            target=position.target,
            quantity=position.quantity,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            return_pct=net_pnl / denominator,
            r_multiple=r_multiple,
            exit_reason=exit_reason,
            holding_bars=index - position.entry_index + 1,
            fees_paid=fees_paid,
            slippage_paid=slippage_paid,
            evidence=position.signal.evidence,
        )
        return trade, new_cash

    def _exit_price(self, position: _Position, bar: MarketBar) -> tuple[float | None, str]:
        """Return the raw exit price and its audit reason.

        ``timeout`` means the position reached its configured calendar-day age and
        was closed at that bar's close. Gap, stop, and target exits take precedence.
        """
        if position.direction == Direction.LONG:
            if bar.open <= position.stop:
                return bar.open, "gap_stop"
            if position.target is not None and bar.open >= position.target:
                return bar.open, "gap_target"
            if bar.low <= position.stop:
                return position.stop, "stop"
            if position.target is not None and bar.high >= position.target:
                return position.target, "target"
        else:
            if bar.open >= position.stop:
                return bar.open, "gap_stop"
            if position.target is not None and bar.open <= position.target:
                return bar.open, "gap_target"
            if bar.high >= position.stop:
                return position.stop, "stop"
            if position.target is not None and bar.low <= position.target:
                return position.target, "target"
        if (
            bar.timestamp.date() - position.entry_time.date()
        ).days >= self.settings.holding_timeout_calendar_days:
            return bar.close, "timeout"
        return None, ""

    def _adverse_exit_fill(self, direction: str, raw_price: float) -> float:
        if direction == Direction.LONG:
            return raw_price * (1.0 - self._slippage_rate())
        return raw_price * (1.0 + self._slippage_rate())

    @staticmethod
    def _directional_pnl(direction: str, entry: float, exit_price: float) -> float:
        if direction == Direction.LONG:
            return exit_price - entry
        return entry - exit_price

    def _mark_to_market_at_open(
        self,
        cash: float,
        positions: dict[str, _Position],
        dataset: MarketDataset,
        index: int,
    ) -> float:
        equity = cash
        for symbol, position in positions.items():
            bars = dataset.bars_by_symbol[symbol]
            if len(bars) <= index:
                continue
            mark = bars[index].open
            equity += (
                position.quantity * mark
                if position.direction == Direction.LONG
                else -position.quantity * mark
            )
        return equity

    @staticmethod
    def _gross_exposure_at_open(
        positions: dict[str, _Position],
        dataset: MarketDataset,
        index: int,
    ) -> float:
        return sum(
            abs(position.quantity * dataset.bars_by_symbol[symbol][index].open)
            for symbol, position in positions.items()
            if len(dataset.bars_by_symbol[symbol]) > index
        )

    def _mark_to_market(
        self,
        cash: float,
        positions: dict[str, _Position],
        dataset: MarketDataset,
        index: int,
    ) -> float:
        equity = cash
        for symbol, position in positions.items():
            bars = dataset.bars_by_symbol[symbol]
            if len(bars) <= index:
                continue
            close = bars[index].close
            if position.direction == Direction.LONG:
                equity += position.quantity * close
            else:
                equity -= position.quantity * close
        return equity

    def _metrics(
        self,
        trades: tuple[TradeRecord, ...],
        equity_curve: tuple[EquityPoint, ...],
        *,
        exposure_position_days: int,
        eligible_symbol_days: int,
    ) -> dict[str, float | int | str | None]:
        final_equity = equity_curve[-1].equity if equity_curve else self.settings.initial_capital
        total_return = final_equity / self.settings.initial_capital - 1.0
        daily_returns = _equity_returns(equity_curve)
        volatility = pstdev(daily_returns) * math.sqrt(252) if len(daily_returns) >= 2 else 0.0
        average_daily_return = mean(daily_returns) if daily_returns else 0.0
        downside = [value for value in daily_returns if value < 0]
        downside_vol = pstdev(downside) * math.sqrt(252) if len(downside) >= 2 else 0.0
        sharpe = (average_daily_return * 252) / volatility if volatility else None
        sortino = (average_daily_return * 252) / downside_vol if downside_vol else None
        max_drawdown = min((point.drawdown_pct for point in equity_curve), default=0.0)
        annualized_return = _annualized_return(total_return, equity_curve)
        calmar = (
            annualized_return / abs(max_drawdown)
            if annualized_return is not None and max_drawdown < 0
            else None
        )
        wins = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
        losses = [trade.net_pnl for trade in trades if trade.net_pnl < 0]
        r_values = [trade.r_multiple for trade in trades]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        average_holding = mean([trade.holding_bars for trade in trades]) if trades else 0.0
        fees_paid = sum(trade.fees_paid for trade in trades)
        slippage_paid = sum(trade.slippage_paid for trade in trades)
        return {
            "initial_capital": self.settings.initial_capital,
            "final_equity": final_equity,
            "total_return_pct": total_return,
            "annualized_return_pct": annualized_return,
            "max_drawdown_pct": max_drawdown,
            "drawdown_duration_bars": _drawdown_duration(equity_curve),
            "volatility_annualized": volatility,
            "sharpe": sharpe,
            "sortino": sortino,
            "calmar": calmar,
            "trade_count": len(trades),
            "win_rate": len(wins) / len(trades) if trades else 0.0,
            "average_win": mean(wins) if wins else 0.0,
            "average_loss": mean(losses) if losses else 0.0,
            "payoff_ratio": (mean(wins) / abs(mean(losses))) if wins and losses else None,
            "profit_factor": gross_profit / gross_loss if gross_loss else None,
            "average_r": mean(r_values) if r_values else 0.0,
            "median_r": median(r_values) if r_values else 0.0,
            "best_trade": max((trade.net_pnl for trade in trades), default=0.0),
            "worst_trade": min((trade.net_pnl for trade in trades), default=0.0),
            "exposure_time_market": (
                exposure_position_days / eligible_symbol_days if eligible_symbol_days else 0.0
            ),
            "average_holding_period_bars": average_holding,
            "fees_paid": fees_paid,
            "slippage_estimate": slippage_paid,
            "expectancy": mean([trade.net_pnl for trade in trades]) if trades else 0.0,
        }

    def _cash_result(self, strategy: StrategySpec, dataset: MarketDataset) -> BacktestResult:
        equity_curve: list[EquityPoint] = []
        peak = self.settings.initial_capital
        for index in range(max((len(bars) for bars in dataset.bars_by_symbol.values()), default=0)):
            timestamp = self._timestamp_for_index(dataset, index)
            if timestamp is None:
                continue
            equity_curve.append(
                EquityPoint(
                    timestamp=timestamp,
                    equity=self.settings.initial_capital,
                    cash=self.settings.initial_capital,
                    open_positions=0,
                    drawdown_pct=(self.settings.initial_capital / peak - 1.0),
                )
            )
        return BacktestResult(
            strategy=strategy,
            trades=(),
            equity_curve=tuple(equity_curve),
            metrics=self._metrics(
                (), tuple(equity_curve), exposure_position_days=0, eligible_symbol_days=0
            ),
            warnings=dataset.warnings,
        )

    def _benchmark_result(
        self,
        strategy: StrategySpec,
        dataset: MarketDataset,
    ) -> BacktestResult:
        """Run the catalog's equal-weight hold-to-end comparator.

        The benchmark deliberately bypasses the alpha risk/reward gate because
        it has no profit target. It still uses next-bar fills, modeled costs,
        the frozen catastrophe stop, and explicit terminal liquidation.
        """

        start_index = int(strategy.parameters.get("start_index", 0))
        entry_index = start_index + 1
        eligible_symbols = tuple(
            symbol
            for symbol in dataset.symbols
            if len(dataset.bars_by_symbol[symbol]) > entry_index
        )
        if not eligible_symbols:
            result = self._cash_result(strategy, dataset)
            return replace(
                result,
                warnings=tuple(
                    dict.fromkeys(
                        (*result.warnings, "benchmark had no symbols with a valid next-bar fill")
                    )
                ),
            )

        cash = self.settings.initial_capital
        allocation = self.settings.initial_capital / len(eligible_symbols)
        positions: dict[str, _Position] = {}
        trades: list[TradeRecord] = []
        equity_curve: list[EquityPoint] = []
        warnings = list(dataset.warnings)
        peak_equity = self.settings.initial_capital
        exposure_position_days = 0
        eligible_symbol_days = 0
        max_bars = max(len(bars) for bars in dataset.bars_by_symbol.values())

        for index in range(max_bars):
            if index == entry_index:
                for symbol in eligible_symbols:
                    bars = dataset.bars_by_symbol[symbol]
                    signal = strategy.signal(dataset, symbol, bars, start_index)
                    if signal is None:
                        warnings.append(f"{symbol}: benchmark entry signal unavailable")
                        continue
                    bar = bars[index]
                    entry_price = bar.open * (1.0 + self._slippage_rate())
                    entry_fee_per_unit = entry_price * self.settings.fee_bps / 10_000.0
                    available = max(0.0, allocation - self.settings.commission_per_trade)
                    quantity = int(available // (entry_price + entry_fee_per_unit))
                    if quantity < 1:
                        warnings.append(f"{symbol}: benchmark allocation could not buy one unit")
                        continue
                    notional = quantity * entry_price
                    entry_fee = self._fee(notional)
                    position = _Position(
                        symbol=symbol,
                        direction=Direction.LONG,
                        quantity=quantity,
                        entry_price=entry_price,
                        stop=signal.stop,
                        target=None,
                        entry_time=bar.timestamp,
                        entry_index=index,
                        entry_notional=notional,
                        entry_fee=entry_fee,
                        entry_slippage=max(0.0, entry_price - bar.open) * quantity,
                        initial_risk=max(
                            0.0,
                            (entry_price - self._adverse_exit_fill(Direction.LONG, signal.stop))
                            * quantity
                            + entry_fee,
                        ),
                        signal=signal,
                    )
                    cash = self._apply_entry_cash(cash, position)
                    positions[symbol] = position

            if index >= entry_index:
                for symbol in list(positions):
                    bars = dataset.bars_by_symbol[symbol]
                    if len(bars) <= index:
                        continue
                    position = positions[symbol]
                    bar = bars[index]
                    if bar.open <= position.stop:
                        raw_exit, reason = bar.open, "gap_stop"
                    elif bar.low <= position.stop:
                        raw_exit, reason = position.stop, "stop"
                    else:
                        continue
                    trade, cash = self._close_position(
                        position,
                        bar,
                        index,
                        raw_exit,
                        reason,
                        cash,
                    )
                    trades.append(trade)
                    del positions[symbol]

            exposure_position_days += len(positions)
            eligible_symbol_days += len(eligible_symbols)
            equity = self._mark_to_market(cash, positions, dataset, index)
            peak_equity = max(peak_equity, equity)
            timestamp = self._timestamp_for_index(dataset, index)
            if timestamp is not None:
                equity_curve.append(
                    EquityPoint(
                        timestamp=timestamp,
                        equity=equity,
                        cash=cash,
                        open_positions=len(positions),
                        drawdown_pct=(equity / peak_equity - 1.0) if peak_equity else 0.0,
                    )
                )

        final_index = max_bars - 1
        for symbol in list(positions):
            bars = dataset.bars_by_symbol[symbol]
            index = min(final_index, len(bars) - 1)
            bar = bars[index]
            trade, cash = self._close_position(
                positions[symbol],
                bar,
                index,
                bar.close,
                "end_of_test_liquidation",
                cash,
            )
            trades.append(trade)
            del positions[symbol]

        if equity_curve:
            peak_equity = max(peak_equity, cash)
            equity_curve.append(
                EquityPoint(
                    timestamp=equity_curve[-1].timestamp,
                    equity=cash,
                    cash=cash,
                    open_positions=0,
                    drawdown_pct=(cash / peak_equity - 1.0) if peak_equity else 0.0,
                )
            )
        return BacktestResult(
            strategy=strategy,
            trades=tuple(trades),
            equity_curve=tuple(equity_curve),
            metrics=self._metrics(
                tuple(trades),
                tuple(equity_curve),
                exposure_position_days=exposure_position_days,
                eligible_symbol_days=eligible_symbol_days,
            ),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _timestamp_for_index(self, dataset: MarketDataset, index: int) -> datetime | None:
        timestamps = [
            bars[index].timestamp for bars in dataset.bars_by_symbol.values() if len(bars) > index
        ]
        return min(timestamps) if timestamps else None

    def _fee(self, notional: float) -> float:
        return self.settings.commission_per_trade + notional * self.settings.fee_bps / 10_000.0

    def _slippage_rate(self) -> float:
        return self.settings.slippage_bps / 10_000.0


def _equity_returns(equity_curve: tuple[EquityPoint, ...]) -> list[float]:
    returns: list[float] = []
    for previous, current in zip(equity_curve, equity_curve[1:], strict=False):
        if previous.equity:
            returns.append(current.equity / previous.equity - 1.0)
    return returns


def _annualized_return(total_return: float, equity_curve: tuple[EquityPoint, ...]) -> float | None:
    if len(equity_curve) < 2:
        return None
    start = equity_curve[0].timestamp
    end = equity_curve[-1].timestamp
    days = max((end - start).days, 1)
    years = days / 365.25
    if years <= 0:
        return None
    if total_return <= -1:
        return -1.0
    return (1.0 + total_return) ** (1.0 / years) - 1.0


def _drawdown_duration(equity_curve: tuple[EquityPoint, ...]) -> int:
    current = 0
    longest = 0
    for point in equity_curve:
        if point.drawdown_pct < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest
