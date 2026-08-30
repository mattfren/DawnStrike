"""Causal event-clock replay for retained intraday evidence.

This engine is deliberately separate from the daily backtester.  It consumes
ordered bar/decision events, enters only after a decision event, and records
provisional cost status instead of presenting an unverified simulation as
promotion evidence.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from intraday_scanner.v2.backtest.intraday_metrics import compute_intraday_metrics


@dataclass(frozen=True, slots=True)
class CausalMarketEvent:
    timestamp: datetime
    symbol: str
    kind: str
    payload: Mapping[str, Any]
    source_artifact_identity: str
    source_artifact_hash_sha256: str
    exchange_session_id: str
    sequence: int = 0

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("causal event timestamp must be timezone-aware UTC")
        if not self.source_artifact_identity or not self.source_artifact_hash_sha256:
            raise ValueError("causal event requires retained source artifact lineage")


@dataclass(frozen=True, slots=True)
class IntradayBacktestSettings:
    initial_equity: float = 100_000.0
    entry_slippage_bps: float = 50.0
    exit_slippage_bps: float = 50.0
    commission_per_share_per_side: float = 0.005
    max_concurrent_positions: int = 3
    empirical_cost_verified: bool = False
    cost_model_version: str = "alphaops-v5-cost-model-50bps-0.005ps"

    def __post_init__(self) -> None:
        if not math.isfinite(self.initial_equity) or self.initial_equity <= 0:
            raise ValueError("initial_equity must be finite and positive")
        for name, value in (
            ("entry_slippage_bps", self.entry_slippage_bps),
            ("exit_slippage_bps", self.exit_slippage_bps),
            ("commission_per_share_per_side", self.commission_per_share_per_side),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions must be positive")


@dataclass(frozen=True, slots=True)
class IntradayReplayTrade:
    trade_id: str
    symbol: str
    direction: str
    decision_at: datetime
    entry_at: datetime
    exit_at: datetime
    entry_price: float
    exit_price: float
    quantity: int
    stop: float
    target: float
    gross_pnl: float
    net_pnl: float
    fees_paid: float
    slippage_paid: float
    exit_reason: str
    source_artifact_hashes: tuple[str, ...]
    path_truth_status: str


@dataclass(frozen=True, slots=True)
class IntradayEquityPoint:
    timestamp: datetime
    session_id: str
    equity: float
    open_positions: int


@dataclass(frozen=True, slots=True)
class IntradayBacktestResult:
    trades: tuple[IntradayReplayTrade, ...]
    equity_curve: tuple[IntradayEquityPoint, ...]
    metrics: dict[str, Any]
    statuses: tuple[str, ...]
    warnings: tuple[str, ...]
    broker_execution_enabled: bool = False


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_id: str
    training_dates: tuple[str, ...]
    validation_dates: tuple[str, ...]
    holdout_dates: tuple[str, ...]
    purged_dates: tuple[str, ...]
    embargoed_dates: tuple[str, ...]
    no_lookahead: bool = True


SignalProvider = Callable[
    [CausalMarketEvent, tuple[CausalMarketEvent, ...]], Mapping[str, Any] | None
]


class IntradayBacktestEngine:
    """Replay a causal event stream with a fail-closed cost status."""

    def __init__(self, settings: IntradayBacktestSettings | None = None) -> None:
        self.settings = settings or IntradayBacktestSettings()

    def run(
        self,
        events: Sequence[CausalMarketEvent],
        *,
        signal_provider: SignalProvider | None = None,
        benchmark_returns: Mapping[str, float] | None = None,
    ) -> IntradayBacktestResult:
        # A source sequence is the authoritative ordering when it is present.
        # Fixtures and some providers omit it (all events then have sequence
        # zero), so use stable event identity as a deterministic tie-breaker.
        # This makes simultaneous symbols invariant to the order in which the
        # caller happened to supply their events without allowing a later
        # event-clock mark to leak into an earlier event.
        ordered = sorted(
            events,
            key=lambda event: (
                event.timestamp,
                event.sequence,
                event.symbol,
                event.kind,
                event.source_artifact_hash_sha256,
            ),
        )
        statuses = ["EMPIRICAL_COST_VERIFIED"] if self.settings.empirical_cost_verified else [
            "COST_MODEL_PROVISIONAL",
            "NOT_EVALUABLE_PENDING_EMPIRICAL_COST",
        ]
        warnings: list[str] = []
        trades: list[IntradayReplayTrade] = []
        equity_curve: list[IntradayEquityPoint] = []
        positions: dict[str, _OpenPosition] = {}
        # Marks are advanced only after the corresponding bar has been
        # processed.  Consequently this map contains exactly the latest mark
        # that was causally available at the current event-clock instant for
        # each symbol; it is intentionally sparse when a symbol has no mark.
        latest_marks: dict[str, float] = {}
        pending: list[_PendingSignal] = []
        history: list[CausalMarketEvent] = []
        previous_timestamp: datetime | None = None

        for event in ordered:
            if previous_timestamp is not None and event.timestamp < previous_timestamp:
                warnings.append("event clock was not monotonic; sorted before replay")
            previous_timestamp = event.timestamp
            if event.kind == "bar":
                self._close_positions_on_bar(event, positions, trades)
                self._enter_pending_on_bar(event, pending, positions, warnings)
                # The entry is at this bar's open; its later extrema are valid
                # causal evidence, with same-bar target/stop ordering explicit.
                self._close_positions_on_bar(event, positions, trades)
                close = _number(event.payload.get("close"))
                if close is not None:
                    latest_marks[event.symbol] = close
                equity_curve.append(
                    IntradayEquityPoint(
                        timestamp=event.timestamp,
                        session_id=event.exchange_session_id,
                        equity=self._mark_to_market(
                            event,
                            positions,
                            trades,
                            latest_marks,
                        ),
                        open_positions=len(positions),
                    )
                )
                if signal_provider is not None:
                    signal = signal_provider(event, tuple(history))
                    if signal is not None:
                        pending.append(
                            _PendingSignal(
                                decision_at=event.timestamp,
                                symbol=event.symbol,
                                signal=dict(signal),
                                artifact_hash=event.source_artifact_hash_sha256,
                            )
                        )
            elif event.kind == "decision":
                signal = _mapping(event.payload.get("signal"))
                if signal is not None:
                    pending.append(
                        _PendingSignal(
                            decision_at=event.timestamp,
                            symbol=event.symbol,
                            signal=dict(signal),
                            artifact_hash=event.source_artifact_hash_sha256,
                        )
                    )
            else:
                warnings.append(f"ignored unknown event kind: {event.kind}")
            history.append(event)

        if positions:
            # Iterating in event-clock order means the final retained bar per
            # symbol is the latest causal liquidation mark (the previous
            # reversed-comprehension selected the *first* bar instead).
            last_bars = {
                event.symbol: event for event in ordered if event.kind == "bar"
            }
            for symbol in sorted(positions):
                position = positions[symbol]
                last_event = last_bars.get(symbol)
                if last_event is None:
                    warnings.append(f"{symbol}: open position lacks liquidation bar")
                    continue
                self._close_position(
                    position,
                    last_event,
                    trades,
                    raw_exit=_number(last_event.payload.get("close")),
                    reason="end_of_test_liquidation",
                )
                del positions[symbol]

        session_returns = _session_returns(equity_curve, self.settings.initial_equity)
        metrics = compute_intraday_metrics(
            trades,
            equity_curve,
            session_returns=session_returns,
            benchmark_returns=benchmark_returns,
        )
        metrics.update(
            {
                "cost_model_version": self.settings.cost_model_version,
                "cost_model_status": statuses[0],
                "evaluation_status": (
                    "NOT_EVALUABLE_PENDING_EMPIRICAL_COST"
                    if not self.settings.empirical_cost_verified
                    else "RESEARCH_ONLY_PENDING_PROTOCOL_APPROVAL"
                ),
                "research_only": True,
                "broker_execution_enabled": False,
            }
        )
        return IntradayBacktestResult(
            trades=tuple(trades),
            equity_curve=tuple(equity_curve),
            metrics=metrics,
            statuses=tuple(statuses),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _enter_pending_on_bar(
        self,
        event: CausalMarketEvent,
        pending: list[_PendingSignal],
        positions: dict[str, _OpenPosition],
        warnings: list[str],
    ) -> None:
        if event.symbol in positions or len(positions) >= self.settings.max_concurrent_positions:
            return
        eligible = [
            item
            for item in pending
            if item.symbol == event.symbol and item.decision_at < event.timestamp
        ]
        if not eligible:
            return
        candidate = eligible[0]
        pending.remove(candidate)
        signal = candidate.signal
        if (
            signal.get("policy_eligible") is False
            or signal.get("eligible_for_official_paper") is False
        ):
            return
        entry_raw = _number(event.payload.get("open"))
        stop = _number(signal.get("stop", signal.get("invalidation_level")))
        target = _number(signal.get("target", signal.get("target_1")))
        quantity = _integer(signal.get("quantity"), default=1)
        if entry_raw is None or stop is None or target is None or quantity < 1:
            warnings.append(f"{event.symbol}: incomplete causal signal was not entered")
            return
        direction = str(signal.get("direction") or "long").lower()
        if direction not in {"long", "short"}:
            warnings.append(f"{event.symbol}: unsupported direction was not entered")
            return
        if direction == "long" and not target > entry_raw > stop:
            return
        if direction == "short" and not target < entry_raw < stop:
            return
        entry_price = _entry_fill(direction, entry_raw, self.settings.entry_slippage_bps)
        entry_fee = quantity * self.settings.commission_per_share_per_side
        positions[event.symbol] = _OpenPosition(
            symbol=event.symbol,
            direction=direction,
            decision_at=candidate.decision_at,
            entry_at=event.timestamp,
            entry_price=entry_price,
            raw_entry=entry_raw,
            stop=stop,
            target=target,
            quantity=quantity,
            entry_fee=entry_fee,
            artifact_hashes=(candidate.artifact_hash, event.source_artifact_hash_sha256),
        )

    def _close_positions_on_bar(
        self,
        event: CausalMarketEvent,
        positions: dict[str, _OpenPosition],
        trades: list[IntradayReplayTrade],
    ) -> None:
        position = positions.get(event.symbol)
        if position is None:
            return
        high = _number(event.payload.get("high"))
        low = _number(event.payload.get("low"))
        opening = _number(event.payload.get("open"))
        if high is None or low is None or opening is None:
            return
        stop_hit, target_hit = _touches(position, opening, high, low)
        if not stop_hit and not target_hit:
            return
        if stop_hit and target_hit:
            raw_exit = position.stop
            reason = "same_minute_ambiguous_stop_first"
            path_status = "SAME_MINUTE_AMBIGUOUS"
        elif stop_hit:
            raw_exit = position.stop
            reason = "stop_first"
            path_status = "RESOLVED_STOP_FIRST"
        else:
            raw_exit = position.target
            reason = "target_first"
            path_status = "RESOLVED_TARGET_FIRST"
        self._close_position(
            position,
            event,
            trades,
            raw_exit=raw_exit,
            reason=reason,
            path_status=path_status,
        )
        del positions[event.symbol]

    def _close_position(
        self,
        position: _OpenPosition,
        event: CausalMarketEvent,
        trades: list[IntradayReplayTrade],
        *,
        raw_exit: float | None,
        reason: str,
        path_status: str = "RESOLVED_SESSION_CLOSE",
    ) -> None:
        if raw_exit is None:
            return
        exit_price = _exit_fill(position.direction, raw_exit, self.settings.exit_slippage_bps)
        exit_fee = position.quantity * self.settings.commission_per_share_per_side
        if position.direction == "long":
            gross = (raw_exit - position.raw_entry) * position.quantity
        else:
            gross = (position.raw_entry - raw_exit) * position.quantity
        slippage = abs(position.entry_price - position.raw_entry) * position.quantity + abs(
            exit_price - raw_exit
        ) * position.quantity
        net = gross - position.entry_fee - exit_fee - slippage
        trades.append(
            IntradayReplayTrade(
                trade_id=f"{position.symbol}:{position.entry_at.isoformat()}:{len(trades) + 1}",
                symbol=position.symbol,
                direction=position.direction,
                decision_at=position.decision_at,
                entry_at=position.entry_at,
                exit_at=event.timestamp,
                entry_price=position.entry_price,
                exit_price=exit_price,
                quantity=position.quantity,
                stop=position.stop,
                target=position.target,
                gross_pnl=gross,
                net_pnl=net,
                fees_paid=position.entry_fee + exit_fee,
                slippage_paid=slippage,
                exit_reason=reason,
                source_artifact_hashes=tuple(
                    dict.fromkeys(
                        (*position.artifact_hashes, event.source_artifact_hash_sha256)
                    )
                ),
                path_truth_status=path_status,
            )
        )

    def _mark_to_market(
        self,
        event: CausalMarketEvent,
        positions: Mapping[str, _OpenPosition],
        trades: Sequence[IntradayReplayTrade],
        latest_marks: Mapping[str, float] | None = None,
    ) -> float:
        equity = self.settings.initial_equity + sum(trade.net_pnl for trade in trades)
        if latest_marks is None:
            # Keep the private helper backwards-compatible for callers that
            # provide only one event.  The replay path always passes its
            # sparse causal mark map above.
            current_close = _number(event.payload.get("close"))
            latest_marks = (
                {event.symbol: current_close} if current_close is not None else {}
            )
        for symbol, position in positions.items():
            close = _number(latest_marks.get(symbol))
            if close is None:
                # Missing evidence is not a price and must not become a
                # synthetic zero or an entry-price mark.
                continue
            direction = 1.0 if position.direction == "long" else -1.0
            equity += (close - position.raw_entry) * direction * position.quantity
        return equity


def run_intraday_backtest(
    events: Sequence[CausalMarketEvent],
    *,
    settings: IntradayBacktestSettings | None = None,
    signal_provider: SignalProvider | None = None,
    benchmark_returns: Mapping[str, float] | None = None,
) -> IntradayBacktestResult:
    return IntradayBacktestEngine(settings).run(
        events,
        signal_provider=signal_provider,
        benchmark_returns=benchmark_returns,
    )


def build_expanding_walk_forward_folds(
    session_dates: Sequence[str | date],
    *,
    minimum_training_sessions: int = 20,
    validation_sessions: int = 5,
    holdout_sessions: int = 5,
    purge_sessions: int = 1,
    embargo_sessions: int = 1,
) -> tuple[WalkForwardFold, ...]:
    """Build chronological folds with purge and embargo gaps."""

    normalized = sorted({
        value.isoformat() if isinstance(value, date) else str(value)
        for value in session_dates
    })
    folds: list[WalkForwardFold] = []
    cursor = minimum_training_sessions
    fold_number = 1
    while (
        cursor
        + purge_sessions
        + validation_sessions
        + embargo_sessions
        + holdout_sessions
        <= len(normalized)
    ):
        training_end = cursor
        purge = normalized[training_end : training_end + purge_sessions]
        validation_start = training_end + purge_sessions
        validation = normalized[validation_start : validation_start + validation_sessions]
        embargo_start = validation_start + validation_sessions
        embargo = normalized[embargo_start : embargo_start + embargo_sessions]
        holdout_start = embargo_start + embargo_sessions
        holdout = normalized[holdout_start : holdout_start + holdout_sessions]
        folds.append(
            WalkForwardFold(
                fold_id=f"intraday-fold-{fold_number}",
                training_dates=tuple(normalized[:training_end]),
                validation_dates=tuple(validation),
                holdout_dates=tuple(holdout),
                purged_dates=tuple(purge),
                embargoed_dates=tuple(embargo),
            )
        )
        cursor = holdout_start + holdout_sessions
        fold_number += 1
    return tuple(folds)


@dataclass(frozen=True, slots=True)
class _PendingSignal:
    decision_at: datetime
    symbol: str
    signal: dict[str, Any]
    artifact_hash: str


@dataclass(frozen=True, slots=True)
class _OpenPosition:
    symbol: str
    direction: str
    decision_at: datetime
    entry_at: datetime
    entry_price: float
    raw_entry: float
    stop: float
    target: float
    quantity: int
    entry_fee: float
    artifact_hashes: tuple[str, ...]


def _touches(
    position: _OpenPosition,
    opening: float,
    high: float,
    low: float,
) -> tuple[bool, bool]:
    if position.direction == "long":
        return (
            opening <= position.stop or low <= position.stop,
            opening >= position.target or high >= position.target,
        )
    return (
        opening >= position.stop or high >= position.stop,
        opening <= position.target or low <= position.target,
    )


def _entry_fill(direction: str, price: float, slippage_bps: float) -> float:
    rate = slippage_bps / 10_000.0
    return price * (1.0 + rate if direction == "long" else 1.0 - rate)


def _exit_fill(direction: str, price: float, slippage_bps: float) -> float:
    rate = slippage_bps / 10_000.0
    return price * (1.0 - rate if direction == "long" else 1.0 + rate)


def _session_returns(
    equity_curve: Sequence[IntradayEquityPoint], initial_equity: float
) -> dict[str, float]:
    closes: dict[str, float] = {}
    for point in equity_curve:
        closes[point.session_id] = point.equity
    previous = initial_equity
    returns: dict[str, float] = {}
    for session_id, equity in closes.items():
        returns[session_id] = equity / previous - 1.0 if previous else 0.0
        previous = equity
    return returns


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any, *, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


__all__ = [
    "CausalMarketEvent",
    "IntradayBacktestEngine",
    "IntradayBacktestResult",
    "IntradayBacktestSettings",
    "IntradayEquityPoint",
    "IntradayReplayTrade",
    "SignalProvider",
    "WalkForwardFold",
    "build_expanding_walk_forward_folds",
    "run_intraday_backtest",
]
