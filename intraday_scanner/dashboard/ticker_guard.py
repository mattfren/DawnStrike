"""Ticker sanitation and dedupe helpers for operator dashboard views."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,5}$")
KNOWN_MALFORMED_TICKERS = {
    "SAGTSAGTEC",
    "CCDTCDT",
    "QTEXQTREX",
    "ECXECARX",
    "INLFINLIF",
    "SHMDSCHMID",
}


@dataclass(frozen=True)
class TickerDiagnostic:
    ticker: str
    reason: str
    source: str = ""


def normalize_ticker(value: Any) -> str:
    return str(value or "").strip().upper()


def is_malformed_ticker(value: Any, *, source_validated: bool = False) -> bool:
    ticker = normalize_ticker(value)
    if not ticker or ticker == "NO_TRADE":
        return True
    if ticker in KNOWN_MALFORMED_TICKERS:
        return True
    if not re.fullmatch(r"[A-Z0-9.\-]+", ticker):
        return True
    if _looks_concatenated(ticker):
        return True
    if len(ticker.replace(".", "").replace("-", "")) > 5 and not source_validated:
        return True
    return False


def is_valid_ticker(value: Any, *, source_validated: bool = False) -> bool:
    ticker = normalize_ticker(value)
    if is_malformed_ticker(ticker, source_validated=source_validated):
        return False
    return bool(TICKER_PATTERN.fullmatch(ticker)) or source_validated


def ticker_diagnostics(rows: Iterable[dict[str, Any]]) -> list[TickerDiagnostic]:
    diagnostics: list[TickerDiagnostic] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        ticker = normalize_ticker(row.get("ticker") or row.get("Ticker"))
        if not ticker:
            continue
        reason = _ticker_reject_reason(ticker)
        if not reason:
            continue
        key = (ticker, reason)
        if key in seen:
            continue
        seen.add(key)
        diagnostics.append(
            TickerDiagnostic(
                ticker=ticker,
                reason=reason,
                source=str(row.get("source") or row.get("data_source_kind") or ""),
            )
        )
    return diagnostics


def filter_valid_ticker_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**dict(row), "ticker": normalize_ticker(row.get("ticker") or row.get("Ticker"))}
        for row in rows
        if is_valid_ticker(row.get("ticker") or row.get("Ticker"))
    ]


def dedupe_rows_by_ticker(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        data = dict(row)
        ticker = normalize_ticker(data.get("ticker") or data.get("Ticker"))
        if not is_valid_ticker(ticker) or ticker in seen:
            continue
        data["ticker"] = ticker
        seen.add(ticker)
        output.append(data)
    return output


def dedupe_rows_by_signal(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        data = dict(row)
        ticker = normalize_ticker(data.get("ticker") or data.get("Ticker"))
        if not is_valid_ticker(ticker):
            continue
        signal_id = str(data.get("signal_id") or data.get("_signal_id") or "").strip()
        generated_at = str(data.get("generated_at") or data.get("timestamp") or "").strip()
        key = (signal_id or ticker, ticker, generated_at)
        if key in seen:
            continue
        data["ticker"] = ticker
        seen.add(key)
        output.append(data)
    return output


def _ticker_reject_reason(ticker: str) -> str:
    if not ticker or ticker == "NO_TRADE":
        return "not an operator ticker"
    if ticker in KNOWN_MALFORMED_TICKERS:
        return "known malformed concatenation"
    if not re.fullmatch(r"[A-Z0-9.\-]+", ticker):
        return "invalid characters"
    if _looks_concatenated(ticker):
        return "looks like two tickers joined together"
    if len(ticker.replace(".", "").replace("-", "")) > 5:
        return "too long for unverified symbol"
    return ""


def _looks_concatenated(ticker: str) -> bool:
    clean = ticker.replace(".", "").replace("-", "")
    if len(clean) < 6:
        return False
    midpoint = len(clean) // 2
    if len(clean) % 2 == 0 and clean[:midpoint] == clean[midpoint:]:
        return True
    return bool(re.fullmatch(r"[A-Z]{2,5}[A-Z]{2,5}", clean)) and any(
        clean.endswith(clean[:index]) for index in range(2, min(5, len(clean) - 1))
    )
