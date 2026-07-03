"""Deterministic research-only paper-pick lifecycle replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from intraday_scanner.v2.data import MarketBar, MarketDataset, ValidationResult
from intraday_scanner.v2.risk import RiskSettings, evaluate_signal_risk
from intraday_scanner.v2.strategies import Direction, StrategySignal, StrategySpec


@dataclass(frozen=True)
class PaperLifecycleSettings:
    account_equity: float = 100_000.0
    max_picks_per_day: int = 3
    max_open_positions: int = 3
    fee_bps: float = 1.0
    slippage_bps: float = 5.0
    min_setup_score: float = 60.0


@dataclass(frozen=True)
class PaperPick:
    pick_id: str
    run_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    signal_time: datetime
    direction: str
    setup_score: float
    entry_reference: float
    stop: float
    target: float | None
    risk_per_unit: float
    reward_risk: float | None
    status: str
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "pick_id": self.pick_id,
            "run_id": self.run_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "signal_time": self.signal_time.isoformat(),
            "direction": self.direction,
            "setup_score": round(self.setup_score, 6),
            "entry_reference": round(self.entry_reference, 6),
            "stop": round(self.stop, 6),
            "target": _round_optional(self.target),
            "risk_per_unit": round(self.risk_per_unit, 6),
            "reward_risk": _round_optional(self.reward_risk),
            "status": self.status,
            "evidence": list(self.evidence),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PaperEntry:
    entry_id: str
    pick_id: str
    strategy_id: str
    symbol: str
    direction: str
    entry_time: datetime
    entry_price: float
    quantity: int
    notional: float
    risk_amount: float
    entry_fee: float
    entry_slippage: float
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "pick_id": self.pick_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_time": self.entry_time.isoformat(),
            "entry_price": round(self.entry_price, 6),
            "quantity": self.quantity,
            "notional": round(self.notional, 6),
            "risk_amount": round(self.risk_amount, 6),
            "entry_fee": round(self.entry_fee, 6),
            "entry_slippage": round(self.entry_slippage, 6),
            "status": self.status,
        }


@dataclass(frozen=True)
class PaperCheck:
    check_id: str
    entry_id: str
    check_time: datetime
    symbol: str
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    stop_hit: bool
    target_hit: bool
    decision: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "entry_id": self.entry_id,
            "check_time": self.check_time.isoformat(),
            "symbol": self.symbol,
            "open_price": round(self.open_price, 6),
            "high_price": round(self.high_price, 6),
            "low_price": round(self.low_price, 6),
            "close_price": round(self.close_price, 6),
            "stop_hit": self.stop_hit,
            "target_hit": self.target_hit,
            "decision": self.decision,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PaperExit:
    exit_id: str
    entry_id: str
    pick_id: str
    strategy_id: str
    symbol: str
    direction: str
    exit_time: datetime
    exit_price: float
    exit_reason: str
    gross_pnl: float
    net_pnl: float
    return_pct: float
    r_multiple: float
    entry_fee: float
    exit_fee: float
    total_fees: float
    entry_slippage: float
    exit_slippage: float
    total_slippage: float

    def to_dict(self) -> dict[str, object]:
        return {
            "exit_id": self.exit_id,
            "entry_id": self.entry_id,
            "pick_id": self.pick_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "exit_time": self.exit_time.isoformat(),
            "exit_price": round(self.exit_price, 6),
            "exit_reason": self.exit_reason,
            "gross_pnl": round(self.gross_pnl, 6),
            "net_pnl": round(self.net_pnl, 6),
            "return_pct": round(self.return_pct, 8),
            "r_multiple": round(self.r_multiple, 6),
            "entry_fee": round(self.entry_fee, 6),
            "exit_fee": round(self.exit_fee, 6),
            "total_fees": round(self.total_fees, 6),
            "entry_slippage": round(self.entry_slippage, 6),
            "exit_slippage": round(self.exit_slippage, 6),
            "total_slippage": round(self.total_slippage, 6),
        }


@dataclass(frozen=True)
class StrategyPnl:
    strategy_id: str
    trade_count: int
    wins: int
    losses: int
    gross_pnl: float
    net_pnl: float
    return_on_equity: float
    fees_paid: float
    slippage_paid: float
    average_r: float
    best_trade: float
    worst_trade: float

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "trade_count": self.trade_count,
            "wins": self.wins,
            "losses": self.losses,
            "gross_pnl": round(self.gross_pnl, 6),
            "net_pnl": round(self.net_pnl, 6),
            "return_on_equity": round(self.return_on_equity, 8),
            "fees_paid": round(self.fees_paid, 6),
            "slippage_paid": round(self.slippage_paid, 6),
            "average_r": round(self.average_r, 6),
            "best_trade": round(self.best_trade, 6),
            "worst_trade": round(self.worst_trade, 6),
        }


@dataclass(frozen=True)
class CalendarReturn:
    market_date: date
    entry_count: int
    exit_count: int
    wins: int
    losses: int
    gross_pnl: float
    net_pnl: float
    return_on_equity: float

    def to_dict(self) -> dict[str, object]:
        return {
            "market_date": self.market_date.isoformat(),
            "entry_count": self.entry_count,
            "exit_count": self.exit_count,
            "wins": self.wins,
            "losses": self.losses,
            "gross_pnl": round(self.gross_pnl, 6),
            "net_pnl": round(self.net_pnl, 6),
            "return_on_equity": round(self.return_on_equity, 8),
        }


@dataclass(frozen=True)
class PaperAuditEvent:
    event_id: str
    event_type: str
    timestamp: datetime
    message: str
    refs: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "message": self.message,
            "refs": list(self.refs),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PaperLifecycleResult:
    run_id: str
    data_snapshot_id: str
    picks: tuple[PaperPick, ...]
    entries: tuple[PaperEntry, ...]
    checks: tuple[PaperCheck, ...]
    exits: tuple[PaperExit, ...]
    strategy_pnl: tuple[StrategyPnl, ...]
    calendar_returns: tuple[CalendarReturn, ...]
    audit_events: tuple[PaperAuditEvent, ...]
    warnings: tuple[str, ...]

    def summary(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "data_snapshot_id": self.data_snapshot_id,
            "pick_count": len(self.picks),
            "entry_count": len(self.entries),
            "check_count": len(self.checks),
            "exit_count": len(self.exits),
            "net_pnl": round(sum(item.net_pnl for item in self.exits), 6),
            "calendar_days": len(self.calendar_returns),
            "warnings": list(self.warnings),
        }


def run_paper_lifecycle(
    dataset: MarketDataset,
    validation: ValidationResult,
    strategies: tuple[StrategySpec, ...],
    *,
    run_id: str,
    data_snapshot_id: str,
    settings: PaperLifecycleSettings | None = None,
) -> PaperLifecycleResult:
    config = settings or PaperLifecycleSettings()
    risk_settings = RiskSettings(
        account_equity=config.account_equity,
        risk_per_trade_pct=0.01,
        max_position_pct=0.20,
    )
    warnings = list(dataset.warnings) + list(validation.warnings)
    audit_events: list[PaperAuditEvent] = [
        PaperAuditEvent(
            event_id=f"{run_id}:data_validated",
            event_type="data_validated",
            timestamp=dataset.latest_timestamp or datetime.min,
            message=(
                f"Dataset {dataset.dataset_id} validation passed={validation.passed}; "
                f"source_kind={dataset.source_kind}."
            ),
            refs=(data_snapshot_id, validation.dataset_id),
            warnings=tuple(warnings),
        )
    ]
    picks: list[PaperPick] = []
    entries: list[PaperEntry] = []
    checks: list[PaperCheck] = []
    exits: list[PaperExit] = []
    eligible_strategies = tuple(
        strategy for strategy in strategies if strategy.status not in {"benchmark", "baseline"}
    )
    max_bars = max((len(bars) for bars in dataset.bars_by_symbol.values()), default=0)

    for entry_index in range(1, max_bars):
        candidates: list[tuple[StrategySignal, MarketBar, MarketBar]] = []
        for strategy in eligible_strategies:
            for symbol in dataset.symbols:
                bars = dataset.bars_by_symbol[symbol]
                if len(bars) <= entry_index:
                    continue
                signal_bar = bars[entry_index - 1]
                signal = strategy.signal(dataset, symbol, bars, entry_index - 1)
                if signal is not None and signal.score >= config.min_setup_score:
                    candidates.append((signal, signal_bar, bars[entry_index]))
        candidates.sort(key=lambda item: (-item[0].score, item[0].symbol, item[0].strategy_id))
        for signal, signal_bar, bar in candidates[: config.max_picks_per_day]:
            pick, entry, check, exit_record, events = _paper_trade(
                signal,
                signal_bar,
                bar,
                run_id=run_id,
                risk_settings=risk_settings,
                settings=config,
            )
            if pick is not None:
                picks.append(pick)
            audit_events.extend(events)
            if entry is None or check is None or exit_record is None:
                continue
            entries.append(entry)
            checks.append(check)
            exits.append(exit_record)

    strategy_pnl = _strategy_pnl(exits, config.account_equity)
    calendar_returns = _calendar_returns(entries, exits, config.account_equity)
    audit_events.extend(
        _summary_events(
            run_id,
            dataset.latest_timestamp or datetime.min,
            strategy_pnl,
            calendar_returns,
        )
    )
    return PaperLifecycleResult(
        run_id=run_id,
        data_snapshot_id=data_snapshot_id,
        picks=tuple(picks),
        entries=tuple(entries),
        checks=tuple(checks),
        exits=tuple(exits),
        strategy_pnl=strategy_pnl,
        calendar_returns=calendar_returns,
        audit_events=tuple(audit_events),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _paper_trade(
    signal: StrategySignal,
    signal_bar: MarketBar,
    bar: MarketBar,
    *,
    run_id: str,
    risk_settings: RiskSettings,
    settings: PaperLifecycleSettings,
) -> tuple[
    PaperPick | None,
    PaperEntry | None,
    PaperCheck | None,
    PaperExit | None,
    list[PaperAuditEvent],
]:
    entry_price, entry_slippage = _entry_price(signal.direction, bar.open, settings.slippage_bps)
    risk_decision = evaluate_signal_risk(signal, entry_price=entry_price, settings=risk_settings)
    events: list[PaperAuditEvent] = []
    pick_id = f"{run_id}:pick:{signal.strategy_id}:{signal.symbol}:{bar.timestamp.date()}"
    pick = PaperPick(
        pick_id=pick_id,
        run_id=run_id,
        strategy_id=signal.strategy_id,
        strategy_version=signal.strategy_version,
        symbol=signal.symbol,
        signal_time=signal_bar.timestamp,
        direction=signal.direction,
        setup_score=signal.score,
        entry_reference=signal.entry_reference,
        stop=signal.stop,
        target=signal.target,
        risk_per_unit=risk_decision.risk_per_unit,
        reward_risk=risk_decision.reward_risk,
        status="selected" if risk_decision.allowed else "rejected_by_risk",
        evidence=signal.evidence,
        warnings=risk_decision.warnings,
    )
    events.append(
        PaperAuditEvent(
            event_id=f"{pick_id}:created",
            event_type="paper_pick_created",
            timestamp=bar.timestamp,
            message=f"Paper pick created for {signal.symbol} via {signal.strategy_id}.",
            refs=(pick_id,),
            warnings=pick.warnings,
        )
    )
    if not risk_decision.allowed or risk_decision.quantity <= 0:
        return pick, None, None, None, events

    entry_id = pick_id.replace(":pick:", ":entry:")
    notional = risk_decision.quantity * entry_price
    entry_fee = _fee(notional, settings.fee_bps)
    entry = PaperEntry(
        entry_id=entry_id,
        pick_id=pick_id,
        strategy_id=signal.strategy_id,
        symbol=signal.symbol,
        direction=signal.direction,
        entry_time=bar.timestamp,
        entry_price=entry_price,
        quantity=risk_decision.quantity,
        notional=notional,
        risk_amount=risk_decision.risk_amount,
        entry_fee=entry_fee,
        entry_slippage=entry_slippage * risk_decision.quantity,
        status="paper_opened",
    )
    events.append(
        PaperAuditEvent(
            event_id=f"{entry_id}:opened",
            event_type="paper_entry_opened",
            timestamp=bar.timestamp,
            message=f"Paper entry opened for {signal.symbol}; no live execution involved.",
            refs=(pick_id, entry_id),
        )
    )
    check, raw_exit_price, exit_reason = _check_bar(signal, entry, bar)
    exit_price, exit_slippage = _exit_price(signal.direction, raw_exit_price, settings.slippage_bps)
    exit_notional = exit_price * entry.quantity
    exit_fee = _fee(exit_notional, settings.fee_bps)
    exit_slippage_total = exit_slippage * entry.quantity
    total_fees = entry.entry_fee + exit_fee
    total_slippage = entry.entry_slippage + exit_slippage_total
    if signal.direction == Direction.LONG:
        gross_pnl = (exit_price - entry.entry_price) * entry.quantity
    else:
        gross_pnl = (entry.entry_price - exit_price) * entry.quantity
    net_pnl = gross_pnl - total_fees
    denominator = entry.notional if entry.notional else 1.0
    exit_record = PaperExit(
        exit_id=entry_id.replace(":entry:", ":exit:"),
        entry_id=entry.entry_id,
        pick_id=pick.pick_id,
        strategy_id=signal.strategy_id,
        symbol=signal.symbol,
        direction=signal.direction,
        exit_time=bar.timestamp,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        return_pct=net_pnl / denominator,
        r_multiple=net_pnl / entry.risk_amount if entry.risk_amount else 0.0,
        entry_fee=entry.entry_fee,
        exit_fee=exit_fee,
        total_fees=total_fees,
        entry_slippage=entry.entry_slippage,
        exit_slippage=exit_slippage_total,
        total_slippage=total_slippage,
    )
    events.extend(
        [
            PaperAuditEvent(
                event_id=f"{check.check_id}:evaluated",
                event_type="intraday_check_evaluated",
                timestamp=bar.timestamp,
                message=f"OHLC check selected {check.decision} for {signal.symbol}.",
                refs=(entry.entry_id, check.check_id),
                warnings=check.warnings,
            ),
            PaperAuditEvent(
                event_id=f"{exit_record.exit_id}:closed",
                event_type="paper_exit_closed",
                timestamp=bar.timestamp,
                message=(
                    f"Paper exit closed for {signal.symbol}; reason={exit_record.exit_reason}; "
                    f"net_pnl={exit_record.net_pnl:.2f}."
                ),
                refs=(entry.entry_id, exit_record.exit_id),
            ),
        ]
    )
    return pick, entry, check, exit_record, events


def _check_bar(
    signal: StrategySignal,
    entry: PaperEntry,
    bar: MarketBar,
) -> tuple[PaperCheck, float, str]:
    if signal.direction == Direction.LONG:
        stop_hit = bar.low <= signal.stop
        target_hit = signal.target is not None and bar.high >= signal.target
        if stop_hit:
            decision = "stop_first" if target_hit else "stop"
            raw_exit_price = signal.stop
            exit_reason = "stop"
        elif target_hit and signal.target is not None:
            decision = "target"
            raw_exit_price = signal.target
            exit_reason = "target"
        else:
            decision = "eod_close"
            raw_exit_price = bar.close
            exit_reason = "eod_close"
    else:
        stop_hit = bar.high >= signal.stop
        target_hit = signal.target is not None and bar.low <= signal.target
        if stop_hit:
            decision = "stop_first" if target_hit else "stop"
            raw_exit_price = signal.stop
            exit_reason = "stop"
        elif target_hit and signal.target is not None:
            decision = "target"
            raw_exit_price = signal.target
            exit_reason = "target"
        else:
            decision = "eod_close"
            raw_exit_price = bar.close
            exit_reason = "eod_close"
    warnings = ("same_bar_stop_target_stop_first",) if stop_hit and target_hit else ()
    return (
        PaperCheck(
            check_id=entry.entry_id.replace(":entry:", ":check:"),
            entry_id=entry.entry_id,
            check_time=bar.timestamp,
            symbol=entry.symbol,
            open_price=bar.open,
            high_price=bar.high,
            low_price=bar.low,
            close_price=bar.close,
            stop_hit=stop_hit,
            target_hit=target_hit,
            decision=decision,
            warnings=warnings,
        ),
        raw_exit_price,
        exit_reason,
    )


def _strategy_pnl(
    exits: list[PaperExit],
    account_equity: float,
) -> tuple[StrategyPnl, ...]:
    rows: list[StrategyPnl] = []
    for strategy_id in sorted({exit_record.strategy_id for exit_record in exits}):
        strategy_exits = [
            exit_record for exit_record in exits if exit_record.strategy_id == strategy_id
        ]
        net_values = [exit_record.net_pnl for exit_record in strategy_exits]
        gross_values = [exit_record.gross_pnl for exit_record in strategy_exits]
        r_values = [exit_record.r_multiple for exit_record in strategy_exits]
        rows.append(
            StrategyPnl(
                strategy_id=strategy_id,
                trade_count=len(strategy_exits),
                wins=sum(1 for value in net_values if value > 0),
                losses=sum(1 for value in net_values if value < 0),
                gross_pnl=sum(gross_values),
                net_pnl=sum(net_values),
                return_on_equity=sum(net_values) / account_equity if account_equity else 0.0,
                fees_paid=sum(exit_record.total_fees for exit_record in strategy_exits),
                slippage_paid=sum(exit_record.total_slippage for exit_record in strategy_exits),
                average_r=sum(r_values) / len(r_values) if r_values else 0.0,
                best_trade=max(net_values) if net_values else 0.0,
                worst_trade=min(net_values) if net_values else 0.0,
            )
        )
    return tuple(rows)


def _calendar_returns(
    entries: list[PaperEntry],
    exits: list[PaperExit],
    account_equity: float,
) -> tuple[CalendarReturn, ...]:
    dates = sorted(
        {entry.entry_time.date() for entry in entries}
        | {exit_record.exit_time.date() for exit_record in exits}
    )
    rows: list[CalendarReturn] = []
    for market_date in dates:
        day_entries = [entry for entry in entries if entry.entry_time.date() == market_date]
        day_exits = [
            exit_record for exit_record in exits if exit_record.exit_time.date() == market_date
        ]
        gross_pnl = sum(exit_record.gross_pnl for exit_record in day_exits)
        net_pnl = sum(exit_record.net_pnl for exit_record in day_exits)
        rows.append(
            CalendarReturn(
                market_date=market_date,
                entry_count=len(day_entries),
                exit_count=len(day_exits),
                wins=sum(1 for exit_record in day_exits if exit_record.net_pnl > 0),
                losses=sum(1 for exit_record in day_exits if exit_record.net_pnl < 0),
                gross_pnl=gross_pnl,
                net_pnl=net_pnl,
                return_on_equity=net_pnl / account_equity if account_equity else 0.0,
            )
        )
    return tuple(rows)


def _summary_events(
    run_id: str,
    timestamp: datetime,
    strategy_pnl: tuple[StrategyPnl, ...],
    calendar_returns: tuple[CalendarReturn, ...],
) -> tuple[PaperAuditEvent, ...]:
    return (
        PaperAuditEvent(
            event_id=f"{run_id}:strategy_pnl",
            event_type="strategy_pnl_summarized",
            timestamp=timestamp,
            message=f"Summarized strategy-level P&L for {len(strategy_pnl)} strategies.",
            refs=tuple(row.strategy_id for row in strategy_pnl),
        ),
        PaperAuditEvent(
            event_id=f"{run_id}:calendar_returns",
            event_type="calendar_returns_summarized",
            timestamp=timestamp,
            message=f"Summarized calendar returns for {len(calendar_returns)} market dates.",
            refs=tuple(row.market_date.isoformat() for row in calendar_returns),
        ),
    )


def _entry_price(direction: str, raw_open: float, slippage_bps: float) -> tuple[float, float]:
    rate = slippage_bps / 10_000.0
    if direction == Direction.LONG:
        price = raw_open * (1.0 + rate)
        return price, price - raw_open
    price = raw_open * (1.0 - rate)
    return price, raw_open - price


def _exit_price(direction: str, raw_exit: float, slippage_bps: float) -> tuple[float, float]:
    rate = slippage_bps / 10_000.0
    if direction == Direction.LONG:
        price = raw_exit * (1.0 - rate)
        return price, raw_exit - price
    price = raw_exit * (1.0 + rate)
    return price, price - raw_exit


def _fee(notional: float, fee_bps: float) -> float:
    return notional * fee_bps / 10_000.0


def _round_optional(value: float | None) -> float | None:
    return None if value is None else round(value, 6)
