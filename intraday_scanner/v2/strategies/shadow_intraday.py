"""Bounded, causal intraday strategy hypotheses for shadow research.

This module is intentionally independent of the champion strategy catalog.  A
shadow hypothesis can produce an observation or a signal for research, but it
cannot produce a broker order.  Every evaluation is point-in-time and routes
any proposed paper entry through :class:`PortfolioRiskAuthority`.

The implementation is deliberately conservative: missing quote, halt,
borrow, SSR, corporate-action, VWAP, or catalyst truth is an explicit
``NOT_EVALUABLE`` result.  It is never converted into a weak signal.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.risk import (
    PortfolioOrderProposal,
    PortfolioRiskAuthority,
    PortfolioRiskDecision,
    PortfolioRiskSnapshot,
)
from intraday_scanner.v2.backtest.intraday_engine import CausalMarketEvent

MARKET_TZ = ZoneInfo("America/Chicago")
SHADOW_PROTOCOL_VERSION = "dawnstrike-intraday-shadow-protocol-v1"
EMPIRICAL_COST_REQUIRED = "NOT_EVALUABLE_PENDING_EMPIRICAL_COST"


@dataclass(frozen=True, slots=True)
class ShadowStrategyConfig:
    strategy_id: str
    version: str
    hypothesis: str
    session_start: time
    session_end: time
    timeout_minutes: int
    stop_geometry: str
    target_r_multiple: float
    required_truth: tuple[str, ...] = ()
    empirical_cost_required: bool = True
    shadow_only: bool = True
    broker_execution_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "hypothesis": self.hypothesis,
            "session_window_ct": {
                "start": self.session_start.isoformat(),
                "end": self.session_end.isoformat(),
            },
            "timeout_minutes": self.timeout_minutes,
            "stop_geometry": self.stop_geometry,
            "target_r_multiple": self.target_r_multiple,
            "required_truth": list(self.required_truth),
            "empirical_cost_required": self.empirical_cost_required,
            "shadow_only": self.shadow_only,
            "broker_execution_enabled": self.broker_execution_enabled,
            "protocol_version": SHADOW_PROTOCOL_VERSION,
        }

    @property
    def config_hash_sha256(self) -> str:
        return _sha256(self.to_dict())


@dataclass(frozen=True, slots=True)
class ShadowSignal:
    strategy_id: str
    strategy_version: str
    symbol: str
    direction: str
    decision_at: datetime
    earliest_entry_at: datetime
    entry_reference: float
    stop_price: float
    target_price: float
    timeout_at: datetime
    evidence: tuple[str, ...]
    config_hash_sha256: str
    research_only: bool = True
    broker_execution_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "direction": self.direction,
            "decision_at": self.decision_at.isoformat(),
            "earliest_entry_at": self.earliest_entry_at.isoformat(),
            "entry_reference": self.entry_reference,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "timeout_at": self.timeout_at.isoformat(),
            "evidence": list(self.evidence),
            "config_hash_sha256": self.config_hash_sha256,
            "research_only": self.research_only,
            "broker_execution_enabled": self.broker_execution_enabled,
        }


@dataclass(frozen=True, slots=True)
class ShadowEvaluation:
    strategy_id: str
    strategy_version: str
    symbol: str
    decision_at: datetime
    status: str
    reason_codes: tuple[str, ...] = ()
    signal: ShadowSignal | None = None
    risk_decision: PortfolioRiskDecision | None = None
    empirical_cost_status: str = EMPIRICAL_COST_REQUIRED
    research_only: bool = True
    broker_execution_enabled: bool = False

    @property
    def eligible(self) -> bool:
        return self.status == "SHADOW_SIGNAL" and self.signal is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "decision_at": self.decision_at.isoformat(),
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "signal": self.signal.to_dict() if self.signal else None,
            "risk_decision": self.risk_decision.to_dict() if self.risk_decision else None,
            "empirical_cost_status": self.empirical_cost_status,
            "research_only": self.research_only,
            "broker_execution_enabled": self.broker_execution_enabled,
        }


@dataclass(frozen=True, slots=True)
class ShadowStrategyRegistry:
    strategies: tuple[ShadowStrategyConfig, ...]
    protocol_version: str = SHADOW_PROTOCOL_VERSION

    def get(self, strategy_id: str) -> ShadowStrategyConfig:
        for strategy in self.strategies:
            if strategy.strategy_id == strategy_id:
                return strategy
        raise KeyError(f"unknown shadow strategy: {strategy_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "dawnstrike.intraday_shadow_registry.v1",
            "protocol_version": self.protocol_version,
            "strategies": [strategy.to_dict() for strategy in self.strategies],
            "automatic_promotion": False,
            "broker_execution_enabled": False,
        }


def build_shadow_strategy_registry() -> ShadowStrategyRegistry:
    """Return the frozen, preregistered strategy families.

    The failed-breakout/gap-fade family is intentionally admitted to the
    registry even when current data cannot evaluate it.  The evaluator emits
    explicit missing-truth codes until all point-in-time safety evidence is
    retained.
    """

    return ShadowStrategyRegistry(
        strategies=(
            ShadowStrategyConfig(
                "shadow_opening_range_continuation",
                "v1.0.0",
                "Continuation after a completed 15-minute opening range.",
                time(9, 45),
                time(10, 45),
                45,
                "below opening-range low",
                2.0,
            ),
            ShadowStrategyConfig(
                "shadow_vwap_reclaim_pullback",
                "v1.0.0",
                "Long only after a prior-bar VWAP pullback and reclaim.",
                time(10, 0),
                time(14, 30),
                60,
                "below pullback low",
                2.0,
                ("vwap",),
            ),
            ShadowStrategyConfig(
                "shadow_catalyst_continuation",
                "v1.0.0",
                "Continuation only after a retained, point-in-time catalyst event.",
                time(9, 35),
                time(12, 0),
                45,
                "below catalyst confirmation low",
                2.0,
                ("catalyst",),
            ),
            ShadowStrategyConfig(
                "shadow_failed_breakout_gap_fade",
                "v1.0.0",
                "Short failed breakout or gap fade with complete market-safety truth.",
                time(10, 0),
                time(14, 30),
                45,
                "above rejection high",
                2.0,
                ("quote", "spread", "halt", "borrow", "ssr", "corporate_action"),
            ),
        )
    )


def evaluate_shadow_strategy(
    strategy_id: str,
    current: CausalMarketEvent,
    history: Sequence[CausalMarketEvent] = (),
    *,
    registry: ShadowStrategyRegistry | None = None,
    risk_snapshot: PortfolioRiskSnapshot | Mapping[str, Any] | None = None,
    risk_authority: PortfolioRiskAuthority | None = None,
    empirical_cost_verified: bool = False,
) -> ShadowEvaluation:
    """Evaluate one event using only events strictly earlier than it."""

    active_registry = registry or build_shadow_strategy_registry()
    config = active_registry.get(strategy_id)
    prior = tuple(
        sorted(
            (event for event in history if event.timestamp < current.timestamp),
            key=lambda event: (event.timestamp, event.sequence),
        )
    )
    symbol = current.symbol.upper()
    base = dict(
        strategy_id=config.strategy_id,
        strategy_version=config.version,
        symbol=symbol,
        decision_at=current.timestamp,
        empirical_cost_status="EMPIRICAL_COST_VERIFIED"
        if empirical_cost_verified
        else EMPIRICAL_COST_REQUIRED,
    )
    reasons = _common_rejections(config, current, prior)
    if reasons:
        return ShadowEvaluation(
            **base, status="NOT_EVALUABLE", reason_codes=tuple(dict.fromkeys(reasons))
        )
    local_time = current.timestamp.astimezone(MARKET_TZ).time().replace(tzinfo=None)
    if not (config.session_start <= local_time < config.session_end):
        return ShadowEvaluation(
            **base, status="NOT_EVALUABLE", reason_codes=("OUTSIDE_SESSION_WINDOW",)
        )

    if strategy_id == "shadow_opening_range_continuation":
        geometry = _opening_range_signal(current, prior)
    elif strategy_id == "shadow_vwap_reclaim_pullback":
        geometry = _vwap_signal(current, prior)
    elif strategy_id == "shadow_catalyst_continuation":
        geometry = _catalyst_signal(current, prior)
    elif strategy_id == "shadow_failed_breakout_gap_fade":
        geometry = _gap_fade_signal(current, prior)
    else:  # pragma: no cover - registry prevents this
        geometry = (None, ("UNKNOWN_STRATEGY",))
    signal_values, setup_reasons = geometry
    if signal_values is None:
        return ShadowEvaluation(**base, status="NOT_EVALUABLE", reason_codes=tuple(setup_reasons))

    signal = _build_signal(config, current, signal_values, setup_reasons)
    authority = risk_authority or PortfolioRiskAuthority()
    snapshot = _snapshot(risk_snapshot)
    proposal = PortfolioOrderProposal(
        symbol=symbol,
        side=signal.direction,
        quantity=1,
        price=signal.entry_reference,
        stop_price=signal.stop_price,
        strategy_id=config.strategy_id,
        metadata_complete=True,
        price_observed_at=current.timestamp.isoformat(),
        live_execution_requested=False,
    )
    risk = authority.evaluate(proposal, snapshot, now=current.timestamp)
    if not risk.allowed:
        return ShadowEvaluation(
            **base,
            status="SHADOW_BLOCKED_RISK",
            reason_codes=tuple(dict.fromkeys((*setup_reasons, *risk.reason_codes))),
            signal=signal,
            risk_decision=risk,
        )
    if not empirical_cost_verified:
        return ShadowEvaluation(
            **base,
            status=EMPIRICAL_COST_REQUIRED,
            reason_codes=tuple(dict.fromkeys((*setup_reasons, EMPIRICAL_COST_REQUIRED))),
            signal=signal,
            risk_decision=risk,
        )
    return ShadowEvaluation(
        **base,
        status="SHADOW_SIGNAL",
        reason_codes=tuple(setup_reasons),
        signal=signal,
        risk_decision=risk,
    )


def evaluate_shadow_event(
    current: CausalMarketEvent,
    history: Sequence[CausalMarketEvent] = (),
    **kwargs: Any,
) -> tuple[ShadowEvaluation, ...]:
    """Evaluate every frozen family; no family is forced to emit a trade."""

    registry = kwargs.pop("registry", None) or build_shadow_strategy_registry()
    return tuple(
        evaluate_shadow_strategy(
            strategy.strategy_id, current, history, registry=registry, **kwargs
        )
        for strategy in registry.strategies
    )


def _common_rejections(
    config: ShadowStrategyConfig,
    current: CausalMarketEvent,
    prior: Sequence[CausalMarketEvent],
) -> list[str]:
    if current.kind != "bar":
        return ["CURRENT_EVENT_NOT_BAR"]
    payload = current.payload
    if not _valid_bar(payload):
        return ["REQUIRED_OHLCV_UNAVAILABLE"]
    if bool(payload.get("halted")) or str(payload.get("halt_status") or "").lower() in {
        "halted",
        "halt",
    }:
        return ["CURRENT_HALT"]
    if any(
        event.kind in {"halt", "trading_halt"}
        and event.symbol.upper() == current.symbol.upper()
        and event.timestamp <= current.timestamp
        for event in prior
    ):
        return ["HALT_TRUTH_BLOCKED"]
    local_time = current.timestamp.astimezone(MARKET_TZ).time().replace(tzinfo=None)
    if not (config.session_start <= local_time < config.session_end):
        return ["OUTSIDE_SESSION_WINDOW"]
    if "vwap" in config.required_truth and _latest_vwap(current, prior) is None:
        return ["VWAP_TRUTH_REQUIRED"]
    if "catalyst" in config.required_truth and not _catalyst_events(current, prior):
        return ["CATALYST_TRUTH_REQUIRED"]
    if "quote" in config.required_truth:
        quote = _latest_quote(current, prior)
        if quote is None:
            return ["QUOTE_TRUTH_REQUIRED"]
        spread = _spread_bps(quote)
        if spread is None:
            return ["SPREAD_TRUTH_REQUIRED"]
        if spread > 50.0:
            return ["WIDE_SPREAD"]
    if (
        "borrow" in config.required_truth
        and _known_bool(current, prior, "borrow_available") is None
    ):
        return ["BORROW_TRUTH_REQUIRED"]
    if "ssr" in config.required_truth and _known_bool(current, prior, "ssr_active") is None:
        return ["SSR_TRUTH_REQUIRED"]
    if (
        "corporate_action" in config.required_truth
        and _corporate_action_basis(current, prior) is None
    ):
        return ["CORPORATE_ACTION_TRUTH_REQUIRED"]
    if (
        "borrow" in config.required_truth
        and _known_bool(current, prior, "borrow_available") is not True
    ):
        return ["BORROW_UNAVAILABLE"]
    if "ssr" in config.required_truth and _known_bool(current, prior, "ssr_active") is True:
        return ["SSR_ACTIVE"]
    return []


def _opening_range_signal(
    current: CausalMarketEvent, prior: Sequence[CausalMarketEvent]
) -> tuple[dict[str, float] | None, tuple[str, ...]]:
    day = current.timestamp.astimezone(MARKET_TZ).date()
    bars = [
        event
        for event in prior
        if event.kind == "bar" and event.timestamp.astimezone(MARKET_TZ).date() == day
    ]
    opening = [
        event
        for event in bars
        if event.timestamp.astimezone(MARKET_TZ).time().replace(tzinfo=None) < time(9, 45)
    ]
    if len(opening) < 5:
        return None, ("OPENING_RANGE_TRUTH_REQUIRED",)
    high = max(float(event.payload["high"]) for event in opening)
    low = min(float(event.payload["low"]) for event in opening)
    close = float(current.payload["close"])
    previous_close = float(bars[-1].payload["close"]) if bars else None
    if previous_close is None or close <= high or previous_close > high:
        return None, ("NO_OPENING_RANGE_CONTINUATION",)
    return {"entry": close, "stop": low, "target": close + 2.0 * (close - low)}, (
        "OPENING_RANGE_BREAKOUT",
    )


def _vwap_signal(
    current: CausalMarketEvent, prior: Sequence[CausalMarketEvent]
) -> tuple[dict[str, float] | None, tuple[str, ...]]:
    day = current.timestamp.astimezone(MARKET_TZ).date()
    bars = [
        event
        for event in prior
        if event.kind == "bar" and event.timestamp.astimezone(MARKET_TZ).date() == day
    ]
    if len(bars) < 3:
        return None, ("VWAP_HISTORY_REQUIRED",)
    previous = bars[-1]
    vwap_now = _latest_vwap(previous, ())
    vwap_before = _latest_vwap(bars[-2], ())
    if vwap_now is None or vwap_before is None:
        return None, ("VWAP_TRUTH_REQUIRED",)
    previous_close = _number(previous.payload.get("close"))
    close = _number(current.payload.get("close"))
    if previous_close is None or close is None or previous_close > vwap_before or close <= vwap_now:
        return None, ("NO_VWAP_RECLAIM",)
    low = min(_number(bar.payload.get("low")) or close for bar in (bars[-2], previous))
    if low >= close:
        return None, ("INVALID_PULLBACK_GEOMETRY",)
    return {"entry": close, "stop": low, "target": close + 2.0 * (close - low)}, ("VWAP_RECLAIM",)


def _catalyst_signal(
    current: CausalMarketEvent, prior: Sequence[CausalMarketEvent]
) -> tuple[dict[str, float] | None, tuple[str, ...]]:
    catalysts = _catalyst_events(current, prior)
    if not catalysts:
        return None, ("CATALYST_TRUTH_REQUIRED",)
    bars = [
        event
        for event in prior
        if event.kind == "bar" and event.symbol.upper() == current.symbol.upper()
    ]
    if not bars:
        return None, ("CATALYST_CONFIRMATION_HISTORY_REQUIRED",)
    close = _number(current.payload.get("close"))
    prior_close = _number(bars[-1].payload.get("close"))
    if close is None or prior_close is None or close <= prior_close:
        return None, ("NO_CATALYST_CONTINUATION",)
    low = _number(current.payload.get("low"))
    if low is None or low >= close:
        return None, ("INVALID_CATALYST_GEOMETRY",)
    return {"entry": close, "stop": low, "target": close + 2.0 * (close - low)}, (
        "CATALYST_CONTINUATION",
    )


def _gap_fade_signal(
    current: CausalMarketEvent, prior: Sequence[CausalMarketEvent]
) -> tuple[dict[str, float] | None, tuple[str, ...]]:
    bars = [
        event
        for event in prior
        if event.kind == "bar" and event.symbol.upper() == current.symbol.upper()
    ]
    if len(bars) < 2:
        return None, ("GAP_FADE_HISTORY_REQUIRED",)
    previous = bars[-1].payload
    payload = current.payload
    open_price = _number(payload.get("open"))
    prior_close = _number(previous.get("close"))
    close = _number(payload.get("close"))
    high = _number(payload.get("high"))
    if any(value is None for value in (open_price, prior_close, close, high)):
        return None, ("GAP_FADE_OHLC_REQUIRED",)
    assert open_price is not None
    assert prior_close is not None
    assert close is not None
    assert high is not None
    gap = (open_price / prior_close) - 1.0 if prior_close else 0.0
    if gap < 0.01 or close >= open_price:
        return None, ("NO_FAILED_BREAKOUT_GAP_FADE",)
    risk = high - close
    if risk <= 0:
        return None, ("INVALID_SHORT_GEOMETRY",)
    return {"entry": close, "stop": high, "target": close - 2.0 * risk}, (
        "FAILED_BREAKOUT_GAP_FADE",
    )


def _build_signal(
    config: ShadowStrategyConfig,
    current: CausalMarketEvent,
    values: Mapping[str, float],
    evidence: Sequence[str],
) -> ShadowSignal:
    entry = float(values["entry"])
    stop = float(values["stop"])
    target = float(values["target"])
    direction = "short" if config.strategy_id == "shadow_failed_breakout_gap_fade" else "long"
    if direction == "long" and not stop < entry < target:
        raise ValueError("long shadow geometry is invalid")
    if direction == "short" and not target < entry < stop:
        raise ValueError("short shadow geometry is invalid")
    timeout = current.timestamp + timedelta(minutes=config.timeout_minutes)
    return ShadowSignal(
        strategy_id=config.strategy_id,
        strategy_version=config.version,
        symbol=current.symbol.upper(),
        direction=direction,
        decision_at=current.timestamp,
        earliest_entry_at=current.timestamp + timedelta(microseconds=1),
        entry_reference=entry,
        stop_price=stop,
        target_price=target,
        timeout_at=timeout,
        evidence=tuple(evidence),
        config_hash_sha256=config.config_hash_sha256,
    )


def _snapshot(value: PortfolioRiskSnapshot | Mapping[str, Any] | None) -> PortfolioRiskSnapshot:
    if isinstance(value, PortfolioRiskSnapshot):
        return value
    if isinstance(value, Mapping):
        return PortfolioRiskSnapshot.from_mappings(
            equity=_number(value.get("equity")),
            positions=value.get("positions", ())
            if isinstance(value.get("positions", ()), Sequence)
            else (),
            pending=value.get("pending", ())
            if isinstance(value.get("pending", ()), Sequence)
            else (),
            daily_realized_pnl=_number(value.get("daily_realized_pnl")),
            daily_unrealized_pnl=_number(value.get("daily_unrealized_pnl")),
            peak_equity=_number(value.get("peak_equity")),
            as_of=str(value.get("as_of") or "") or None,
            metadata_complete=bool(value.get("metadata_complete", False)),
        )
    return PortfolioRiskSnapshot(equity=None, metadata_complete=False)


def _valid_bar(payload: Mapping[str, Any]) -> bool:
    values = [_number(payload.get(key)) for key in ("open", "high", "low", "close", "volume")]
    return (
        all(value is not None and value > 0 for value in values[:4])
        and values[4] is not None
        and values[4] >= 0
    )


def _latest_vwap(current: CausalMarketEvent, prior: Sequence[CausalMarketEvent]) -> float | None:
    events: Iterable[CausalMarketEvent]
    if isinstance(current, CausalMarketEvent):
        events = (current,)
    else:
        events = ()
    for event in reversed(tuple(events) + tuple(prior)):
        if event.kind == "bar":
            value = _number(event.payload.get("vwap"))
            if value is not None and value > 0:
                return value
    return None


def _latest_quote(
    current: CausalMarketEvent, prior: Sequence[CausalMarketEvent]
) -> Mapping[str, Any] | None:
    candidates = [current, *prior]
    for event in reversed(candidates):
        if event.kind == "quote" or any(
            key in event.payload for key in ("bid", "ask", "spread_bps")
        ):
            return event.payload
    return None


def _spread_bps(payload: Mapping[str, Any]) -> float | None:
    direct = _number(payload.get("spread_bps"))
    if direct is not None:
        return direct
    bid, ask = _number(payload.get("bid")), _number(payload.get("ask"))
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    return (
        ((ask - bid) / mid * 10_000.0)
        if mid and ask is not None and bid is not None and ask >= bid
        else None
    )


def _known_bool(
    current: CausalMarketEvent, prior: Sequence[CausalMarketEvent], key: str
) -> bool | None:
    for event in reversed((current, *prior)):
        if key in event.payload and isinstance(event.payload[key], bool):
            return event.payload[key]
    return None


def _corporate_action_basis(
    current: CausalMarketEvent, prior: Sequence[CausalMarketEvent]
) -> str | None:
    for event in reversed((current, *prior)):
        value = event.payload.get("corporate_action_basis")
        if value is not None and str(value).strip().lower() not in {
            "",
            "unknown",
            "raw",
            "unadjusted",
        }:
            return str(value)
    return None


def _catalyst_events(
    current: CausalMarketEvent, prior: Sequence[CausalMarketEvent]
) -> list[CausalMarketEvent]:
    return [
        event
        for event in prior
        if event.symbol.upper() == current.symbol.upper()
        and event.kind in {"catalyst", "news"}
        and event.timestamp < current.timestamp
        and current.timestamp - event.timestamp <= timedelta(minutes=30)
        and str(
            event.payload.get("source_artifact_hash_sha256") or event.source_artifact_hash_sha256
        ).strip()
        and str(event.payload.get("headline") or event.payload.get("catalyst_type") or "").strip()
    ]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


__all__ = [
    "EMPIRICAL_COST_REQUIRED",
    "SHADOW_PROTOCOL_VERSION",
    "ShadowEvaluation",
    "ShadowSignal",
    "ShadowStrategyConfig",
    "ShadowStrategyRegistry",
    "build_shadow_strategy_registry",
    "evaluate_shadow_event",
    "evaluate_shadow_strategy",
]
