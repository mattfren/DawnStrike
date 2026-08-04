"""News provider abstractions and offline-safe defaults."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.network_safety import open_allowlisted_url
from intraday_scanner.providers.base import NewsItem, NewsProvider
from intraday_scanner.scenario.contracts import ScenarioNewsArticle, canonical_hash, utc_now_iso


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


class AlpacaNewsProvider(NewsProvider):
    """Read-only Alpaca historical/current news transport with durable lineage."""

    endpoint = "https://data.alpaca.markets/v1beta1/news"

    def __init__(self, config: ScannerConfig):
        self.api_key = config.alpaca_api_key_id
        self.secret_key = config.alpaca_api_secret_key
        self.timeout = config.request_timeout_seconds
        self.retries = config.request_retries

    def validate_credentials(self) -> None:
        missing = []
        if not self.api_key:
            missing.append("ALPACA_API_KEY_ID")
        if not self.secret_key:
            missing.append("ALPACA_API_SECRET_KEY")
        if missing:
            raise DataProviderError(
                "Missing Alpaca news credential(s): "
                + ", ".join(missing)
                + ". No API keys were logged."
            )

    def get_news(self, symbols: Sequence[str], since: str | None = None) -> list[NewsItem]:
        rows = self.get_articles(symbols, since=since)
        items: list[NewsItem] = []
        wanted = {symbol.upper() for symbol in symbols}
        for article in rows:
            for ticker in article.symbols:
                if ticker in wanted:
                    items.append(
                        NewsItem(
                            ticker=ticker,
                            headline=article.headline,
                            published_at=article.created_at,
                            source=article.source or "alpaca",
                            url=article.source_url,
                            summary=article.summary,
                        )
                    )
        return items

    def get_articles(
        self,
        symbols: Sequence[str],
        *,
        since: str | None = None,
        until: str | None = None,
        historical: bool = False,
        limit: int = 50,
    ) -> list[ScenarioNewsArticle]:
        self.validate_credentials()
        normalized = sorted(
            {str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()}
        )
        if not normalized:
            return []
        token: str | None = None
        articles: list[ScenarioNewsArticle] = []
        seen: set[str] = set()
        while True:
            params = {
                "symbols": ",".join(normalized),
                "sort": "asc",
                "limit": str(min(max(limit, 1), 50)),
                "include_content": "true",
            }
            if since:
                params["start"] = since
            if until:
                params["end"] = until
            if token:
                params["page_token"] = token
            payload = self._request_json(params)
            for raw in payload.get("news", []):
                if not isinstance(raw, dict):
                    continue
                article = _scenario_article_from_alpaca(raw, historical=historical)
                if article is not None and article.article_id not in seen:
                    articles.append(article)
                    seen.add(article.article_id)
            token = str(payload.get("next_page_token") or "").strip() or None
            if not token or len(articles) >= limit:
                break
        return articles[:limit]

    def _request_json(self, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
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
        for attempt in range(1, self.retries + 1):
            try:
                with open_allowlisted_url(
                    request,
                    timeout=self.timeout,
                    allowed_hosts=("data.alpaca.markets",),
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise DataProviderError("Alpaca news returned an invalid JSON object.")
                    return payload
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise DataProviderError(
                        f"Alpaca news request failed with HTTP {exc.code}."
                    ) from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(min(2 ** (attempt - 1), 8))
        raise DataProviderError("Alpaca news request failed after bounded retries.") from last_error


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
            with open_allowlisted_url(
                url,
                timeout=self.timeout,
                allowed_hosts=("newsapi.org",),
            ) as response:
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
                with open_allowlisted_url(
                    url,
                    timeout=self.timeout,
                    allowed_hosts=("finnhub.io",),
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DataProviderError(f"Finnhub news request failed for {symbol}: {exc}") from exc
            rows.extend(
                _news_item_from_finnhub(symbol, item) for item in payload if isinstance(item, dict)
            )
        return rows


def build_news_provider(config: ScannerConfig) -> NewsProvider:
    if config.alpaca_api_key_id and config.alpaca_api_secret_key:
        return AlpacaNewsProvider(config)
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


def _scenario_article_from_alpaca(
    raw: dict[str, Any], *, historical: bool
) -> ScenarioNewsArticle | None:
    raw_symbols = raw.get("symbols") or []
    if not isinstance(raw_symbols, list):
        raw_symbols = []
    symbols = tuple(
        sorted({str(symbol).upper().strip() for symbol in raw_symbols if str(symbol).strip()})
    )
    created_at = str(raw.get("created_at") or raw.get("updated_at") or "")
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        # A fabricated "now" would make an unsafely timed record look forward
        # eligible. Keep this record out of the governed signal path instead.
        return None
    provider_id = str(raw.get("id") or "").strip()
    content = str(raw.get("content") or "")
    if not provider_id:
        provider_id = canonical_hash(
            {
                "symbols": symbols,
                "headline": raw.get("headline"),
                "created_at": created_at,
                "content": content,
            }
        )
    now = utc_now_iso()
    return ScenarioNewsArticle(
        article_id=provider_id,
        symbols=symbols,
        headline=str(raw.get("headline") or "").strip(),
        summary=str(raw.get("summary") or "").strip(),
        content=content,
        source=str(raw.get("source") or "alpaca_news").strip(),
        source_url=str(raw.get("url") or "").strip(),
        created_at=created_at,
        updated_at=str(raw.get("updated_at") or "").strip(),
        author=str(raw.get("author") or "").strip(),
        provider="alpaca",
        fetched_at=now,
        first_seen_at=now,
        timing_kind="provider_published_at_proxy" if historical else "forward_observed",
    )
