"""Outcome labels for shadow AlphaOps signals."""

from __future__ import annotations

from typing import Any


def label_outcome(signal: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    entry = _float(
        outcome.get("entry")
        or outcome.get("entry_price")
        or signal.get("breakout_trigger")
        or signal.get("premarket_price")
    )
    high = _float(outcome.get("high") or outcome.get("high_after_entry"))
    low = _float(outcome.get("low") or outcome.get("low_after_entry"))
    close = _float(outcome.get("close") or outcome.get("close_price"))
    price_1m = _float(outcome.get("price_1m") or outcome.get("one_minute"))
    price_5m = _float(outcome.get("price_5m") or outcome.get("five_minute"))
    price_15m = _float(outcome.get("price_15m") or outcome.get("fifteen_minute"))
    lunch = _float(outcome.get("lunch") or outcome.get("lunch_price"))
    target = _float(signal.get("first_target") or signal.get("target_1"))
    invalidation = _float(signal.get("invalidation_level") or signal.get("invalidation"))
    return {
        "scan_id": signal.get("scan_id") or signal.get("run_id") or outcome.get("scan_id"),
        "ticker": signal.get("ticker") or outcome.get("ticker"),
        "setup_key": signal.get("setup_key") or outcome.get("setup_key") or "",
        "winner_1m": _winner(price_1m, entry),
        "winner_5m": _winner(price_5m, entry),
        "winner_15m": _winner(price_15m, entry),
        "winner_lunch": _winner(lunch, entry),
        "winner_close": _winner(close, entry),
        "high_after_entry_return": _return_pct(high, entry),
        "low_after_entry_drawdown": _return_pct(low, entry),
        "max_favorable_excursion": _return_pct(high, entry),
        "max_adverse_excursion": _return_pct(low, entry),
        "failed_fast": bool(invalidation and low is not None and low <= invalidation),
        "held_up": bool(entry and close is not None and close >= entry),
        "squeeze_candidate": bool(target and high is not None and high >= target),
        "trap_candidate": bool(
            high is not None
            and entry is not None
            and entry != 0.0
            and low is not None
            and high > entry
            and low < entry
        ),
        "entry_price": entry,
        "target_price": target,
        "invalidation_price": invalidation,
        "missing_outcome_high": high is None,
    }


def label_outcomes(
    signals: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_ticker = {str(row.get("ticker") or "").upper(): row for row in outcomes}
    labels: list[dict[str, Any]] = []
    for signal in signals:
        outcome = by_ticker.get(str(signal.get("ticker") or "").upper())
        if outcome is not None:
            labels.append(label_outcome(signal, outcome))
    return labels


def _winner(price: float | None, entry: float | None) -> bool | None:
    if price is None or entry is None or entry == 0.0:
        return None
    return price > entry


def _return_pct(price: float | None, entry: float | None) -> float | None:
    if price is None or entry is None or entry == 0.0:
        return None
    return round(((price - entry) / entry) * 100.0, 4)


def _float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
