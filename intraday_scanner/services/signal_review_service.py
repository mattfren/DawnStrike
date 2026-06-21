"""Operator-facing AlphaOps signal review helpers."""

from __future__ import annotations

from typing import Any

from intraday_scanner.alpha.no_trade_filter import NoTradeDecision, evaluate_no_trade


def review_alpha_signals(
    signals: list[dict[str, Any]],
    *,
    source_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decision = evaluate_no_trade(signals, source_summary=source_summary)
    clean = [row for row in signals if row.get("can_alert") and not row.get("no_trade_reason")]
    blocked = [row for row in signals if not row.get("can_alert") or row.get("no_trade_reason")]
    return {
        "decision": decision.to_dict(),
        "watchlist": clean[:5],
        "blocked": blocked,
        "plain_read": _plain_read(clean, blocked, decision),
    }


def monitor_alpha_signals(
    signals: list[dict[str, Any]],
    *,
    current_prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    current_prices = dict(current_prices or {})
    if not current_prices:
        tickers = [str(row.get("ticker") or "") for row in signals[:5] if row.get("ticker")]
        return {
            "status": "manual_monitor_required",
            "label": "MANUAL REVIEW",
            "message": "No live/current price source configured.",
            "tickers": tickers,
            "events": [],
        }
    events: list[dict[str, Any]] = []
    for row in signals:
        ticker = str(row.get("ticker") or "").upper()
        price = current_prices.get(ticker)
        if price is None:
            continue
        trigger = _float(row.get("breakout_trigger") or row.get("entry_trigger"))
        invalidation = _float(row.get("invalidation_level") or row.get("invalidation"))
        target = _float(row.get("first_target") or row.get("target_1"))
        label = "BREAKOUT WATCH"
        if invalidation and price <= invalidation:
            label = "INVALIDATED"
        elif target and price >= target:
            label = "CAUTION"
        elif trigger and price < trigger:
            label = "BREAKOUT WATCH"
        events.append({
            "ticker": ticker,
            "current_price": price,
            "label": label,
            "status": label.lower().replace(" ", "_"),
        })
    return {
        "status": "checked",
        "label": "BREAKOUT WATCH" if events else "MANUAL REVIEW",
        "message": "Checked configured current prices.",
        "events": events,
    }


def _plain_read(
    clean: list[dict[str, Any]],
    blocked: list[dict[str, Any]],
    decision: NoTradeDecision,
) -> str:
    if decision.no_trade:
        return f"No clean edge today. Reason: {decision.reason}"
    top = clean[0]
    return (
        f"{top.get('ticker')} is the lead research setup with Alpha "
        f"{top.get('alpha_score')}. Watch {top.get('entry_trigger')} as the trigger, "
        f"{top.get('invalidation')} as invalidation, and {top.get('target_1')} as target. "
        f"{len(blocked)} names are blocked by risk filters."
    )


def _float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value).replace("$", ""))
    except (TypeError, ValueError):
        return None
