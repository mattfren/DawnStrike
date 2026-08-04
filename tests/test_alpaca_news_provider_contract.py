from __future__ import annotations

from pathlib import Path

from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.news_provider import (
    AlpacaNewsProvider,
    _scenario_article_from_alpaca,
)


def _config(tmp_path: Path) -> ScannerConfig:
    return ScannerConfig(
        database_path=tmp_path / "scanner.sqlite",
        alpaca_api_key_id="test-key",
        alpaca_api_secret_key="test-secret",  # pragma: allowlist secret
        scenario_news_symbol_batch_size=2,
    )


def _raw(article_id: str, ticker: str, created_at: str) -> dict[str, object]:
    return {
        "id": article_id,
        "symbols": [ticker],
        "headline": article_id,
        "summary": "source summary",
        "content": "bounded source content",
        "source": "Reuters",
        "url": f"https://www.reuters.com/{article_id}",
        "created_at": created_at,
        "updated_at": created_at,
    }


def test_alpaca_news_batches_symbols_and_sorts_the_bounded_sample(tmp_path: Path) -> None:
    provider = AlpacaNewsProvider(_config(tmp_path))
    calls: list[dict[str, str]] = []

    def fake_request(params: dict[str, str]) -> dict[str, object]:
        calls.append(params)
        symbol = params["symbols"].split(",")[0]
        created = "2026-08-03T14:01:00Z" if symbol == "AAA" else "2026-08-03T14:00:00Z"
        return {"news": [_raw(f"article-{symbol}", symbol, created)]}

    provider._request_json = fake_request  # type: ignore[method-assign]
    articles = provider.get_articles(["ccc", "aaa", "bbb"], limit=2)

    assert [call["symbols"] for call in calls] == ["AAA,BBB", "CCC"]
    assert [article.article_id for article in articles] == ["article-CCC", "article-AAA"]
    assert all(article.provider_delay_seconds is not None for article in articles)


def test_alpaca_news_rejects_timezone_naive_provider_timestamps() -> None:
    assert _scenario_article_from_alpaca(
        _raw("naive", "NOVA", "2026-08-03T14:00:00"), historical=False
    ) is None


def test_alpaca_news_follows_page_token_within_the_bounded_collection_limit(
    tmp_path: Path,
) -> None:
    provider = AlpacaNewsProvider(_config(tmp_path))
    calls: list[dict[str, str]] = []

    def fake_request(params: dict[str, str]) -> dict[str, object]:
        calls.append(params)
        if params.get("page_token") == "page-two":
            return {"news": [_raw("two", "NOVA", "2026-08-03T14:01:00Z")]}
        return {
            "news": [_raw("one", "NOVA", "2026-08-03T14:00:00Z")],
            "next_page_token": "page-two",
        }

    provider._request_json = fake_request  # type: ignore[method-assign]
    articles = provider.get_articles(["nova"], limit=51)

    assert [call.get("page_token") for call in calls] == [None, "page-two"]
    assert [article.article_id for article in articles] == ["one", "two"]
