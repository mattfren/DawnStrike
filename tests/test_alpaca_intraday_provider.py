from __future__ import annotations

from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.alpaca_provider import AlpacaProvider


def test_alpaca_pages_preserve_requested_sip_feed_and_restart_cursor(monkeypatch) -> None:
    config = ScannerConfig(
        alpaca_api_key_id="id",
        alpaca_api_secret_key="secret",
        alpaca_data_feed="sip",
        historical_intraday_page_limit=1,
    )
    provider = AlpacaProvider(config)
    calls: list[dict[str, str]] = []

    def fake_request(path, params, config):
        calls.append(dict(params))
        if params.get("page_token"):
            return {"bars": {"TST": [{"t": "2026-08-07T13:31:00Z", "o": 2}]}}
        return {
            "bars": {"TST": [{"t": "2026-08-07T13:30:00Z", "o": 1}]},
            "next_page_token": "cursor-2",
        }

    monkeypatch.setattr(provider, "_request_json", fake_request)
    first = provider.get_bars_page(["TST"], "2026-08-07T13:30:00Z", "2026-08-07T14:00:00Z", config)
    second = provider.get_bars_page(
        ["TST"],
        "2026-08-07T13:30:00Z",
        "2026-08-07T14:00:00Z",
        config,
        page_token=first.next_page_token,
    )

    assert first.feed == second.feed == "sip"
    assert first.endpoint == second.endpoint == "bars"
    assert first.next_page_token == "cursor-2"
    assert second.next_page_token is None
    assert first.items[0]["symbol"] == "TST"
    assert calls[1]["page_token"] == "cursor-2"


def test_alpaca_minute_bars_walk_all_pages_without_changing_legacy_shape(monkeypatch) -> None:
    config = ScannerConfig(
        alpaca_api_key_id="id",
        alpaca_api_secret_key="secret",
        historical_intraday_max_pages=3,
    )
    provider = AlpacaProvider(config)
    responses = iter(
        [
            {
                "bars": {"TST": [{"t": "a", "o": 1, "h": 2, "l": 1, "c": 2, "v": 3}]},
                "next_page_token": "next",
            },
            {"bars": {"TST": [{"t": "b", "o": 2, "h": 3, "l": 2, "c": 3, "v": 4}]}},
        ]
    )
    monkeypatch.setattr(provider, "_request_json", lambda *args, **kwargs: next(responses))

    rows = provider.get_minute_bars(["TST"], "start", "end", config)

    assert [row["timestamp"] for row in rows] == ["a", "b"]
    assert rows[0]["ticker"] == "TST"


def test_alpaca_corporate_actions_use_current_market_data_endpoint(monkeypatch) -> None:
    config = ScannerConfig(
        alpaca_api_key_id="id",
        alpaca_api_secret_key="secret",  # pragma: allowlist secret - fixture
        alpaca_data_feed="sip",
        historical_intraday_page_limit=100,
    )
    provider = AlpacaProvider(config)
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_request(path, params, config):
        calls.append((path, dict(params)))
        return {
            "forward_splits": [{"id": "split-1", "symbol": "TST"}],
            "cash_dividends": [{"id": "dividend-1", "symbol": "TST"}],
            "next_page_token": "ca-next",
        }

    monkeypatch.setattr(provider, "_request_json", fake_request)
    page = provider.get_corporate_actions_page(
        ["TST"],
        "2026-08-28T13:30:00+00:00",
        "2026-08-28T14:00:00+00:00",
        config,
    )

    assert calls == [
        (
            "/v1/corporate-actions",
            {
                "symbols": "TST",
                "start": "2026-08-28",
                "end": "2026-08-28",
                "region": "us",
                "data_quality": "complete",
                "sort": "asc",
                "limit": "100",
            },
        )
    ]
    assert page.endpoint == "corporate_actions"
    assert page.feed == "sip"
    assert page.next_page_token == "ca-next"
    assert {row["corporate_action_collection"] for row in page.items} == {
        "forward_splits",
        "cash_dividends",
    }


def test_alpaca_corporate_actions_cap_provider_page_limit(monkeypatch) -> None:
    config = ScannerConfig(
        alpaca_api_key_id="id",
        alpaca_api_secret_key="secret",  # pragma: allowlist secret - fixture
        alpaca_data_feed="sip",
        historical_intraday_page_limit=10_000,
    )
    provider = AlpacaProvider(config)
    calls: list[dict[str, str]] = []

    def fake_request(path, params, config):
        calls.append(dict(params))
        return {}

    monkeypatch.setattr(provider, "_request_json", fake_request)
    provider.get_corporate_actions_page(
        ["TST"],
        "2026-08-28T13:30:00+00:00",
        "2026-08-28T14:00:00+00:00",
        config,
    )

    assert calls[0]["limit"] == "1000"
