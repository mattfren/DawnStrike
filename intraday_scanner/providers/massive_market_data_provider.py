"""Read-only Massive (Polygon-compatible) historical market-data adapter."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.network_safety import open_allowlisted_url
from intraday_scanner.providers.base import IntradayPage


class MassiveMarketDataProvider:
    """Use only Massive's market-data endpoints; no trading API is present."""

    provider_name = "massive"
    base_url = "https://api.polygon.io"

    def __init__(self, config: ScannerConfig):
        self.api_key = config.massive_api_key or config.polygon_api_key
        self.feed = "massive_consolidated"

    def validate_credentials(self) -> None:
        if not self.api_key:
            raise DataProviderError(
                "Massive historical proof requires MASSIVE_API_KEY; "
                "POLYGON_API_KEY is accepted only as a compatibility alias."
            )

    def capability_probe(self, config: ScannerConfig) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "feed": self.feed,
            "credential_present": bool(self.api_key),
            "entitlement_status": "unknown_until_operator_plan_probe"
            if self.api_key
            else "BLOCKED_EXTERNAL_MARKET_DATA_ENTITLEMENT",
            "capabilities": {
                "bars": "read_only_endpoint",
                "trades": "read_only_endpoint",
                "quotes": "read_only_endpoint",
                "corporate_actions": "read_only_endpoint",
                "pagination": True,
            },
            "probe_network_performed": False,
            "request_timeout_seconds": config.request_timeout_seconds,
        }

    def get_bars_page(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        config: ScannerConfig,
        *,
        page_token: str | None = None,
    ) -> IntradayPage:
        symbol = _first_symbol(symbols)
        path = f"/v2/aggs/ticker/{urllib.parse.quote(symbol)}/range/1/minute/{start}/{end}"
        params = {
            "limit": str(config.historical_intraday_page_limit),
            "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token
        return self._page(path, params, config, "results", "bars")

    def get_trades_page(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        config: ScannerConfig,
        *,
        page_token: str | None = None,
    ) -> IntradayPage:
        symbol = _first_symbol(symbols)
        path = f"/v3/trades/{urllib.parse.quote(symbol)}"
        params = {"timestamp.gte": start, "timestamp.lte": end, "sort": "asc"}
        if page_token:
            params["page_token"] = page_token
        return self._page(path, params, config, "results", "trades")

    def get_quotes_page(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        config: ScannerConfig,
        *,
        page_token: str | None = None,
    ) -> IntradayPage:
        symbol = _first_symbol(symbols)
        path = f"/v3/quotes/{urllib.parse.quote(symbol)}"
        params = {"timestamp.gte": start, "timestamp.lte": end, "sort": "asc"}
        if page_token:
            params["page_token"] = page_token
        return self._page(path, params, config, "results", "quotes")

    def get_corporate_actions_page(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        config: ScannerConfig,
        *,
        page_token: str | None = None,
    ) -> IntradayPage:
        params = {"ticker": _first_symbol(symbols), "sort": "asc"}
        if page_token:
            params["page_token"] = page_token
        return self._page(
            "/v3/reference/corporate-actions",
            params,
            config,
            "results",
            "corporate_actions",
        )

    def _page(
        self,
        path: str,
        params: dict[str, str],
        config: ScannerConfig,
        payload_key: str,
        endpoint_kind: str,
    ) -> IntradayPage:
        payload = self._request_json(path, params, config)
        raw_items = payload.get(payload_key) or []
        items = tuple(dict(row) for row in raw_items if isinstance(row, dict))
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return IntradayPage(
            provider=self.provider_name,
            feed=self.feed,
            endpoint=endpoint_kind,
            items=items,
            next_page_token=str(payload.get("next_url") or payload.get("next_page_token") or "")
            or None,
            raw_payload_hash_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            request_id=str(payload.get("request_id") or ""),
        )

    def _request_json(
        self, path: str, params: dict[str, str], config: ScannerConfig
    ) -> dict[str, Any]:
        self.validate_credentials()
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{self.base_url}{path}?{query}",
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
            method="GET",
        )
        last_error: Exception | None = None
        for attempt in range(1, config.request_retries + 1):
            try:
                with open_allowlisted_url(
                    request,
                    timeout=config.request_timeout_seconds,
                    allowed_hosts=("api.polygon.io",),
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise DataProviderError("Massive response must be a JSON object")
                    return payload
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < config.request_retries:
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue
                last_error = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < config.request_retries:
                time.sleep(min(2 ** (attempt - 1), 8))
        raise DataProviderError(
            f"Massive market-data request failed after retries: {last_error}"
        ) from last_error


def _first_symbol(symbols: Sequence[str]) -> str:
    for symbol in symbols:
        clean = str(symbol).strip().upper()
        if clean:
            return clean
    raise DataProviderError("historical intraday requests require at least one symbol")


__all__ = ["MassiveMarketDataProvider"]
