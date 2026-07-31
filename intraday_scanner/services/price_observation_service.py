"""Collect auditable time-specific stock prices.

The service only records research prices. It does not place, route, or prepare
orders. Usable observations always come from bars at or before the requested
timestamp so historical checks do not accidentally look into the future.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.config import ScannerConfig, load_config
from intraday_scanner.dashboard.ticker_guard import is_valid_ticker, normalize_ticker
from intraday_scanner.errors import DataProviderError, SnapshotValidationError
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.providers.yahoo_chart_provider import (
    bars_from_yahoo_chart_payload,
    fetch_yahoo_chart,
)
from intraday_scanner.snapshot_builder import MINUTE_BAR_COLUMNS
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc


@dataclass(frozen=True)
class PriceTarget:
    ticker: str
    signal_id: str = ""
    market_date: str = ""


def collect_price_observations(
    *,
    db_path: str | Path,
    source: str = "auto",
    tickers: list[str] | None = None,
    market_date: str | None = None,
    requested_at: str | None = None,
    minute_bars: str | Path | None = None,
    max_age_seconds: int = 360,
    persist: bool = True,
    config: ScannerConfig | None = None,
) -> dict[str, Any]:
    """Collect and optionally persist latest usable prices for targets."""

    if max_age_seconds <= 0:
        raise SnapshotValidationError("max_age_seconds must be positive.")
    store = SQLiteScanStore(db_path)
    store.initialize()
    resolved_source = _resolve_source(source, minute_bars)
    at = parse_requested_at(requested_at, market_date=market_date)
    request_market_date = market_date or at.astimezone(EASTERN).date().isoformat()
    targets = _price_targets(store, tickers=tickers, market_date=request_market_date)
    if not targets:
        return {
            "status": "no_targets",
            "source": resolved_source,
            "requested_at": _iso_utc(at),
            "market_date": request_market_date,
            "target_count": 0,
            "usable_count": 0,
            "rejected_count": 0,
            "persisted": {"inserted": 0, "skipped": 0, "row_count": 0},
            "observations": [],
            "message": "No tickers or saved signals were available for price observation.",
        }
    bars = _load_bars(
        source=resolved_source,
        targets=targets,
        requested_at=at,
        max_age_seconds=max_age_seconds,
        minute_bars=minute_bars,
        config=config,
    )
    created_at = _iso_utc(datetime.now(UTC))
    observations = [
        _observation_from_bars(
            target=target,
            bars=bars.get(target.ticker, []),
            requested_at=at,
            market_date=request_market_date,
            source=resolved_source,
            max_age_seconds=max_age_seconds,
            created_at=created_at,
        )
        for target in targets
    ]
    persisted = (
        _persist_price_observations(store, observations, replace=True)
        if persist
        else {"inserted": 0, "skipped": 0, "row_count": len(observations)}
    )
    usable_count = sum(1 for row in observations if row.get("is_usable"))
    rejected_count = len(observations) - usable_count
    return {
        "status": "ok" if usable_count else "no_usable_prices",
        "source": resolved_source,
        "requested_at": _iso_utc(at),
        "market_date": request_market_date,
        "target_count": len(targets),
        "usable_count": usable_count,
        "rejected_count": rejected_count,
        "persisted": persisted,
        "observations": observations,
        "no_lookahead": True,
        "note": "Only bars observed at or before requested_at are eligible.",
    }


def parse_requested_at(value: str | None, *, market_date: str | None = None) -> datetime:
    if not value:
        return datetime.now(UTC)
    text = value.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text):
        if not market_date:
            raise SnapshotValidationError("--at HH:MM requires --market-date.")
        suffix = ":00" if text.count(":") == 1 else ""
        return datetime.fromisoformat(f"{market_date}T{text}{suffix}").replace(tzinfo=EASTERN)
    try:
        parsed = _parse_datetime(text)
    except ValueError as exc:
        raise SnapshotValidationError(f"Invalid requested price timestamp: {value}") from exc
    return parsed


def _resolve_source(source: str, minute_bars: str | Path | None) -> str:
    normalized = source.strip().lower()
    if normalized == "auto":
        return "csv" if minute_bars else "yahoo"
    if normalized not in {"csv", "alpaca", "yahoo"}:
        raise SnapshotValidationError("price source must be one of: auto, csv, alpaca, yahoo.")
    return normalized


def _price_targets(
    store: SQLiteScanStore,
    *,
    tickers: list[str] | None,
    market_date: str,
) -> list[PriceTarget]:
    if tickers:
        return _dedupe_targets(
            [
                PriceTarget(ticker=ticker.strip().upper(), market_date=market_date)
                for ticker in tickers
                if ticker.strip()
            ]
        )
    signals = store.load_historical_signals(market_date=market_date, limit=50)
    if signals:
        return _dedupe_targets(
            [
                PriceTarget(
                    ticker=str(row.get("ticker") or "").upper(),
                    signal_id=str(row.get("signal_id") or ""),
                    market_date=str(row.get("market_date") or market_date)[:10],
                )
                for row in signals
                if str(row.get("ticker") or "").strip()
            ]
        )
    latest = store.load_latest_scan() or {}
    raw_candidates = latest.get("ranked_candidates") or latest.get("top_explosive") or []
    if not isinstance(raw_candidates, list):
        raw_candidates = []
    candidates = [dict(row) for row in raw_candidates if isinstance(row, dict)]
    return _dedupe_targets(
        [
            PriceTarget(
                ticker=str(row.get("ticker") or "").upper(),
                signal_id=str(row.get("signal_id") or row.get("signal_key") or ""),
                market_date=market_date,
            )
            for row in candidates[:50]
            if str(row.get("ticker") or "").strip()
        ]
    )


def _dedupe_targets(targets: list[PriceTarget]) -> list[PriceTarget]:
    seen: set[str] = set()
    output: list[PriceTarget] = []
    for target in targets:
        ticker = normalize_ticker(target.ticker)
        if not is_valid_ticker(ticker) or ticker in seen:
            continue
        seen.add(ticker)
        output.append(
            PriceTarget(ticker=ticker, signal_id=target.signal_id, market_date=target.market_date)
        )
    return output


def _load_bars(
    *,
    source: str,
    targets: list[PriceTarget],
    requested_at: datetime,
    max_age_seconds: int,
    minute_bars: str | Path | None,
    config: ScannerConfig | None,
) -> dict[str, list[dict[str, Any]]]:
    if source == "csv":
        if minute_bars is None:
            raise SnapshotValidationError("--minute-bars is required when --source csv.")
        return _read_minute_bars(minute_bars)
    if source == "alpaca":
        scanner_config = config or load_config()
        provider = AlpacaProvider(scanner_config)
        provider.validate_credentials()
        symbols = sorted({target.ticker for target in targets if target.ticker})
        start = _iso_utc(requested_at - timedelta(seconds=max_age_seconds + 120))
        end = _iso_utc(requested_at)
        rows = provider.get_minute_bars(symbols, start, end, scanner_config)
        return _group_bars(rows)
    if source == "yahoo":
        scanner_config = config or load_config()
        symbols = sorted({target.ticker for target in targets if target.ticker})
        yahoo_rows: list[dict[str, Any]] = []
        for symbol in symbols:
            try:
                payload = _fetch_yahoo_chart(symbol, scanner_config)
            except DataProviderError as exc:
                yahoo_rows.append(
                    {
                        "ticker": symbol,
                        "timestamp": _iso_utc(requested_at),
                        "close": None,
                        "provider_status": "provider_error",
                        "error": str(exc),
                        "source": "yahoo_finance_chart",
                    }
                )
                continue
            yahoo_rows.extend(_bars_from_yahoo_chart_payload(symbol, payload))
        return _group_bars(yahoo_rows)
    raise SnapshotValidationError(f"Unsupported price source: {source}")


def _fetch_yahoo_chart(symbol: str, config: ScannerConfig) -> dict[str, Any]:
    return fetch_yahoo_chart(symbol, config)


def _bars_from_yahoo_chart_payload(symbol: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    return bars_from_yahoo_chart_payload(symbol, payload)


def _read_minute_bars(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise SnapshotValidationError(f"Minute bars file does not exist: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or [])
        missing = [column for column in MINUTE_BAR_COLUMNS if column not in fieldnames]
        if missing:
            raise SnapshotValidationError(
                f"Minute bars file missing required column(s): {', '.join(missing)}"
            )
        return _group_bars(list(reader))


def _group_bars(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        ticker = str(row.get("ticker") or row.get("symbol") or "").upper()
        if ticker:
            grouped[ticker].append(dict(row))
    for ticker in grouped:
        grouped[ticker].sort(key=lambda row: _bar_time(row) or datetime.min.replace(tzinfo=UTC))
    return grouped


def _observation_from_bars(
    *,
    target: PriceTarget,
    bars: list[dict[str, Any]],
    requested_at: datetime,
    market_date: str,
    source: str,
    max_age_seconds: int,
    created_at: str,
) -> dict[str, Any]:
    prior = [
        row
        for row in bars
        if (timestamp := _bar_time(row)) is not None and timestamp <= requested_at
    ]
    prior.sort(key=lambda row: _bar_time(row) or datetime.min.replace(tzinfo=UTC), reverse=True)
    if not prior:
        return _rejected_observation(
            target=target,
            requested_at=requested_at,
            market_date=market_date,
            source=source,
            status="no_prior_bar",
            max_age_seconds=max_age_seconds,
            created_at=created_at,
        )
    bar = prior[0]
    observed_at = _bar_time(bar)
    price = _bar_price(bar)
    if observed_at is None or price is None or price <= 0:
        return _rejected_observation(
            target=target,
            requested_at=requested_at,
            market_date=market_date,
            source=source,
            status=str(bar.get("provider_status") or "malformed_bar"),
            max_age_seconds=max_age_seconds,
            created_at=created_at,
            bar=bar,
        )
    freshness = int((requested_at - observed_at).total_seconds())
    if freshness > max_age_seconds:
        return _rejected_observation(
            target=target,
            requested_at=requested_at,
            market_date=market_date,
            source=source,
            status="stale_rejected",
            max_age_seconds=max_age_seconds,
            created_at=created_at,
            observed_at=observed_at,
            freshness_seconds=freshness,
            bar=bar,
        )
    requested_iso = _iso_utc(requested_at)
    observed_iso = _iso_utc(observed_at)
    return {
        "observation_id": _observation_id(target, source, requested_iso),
        "signal_id": target.signal_id,
        "market_date": market_date,
        "ticker": target.ticker,
        "requested_at": requested_iso,
        "observed_at": observed_iso,
        "price": price,
        "current_price": price,
        "price_type": "last_bar_close_at_or_before",
        "source": source,
        "source_kind": _source_kind(source),
        "provider": _provider_name(source),
        "provider_status": "exact" if freshness == 0 else "fresh_prior_bar",
        "freshness_seconds": freshness,
        "tolerance_seconds": max_age_seconds,
        "is_usable": True,
        "created_at": created_at,
        "payload_json": {
            "bar": _safe_bar_payload(bar),
            "no_lookahead": True,
            "price_rule": "latest bar timestamp <= requested_at",
        },
    }


def _rejected_observation(
    *,
    target: PriceTarget,
    requested_at: datetime,
    market_date: str,
    source: str,
    status: str,
    max_age_seconds: int,
    created_at: str,
    observed_at: datetime | None = None,
    freshness_seconds: int | None = None,
    bar: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested_iso = _iso_utc(requested_at)
    return {
        "observation_id": _observation_id(target, source, requested_iso),
        "signal_id": target.signal_id,
        "market_date": market_date,
        "ticker": target.ticker,
        "requested_at": requested_iso,
        "observed_at": _iso_utc(observed_at) if observed_at is not None else "",
        "price": None,
        "current_price": None,
        "price_type": "last_bar_close_at_or_before",
        "source": source,
        "source_kind": _source_kind(source),
        "provider": _provider_name(source),
        "provider_status": status,
        "freshness_seconds": freshness_seconds,
        "tolerance_seconds": max_age_seconds,
        "is_usable": False,
        "created_at": created_at,
        "payload_json": {
            "bar": _safe_bar_payload(bar or {}),
            "no_lookahead": True,
            "reject_reason": status,
        },
    }


def _observation_id(target: PriceTarget, source: str, requested_at: str) -> str:
    basis = f"{target.signal_id or target.ticker}:{target.ticker}:{source}:{requested_at}"
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", basis)


def _source_kind(source: str) -> str:
    if source == "csv":
        return "local_minute_bars"
    if source == "yahoo":
        return "public_web_market_data"
    return "market_data_api"


def _provider_name(source: str) -> str:
    if source == "csv":
        return "csv_minute_bars"
    if source == "yahoo":
        return "yahoo_finance_chart"
    return "alpaca_market_data"


def _bar_time(row: dict[str, Any]) -> datetime | None:
    raw = row.get("timestamp") or row.get("t") or row.get("time")
    if raw in {None, ""}:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value > 10_000_000_000:
            value = value / 1000
        return datetime.fromtimestamp(value, tz=UTC)
    try:
        return _parse_datetime(str(raw))
    except ValueError:
        return None


def _bar_price(row: dict[str, Any]) -> float | None:
    for key in ("close", "c", "price", "last_price", "current_price"):
        value = _clean_float(row.get(key))
        if value is not None:
            return value
    return None


def _clean_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    text = str(value).replace("$", "").replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        cleaned = re.sub(r"[^0-9.\-]", "", text)
        if cleaned in {"", ".", "-", "-."}:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None


def _parse_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    return parsed.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=EASTERN)
    return normalized.astimezone(UTC).isoformat()


def _safe_bar_payload(row: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "ticker",
        "symbol",
        "timestamp",
        "t",
        "open",
        "o",
        "high",
        "h",
        "low",
        "l",
        "close",
        "c",
        "volume",
        "v",
        "source",
        "provider_status",
        "error",
    }
    return {key: value for key, value in row.items() if key in allowed}


def _persist_price_observations(
    store: SQLiteScanStore,
    observations: list[dict[str, Any]],
    *,
    replace: bool,
) -> dict[str, int]:
    method = getattr(store, "persist_price_observations", None)
    if callable(method):
        result = method(observations, replace=replace)
        return dict(result)
    return _persist_price_observations_direct(store.db_path, observations, replace=replace)


def _persist_price_observations_direct(
    db_path: Path,
    observations: list[dict[str, Any]],
    *,
    replace: bool,
) -> dict[str, int]:
    statement = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    inserted = 0
    skipped = 0
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        _ensure_price_observations_table(connection)
        for row in observations:
            observation_id = str(row.get("observation_id") or "")
            ticker = str(row.get("ticker") or "").upper()
            market_date = str(row.get("market_date") or "")[:10]
            requested_at = str(row.get("requested_at") or "")
            if not observation_id or not ticker or not market_date or not requested_at:
                continue
            cursor = connection.execute(
                f"""
                {statement} INTO price_observations
                (observation_id, signal_id, market_date, ticker, requested_at,
                 observed_at, price, price_type, source, source_kind, provider,
                 provider_status, freshness_seconds, tolerance_seconds, is_usable,
                 created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,  # noqa: S608
                (
                    observation_id,
                    str(row.get("signal_id") or ""),
                    market_date,
                    ticker,
                    requested_at,
                    str(row.get("observed_at") or ""),
                    _clean_float(row.get("price")),
                    str(row.get("price_type") or "last_bar_close_at_or_before"),
                    str(row.get("source") or ""),
                    str(row.get("source_kind") or ""),
                    str(row.get("provider") or ""),
                    str(row.get("provider_status") or ""),
                    _int_or_none(row.get("freshness_seconds")),
                    int(_int_or_none(row.get("tolerance_seconds")) or 0),
                    1 if row.get("is_usable") else 0,
                    str(row.get("created_at") or ""),
                    json.dumps(row.get("payload_json") or row, sort_keys=True),
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                skipped += 1
    return {"inserted": inserted, "skipped": skipped, "row_count": len(observations)}


def _ensure_price_observations_table(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS price_observations (
            observation_id TEXT PRIMARY KEY,
            signal_id TEXT,
            market_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            observed_at TEXT,
            price REAL,
            price_type TEXT NOT NULL,
            source TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_status TEXT NOT NULL,
            freshness_seconds INTEGER,
            tolerance_seconds INTEGER NOT NULL,
            is_usable INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_price_observations_date_ticker
        ON price_observations(market_date, ticker, observed_at);
        CREATE INDEX IF NOT EXISTS idx_price_observations_signal
        ON price_observations(signal_id, observed_at);
        """
    )


def _int_or_none(value: Any) -> int | None:
    parsed = _clean_float(value)
    return int(parsed) if parsed is not None else None
