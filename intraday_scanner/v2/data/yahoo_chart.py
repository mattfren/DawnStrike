"""Pure public Yahoo Finance chart payload parsing for v2 Alpha Lab."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.v2.data.market import MarketBar, MarketDataset

DEFAULT_YAHOO_CHART_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN")


@dataclass(frozen=True)
class YahooChartFetchResult:
    dataset: MarketDataset
    raw_payload_paths: tuple[Path, ...]
    warnings: tuple[str, ...]


def dataset_from_yahoo_chart_payloads(
    payloads: dict[str, dict[str, Any]],
    *,
    dataset_id: str,
    source_kind: str,
    source_refs: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> MarketDataset:
    bars_by_symbol: dict[str, tuple[MarketBar, ...]] = {}
    mutable_warnings = list(warnings)
    for symbol, payload in sorted(payloads.items()):
        bars, symbol_warnings = _bars_from_payload(symbol, payload)
        mutable_warnings.extend(symbol_warnings)
        if bars:
            bars_by_symbol[symbol] = tuple(sorted(bars, key=lambda bar: bar.timestamp))
    return MarketDataset(
        dataset_id=dataset_id,
        source_kind=source_kind,
        timeframe="1d",
        bars_by_symbol=bars_by_symbol,
        warnings=tuple(dict.fromkeys(mutable_warnings)),
        source_refs=source_refs,
    )


def _bars_from_payload(symbol: str, payload: dict[str, Any]) -> tuple[list[MarketBar], list[str]]:
    warnings: list[str] = []
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        return [], [f"{symbol}: missing chart object"]
    error = chart.get("error")
    if error:
        return [], [f"{symbol}: chart error {error}"]
    result_items = chart.get("result")
    if not isinstance(result_items, list) or not result_items:
        return [], [f"{symbol}: missing chart result"]
    result = result_items[0]
    if not isinstance(result, dict):
        return [], [f"{symbol}: invalid chart result"]
    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    if not isinstance(timestamps, list) or not isinstance(indicators, dict):
        return [], [f"{symbol}: missing timestamps or indicators"]
    quote_items = indicators.get("quote")
    if not isinstance(quote_items, list) or not quote_items or not isinstance(quote_items[0], dict):
        return [], [f"{symbol}: missing quote indicators"]
    quote = quote_items[0]
    bars: list[MarketBar] = []
    for index, timestamp_value in enumerate(timestamps):
        try:
            open_value = _number_at(quote, "open", index)
            high_value = _number_at(quote, "high", index)
            low_value = _number_at(quote, "low", index)
            close_value = _number_at(quote, "close", index)
            volume_value = _number_at(quote, "volume", index)
        except (TypeError, ValueError, IndexError) as exc:
            warnings.append(f"{symbol}: skipped bar {index} ({exc})")
            continue
        if (
            open_value is None
            or high_value is None
            or low_value is None
            or close_value is None
        ):
            warnings.append(f"{symbol}: skipped bar {index} with incomplete OHLC")
            continue
        open_price = float(open_value)
        high_price = float(high_value)
        low_price = float(low_value)
        close_price = float(close_value)
        if not all(
            math.isfinite(value)
            for value in (open_price, high_price, low_price, close_price)
        ):
            warnings.append(f"{symbol}: skipped bar {index} with non-finite OHLC")
            continue
        if not isinstance(volume_value, int) or isinstance(volume_value, bool):
            warnings.append(f"{symbol}: skipped bar {index} with non-integer volume")
            continue
        volume = float(volume_value)
        if not math.isfinite(volume) or volume < 0:
            warnings.append(f"{symbol}: skipped bar {index} with invalid volume")
            continue
        try:
            timestamp = float(timestamp_value)
        except (TypeError, ValueError):
            warnings.append(f"{symbol}: skipped bar {index} with invalid timestamp")
            continue
        if isinstance(timestamp_value, bool):
            warnings.append(f"{symbol}: skipped bar {index} with invalid timestamp")
            continue
        if not math.isfinite(timestamp):
            warnings.append(f"{symbol}: skipped bar {index} with non-finite timestamp")
            continue
        if min(open_price, high_price, low_price, close_price) <= 0:
            warnings.append(f"{symbol}: skipped bar {index} with non-positive OHLC")
            continue
        if high_price < max(open_price, close_price, low_price):
            warnings.append(f"{symbol}: skipped bar {index} with invalid high")
            continue
        if low_price > min(open_price, close_price, high_price):
            warnings.append(f"{symbol}: skipped bar {index} with invalid low")
            continue
        try:
            parsed_timestamp = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            warnings.append(f"{symbol}: skipped bar {index} with invalid timestamp")
            continue
        bars.append(
            MarketBar(
                symbol=symbol,
                timestamp=parsed_timestamp,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=int(volume),
            )
        )
    return bars, warnings


def _number_at(
    quote: dict[str, Any],
    key: str,
    index: int,
) -> float | int | None:
    values = quote.get(key)
    if not isinstance(values, list):
        raise TypeError(f"missing {key} series")
    value = values[index]
    if value is None:
        raise ValueError(f"missing {key}")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"invalid {key} value")
    return value
