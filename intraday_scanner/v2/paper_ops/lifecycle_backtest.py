"""Historical adapter for the canonical PaperOps order and position lifecycle.

This is deliberately separate from the Alpha Lab backtester.  It reuses the
production PaperOps order, fill, risk-gate, position-check, and account functions
so scan-card historical summaries are evaluated under the same paper policy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from statistics import mean

from intraday_scanner.v2.backtest import (
    BacktestEngine,
    BacktestResult,
    BacktestSettings,
    EquityPoint,
    TradeRecord,
)
from intraday_scanner.v2.data import MarketBar, MarketDataset, timestamp_alignment_issues
from intraday_scanner.v2.paper_ops.engine import (
    _apply_close,
    _blocked_order_payload,
    _check_position,
    _fill_entry_block_reason,
    _fill_order,
    _latest_bar_on_or_before,
    _next_bar_after,
    _order_entry_block_reason,
    _order_from_pick,
    _paper_run,
    _picks_from_scan,
    _position_from_fill,
    _recalculate_unrealized_accounts,
    _strategy_semantics_fingerprint,
)
from intraday_scanner.v2.paper_ops.models import (
    LEGACY_PAPER_EXECUTION_POLICY_VERSION,
    PaperClose,
    PaperFill,
    PaperOpsConfig,
    PaperOrder,
    PaperPick,
    PaperPickDecision,
    PaperPosition,
    PaperRun,
    PaperRunMode,
    StrategyPaperAccount,
)
from intraday_scanner.v2.risk import RiskSettings, evaluate_signal_risk
from intraday_scanner.v2.scanner import ScanCard, ScanOutput
from intraday_scanner.v2.strategies import StrategySignal, StrategySpec
from intraday_scanner.v2.strategies import catalog as legacy_strategy_catalog

_AUDITED_CAUSAL_IDENTITIES = frozenset(
    {
        (
            "ts_momentum_sma_atr",
            "v1.0",
            "30585b86085f588041cdebca394fc8fe42aed8daf73a18aceb6e57bbf31bb602",
        ),
        (
            "donchian_breakout_20_10",
            "v1.0",
            "7e00b23b67ae059f30671b3b1086096fa83c90cea9755a617bcf7dadfc4912f0",
        ),
        (
            "cross_sectional_relative_strength",
            "v1.0",
            "5eaeb6846dac04479212a72c7f6a04a1e44e91b9f5dcffa4024c141ecbaf6fe0",
        ),
        (
            "pullback_reclaim_uptrend",
            "v1.0",
            "e13691fa30994163372d106f8ccd7b253b1417ec25b2179099c6906622740a3e",
        ),
        (
            "volatility_contraction_breakout",
            "v1.0",
            "045e4abcd4f86379d469fde5a30126684e4fb4727efba42726233b353c7f69c1",
        ),
        (
            "failed_breakout_reversal_short",
            "v1.0",
            "24d94ee059695bb8e47b7bf721c52f63bbeaf85fd8840756009656df37b5cec8",
        ),
        (
            "bullish_fvg_continuation",
            "v1.0",
            "8dadc36ae35f119159fa811ac53396ec63983bffd3b43577bce4f5b4a4813ed7",
        ),
        (
            "gap_up_continuation",
            "v1.0",
            "e5388d9dc52bbc827426d733e8f31cd8cc41a17e3792091b598bedde816e30e3",
        ),
        (
            "gap_up_continuation_atr",
            "v1.0",
            "441bbf043349c79d6ae87d79cbb68509fb5eaaae9ba0b790f066555005393608",
        ),
    }
)


@dataclass(frozen=True)
class PaperOpsLifecycleAudit:
    """Deterministic lifecycle evidence retained for parity tests and diagnostics."""

    runs: tuple[PaperRun, ...]
    picks: tuple[PaperPick, ...]
    orders_created: tuple[PaperOrder, ...]
    entry_blocks: tuple[dict[str, object], ...]
    fills: tuple[PaperFill, ...]
    fill_blocks: tuple[dict[str, object], ...]
    closes: tuple[PaperClose, ...]
    final_pending_orders: tuple[PaperOrder, ...]
    final_open_positions: tuple[PaperPosition, ...]
    final_accounts: tuple[StrategyPaperAccount, ...]


class PaperOpsLifecycleBacktestEngine:
    """Run historical bars through the production PaperOps lifecycle functions."""

    execution_model = "paper_ops_lifecycle"

    def __init__(
        self,
        config: PaperOpsConfig,
        *,
        mode: PaperRunMode = PaperRunMode.REPLAY,
    ) -> None:
        self.config = config
        self.mode = mode
        self.audit: PaperOpsLifecycleAudit | None = None
        self._metric_engine = BacktestEngine(
            BacktestSettings(
                initial_capital=config.starting_equity,
                fee_bps=config.fee_bps,
                slippage_bps=config.slippage_bps,
                max_gross_exposure_pct=config.max_gross_exposure_pct,
                max_concurrent_positions=config.max_concurrent_positions,
                risk=RiskSettings(
                    account_equity=config.starting_equity,
                    risk_per_trade_pct=config.risk_per_trade_pct,
                    max_position_pct=config.max_gross_exposure_pct,
                    min_reward_risk=config.min_reward_risk,
                    max_stop_distance_pct=config.max_stop_distance_pct,
                    max_risk_per_trade_pct=config.risk_per_trade_pct,
                    enforce_governed_common_gates=(
                        config.execution_policy_version
                        != LEGACY_PAPER_EXECUTION_POLICY_VERSION
                    ),
                ),
            )
        )

    def run(
        self,
        strategies: tuple[StrategySpec, ...],
        dataset: MarketDataset,
    ) -> dict[str, BacktestResult]:
        issues = timestamp_alignment_issues(dataset)
        if issues:
            raise ValueError(
                "PaperOps lifecycle backtests require timestamp-aligned bars: "
                + "; ".join(issues)
            )
        strategy_ids = [strategy.strategy_id for strategy in strategies]
        if len(strategy_ids) != len(set(strategy_ids)):
            raise ValueError("PaperOps lifecycle backtests require unique strategy ids")

        semantics = {
            strategy.strategy_id: _strategy_semantics_fingerprint(strategy)
            for strategy in strategies
        }
        accounts = {
            strategy.strategy_id: StrategyPaperAccount(
                strategy_id=strategy.strategy_id,
                strategy_version=strategy.version,
                starting_equity=self.config.starting_equity,
                current_equity=self.config.starting_equity,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                execution_policy_version=self.config.execution_policy_version,
                strategy_semantics_fingerprint=semantics[strategy.strategy_id],
            )
            for strategy in strategies
        }
        pending: list[PaperOrder] = []
        positions: list[PaperPosition] = []
        runs: list[PaperRun] = []
        all_picks: list[PaperPick] = []
        orders_created: list[PaperOrder] = []
        entry_blocks: list[dict[str, object]] = []
        fills: list[PaperFill] = []
        fill_blocks: list[dict[str, object]] = []
        closes: list[PaperClose] = []
        trades: dict[str, list[TradeRecord]] = {
            strategy.strategy_id: [] for strategy in strategies
        }
        equity_curves: dict[str, list[EquityPoint]] = {
            strategy.strategy_id: [] for strategy in strategies
        }
        exposure_position_days = {strategy.strategy_id: 0 for strategy in strategies}
        eligible_symbol_days = {strategy.strategy_id: 0 for strategy in strategies}
        peaks = {
            strategy.strategy_id: self.config.starting_equity for strategy in strategies
        }
        orders_by_id: dict[str, PaperOrder] = {}
        picks_by_id: dict[str, PaperPick] = {}
        fills_by_order: dict[str, PaperFill] = {}
        prior_daily_net: dict[tuple[str, str, str, str, str], float] = {}
        signal_cache = _CausalSignalCache(dataset, strategies, semantics)

        max_bars = max((len(bars) for bars in dataset.bars_by_symbol.values()), default=0)
        for index in range(max_bars):
            timestamp = _timestamp_for_index(dataset, index)
            if timestamp is None:
                continue
            as_of_dataset = _dataset_through_index(dataset, index)
            run_date = timestamp.date()
            snapshot_id = f"{dataset.dataset_id}:{run_date.isoformat()}"
            run = replace(
                _paper_run(
                    run_date=run_date,
                    mode=self.mode,
                    data_snapshot_id=snapshot_id,
                ),
                created_at=timestamp.isoformat(),
            )
            runs.append(run)
            daily_net_snapshot = {
                key[1:]: value
                for key, value in prior_daily_net.items()
                if key[0] == run.run_date
            }

            cards = _signal_cards(
                dataset=as_of_dataset,
                strategies=strategies,
                index=index,
                run=run,
                config=self.config,
                signal_cache=signal_cache,
            )
            picks = _picks_from_scan(
                ScanOutput(cards=cards, no_setup=(), warnings=dataset.warnings),
                strategies,
                run,
                self.config,
                dataset.warnings,
            )
            all_picks.extend(picks)
            picks_by_id.update((pick.pick_id, pick) for pick in picks)

            position_rows_before_check = [position.to_dict() for position in positions]
            for pick in picks:
                if pick.decision is not PaperPickDecision.ACCEPTED:
                    continue
                account = accounts[pick.strategy_id]
                order = _order_from_pick(
                    pick,
                    run,
                    self.config,
                    as_of_dataset,
                    equity_basis=account.current_equity,
                )
                reason = _order_entry_block_reason(
                    order,
                    position_rows=position_rows_before_check,
                    pending_rows=[item.to_dict() for item in pending],
                    account=account,
                    config=self.config,
                    daily_closed_net=daily_net_snapshot.get(
                        _strategy_lineage(order),
                        0.0,
                    ),
                    management_only=(
                        self.config.execution_policy_version
                        == LEGACY_PAPER_EXECUTION_POLICY_VERSION
                        and self.mode is PaperRunMode.FORWARD
                    ),
                )
                if reason is not None:
                    entry_blocks.append(_blocked_order_payload(order, reason, run))
                    continue
                pending.append(order)
                orders_created.append(order)
                orders_by_id[order.order_id] = order

            terminal_order_ids: set[str] = set()
            new_positions: list[PaperPosition] = []
            for order in pending:
                fill_bar = _next_bar_after(
                    dataset,
                    order.symbol,
                    order.signal_time,
                    run_date,
                )
                if fill_bar is None:
                    continue
                if fill_bar.timestamp.date() < run_date:
                    fill_blocks.append(
                        _blocked_order_payload(
                            order,
                            "missed_fill_session",
                            run,
                            source_bar=fill_bar,
                        )
                    )
                    terminal_order_ids.add(order.order_id)
                    continue
                account = accounts[order.strategy_id]
                fill = _fill_order(order, fill_bar, run, self.config)
                position = _position_from_fill(order, fill)
                reason = _fill_entry_block_reason(
                    order,
                    fill=fill,
                    position=position,
                    fill_bar=fill_bar,
                    position_rows=[
                        item.to_dict() for item in positions + new_positions
                    ],
                    pending_rows=[],
                    account=account,
                    config=self.config,
                    daily_closed_net=daily_net_snapshot.get(
                        _strategy_lineage(order),
                        0.0,
                    ),
                    management_only=(
                        self.config.execution_policy_version
                        == LEGACY_PAPER_EXECUTION_POLICY_VERSION
                        and self.mode is PaperRunMode.FORWARD
                    ),
                )
                terminal_order_ids.add(order.order_id)
                if reason is not None:
                    fill_blocks.append(
                        _blocked_order_payload(
                            order,
                            reason,
                            run,
                            source_bar=fill_bar,
                        )
                    )
                    continue
                fills.append(fill)
                fills_by_order[order.order_id] = fill
                new_positions.append(position)
            pending = [
                order for order in pending if order.order_id not in terminal_order_ids
            ]

            updated_positions: list[PaperPosition] = []
            day_closes: list[PaperClose] = []
            for position in positions + new_positions:
                bar = _latest_bar_on_or_before(dataset, position.symbol, run_date)
                if bar is None:
                    updated_positions.append(position)
                    continue
                checked, close_record = _check_position(
                    position,
                    bar,
                    run,
                    self.config,
                )
                if close_record is None:
                    updated_positions.append(checked)
                    continue
                closes.append(close_record)
                day_closes.append(close_record)
                accounts = _apply_close(accounts, close_record)
                order = orders_by_id[position.order_id]
                trades[position.strategy_id].append(
                    _trade_record(
                        dataset=dataset,
                        position=position,
                        close_record=close_record,
                        order=order,
                        pick=picks_by_id[order.pick_id],
                        fill=fills_by_order[order.order_id],
                    )
                )
            positions = updated_positions
            accounts = _recalculate_unrealized_accounts(
                accounts,
                [position.to_dict() for position in positions],
            )
            for close_record in day_closes:
                lineage = _close_lineage(close_record)
                daily_key = (run.run_date, *lineage)
                prior_daily_net[daily_key] = (
                    prior_daily_net.get(daily_key, 0.0) + close_record.net_pnl
                )

            eligible_count = sum(
                1
                for symbol in dataset.symbols
                if len(dataset.bars_by_symbol[symbol]) > index
            )
            for strategy in strategies:
                strategy_id = strategy.strategy_id
                account = accounts[strategy_id]
                open_count = sum(
                    1 for position in positions if position.strategy_id == strategy_id
                )
                exposure_position_days[strategy_id] += open_count
                eligible_symbol_days[strategy_id] += eligible_count
                peaks[strategy_id] = max(peaks[strategy_id], account.current_equity)
                drawdown = (
                    account.current_equity / peaks[strategy_id] - 1.0
                    if peaks[strategy_id]
                    else 0.0
                )
                equity_curves[strategy_id].append(
                    EquityPoint(
                        timestamp=timestamp,
                        equity=account.current_equity,
                        cash=account.starting_equity + account.realized_pnl,
                        open_positions=open_count,
                        drawdown_pct=drawdown,
                    )
                )

        self.audit = PaperOpsLifecycleAudit(
            runs=tuple(runs),
            picks=tuple(all_picks),
            orders_created=tuple(orders_created),
            entry_blocks=tuple(entry_blocks),
            fills=tuple(fills),
            fill_blocks=tuple(fill_blocks),
            closes=tuple(closes),
            final_pending_orders=tuple(pending),
            final_open_positions=tuple(positions),
            final_accounts=tuple(accounts[key] for key in sorted(accounts)),
        )
        return self._results(
            strategies=strategies,
            dataset=dataset,
            trades=trades,
            equity_curves=equity_curves,
            exposure_position_days=exposure_position_days,
            eligible_symbol_days=eligible_symbol_days,
        )

    def _results(
        self,
        *,
        strategies: tuple[StrategySpec, ...],
        dataset: MarketDataset,
        trades: dict[str, list[TradeRecord]],
        equity_curves: dict[str, list[EquityPoint]],
        exposure_position_days: dict[str, int],
        eligible_symbol_days: dict[str, int],
    ) -> dict[str, BacktestResult]:
        assert self.audit is not None
        results: dict[str, BacktestResult] = {}
        for strategy in strategies:
            strategy_id = strategy.strategy_id
            if strategy.status in {"benchmark", "baseline"}:
                comparator = self._metric_engine.run(strategy, dataset)
                comparator_metrics = dict(comparator.metrics)
                comparator_metrics.update(
                    {
                        "execution_model": "dedicated_comparator",
                        "orders_created": len(comparator.trades),
                        "orders_blocked": 0,
                        "fills": len(comparator.trades),
                        "open_position_count": 0,
                        "pending_order_count": 0,
                        "end_of_test_liquidations": sum(
                            trade.exit_reason == "end_of_test_liquidation"
                            for trade in comparator.trades
                        ),
                        "open_positions_marked_not_liquidated": 0,
                    }
                )
                results[strategy_id] = replace(
                    comparator,
                    metrics=comparator_metrics,
                )
                continue
            strategy_trades = tuple(trades[strategy_id])
            strategy_equity = tuple(equity_curves[strategy_id])
            metrics = self._metric_engine._metrics(
                strategy_trades,
                strategy_equity,
                exposure_position_days=exposure_position_days[strategy_id],
                eligible_symbol_days=eligible_symbol_days[strategy_id],
            )
            open_count = sum(
                1
                for position in self.audit.final_open_positions
                if position.strategy_id == strategy_id
            )
            pending_count = sum(
                1
                for order in self.audit.final_pending_orders
                if order.strategy_id == strategy_id
            )
            strategy_fills = tuple(
                fill for fill in self.audit.fills if fill.strategy_id == strategy_id
            )
            strategy_closes = tuple(
                close for close in self.audit.closes if close.strategy_id == strategy_id
            )
            metrics.update(
                {
                    "execution_model": self.execution_model,
                    "orders_created": sum(
                        1
                        for order in self.audit.orders_created
                        if order.strategy_id == strategy_id
                    ),
                    "orders_blocked": sum(
                        1
                        for row in (*self.audit.entry_blocks, *self.audit.fill_blocks)
                        if str(row.get("strategy_id") or "") == strategy_id
                    ),
                    "fills": len(strategy_fills),
                    "fees_paid": sum(fill.fee for fill in strategy_fills)
                    + sum(close.fee for close in strategy_closes),
                    "slippage_estimate": sum(
                        fill.slippage for fill in strategy_fills
                    )
                    + sum(close.slippage for close in strategy_closes),
                    "open_position_count": open_count,
                    "pending_order_count": pending_count,
                    "end_of_test_liquidations": 0,
                    "open_positions_marked_not_liquidated": open_count,
                }
            )
            warnings = list(dataset.warnings)
            if open_count:
                warnings.append(
                    f"paper_ops_lifecycle: {open_count} open position(s) were marked "
                    "at the final observed close and not liquidated"
                )
            if pending_count:
                warnings.append(
                    f"paper_ops_lifecycle: {pending_count} signal-close order(s) remained "
                    "pending without a future fill bar"
                )
            results[strategy_id] = BacktestResult(
                strategy=strategy,
                trades=strategy_trades,
                equity_curve=strategy_equity,
                metrics=metrics,
                warnings=tuple(dict.fromkeys(warnings)),
            )
        return results


def _signal_cards(
    *,
    dataset: MarketDataset,
    strategies: tuple[StrategySpec, ...],
    index: int,
    run: PaperRun,
    config: PaperOpsConfig,
    signal_cache: _CausalSignalCache,
) -> tuple[ScanCard, ...]:
    risk_settings = RiskSettings(
        account_equity=config.starting_equity,
        risk_per_trade_pct=config.risk_per_trade_pct,
        max_position_pct=config.max_gross_exposure_pct,
        min_reward_risk=config.min_reward_risk,
        max_stop_distance_pct=config.max_stop_distance_pct,
        max_risk_per_trade_pct=config.risk_per_trade_pct,
        enforce_governed_common_gates=(
            config.execution_policy_version != LEGACY_PAPER_EXECUTION_POLICY_VERSION
        ),
    )
    cards: list[ScanCard] = []
    for strategy in strategies:
        if strategy.status in {"benchmark", "baseline"}:
            continue
        for symbol in dataset.symbols:
            bars = dataset.bars_by_symbol[symbol]
            if len(bars) <= index:
                continue
            signal = signal_cache.signal(
                strategy=strategy,
                as_of_dataset=dataset,
                symbol=symbol,
                bars=bars,
                index=index,
            )
            if signal is None:
                continue
            risk = evaluate_signal_risk(
                signal,
                entry_price=signal.entry_reference,
                settings=risk_settings,
                stale=False,
            )
            cards.append(
                ScanCard(
                    symbol=symbol,
                    timestamp=bars[index].timestamp,
                    strategy_id=strategy.strategy_id,
                    strategy_version=strategy.version,
                    direction=signal.direction,
                    status="candidate",
                    setup_score=signal.score,
                    entry_trigger=(
                        f"Signal at close {signal.entry_reference:.2f}; default execution "
                        "is next bar open."
                    ),
                    stop=signal.stop,
                    target=signal.target,
                    risk_per_share=risk.risk_per_unit,
                    reward=risk.reward,
                    reward_risk=risk.reward_risk,
                    invalidation=signal.invalidation,
                    evidence=signal.evidence,
                    historical_summary="Canonical PaperOps lifecycle history.",
                    warnings=tuple(
                        dict.fromkeys(risk.warnings + signal.warnings)
                    ),
                    data_snapshot_id=run.data_snapshot_id,
                    run_manifest_id=run.run_id,
                )
            )
    return tuple(
        sorted(cards, key=lambda card: (-card.setup_score, card.symbol, card.strategy_id))
    )


class _CausalSignalCache:
    """Precompute audited built-in signals without exposing future bars.

    The original catalog functions are index-addressed and only read observations at
    or before ``index``. Giving those audited functions stable full-history tuples is
    therefore causally equivalent to the old prefix tuples, while allowing the legacy
    indicator cache to reuse one calculation per symbol. Unknown/custom strategies
    retain the stricter prefix-only call path below.

    The two research gap strategies build fresh full indicator histories internally.
    Their exact necessary conditions are evaluated cheaply first; the authoritative
    strategy function is still called for every possible signal, so this cache cannot
    synthesize or alter a pick.
    """

    def __init__(
        self,
        dataset: MarketDataset,
        strategies: tuple[StrategySpec, ...],
        semantics: dict[str, str],
    ) -> None:
        self._causal_strategy_objects = {
            id(strategy)
            for strategy in strategies
            if (
                strategy.strategy_id,
                strategy.version,
                semantics[strategy.strategy_id],
            )
            in _AUDITED_CAUSAL_IDENTITIES
        }
        self._signals: dict[tuple[int, str, int], StrategySignal] = {}

        # catalog.py keys reusable features by tuple identity. Remove only keys
        # colliding with this live dataset instead of globally clearing shared work.
        _discard_stale_legacy_features(dataset)
        for strategy in strategies:
            if id(strategy) not in self._causal_strategy_objects:
                continue
            if strategy.status in {"benchmark", "baseline"}:
                continue
            for symbol in dataset.symbols:
                bars = dataset.bars_by_symbol[symbol]
                for index in range(len(bars)):
                    if _research_prefilter_rejects(strategy, bars, index):
                        continue
                    signal = strategy.signal(dataset, symbol, bars, index)
                    if signal is not None:
                        self._signals[(id(strategy), symbol, index)] = signal

    def signal(
        self,
        *,
        strategy: StrategySpec,
        as_of_dataset: MarketDataset,
        symbol: str,
        bars: tuple[MarketBar, ...],
        index: int,
    ) -> StrategySignal | None:
        if id(strategy) in self._causal_strategy_objects:
            return self._signals.get((id(strategy), symbol, index))
        return strategy.signal(
            as_of_dataset,
            symbol,
            bars,
            index,
        )


def _research_prefilter_rejects(
    strategy: StrategySpec,
    bars: tuple[MarketBar, ...],
    index: int,
) -> bool:
    """Return true only where the authoritative research function must return None."""

    if strategy.strategy_id not in {
        "gap_up_continuation",
        "gap_up_continuation_atr",
    }:
        return False
    normalized = strategy.strategy_id == "gap_up_continuation_atr"

    trend_period = int(strategy.parameters["trend_sma_period"])
    volume_window = int(strategy.parameters["volume_window"])
    atr_period = int(strategy.parameters["atr_period"])
    minimum_index = max(
        trend_period - 1,
        volume_window,
        atr_period if normalized else atr_period - 1,
        1,
    )
    if index < minimum_index:
        return True

    bar = bars[index]
    previous = bars[index - 1]
    if not normalized:
        if previous.close <= 0:
            return True
        gap_pct = bar.open / previous.close - 1.0
        if gap_pct < float(strategy.parameters["min_gap_pct"]):
            return True
    else:
        atr_previous = _atr_at(bars, index - 1, atr_period)
        if atr_previous <= 0:
            return True
        gap = bar.open - previous.close
        if gap < float(strategy.parameters["min_gap_atr"]) * atr_previous:
            return True

    if bar.close <= bar.open:
        return True
    bar_range = bar.high - bar.low
    if bar_range <= 0:
        return True
    close_location = (bar.close - bar.low) / bar_range
    if close_location < float(strategy.parameters["min_close_location"]):
        return True

    trend = mean(
        item.close
        for item in bars[index + 1 - trend_period : index + 1]
    )
    if bar.close <= trend:
        return True
    prior_volume_mean = mean(
        item.volume
        for item in bars[index - volume_window : index]
    )
    if bar.volume < prior_volume_mean:
        return True

    atr_current = _atr_at(bars, index, atr_period)
    stop = bar.low - float(strategy.parameters["stop_atr_buffer"]) * atr_current
    return stop <= 0 or stop >= bar.close


def _atr_at(bars: tuple[MarketBar, ...], index: int, period: int) -> float:
    """Return the existing simple-window ATR value for one causally known index."""

    true_ranges: list[float] = []
    for cursor in range(index + 1 - period, index + 1):
        bar = bars[cursor]
        if cursor == 0:
            true_ranges.append(bar.high - bar.low)
            continue
        previous_close = bars[cursor - 1].close
        true_ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
    return mean(true_ranges)


def _discard_stale_legacy_features(dataset: MarketDataset) -> None:
    """Evict only legacy cache keys whose object ids belong to this dataset.

    The legacy cache stores ids without retaining their objects, so a collected
    tuple id can eventually be reused. Targeted eviction prevents a stale collision
    without globally clearing features that another evaluation may be using. A
    concurrent evaluator of this same live dataset can only repopulate the same
    deterministic values.
    """

    bar_tuple_ids = {id(bars) for bars in dataset.bars_by_symbol.values()}
    dataset_object_id = id(dataset)
    cache = legacy_strategy_catalog._FEATURE_CACHE
    cache_keys = tuple(cache)
    stale_keys = tuple(
        key
        for key in cache_keys
        if len(key) >= 2
        and (
            (key[0] == "rs_scores" and key[1] == dataset_object_id)
            or (key[0] != "rs_scores" and key[1] in bar_tuple_ids)
        )
    )
    for key in stale_keys:
        cache.pop(key, None)


def _trade_record(
    *,
    dataset: MarketDataset,
    position: PaperPosition,
    close_record: PaperClose,
    order: PaperOrder,
    pick: PaperPick,
    fill: PaperFill,
) -> TradeRecord:
    entry_time = datetime.fromisoformat(position.opened_at)
    exit_time = datetime.fromisoformat(close_record.close_time)
    entry_notional = position.entry_price * position.quantity
    holding_bars = sum(
        1
        for bar in dataset.bars_by_symbol.get(position.symbol, ())
        if entry_time <= bar.timestamp <= exit_time
    )
    return TradeRecord(
        trade_id=close_record.close_id,
        strategy_id=position.strategy_id,
        strategy_version=position.strategy_version,
        symbol=position.symbol,
        direction=position.direction,
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=position.entry_price,
        exit_price=close_record.close_price,
        stop=position.stop,
        target=position.target,
        quantity=position.quantity,
        gross_pnl=close_record.gross_pnl,
        net_pnl=close_record.net_pnl,
        return_pct=(close_record.net_pnl / entry_notional if entry_notional else 0.0),
        r_multiple=close_record.r_multiple,
        exit_reason=close_record.close_reason.value,
        holding_bars=holding_bars,
        fees_paid=position.entry_fee + close_record.fee,
        slippage_paid=fill.slippage + close_record.slippage,
        evidence=pick.evidence,
    )


def _timestamp_for_index(dataset: MarketDataset, index: int) -> datetime | None:
    timestamps = [
        bars[index].timestamp
        for bars in dataset.bars_by_symbol.values()
        if len(bars) > index
    ]
    return min(timestamps) if timestamps else None


def _dataset_through_index(dataset: MarketDataset, index: int) -> MarketDataset:
    """Present each historical scan with only information known at that close."""

    return MarketDataset(
        dataset_id=dataset.dataset_id,
        source_kind=dataset.source_kind,
        timeframe=dataset.timeframe,
        bars_by_symbol={
            symbol: bars[: index + 1]
            for symbol, bars in dataset.bars_by_symbol.items()
        },
        source_path=dataset.source_path,
        warnings=dataset.warnings,
        source_refs=dataset.source_refs,
    )


def _strategy_lineage(order: PaperOrder) -> tuple[str, str, str, str]:
    return (
        order.strategy_id,
        order.strategy_version,
        order.execution_policy_version,
        order.strategy_semantics_fingerprint,
    )


def _close_lineage(close_record: PaperClose) -> tuple[str, str, str, str]:
    return (
        close_record.strategy_id,
        close_record.strategy_version,
        close_record.execution_policy_version,
        close_record.strategy_semantics_fingerprint,
    )


__all__ = ["PaperOpsLifecycleAudit", "PaperOpsLifecycleBacktestEngine"]
