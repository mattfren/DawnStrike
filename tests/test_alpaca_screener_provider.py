from datetime import datetime, timezone

import pytest

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import SnapshotRow
from intraday_scanner.providers import alpaca_screener_provider
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.providers.alpaca_screener_provider import AlpacaScreenerProvider


class _FakeMarketData:
    def validate_credentials(self):
        return None

    def _request_json(self, path, params, config):
        if path.endswith("most-actives"):
            return {
                "most_actives": [{"symbol": "NOVA"}, {"symbol": "SQQQ"}],
                "last_updated": "2026-08-06T12:55:00Z",
            }
        return {
            "gainers": [{"symbol": "NOVA"}],
            "losers": [],
            "last_updated": "2026-08-06T12:55:00Z",
        }

    def get_premarket_snapshot(self, symbols, config):
        assert symbols == ["NOVA"]
        return [
            SnapshotRow.from_mapping(
                {
                    "ticker": "NOVA",
                    "company": "Nova Common Stock",
                    "premarket_price": 10.0,
                    "previous_close": 9.0,
                    "premarket_high": 10.1,
                    "premarket_low": 9.7,
                    "premarket_volume": 200_000,
                    "dollar_volume": 2_000_000,
                    "gap_pct": 11.11,
                    "float_shares": "",
                    "market_cap": "",
                    "spread_pct": 0.5,
                    "short_float_pct": "",
                    "has_news": False,
                    "catalyst_headline": "",
                    "catalyst_url": "",
                    "current_halt": False,
                    "recent_offering": False,
                    "reverse_split_90d": False,
                    "source": "alpaca",
                    "as_of_timestamp": "2026-08-06T12:55:00Z",
                }
            )
        ]


def test_alpaca_screener_discovers_read_only_common_stock_universe(monkeypatch):
    provider = AlpacaScreenerProvider(
        ScannerConfig(
            alpaca_api_key_id="key",  # pragma: allowlist secret
            alpaca_api_secret_key="secret",  # pragma: allowlist secret
        )
    )
    provider.market_data = _FakeMarketData()  # type: ignore[assignment]
    monkeypatch.setattr(
        provider,
        "_active_assets",
        lambda: [
            {
                "id": "nova-id",
                "symbol": "NOVA",
                "name": "Nova Common Stock",
                "status": "active",
                "class": "us_equity",
                "exchange": "NASDAQ",
                "tradable": True,
            },
            {
                "id": "sqqq-id",
                "symbol": "SQQQ",
                "name": "ProShares UltraPro Short QQQ",
                "status": "active",
                "class": "us_equity",
                "exchange": "NASDAQ",
                "tradable": True,
            },
        ],
    )

    result = provider.collect()

    assert result["status"] == "success"
    assert [row["ticker"] for row in result["rows"]] == ["NOVA"]
    assert result["rejection_reason_counts"] == {"non_common_security_name": 1}
    assert result["research_only"] is True
    assert result["broker_execution_enabled"] is False


def test_alpaca_screener_rejects_historical_as_of_before_current_discovery(
    monkeypatch,
):
    provider = AlpacaScreenerProvider(
        ScannerConfig(
            alpaca_api_key_id="key",  # pragma: allowlist secret
            alpaca_api_secret_key="secret",  # pragma: allowlist secret
        )
    )
    provider.market_data = _FakeMarketData()  # type: ignore[assignment]
    monkeypatch.setattr(
        alpaca_screener_provider,
        "utc_now_iso",
        lambda: "2026-08-27T13:00:00+00:00",
    )

    with pytest.raises(DataProviderError, match="POINT_IN_TIME_MOVER_DISCOVERY_UNAVAILABLE"):
        provider.collect(
            observed_at=datetime(2026, 8, 26, 13, 0, tzinfo=timezone.utc)
        )


def test_alpaca_latest_quotes_expose_bid_ask_spread(monkeypatch):
    config = ScannerConfig(
        alpaca_api_key_id="key",  # pragma: allowlist secret
        alpaca_api_secret_key="secret",  # pragma: allowlist secret
    )
    provider = AlpacaProvider(config)
    monkeypatch.setattr(
        provider,
        "_request_json",
        lambda path, params, request_config: {
            "quotes": {
                "NOVA": {
                    "bp": 9.98,
                    "ap": 10.02,
                    "t": "2026-08-06T14:35:00Z",
                }
            }
        },
    )

    quotes = provider.get_latest_quotes(["NOVA"], config)

    assert quotes["NOVA"]["bid"] == 9.98
    assert quotes["NOVA"]["ask"] == 10.02
    assert quotes["NOVA"]["spread_pct"] == 0.4
