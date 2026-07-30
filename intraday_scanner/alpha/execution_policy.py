"""Deterministic paper-entry policy layered on top of fail-closed risk gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time
from typing import Any

from intraday_scanner.risk.policy import RiskInput, evaluate_risk

EXECUTION_POLICY_VERSION = "dawnstrike-execution-policy-v1"


@dataclass(frozen=True, slots=True)
class ExecutionPolicyInput:
    ticker: str
    decision_time: str
    expected_exit_time: str | None
    session_close_time: str | None
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
class ExecutionPolicyDecision:
    allowed_for_paper: bool
    action: str
    reasons: tuple[str, ...]
    policy_version: str
    risk_policy_version: str
    computed: dict[str, float | int | None]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "reasons": list(self.reasons)}


def evaluate_execution_policy(request: ExecutionPolicyInput) -> ExecutionPolicyDecision:
    risk = evaluate_risk(
        RiskInput(
            ticker=request.ticker,
            decision_time=request.decision_time,
            equity_cents=request.equity_cents,
            entry_price=request.entry_price,
            stop_price=request.stop_price,
            proposed_notional_cents=request.proposed_notional_cents,
            daily_realized_loss_cents=request.daily_realized_loss_cents,
            ticker_notional_cents=request.ticker_notional_cents,
            correlated_position_count=request.correlated_position_count,
            halt_status=request.halt_status,
            corporate_action_status=request.corporate_action_status,
            sec_risk_status=request.sec_risk_status,
            source_quality_status=request.source_quality_status,
            spread_bps=request.spread_bps,
            live_execution_requested=request.live_execution_requested,
        )
    )
    reasons = list(risk.reasons)
    expected_exit = _parse_time(request.expected_exit_time)
    session_close = _parse_time(request.session_close_time)
    if request.expected_exit_time is None or request.session_close_time is None:
        reasons.append("holding_window_unknown")
    elif expected_exit is None or session_close is None:
        reasons.append("holding_window_invalid")
    elif expected_exit > session_close:
        reasons.append("holding_window_extends_past_session")
    unique_reasons = tuple(dict.fromkeys(reasons))
    return ExecutionPolicyDecision(
        allowed_for_paper=not unique_reasons,
        action="PAPER_ALLOW" if not unique_reasons else "SHADOW_BLOCK",
        reasons=unique_reasons,
        policy_version=EXECUTION_POLICY_VERSION,
        risk_policy_version=str(risk.policy_version),
        computed=risk.computed,
    )


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timetz().replace(tzinfo=None)
    except ValueError:
        try:
            return time.fromisoformat(value)
        except ValueError:
            return None
