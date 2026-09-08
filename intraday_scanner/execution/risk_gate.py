"""Position sizing and the risk interlocks that stand in front of every order.

Every refusal returns a named reason, so a zero-trade session is explainable
rather than mysterious. Defaults are deliberately conservative and paper-only:
long-only, no leverage, and a notional cap that cannot exceed settled cash.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Conservative paper-only defaults. These do not add leverage, shorting, or
# any new asset class; the broker permits 4x and shorting, and we decline both.
DEFAULT_RISK_PCT = 0.5
DEFAULT_MAX_POSITION_PCT = 10.0
DEFAULT_MAX_CONCURRENT = 3
DEFAULT_MAX_ENTRIES_PER_DAY = 5
DEFAULT_DAILY_LOSS_LIMIT_PCT = 2.0
DEFAULT_MAX_STALENESS_SECONDS = 900

KILL_SWITCH_ENV = "DAWNSTRIKE_PAPER_KILL_SWITCH"
ENTRIES_ENABLED_ENV = "DAWNSTRIKE_PAPER_ENTRIES_ENABLED"


@dataclass(frozen=True)
class RiskSettings:
    risk_pct: float = DEFAULT_RISK_PCT
    max_position_pct: float = DEFAULT_MAX_POSITION_PCT
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    max_entries_per_day: int = DEFAULT_MAX_ENTRIES_PER_DAY
    daily_loss_limit_pct: float = DEFAULT_DAILY_LOSS_LIMIT_PCT
    max_staleness_seconds: int = DEFAULT_MAX_STALENESS_SECONDS
    kill_switch_path: Path | None = None

    @classmethod
    def from_env(cls) -> RiskSettings:
        def num(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, "") or default)
            except ValueError:
                return default

        return cls(
            risk_pct=num("DAWNSTRIKE_PAPER_RISK_PCT", DEFAULT_RISK_PCT),
            max_position_pct=num("DAWNSTRIKE_PAPER_MAX_POSITION_PCT", DEFAULT_MAX_POSITION_PCT),
            max_concurrent=int(num("DAWNSTRIKE_PAPER_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT)),
            max_entries_per_day=int(
                num("DAWNSTRIKE_PAPER_MAX_ENTRIES_PER_DAY", DEFAULT_MAX_ENTRIES_PER_DAY)
            ),
            daily_loss_limit_pct=num(
                "DAWNSTRIKE_PAPER_DAILY_LOSS_LIMIT_PCT", DEFAULT_DAILY_LOSS_LIMIT_PCT
            ),
            max_staleness_seconds=int(
                num("DAWNSTRIKE_PAPER_MAX_STALENESS_SECONDS", DEFAULT_MAX_STALENESS_SECONDS)
            ),
            kill_switch_path=Path(os.environ[KILL_SWITCH_ENV])
            if os.environ.get(KILL_SWITCH_ENV)
            else None,
        )


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    qty: int = 0
    notional: float = 0.0
    risk_amount: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)


def entries_enabled() -> bool:
    """Entries are opt-in. Absent or unset means disabled."""

    return str(os.environ.get(ENTRIES_ENABLED_ENV, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def kill_switch_engaged(settings: RiskSettings) -> bool:
    if settings.kill_switch_path and settings.kill_switch_path.exists():
        return True
    return str(os.environ.get(KILL_SWITCH_ENV + "_ENGAGED", "")).strip().lower() in {
        "1",
        "true",
        "yes",
    }


def evaluate_entry(
    *,
    entry: float,
    stop: float,
    target: float,
    account: dict[str, Any],
    open_positions: int,
    entries_today: int,
    day_pnl_pct: float,
    data_age_seconds: float,
    settings: RiskSettings,
) -> RiskDecision:
    """Approve or refuse one long paper entry, with a named reason either way.

    Note on ordering: the kill switch and the entry toggle are checked first,
    so an operator stop is never masked by an unrelated refusal.
    """

    if kill_switch_engaged(settings):
        return RiskDecision(False, "kill_switch_engaged")
    if not entries_enabled():
        return RiskDecision(False, "entries_disabled")

    # Stale market data blocks NEW entries only. Managing existing positions is
    # deliberately not gated here - see PaperExecutionEngine.manage_positions.
    if data_age_seconds > settings.max_staleness_seconds:
        return RiskDecision(
            False,
            "stale_market_data",
            detail={"age_s": round(data_age_seconds, 1), "limit_s": settings.max_staleness_seconds},
        )

    if day_pnl_pct <= -abs(settings.daily_loss_limit_pct):
        return RiskDecision(
            False,
            "daily_loss_limit_reached",
            detail={"day_pnl_pct": day_pnl_pct, "limit_pct": -abs(settings.daily_loss_limit_pct)},
        )
    if open_positions >= settings.max_concurrent:
        return RiskDecision(False, "max_concurrent_positions", detail={"open": open_positions})
    if entries_today >= settings.max_entries_per_day:
        return RiskDecision(False, "max_entries_per_day", detail={"entries_today": entries_today})

    if not (stop < entry < target):
        return RiskDecision(
            False, "incoherent_plan", detail={"entry": entry, "stop": stop, "target": target}
        )

    equity = float(account.get("equity") or 0.0)
    cash = float(account.get("cash") or 0.0)
    if equity <= 0:
        return RiskDecision(False, "no_equity")

    risk_amount = equity * (settings.risk_pct / 100.0)
    per_share_risk = entry - stop
    if per_share_risk <= 0:
        return RiskDecision(False, "non_positive_risk_per_share")

    qty = int(math.floor(risk_amount / per_share_risk))
    if qty <= 0:
        return RiskDecision(
            False,
            "risk_budget_below_one_share",
            detail={
                "risk_amount": round(risk_amount, 2),
                "per_share_risk": round(per_share_risk, 4),
            },
        )

    # Cap by position size, then by settled cash. Never by buying_power: the
    # broker offers 4x margin and this experiment declines it entirely.
    max_notional = min(equity * (settings.max_position_pct / 100.0), cash)
    if max_notional <= 0:
        return RiskDecision(False, "no_settled_cash")
    qty = min(qty, int(math.floor(max_notional / entry)))
    if qty <= 0:
        return RiskDecision(
            False, "position_cap_below_one_share", detail={"max_notional": round(max_notional, 2)}
        )

    notional = qty * entry
    if notional > cash:
        return RiskDecision(False, "insufficient_settled_cash", detail={"notional": notional})

    return RiskDecision(
        True,
        "approved",
        qty=qty,
        notional=round(notional, 2),
        risk_amount=round(qty * per_share_risk, 2),
        detail={
            "risk_pct": settings.risk_pct,
            "per_share_risk": round(per_share_risk, 4),
            "planned_r_multiple": round((target - entry) / per_share_risk, 3),
        },
    )
