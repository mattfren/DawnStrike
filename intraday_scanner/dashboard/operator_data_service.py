"""Canonical operator data calculations shared by dashboard and CLI tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from intraday_scanner.dashboard.ticker_guard import is_valid_ticker, normalize_ticker
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def calculate_missing_outcome_status(
    db_path: str | Path,
    market_date: str | None = None,
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    selected_date, signal_rows, outcomes = _canonical_signal_context(store, market_date)
    missing = _missing_rows(signal_rows, outcomes)
    partial = [
        row
        for row in outcomes
        if _has_partial_outcome(row)
        and not _has_complete_outcome(row)
    ]
    audited_count = sum(1 for row in outcomes if _has_complete_outcome(row))
    required_count = len(signal_rows)
    missing_count = len(missing)
    missing_rate = round((missing_count / required_count) * 100.0, 2) if required_count else 0.0
    audited_day_count = _audited_day_count(store)
    evidence_status = _evidence_status(audited_day_count)
    return {
        "market_date": selected_date,
        "total_signals": len(signal_rows),
        "signal_count": len(signal_rows),
        "signals_requiring_outcomes": required_count,
        "signals_requiring_outcome": required_count,
        "partial_outcomes": len(partial),
        "outcomes_imported": len(outcomes),
        "missing_outcomes": missing_count,
        "missing_outcome_count": missing_count,
        "missing_tickers": [normalize_ticker(row.get("ticker")) for row in missing],
        "missing_outcome_rate": missing_rate,
        "partial_outcome_count": len(partial),
        "audited_count": audited_count,
        "audited_day_count": audited_day_count,
        "evidence_status": evidence_status,
    }


def canonical_missing_outcome_rows(
    db_path: str | Path,
    market_date: str | None = None,
) -> list[dict[str, Any]]:
    store = SQLiteScanStore(db_path)
    selected_date, signal_rows, outcomes = _canonical_signal_context(store, market_date)
    return [
        {
            "market_date": selected_date,
            "ticker": normalize_ticker(row.get("ticker")),
            "signal_id": str(row.get("signal_id") or ""),
            "rank": row.get("rank"),
            "expected_csv": f"data\\inbox\\outcomes\\outcomes_{selected_date}.csv",
            "audit_status": "Outcome needed",
        }
        for row in _missing_rows(signal_rows, outcomes)
    ]


def _canonical_signal_context(
    store: SQLiteScanStore,
    market_date: str | None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    signals = store.load_historical_signals(market_date=market_date, limit=50000)
    dates = _available_dates(signals)
    selected_date = (market_date or (dates[-1] if dates else ""))[:10]
    if selected_date:
        signals = [
            row for row in signals if str(row.get("market_date") or "")[:10] == selected_date
        ]
    signal_rows = _unique_outcome_required_signals(signals)
    outcomes = store.load_signal_outcomes(
        start=selected_date or None,
        end=selected_date or None,
        limit=50000,
    )
    return selected_date, signal_rows, outcomes


def _missing_rows(
    signal_rows: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    outcome_signal_ids = {
        str(row.get("signal_id") or "")
        for row in outcomes
        if str(row.get("signal_id") or "").strip()
    }
    outcome_tickers = {
        normalize_ticker(row.get("ticker"))
        for row in outcomes
        if normalize_ticker(row.get("ticker"))
    }
    missing = [
        row
        for row in signal_rows
        if str(row.get("signal_id") or "") not in outcome_signal_ids
        and normalize_ticker(row.get("ticker")) not in outcome_tickers
    ]
    return missing


def _unique_outcome_required_signals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in sorted(
        rows,
        key=lambda item: (
            str(item.get("market_date") or ""),
            str(item.get("generated_at") or ""),
            _safe_rank(item.get("rank")),
            normalize_ticker(item.get("ticker")),
        ),
        reverse=True,
    ):
        ticker = normalize_ticker(row.get("ticker"))
        if not is_valid_ticker(ticker):
            continue
        if not signal_requires_outcome(row):
            continue
        key = ticker
        if key in seen:
            continue
        seen.add(key)
        output.append(dict(row))
    return sorted(
        output,
        key=lambda item: (_safe_rank(item.get("rank")), normalize_ticker(item.get("ticker"))),
    )


def signal_requires_outcome(row: dict[str, Any]) -> bool:
    """Return True only for saved signals that require real outcome evidence."""
    ticker = normalize_ticker(row.get("ticker"))
    if ticker == "NO_TRADE":
        return False
    label = str(row.get("signal_label") or row.get("status") or "").strip().upper()
    if label in {"NO CLEAN EDGE", "DATA MISSING", "NO_TRADE"}:
        raw = _raw_payload(row)
        raw_label = str(raw.get("signal_label") or raw.get("label") or "").strip().upper()
        if raw_label not in {
            "WATCH",
            "WATCH ONLY",
            "ENTRY WATCH",
            "ENTRY TRIGGERED",
            "TARGET HIT",
            "EXIT SIGNAL",
        }:
            return False
    if str(row.get("no_trade_reason") or "").strip():
        return False
    raw = _raw_payload(row)
    if raw.get("can_alert") is False:
        return False
    if raw.get("trade_plan_blocks_alert") is True:
        return False
    if str(raw.get("no_trade_reason") or "").strip():
        return False
    return True


def _raw_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("raw_payload_json") or row.get("payload_json") or {}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str) and payload.strip():
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _available_dates(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(row.get("market_date") or "")[:10]
            for row in rows
            if str(row.get("market_date") or "")[:10]
        }
    )


def _has_partial_outcome(row: dict[str, Any]) -> bool:
    return any(
        row.get(field) not in {None, ""}
        for field in (
            "entry_price",
            "price_1m",
            "price_5m",
            "price_15m",
            "lunch_price",
            "close_price",
            "high_after_entry",
            "low_after_entry",
        )
    )


def _has_complete_outcome(row: dict[str, Any]) -> bool:
    return row.get("entry_price") not in {None, ""} and row.get("close_price") not in {None, ""}


def _audited_day_count(store: SQLiteScanStore) -> int:
    signals = store.load_historical_signals(limit=50000)
    dates = _available_dates(signals)
    audited = 0
    for day in dates:
        day_signals = _unique_outcome_required_signals(
            [
                row
                for row in signals
                if str(row.get("market_date") or "")[:10] == day
            ]
        )
        if not day_signals:
            continue
        outcomes = store.load_signal_outcomes(start=day, end=day, limit=50000)
        missing = _missing_rows(day_signals, outcomes)
        complete = sum(1 for row in outcomes if _has_complete_outcome(row))
        if not missing and complete >= len(day_signals):
            audited += 1
    return audited


def _evidence_status(audited_day_count: int) -> str:
    if audited_day_count <= 0:
        return "NO_AUDITED_DAYS"
    if audited_day_count < 20:
        return "INSUFFICIENT_HISTORY"
    return "READY_FOR_EARLY_REVIEW"


def _safe_rank(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 999999
