from __future__ import annotations

import inspect

import pytest

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.providers.massive_market_data_provider import MassiveMarketDataProvider


def test_massive_requires_key_without_logging_or_trading_surface() -> None:
    provider = MassiveMarketDataProvider(ScannerConfig())

    with pytest.raises(DataProviderError, match="MASSIVE_API_KEY"):
        provider.validate_credentials()
    assert "order" not in inspect.getsource(MassiveMarketDataProvider).lower()


def test_massive_fixture_page_is_read_only_and_content_hashed(monkeypatch) -> None:
    config = ScannerConfig(massive_api_key="test-key")
    provider = MassiveMarketDataProvider(config)
    monkeypatch.setattr(
        provider,
        "_request_json",
        lambda *args, **kwargs: {
            "results": [{"t": 1, "o": 10, "c": 11}],
            "next_page_token": "page-2",
        },
    )

    page = provider.get_bars_page(
        ["TST"], "2026-08-07T13:30:00Z", "2026-08-07T14:00:00Z", config
    )

    assert page.provider == "massive"
    assert page.feed == "massive_consolidated"
    assert page.next_page_token == "page-2"
    assert page.raw_payload_hash_sha256
