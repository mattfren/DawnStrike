"""Optional read-only Nasdaq historical daily OHLCV fetch/cache adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from intraday_scanner.v2.data.market import MarketBar, MarketDataset, write_ohlcv_csv
from intraday_scanner.v2.data.yahoo_chart import DEFAULT_YAHOO_CHART_SYMBOLS

NASDAQ_BASE_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"
ETF_SYMBOLS = {"DIA", "IWM", "QQQ", "SPY", "VTI"}


@dataclass(frozen=True)
class NasdaqHistoricalFetchResult:
    dataset: MarketDataset
    raw_payload_paths: tuple[Path, ...]
    warnings: tuple[str, ...]


def fetch_nasdaq_historical_daily_dataset(
    *,
    symbols: tuple[str, ...] = DEFAULT_YAHOO_CHART_SYMBOLS,
    cache_dir: Path,
    start: date,
    end: date,
    timeout_seconds: float = 20.0,
) -> NasdaqHistoricalFetchResult:
    """Fetch public Nasdaq daily OHLCV rows and cache raw payloads for audit."""

    cache_dir.mkdir(parents=True, exist_ok=True)
    bars_by_symbol: dict[str, tuple[MarketBar, ...]] = {}
    raw_paths: list[Path] = []
    source_refs: list[str] = []
    warnings: list[str] = [
        "public_nasdaq_historical: free public endpoint; not an institutional feed",
        "public_nasdaq_historical: no independent corporate-action adjustment warranty",
    ]
    for symbol in symbols:
        normalized = symbol.strip().upper()
        if not normalized:
            continue
        url = _historical_url(normalized, start=start, end=end)
        source_refs.append(url)
        try:
            payload = _fetch_json(url, timeout_seconds=timeout_seconds)
        except (OSError, TimeoutError, TypeError, json.JSONDecodeError) as exc:
            warnings.append(f"{normalized}: Nasdaq historical fetch failed ({exc})")
            cached = cache_dir / f"{normalized.lower()}_nasdaq_historical.json"
            if cached.exists():
                payload = json.loads(cached.read_text(encoding="utf-8"))
                warnings.append(f"{normalized}: using cached Nasdaq historical payload")
            else:
                continue
        raw_path = cache_dir / f"{normalized.lower()}_nasdaq_historical.json"
        raw_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raw_paths.append(raw_path)
        source_refs.append(raw_path.as_posix())
        symbol_bars, symbol_warnings = _bars_from_payload(normalized, payload)
        warnings.extend(symbol_warnings)
        if symbol_bars:
            bars_by_symbol[normalized] = tuple(sorted(symbol_bars, key=lambda bar: bar.timestamp))

    dataset = MarketDataset(
        dataset_id="public_nasdaq_historical_1d",
        source_kind="public_nasdaq_historical",
        timeframe="1d",
        bars_by_symbol=bars_by_symbol,
        warnings=tuple(dict.fromkeys(warnings)),
        source_refs=tuple(source_refs),
    )
    csv_path = cache_dir / "public_nasdaq_ohlcv.csv"
    if dataset.total_bars:
        write_ohlcv_csv(dataset, csv_path)
        dataset = MarketDataset(
            dataset_id=dataset.dataset_id,
            source_kind=dataset.source_kind,
            timeframe=dataset.timeframe,
            bars_by_symbol=dataset.bars_by_symbol,
            source_path=csv_path.as_posix(),
            warnings=dataset.warnings,
            source_refs=(csv_path.as_posix(),) + dataset.source_refs,
        )
    return NasdaqHistoricalFetchResult(
        dataset=dataset,
        raw_payload_paths=tuple(raw_paths),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _historical_url(symbol: str, *, start: date, end: date) -> str:
    query = urlencode(
        {
            "assetclass": "etf" if symbol in ETF_SYMBOLS else "stocks",
            "fromdate": start.isoformat(),
            "todate": end.isoformat(),
            "limit": "9999",
        }
    )
    return f"{NASDAQ_BASE_URL.format(symbol=symbol)}?{query}"


def _fetch_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Dawnstrike-v2-DataTruth/1.0 research-only",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read().decode("utf-8")
    loaded = json.loads(payload)
    if not isinstance(loaded, dict):
        raise TypeError("expected JSON object")
    return loaded


def _bars_from_payload(
    symbol: str,
    payload: dict[str, Any],
) -> tuple[tuple[MarketBar, ...], list[str]]:
    warnings: list[str] = []
    rows = (
        payload.get("data", {})
        .get("tradesTable", {})
        .get("rows", [])
        if isinstance(payload.get("data"), dict)
        else []
    )
    if not isinstance(rows, list):
        return (), [f"{symbol}: Nasdaq historical payload rows were not a list"]
    bars: list[MarketBar] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            warnings.append(f"{symbol}: row {index} was not an object")
            continue
        try:
            market_date = datetime.strptime(str(row["date"]), "%m/%d/%Y").date()
            volume = int(_parse_money(row["volume"]))
            if volume == 9_999_999:
                warnings.append(
                    f"{symbol}: skipped Nasdaq placeholder volume row "
                    f"{market_date.isoformat()}"
                )
                continue
            bars.append(
                MarketBar(
                    symbol=symbol,
                    timestamp=datetime.combine(market_date, time(13, 30), tzinfo=timezone.utc),
                    open=_parse_money(row["open"]),
                    high=_parse_money(row["high"]),
                    low=_parse_money(row["low"]),
                    close=_parse_money(row["close"]),
                    volume=volume,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"{symbol}: invalid Nasdaq historical row {index} ({exc})")
    return tuple(bars), warnings


def _parse_money(value: object) -> float:
    text = str(value).replace("$", "").replace(",", "").strip()
    if not text or text.lower() in {"n/a", "nan"}:
        raise ValueError("empty numeric field")
    return float(text)
