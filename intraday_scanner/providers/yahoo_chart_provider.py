"""Read-only Yahoo chart transport shared by research workflows.

The provider returns public market-data observations only.  It has no order,
broker, or recommendation capability.  Callers remain responsible for
point-in-time eligibility and market-session interpretation.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.network_safety import open_allowlisted_url

YAHOO_CHART_BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_SOURCE_NAME = "yahoo_finance_chart"


def yahoo_provider_symbol(symbol: str) -> str:
    """Map a canonical US equity symbol to Yahoo's explicit path spelling."""

    return str(symbol or "").strip().upper().replace(".", "-")


def yahoo_chart_url(
    symbol: str,
    *,
    range_name: str = "1d",
    interval: str = "1m",
    include_pre_post: bool = True,
) -> str:
    query = urllib.parse.urlencode(
        {
            "range": range_name,
            "interval": interval,
            "includePrePost": "true" if include_pre_post else "false",
        }
    )
    provider_symbol = yahoo_provider_symbol(symbol)
    return f"{YAHOO_CHART_BASE_URL}/{urllib.parse.quote(provider_symbol)}?{query}"


def fetch_yahoo_chart(
    symbol: str,
    config: ScannerConfig,
    *,
    range_name: str = "1d",
    interval: str = "1m",
    include_pre_post: bool = True,
) -> dict[str, Any]:
    """Fetch a read-only Yahoo chart payload for an explicit observation window."""

    url = yahoo_chart_url(
        symbol,
        range_name=range_name,
        interval=interval,
        include_pre_post=include_pre_post,
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Dawnstrike research price observer",
        },
        method="GET",
    )
    last_error: Exception | None = None
    for attempt in range(1, config.request_retries + 1):
        try:
            with open_allowlisted_url(
                request,
                timeout=config.request_timeout_seconds,
                allowed_hosts=("query1.finance.yahoo.com",),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise DataProviderError("Yahoo Finance chart response was not an object.")
                return payload
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = exc
            if 400 <= exc.code < 500:
                raise DataProviderError(
                    f"Yahoo Finance price request failed with HTTP {exc.code}: {body[:180]}"
                ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < config.request_retries:
            time.sleep(min(2 ** (attempt - 1), 4))
    raise DataProviderError(f"Yahoo Finance price request failed after retries: {last_error}")


def bars_from_yahoo_chart_payload(
    symbol: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Normalize Yahoo quote arrays without inventing missing bar values."""

    result = chart_result(payload)
    if not result:
        return []
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quotes = indicators.get("quote") if isinstance(indicators, dict) else []
    quote = quotes[0] if isinstance(quotes, list) and quotes else {}
    if not isinstance(quote, dict) or not isinstance(timestamps, list):
        return []
    closes = quote.get("close") or []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    volumes = quote.get("volume") or []
    rows = [
        {
            "ticker": symbol.upper(),
            "timestamp": int(timestamp),
            "open": _list_value(opens, index),
            "high": _list_value(highs, index),
            "low": _list_value(lows, index),
            "close": close,
            "volume": _list_value(volumes, index),
            "source": YAHOO_SOURCE_NAME,
        }
        for index, timestamp in enumerate(timestamps)
        if _list_value(closes, index) not in {None, ""}
        for close in [_list_value(closes, index)]
    ]
    meta = result.get("meta") or {}
    if isinstance(meta, dict):
        meta_price = _clean_float(meta.get("regularMarketPrice"))
        market_time = meta.get("regularMarketTime")
        if meta_price is not None and market_time not in {None, ""}:
            rows.append(
                {
                    "ticker": symbol.upper(),
                    "timestamp": market_time,
                    "close": meta_price,
                    "source": f"{YAHOO_SOURCE_NAME}_meta",
                }
            )
    return rows


def chart_result(payload: dict[str, Any]) -> dict[str, Any]:
    chart = payload.get("chart") if isinstance(payload, dict) else {}
    if not isinstance(chart, dict):
        return {}
    results = chart.get("result") or []
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return {}
    return dict(results[0])


def _list_value(values: Any, index: int) -> Any:
    if not isinstance(values, list) or index >= len(values):
        return None
    return values[index]


def _clean_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
