"""Paper pilot: an explicit, labelled, switchable path for PAPER entries only.

Why this exists. As configured, the alert gate cannot approve anything: two of
its required checks (corporate-action status, source-quality status) have no
producer on free data, so `can_alert` has been false for every candidate on
record. Separately, morning candidates are ~30 minutes old by the open, so the
risk gate's staleness check refuses them even when they are approved. Together
these made the paper path unable to trade by construction.

The owner authorised a paper pilot (2026-09-23) to get a real forward record.
The pilot does two things, and nothing else:

1. For candidates the gate REJECTED, it waives the gate's quality and
   verification checks - under the one fixed rule below - for paper entries.
2. It replaces the stale morning observation with a live quote taken at trade
   time, so the risk gate's staleness check is satisfied by genuinely fresh
   data rather than by loosening the threshold.

It does NOT bypass the risk gate. Kill switch, the entries flag, future/stale
market-data rejection, max entries per day, max concurrent positions, the daily
loss limit, position sizing, the market-open check, the too-close-to-the-bell
check, the broker bracket and end-of-session flatten all apply unchanged.

Pilot rule `paper-pilot-v1` (fixed before any result; do not tune it):
  - the candidate's own plan is valid: 0 < stop < entry < target;
  - morning spread, when known, is below the extreme line (12%);
  - not halted and not in an active offering, per the morning data;
  - a live quote exists, with bid and ask, taken at trade time;
  - live price >= the minimum price, and stop < live price < target;
  - live spread is below the extreme line.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from intraday_scanner.alpha.alert_gate import EXTREME_SPREAD_PCT

PILOT_ENABLED_ENV = "DAWNSTRIKE_PAPER_PILOT_ENABLED"
PILOT_RULE_ID = "paper-pilot-v1"
# Also the strategy_version on every pilot order, so the deterministic client
# order id - and therefore the broker record - is permanently labelled.
PILOT_STRATEGY_VERSION = PILOT_RULE_ID
PILOT_MAX_SPREAD_PCT = EXTREME_SPREAD_PCT
# Same floor the alpha risk governor enforces by default (RiskGovernor.min_price).
PILOT_MIN_PRICE = 0.50

_HALTED_STATUSES = frozenset({"BLOCKED", "HALTED", "HALT", "PAUSED"})


def pilot_enabled() -> bool:
    """Opt-in, exactly like entries_enabled(): absent or unset means off."""

    return str(os.environ.get(PILOT_ENABLED_ENV, "")).strip().lower() in {
        "1", "true", "yes", "y", "on",
    }


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return parsed


def _risk_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("risk_flags", "coverage_warning", "risk_flag_text"):
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    return " ".join(parts).lower()


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def plan_refusal(entry: float, stop: float, target: float) -> str | None:
    if not (0 < stop < entry < target):
        return "pilot_plan_invalid"
    return None


def payload_refusal(payload: dict[str, Any]) -> str | None:
    """Morning-data checks. Returns a refusal reason, or None if eligible."""

    spread = _float(payload.get("spread_pct"))
    if spread is not None and spread >= PILOT_MAX_SPREAD_PCT:
        return "pilot_extreme_spread"
    risk = _risk_text(payload)
    halt_status = str(payload.get("halt_status") or "").strip().upper()
    if (
        _truthy(payload.get("current_halt"))
        or halt_status in _HALTED_STATUSES
        or "current_halt" in risk
    ):
        return "pilot_halted"
    if _truthy(payload.get("recent_offering")) or "recent_offering" in risk:
        return "pilot_active_offering"
    return None


@dataclass(frozen=True)
class LiveQuote:
    symbol: str
    price: float | None
    bid: float | None
    ask: float | None
    as_of: datetime | None

    @property
    def spread_pct(self) -> float | None:
        if not self.bid or not self.ask or self.bid <= 0 or self.ask < self.bid:
            return None
        mid = (self.bid + self.ask) / 2.0
        return (self.ask - self.bid) / mid * 100.0


def live_refusal(
    *, stop: float, target: float, quote: LiveQuote | None
) -> str | None:
    """Trade-time checks against a live quote. No quote means no trade."""

    if quote is None or quote.price is None or quote.price <= 0 or quote.as_of is None:
        return "pilot_no_live_quote"
    spread = quote.spread_pct
    if spread is None:
        return "pilot_no_live_quote"
    if quote.price < PILOT_MIN_PRICE:
        return "pilot_sub_min_price"
    if spread >= PILOT_MAX_SPREAD_PCT:
        return "pilot_extreme_spread_live"
    if not (stop < quote.price < target):
        return "pilot_price_outside_plan"
    return None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def quotes_from_snapshots(payload: dict[str, Any]) -> dict[str, LiveQuote]:
    """Parse Alpaca's /v2/stocks/snapshots response."""

    snapshots = payload.get("snapshots", payload) if isinstance(payload, dict) else {}
    out: dict[str, LiveQuote] = {}
    for symbol, snap in (snapshots or {}).items():
        if not isinstance(snap, dict):
            continue
        trade = snap.get("latestTrade") or {}
        quote = snap.get("latestQuote") or {}
        out[str(symbol).upper()] = LiveQuote(
            symbol=str(symbol).upper(),
            price=_float(trade.get("p")),
            bid=_float(quote.get("bp")),
            ask=_float(quote.get("ap")),
            as_of=_parse_time(trade.get("t")),
        )
    return out


QuoteFetcher = Callable[[Iterable[str]], dict[str, LiveQuote]]


def fetch_live_quotes(symbols: Iterable[str]) -> dict[str, LiveQuote]:
    """One batched snapshot call on the configured (free IEX) feed.

    Fails closed: any error returns an empty map, which means no pilot trade.
    """

    wanted = sorted({str(s).upper() for s in symbols if s})
    if not wanted:
        return {}
    try:
        from intraday_scanner.config import load_config
        from intraday_scanner.providers.alpaca_provider import AlpacaProvider

        config = load_config()
        provider = AlpacaProvider(config)
        provider.validate_credentials()
        payload = provider._request_json(
            "/v2/stocks/snapshots",
            {"symbols": ",".join(wanted), "feed": provider.feed},
            config,
        )
    except Exception:  # noqa: BLE001 - fail closed on any provider/network error
        return {}
    return quotes_from_snapshots(payload)
