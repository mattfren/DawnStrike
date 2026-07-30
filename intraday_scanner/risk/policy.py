"""Deterministic, fail-closed risk gates for paper/shadow research."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

POLICY_VERSION = "dawnstrike-risk-policy-v1"
MAX_RISK_PER_TRADE_PCT = 0.25
MAX_DAILY_LOSS_PCT = 1.0
MAX_TICKER_NOTIONAL_PCT = 10.0
MAX_CORRELATED_POSITIONS = 2
MAX_SPREAD_BPS = 200.0


@dataclass(frozen=True, slots=True)
class RiskInput:
    ticker: str
    decision_time: str
    equity_cents: int | None
    entry_price: float | None
    stop_price: float | None
    proposed_notional_cents: int | None
    daily_realized_loss_cents: int | None
    ticker_notional_cents: int | None
    correlated_position_count: int | None
    halt_status: str | None
    corporate_action_status: str | None
    sec_risk_status: str | None
    source_quality_status: str | None
    spread_bps: float | None
    live_execution_requested: bool = False


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed_for_paper: bool
    action: str
    reasons: tuple[str, ...]
    policy_version: str
    computed: dict[str, float | int | None]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "reasons": list(self.reasons)}


def evaluate_risk(request: RiskInput) -> RiskDecision:
    reasons: list[str] = []
    computed: dict[str, float | int | None] = {}
    equity = request.equity_cents
    if equity is None or equity <= 0:
        reasons.append("equity_unknown")
    else:
        risk_pct = _risk_pct(request)
        computed["risk_per_trade_pct"] = risk_pct
        if risk_pct is None:
            reasons.append("entry_or_stop_unknown")
        elif risk_pct > MAX_RISK_PER_TRADE_PCT:
            reasons.append("risk_per_trade_exceeds_0_25_pct")
        if request.daily_realized_loss_cents is None:
            reasons.append("daily_loss_unknown")
        elif abs(request.daily_realized_loss_cents) > equity * MAX_DAILY_LOSS_PCT / 100:
            reasons.append("daily_loss_exceeds_1_pct")
        if request.ticker_notional_cents is None or request.proposed_notional_cents is None:
            reasons.append("ticker_notional_unknown")
        elif (
            request.ticker_notional_cents + request.proposed_notional_cents
            > equity * MAX_TICKER_NOTIONAL_PCT / 100
        ):
            reasons.append("ticker_notional_exceeds_10_pct")
    if request.correlated_position_count is None:
        reasons.append("correlation_unknown")
    elif request.correlated_position_count >= MAX_CORRELATED_POSITIONS:
        reasons.append("correlated_position_limit_reached")
    _status_gate(reasons, request.halt_status, "halt_status")
    _status_gate(reasons, request.corporate_action_status, "corporate_action_status")
    _status_gate(reasons, request.sec_risk_status, "sec_risk_status")
    _status_gate(reasons, request.source_quality_status, "source_quality_status")
    if request.spread_bps is None:
        reasons.append("spread_unknown")
    elif request.spread_bps > MAX_SPREAD_BPS:
        reasons.append("spread_exceeds_200_bps")
    if _within_eod_window(request.decision_time):
        reasons.append("new_entry_within_30_minutes_of_close")
    if request.live_execution_requested:
        reasons.append("live_execution_disabled_research_only")
    unique_reasons = tuple(dict.fromkeys(reasons))
    return RiskDecision(
        allowed_for_paper=not unique_reasons,
        action="PAPER_ALLOW" if not unique_reasons else "SHADOW_BLOCK",
        reasons=unique_reasons,
        policy_version=POLICY_VERSION,
        computed=computed,
    )


def _risk_pct(request: RiskInput) -> float | None:
    if (
        request.entry_price is None
        or request.stop_price is None
        or request.entry_price <= 0
        or request.proposed_notional_cents is None
        or request.equity_cents is None
    ):
        return None
    stop_distance_pct = abs(request.entry_price - request.stop_price) / request.entry_price * 100
    return request.proposed_notional_cents / request.equity_cents * stop_distance_pct


def _status_gate(reasons: list[str], value: str | None, label: str) -> None:
    normalized = str(value or "").strip().lower()
    if normalized not in {"clear", "verified", "ok", "pass"}:
        reasons.append(f"{label}_{'unknown' if not normalized else 'blocked'}")


def _within_eod_window(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo("America/Chicago"))
    local = parsed.timetz().replace(tzinfo=None)
    return local >= time(15, 30) or local < time(9, 0)
