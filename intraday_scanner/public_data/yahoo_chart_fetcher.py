"""Optional read-only Yahoo Finance chart fetch/cache adapter."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from intraday_scanner.v2.data.market import MarketDataset, write_ohlcv_csv
from intraday_scanner.v2.data.yahoo_chart import (
    DEFAULT_YAHOO_CHART_SYMBOLS,
    YahooChartFetchResult,
    dataset_from_yahoo_chart_payloads,
)

YAHOO_CHART_BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def fetch_yahoo_chart_daily_dataset(
    *,
    symbols: tuple[str, ...] = DEFAULT_YAHOO_CHART_SYMBOLS,
    cache_dir: Path,
    range_period: str = "2y",
    interval: str = "1d",
    timeout_seconds: float = 20.0,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 0.25,
) -> YahooChartFetchResult:
    """Fetch public daily OHLCV bars and cache raw payloads for audit."""

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if retry_backoff_seconds < 0:
        raise ValueError("retry_backoff_seconds cannot be negative")
    cache_dir.mkdir(parents=True, exist_ok=True)
    requested_symbols = tuple(
        dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip())
    )
    payloads: dict[str, dict[str, Any]] = {}
    warnings: list[str] = [
        "public_yahoo_chart: free public chart data; not an institutional feed",
        "public_yahoo_chart: no independent corporate-action reconciliation",
    ]
    urls: dict[str, str] = {}
    for normalized in requested_symbols:
        url = _chart_url(normalized, range_period=range_period, interval=interval)
        urls[normalized] = url
        payload: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                payload = _fetch_json(url, timeout_seconds=timeout_seconds)
                if attempt > 1:
                    warnings.append(
                        f"{normalized}: public chart fetch succeeded after {attempt} attempts"
                    )
                break
            except (OSError, TimeoutError, TypeError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < max_attempts and retry_backoff_seconds:
                    time.sleep(
                        min(retry_backoff_seconds * (2 ** (attempt - 1)), 4.0)
                    )
        if payload is None:
            warnings.append(
                f"{normalized}: public chart fetch failed after {max_attempts} attempts "
                f"({last_error})"
            )
            continue
        payloads[normalized] = payload

    dataset = dataset_from_yahoo_chart_payloads(
        payloads,
        dataset_id=f"public_yahoo_chart_{range_period}_{interval}",
        source_kind="public_yahoo_chart",
        source_refs=tuple(urls[symbol] for symbol in requested_symbols),
        warnings=tuple(warnings),
    )
    missing_symbols = tuple(
        symbol for symbol in requested_symbols if symbol not in dataset.bars_by_symbol
    )
    if missing_symbols:
        warnings.append(
            "public_yahoo_chart: incomplete requested symbol set; cache not updated; "
            f"missing={list(missing_symbols)}"
        )
        dataset = MarketDataset(
            dataset_id=dataset.dataset_id,
            source_kind=dataset.source_kind,
            timeframe=dataset.timeframe,
            bars_by_symbol=dataset.bars_by_symbol,
            warnings=tuple(dict.fromkeys((*dataset.warnings, *warnings))),
            source_refs=dataset.source_refs,
        )
        return YahooChartFetchResult(
            dataset=dataset,
            raw_payload_paths=(),
            warnings=dataset.warnings,
        )

    raw_paths: list[Path] = []
    source_refs: list[str] = []
    for normalized in requested_symbols:
        raw_path = cache_dir / f"{normalized.lower()}_chart.json"
        raw_path.write_text(
            json.dumps(payloads[normalized], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raw_paths.append(raw_path)
        source_refs.extend((urls[normalized], raw_path.as_posix()))

    csv_path = cache_dir / "public_yahoo_ohlcv.csv"
    write_ohlcv_csv(dataset, csv_path)
    dataset = MarketDataset(
        dataset_id=dataset.dataset_id,
        source_kind=dataset.source_kind,
        timeframe=dataset.timeframe,
        bars_by_symbol=dataset.bars_by_symbol,
        source_path=csv_path.as_posix(),
        warnings=dataset.warnings,
        source_refs=(csv_path.as_posix(), *source_refs),
    )
    return YahooChartFetchResult(
        dataset=dataset,
        raw_payload_paths=tuple(raw_paths),
        warnings=dataset.warnings,
    )


def _chart_url(symbol: str, *, range_period: str, interval: str) -> str:
    query = urlencode(
        {
            "range": range_period,
            "interval": interval,
            "includePrePost": "false",
            "events": "history",
        }
    )
    return f"{YAHOO_CHART_BASE_URL.format(symbol=symbol)}?{query}"


def _fetch_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Dawnstrike-v2-AlphaLab/1.0 research-only",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read().decode("utf-8")
    loaded = json.loads(payload)
    if not isinstance(loaded, dict):
        raise TypeError("expected JSON object")
    return loaded
