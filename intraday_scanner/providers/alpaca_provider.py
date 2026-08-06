"""Alpaca market data provider.

This module consumes Alpaca's market-data endpoints only. It intentionally does not
touch trading or order-submission APIs.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import SnapshotRow
from intraday_scanner.network_safety import open_allowlisted_url
from intraday_scanner.providers.base import MarketDataProvider

LOGGER = logging.getLogger(__name__)


class AlpacaProvider(MarketDataProvider):
    base_url = "https://data.alpaca.markets"

    def __init__(self, config: ScannerConfig):
        self.api_key = config.alpaca_api_key_id
        self.secret_key = config.alpaca_api_secret_key
        self.feed = config.alpaca_data_feed

    def validate_credentials(self) -> None:
        missing = []
        if not self.api_key:
            missing.append("ALPACA_API_KEY_ID")
        if not self.secret_key:
            missing.append("ALPACA_API_SECRET_KEY")
        if missing:
            raise DataProviderError(
                "Missing Alpaca market-data credential(s): "
                + ", ".join(missing)
                + ". Add them to your environment or .env file. No API keys were logged."
            )

    def _request_json(
        self, path: str, params: dict[str, str], config: ScannerConfig
    ) -> dict[str, Any]:
        self.validate_credentials()
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
                "Accept": "application/json",
            },
            method="GET",
        )
        last_error: Exception | None = None
        for attempt in range(1, config.request_retries + 1):
            try:
                with open_allowlisted_url(
                    request,
                    timeout=config.request_timeout_seconds,
                    allowed_hosts=("data.alpaca.markets",),
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if 400 <= exc.code < 500:
                    raise DataProviderError(
                        f"Alpaca request failed with HTTP {exc.code}: {body[:300]}"
                    ) from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < config.request_retries:
                sleep_seconds = min(2 ** (attempt - 1), 8)
                LOGGER.warning("Alpaca request failed; retrying in %ss", sleep_seconds)
                time.sleep(sleep_seconds)
        raise DataProviderError(
            f"Alpaca request failed after retries: {last_error}"
        ) from last_error

    def get_premarket_snapshot(
        self, symbols: Sequence[str] | None, config: ScannerConfig
    ) -> list[SnapshotRow]:
        if not symbols:
            raise DataProviderError("live-scan requires --symbols for Alpaca provider runs")
        payload = self._request_json(
            "/v2/stocks/snapshots",
            {"symbols": ",".join(symbols), "feed": self.feed},
            config,
        )
        snapshots = payload.get("snapshots", payload)
        rows: list[SnapshotRow] = []
        for symbol, snapshot in snapshots.items():
            if not isinstance(snapshot, dict):
                continue
            minute_bar = snapshot.get("minuteBar") or {}
            daily_bar = snapshot.get("dailyBar") or {}
            prev_daily_bar = snapshot.get("prevDailyBar") or {}
            latest_quote = snapshot.get("latestQuote") or {}
            latest_trade = snapshot.get("latestTrade") or {}
            price = _first_number(latest_trade.get("p"), minute_bar.get("c"), daily_bar.get("c"))
            previous_close = _first_number(prev_daily_bar.get("c"), 0.0)
            high = _first_number(daily_bar.get("h"), minute_bar.get("h"), price)
            low = _first_number(daily_bar.get("l"), minute_bar.get("l"), price)
            # The latest minute's volume is not a usable premarket-liquidity
            # screen. Alpaca's current daily bar is cumulative and is therefore
            # the correct discovery-stage volume; the enrichment service later
            # replaces it with an exact 04:00-to-decision premarket sum.
            volume = int(_first_number(daily_bar.get("v"), minute_bar.get("v"), 0.0))
            bid = _first_number(latest_quote.get("bp"), 0.0)
            ask = _first_number(latest_quote.get("ap"), 0.0)
            spread_pct = _spread_pct(bid, ask)
            as_of = str(
                latest_trade.get("t")
                or minute_bar.get("t")
                or datetime.now(timezone.utc).isoformat()
            )
            rows.append(
                SnapshotRow(
                    ticker=str(symbol).upper(),
                    company=str(symbol).upper(),
                    premarket_price=price,
                    previous_close=previous_close,
                    premarket_high=high,
                    premarket_low=low,
                    premarket_volume=volume,
                    float_shares=None,
                    market_cap=None,
                    spread_pct=spread_pct,
                    short_float_pct=None,
                    has_news=False,
                    current_halt=False,
                    recent_offering=False,
                    reverse_split_90d=False,
                    source="alpaca",
                    as_of_timestamp=as_of,
                    dollar_volume=round(price * volume, 2),
                    gap_pct=round(_gap_pct(price, previous_close), 2),
                    catalyst_headline="",
                )
            )
        return rows

    def get_minute_bars(
        self, symbols: Sequence[str], start: str, end: str, config: ScannerConfig
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            "/v2/stocks/bars",
            {
                "symbols": ",".join(symbols),
                "timeframe": "1Min",
                "start": start,
                "end": end,
                "feed": self.feed,
                "limit": "10000",
            },
            config,
        )
        bars = payload.get("bars", {})
        rows: list[dict[str, Any]] = []
        for symbol, symbol_bars in bars.items():
            for bar in symbol_bars:
                rows.append(
                    {
                        "ticker": str(symbol).upper(),
                        "timestamp": bar.get("t"),
                        "open": bar.get("o"),
                        "high": bar.get("h"),
                        "low": bar.get("l"),
                        "close": bar.get("c"),
                        "volume": bar.get("v"),
                    }
                )
        return rows

    def get_previous_close(self, symbols: Sequence[str], config: ScannerConfig) -> dict[str, float]:
        snapshots = self.get_premarket_snapshot(symbols, config)
        return {snapshot.ticker: snapshot.previous_close for snapshot in snapshots}

    def get_latest_quotes(
        self,
        symbols: Sequence[str],
        config: ScannerConfig,
    ) -> dict[str, dict[str, Any]]:
        """Return current bid/ask evidence for pre-entry liquidity checks."""

        clean = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
        if not clean:
            return {}
        payload = self._request_json(
            "/v2/stocks/quotes/latest",
            {"symbols": ",".join(clean), "feed": self.feed},
            config,
        )
        raw_quotes = payload.get("quotes") or {}
        if not isinstance(raw_quotes, dict):
            return {}
        output: dict[str, dict[str, Any]] = {}
        for symbol in clean:
            row = raw_quotes.get(symbol)
            if not isinstance(row, dict):
                continue
            bid = _first_number(row.get("bp"), 0.0)
            ask = _first_number(row.get("ap"), 0.0)
            if bid <= 0 or ask < bid:
                continue
            output[symbol] = {
                "ticker": symbol,
                "timestamp": str(row.get("t") or ""),
                "bid": bid,
                "ask": ask,
                "spread_pct": _spread_pct(bid, ask),
                "source": f"alpaca_market_data_{self.feed}",
            }
        return output

    def get_first_quote_after(
        self,
        symbol: str,
        *,
        start: str,
        end: str,
        config: ScannerConfig,
    ) -> dict[str, Any] | None:
        """Return the first sourced quote after an event; never use a later-day proxy."""

        payload = self._request_json(
            "/v2/stocks/quotes",
            {
                "symbols": symbol.upper(),
                "start": start,
                "end": end,
                "feed": self.feed,
                "limit": "50",
                "sort": "asc",
            },
            config,
        )
        rows = payload.get("quotes", {}).get(symbol.upper(), [])
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            bid = _first_number(row.get("bp"), 0.0)
            ask = _first_number(row.get("ap"), 0.0)
            if bid > 0 and ask >= bid:
                return {
                    "timestamp": str(row.get("t") or ""),
                    "bid": bid,
                    "ask": ask,
                    "spread_pct": _spread_pct(bid, ask),
                    "raw": row,
                }
        return None


def _first_number(*values: Any) -> float:
    for value in values:
        if value not in {None, ""}:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def _spread_pct(bid: float, ask: float) -> float:
    if bid <= 0 or ask <= 0 or ask < bid:
        return 0.0
    midpoint = (bid + ask) / 2
    if midpoint <= 0:
        return 0.0
    return round(((ask - bid) / midpoint) * 100, 4)


def _gap_pct(price: float, previous_close: float) -> float:
    if previous_close <= 0:
        return 0.0
    return ((price - previous_close) / previous_close) * 100
