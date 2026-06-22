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
    clean = _clean_signals(signals)
    fallback = _fallback_signals(signals)
    watchlist = clean if decision.decision_tier == "clean_edge" else fallback
    blocked = [
        row
        for row in signals
        if row not in watchlist
        and (not row.get("can_alert") or row.get("no_trade_reason") or row not in clean)
    ]
    return {
        "decision": decision.to_dict(),
        "watchlist": [
            _with_review_tier(row, decision.decision_tier) for row in watchlist[:5]
        ],
        "blocked": blocked,
        "plain_read": _plain_read(watchlist, blocked, decision),
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
    watchlist: list[dict[str, Any]],
    blocked: list[dict[str, Any]],
    decision: NoTradeDecision,
) -> str:
    if decision.no_trade:
        return f"No clean edge today. Reason: {decision.reason}"
    top = watchlist[0]
    if decision.decision_tier == "probability_fallback":
        return (
            f"{top.get('ticker')} is the best probability watch, not a clean edge. "
            f"Alpha {top.get('alpha_score')} with source confidence "
            f"{top.get('source_confidence')}. Confirm ticker, catalyst, volume, "
            f"and price action manually. {len(blocked)} names are blocked or weaker."
        )
    return (
        f"{top.get('ticker')} is the lead research setup with Alpha "
        f"{top.get('alpha_score')}. Watch {top.get('entry_trigger')} as the trigger, "
        f"{top.get('invalidation')} as invalidation, and {top.get('target_1')} as target. "
        f"{len(blocked)} names are blocked by risk filters."
    )


def _clean_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in signals
        if row.get("can_alert")
        and not row.get("no_trade_reason")
        and _float(row.get("alpha_score")) is not None
        and (_float(row.get("alpha_score")) or 0.0) >= 45.0
    ]


def _fallback_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        row
        for row in signals
        if row.get("can_alert")
        and not row.get("no_trade_reason")
        and (_float(row.get("alpha_score")) or 0.0) >= 32.0
        and (_float(row.get("source_confidence")) or 0.0) >= 20.0
        and (_float(row.get("risk_score")) or 0.0) >= 55.0
        and str(row.get("drawdown_risk_bucket") or "").upper() != "HIGH"
        and (
            (_float(row.get("score") or row.get("total_score")) or 0.0) >= 40.0
            or (_float(row.get("dollar_volume")) or 0.0) >= 1_000_000.0
        )
    ]
    return sorted(rows, key=_rank_key, reverse=True)


def _with_review_tier(row: dict[str, Any], decision_tier: str) -> dict[str, Any]:
    output = dict(row)
    output["decision_tier"] = decision_tier
    if decision_tier == "probability_fallback":
        output["classification"] = "WATCH ONLY"
        output["review_label"] = "PROBABILITY WATCH"
    return output


def _rank_key(row: dict[str, Any]) -> tuple[float, float]:
    return _float(row.get("alpha_score")) or 0.0, _float(row.get("dollar_volume")) or 0.0


def _float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value).replace("$", ""))
    except (TypeError, ValueError):
        return None
