"""Outcome labels for shadow AlphaOps signals."""

from __future__ import annotations

from typing import Any


def label_outcome(signal: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    entry = _float(
        outcome.get("entry")
        or outcome.get("entry_price")
        or signal.get("entry_trigger")
        or signal.get("breakout_trigger")
        or signal.get("entry_watch_level")
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
    hit_target_1 = bool(target is not None and high is not None and high >= target)
    hit_invalidation = bool(
        invalidation is not None and low is not None and low <= invalidation
    )
    target_return = _return_pct(target, entry)
    invalidation_return = _return_pct(invalidation, entry)
    close_return = _return_pct(close, entry)
    planned_return, planned_outcome = _planned_first_touch_return(
        hit_target_1=hit_target_1,
        hit_invalidation=hit_invalidation,
        target_return=target_return,
        invalidation_return=invalidation_return,
        close_return=close_return,
        explicit_outcome=str(outcome.get("planned_first_touch_outcome") or ""),
    )
    signal_id = str(signal.get("signal_id") or outcome.get("signal_id") or "")
    scan_id = str(signal.get("scan_id") or signal.get("run_id") or outcome.get("scan_id") or "")
    ticker = str(signal.get("ticker") or outcome.get("ticker") or "").upper()
    outcome_key = str(
        outcome.get("outcome_key")
        or outcome.get("signal_id")
        or f"{scan_id}:{ticker}:{outcome.get('date') or outcome.get('market_date') or ''}"
    )
    outcome_source = str(outcome.get("outcome_source") or outcome.get("source") or "")
    learning_eligible = bool(
        outcome.get("learning_eligible", True) is not False
        and entry is not None
        and high is not None
        and low is not None
        and close is not None
        and outcome_source
    )
    return {
        "label_key": f"{signal_id or scan_id}:{ticker}:{outcome_key}",
        "outcome_key": outcome_key,
        "signal_id": signal_id,
        "scan_id": scan_id,
        "ticker": ticker,
        "market_date": (
            outcome.get("market_date")
            or outcome.get("date")
            or signal.get("market_date")
            or ""
        ),
        "recommendation_timestamp": (
            signal.get("generated_at")
            or signal.get("timestamp")
            or outcome.get("recommendation_timestamp")
            or ""
        ),
        "setup_key": (
            signal.get("setup_key")
            or signal.get("primary_setup")
            or outcome.get("setup_key")
            or ""
        ),
        "created_at": (
            outcome.get("captured_at")
            or outcome.get("imported_at")
            or outcome.get("uploaded_at")
            or ""
        ),
        "outcome_source": outcome_source,
        "outcome_source_url": outcome.get("source_url") or "",
        "outcome_source_bar_hash_sha256": outcome.get("source_bar_hash_sha256") or "",
        "outcome_status": outcome.get("outcome_status") or "",
        "learning_eligible": learning_eligible,
        "automatic_sourced_data": bool(outcome.get("automatic_sourced_data")),
        "manual_uploaded_data": bool(outcome.get("manual_uploaded_data")),
        "no_lookahead": bool(outcome.get("no_lookahead")),
        "winner_1m": _winner(price_1m, entry),
        "winner_5m": _winner(price_5m, entry),
        "winner_15m": _winner(price_15m, entry),
        "winner_lunch": _winner(lunch, entry),
        "winner_close": _winner(close, entry),
        "high_after_entry_return": _return_pct(high, entry),
        "low_after_entry_drawdown": _return_pct(low, entry),
        "max_favorable_excursion": _return_pct(high, entry),
        "max_adverse_excursion": _return_pct(low, entry),
        "failed_fast": hit_invalidation,
        "held_up": bool(entry and close is not None and close >= entry),
        "squeeze_candidate": hit_target_1,
        "hit_target_1": hit_target_1,
        "hit_invalidation": hit_invalidation,
        "target_return_pct": target_return,
        "invalidation_return_pct": invalidation_return,
        "close_return_pct": close_return,
        "reward_risk_ratio": _reward_risk_ratio(entry, target, invalidation),
        "planned_r_multiple": _planned_r_multiple(planned_return, entry, invalidation),
        "planned_first_touch_return_pct": planned_return,
        "planned_first_touch_outcome": planned_outcome,
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
    labels: list[dict[str, Any]] = []
    for signal in signals:
        outcome = _matching_outcome(signal, outcomes)
        if outcome is not None:
            labels.append(label_outcome(signal, outcome))
    return labels


def _matching_outcome(
    signal: dict[str, Any], outcomes: list[dict[str, Any]]
) -> dict[str, Any] | None:
    ticker = str(signal.get("ticker") or "").upper()
    candidates = [
        row for row in outcomes if str(row.get("ticker") or "").upper() == ticker
    ]
    if not candidates:
        return None
    signal_id = str(signal.get("signal_id") or "")
    if signal_id:
        exact_signal = [
            row for row in candidates if str(row.get("signal_id") or "") == signal_id
        ]
        if exact_signal:
            return exact_signal[0]
        if any(str(row.get("signal_id") or "") for row in candidates):
            return None
    scan_id = str(signal.get("scan_id") or signal.get("run_id") or "")
    if scan_id:
        exact_scan = [row for row in candidates if str(row.get("scan_id") or "") == scan_id]
        if exact_scan:
            return exact_scan[0]
        if any(str(row.get("scan_id") or "") for row in candidates):
            return None
    signal_date = str(
        signal.get("market_date")
        or signal.get("timestamp")
        or signal.get("generated_at")
        or ""
    )[:10]
    if signal_date:
        exact_date = [
            row
            for row in candidates
            if str(row.get("market_date") or row.get("date") or "")[:10] == signal_date
        ]
        if exact_date:
            return exact_date[0]
    return candidates[0] if len(candidates) == 1 else None


def _winner(price: float | None, entry: float | None) -> bool | None:
    if price is None or entry is None or entry == 0.0:
        return None
    return price > entry


def _return_pct(price: float | None, entry: float | None) -> float | None:
    if price is None or entry is None or entry == 0.0:
        return None
    return round(((price - entry) / entry) * 100.0, 4)


def _planned_first_touch_return(
    *,
    hit_target_1: bool,
    hit_invalidation: bool,
    target_return: float | None,
    invalidation_return: float | None,
    close_return: float | None,
    explicit_outcome: str = "",
) -> tuple[float | None, str]:
    if explicit_outcome == "target_1":
        return target_return, "target_1"
    if explicit_outcome == "invalidation":
        return invalidation_return, "invalidation"
    if explicit_outcome.startswith("ambiguous_"):
        return invalidation_return, explicit_outcome
    if explicit_outcome == "close":
        return close_return, "close"
    if hit_target_1 and hit_invalidation:
        return invalidation_return, "ambiguous_target_and_invalidation_counted_as_invalidation"
    if hit_invalidation:
        return invalidation_return, "invalidation"
    if hit_target_1:
        return target_return, "target_1"
    if close_return is not None:
        return close_return, "close"
    return None, "unresolved"


def _reward_risk_ratio(
    entry: float | None,
    target: float | None,
    invalidation: float | None,
) -> float | None:
    if entry is None or entry <= 0 or target is None or invalidation is None:
        return None
    reward = target - entry
    risk = entry - invalidation
    if reward <= 0 or risk <= 0:
        return None
    return round(reward / risk, 4)


def _planned_r_multiple(
    planned_return: float | None,
    entry: float | None,
    invalidation: float | None,
) -> float | None:
    if planned_return is None or entry is None or entry <= 0 or invalidation is None:
        return None
    risk_pct = abs(_return_pct(invalidation, entry) or 0.0)
    if risk_pct <= 0:
        return None
    return round(planned_return / risk_pct, 4)


def _float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
