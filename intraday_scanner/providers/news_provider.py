"""News provider abstractions and offline-safe defaults."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Sequence
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.providers.base import NewsItem, NewsProvider


class NullNewsProvider(NewsProvider):
    """Offline provider used when no news API is configured."""

    def validate_credentials(self) -> None:
        return None

    def get_news(self, symbols: Sequence[str], since: str | None = None) -> list[NewsItem]:
        return []


class MockNewsProvider(NewsProvider):
    def __init__(self, items: list[NewsItem] | None = None):
        self.items = items or []

    def validate_credentials(self) -> None:
        return None

    def get_news(self, symbols: Sequence[str], since: str | None = None) -> list[NewsItem]:
        wanted = {symbol.upper() for symbol in symbols}
        return [item for item in self.items if item.ticker.upper() in wanted]


class NewsAPIProvider(NewsProvider):
    endpoint = "https://newsapi.org/v2/everything"

    def __init__(self, config: ScannerConfig):
        self.api_key = config.news_api_key
        self.timeout = config.request_timeout_seconds

    def validate_credentials(self) -> None:
        if not self.api_key:
            raise DataProviderError(
                "Missing NEWS_API_KEY. Add it to your environment or .env file. "
                "No API key was logged."
            )

    def get_news(self, symbols: Sequence[str], since: str | None = None) -> list[NewsItem]:
        self.validate_credentials()
        rows: list[NewsItem] = []
        for symbol in symbols:
            rows.extend(self._request_symbol(symbol, since))
        return rows

    def _request_symbol(self, symbol: str, since: str | None) -> list[NewsItem]:
        params = {
            "q": symbol,
            "sortBy": "publishedAt",
            "language": "en",
            "pageSize": "10",
            "apiKey": self.api_key,
        }
        if since:
            params["from"] = since
        url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataProviderError(f"NewsAPI request failed for {symbol}: {exc}") from exc
        if payload.get("status") == "error":
            message = payload.get("message")
            raise DataProviderError(f"NewsAPI request failed for {symbol}: {message}")
        return [
            _news_item_from_newsapi(symbol, article)
            for article in payload.get("articles", [])
            if isinstance(article, dict)
        ]


class FinnhubNewsProvider(NewsProvider):
    endpoint = "https://finnhub.io/api/v1/company-news"

    def __init__(self, config: ScannerConfig):
        self.api_key = config.finnhub_api_key
        self.timeout = config.request_timeout_seconds

    def validate_credentials(self) -> None:
        if not self.api_key:
            raise DataProviderError(
                "Missing FINNHUB_API_KEY. Add it to your environment or .env file. "
                "No API key was logged."
            )

    def get_news(self, symbols: Sequence[str], since: str | None = None) -> list[NewsItem]:
        self.validate_credentials()
        rows: list[NewsItem] = []
        start = (since or "")[:10] or "2020-01-01"
        # Finnhub requires a to date; use a far-future bound for provider readiness.
        end = "2099-12-31"
        for symbol in symbols:
            params = {"symbol": symbol, "from": start, "to": end, "token": self.api_key}
            url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as response:  # noqa: S310
                    payload = json.loads(response.read().decode("utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DataProviderError(f"Finnhub news request failed for {symbol}: {exc}") from exc
            rows.extend(
                _news_item_from_finnhub(symbol, item)
                for item in payload
                if isinstance(item, dict)
            )
        return rows


def build_news_provider(config: ScannerConfig) -> NewsProvider:
    if config.news_api_key:
        return NewsAPIProvider(config)
    if config.finnhub_api_key:
        return FinnhubNewsProvider(config)
    return NullNewsProvider()


def headline_has_dilution_risk(headline: str) -> bool:
    normalized = headline.lower()
    risk_terms = ("offering", "atm", "shelf", "warrant", "dilution", "registered direct")
    return any(term in normalized for term in risk_terms)


def _news_item_from_newsapi(symbol: str, article: dict[str, Any]) -> NewsItem:
    source = article.get("source") or {}
    return NewsItem(
        ticker=symbol.upper(),
        headline=str(article.get("title") or ""),
        published_at=str(article.get("publishedAt") or ""),
        source=str(source.get("name") or "newsapi"),
        url=str(article.get("url") or ""),
        summary=str(article.get("description") or ""),
    )


def _news_item_from_finnhub(symbol: str, item: dict[str, Any]) -> NewsItem:
    return NewsItem(
        ticker=symbol.upper(),
        headline=str(item.get("headline") or ""),
        published_at=str(item.get("datetime") or ""),
        source=str(item.get("source") or "finnhub"),
        url=str(item.get("url") or ""),
        summary=str(item.get("summary") or ""),
    )
