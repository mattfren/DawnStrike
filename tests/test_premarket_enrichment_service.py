from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.services.premarket_enrichment_service import (
    PremarketObservation,
    enrich_premarket_rows,
    observation_from_alpaca_bars,
    observation_from_chart_payload,
)

UTC = timezone.utc


def test_verified_premarket_observation_requires_completed_bar_to_be_usable():
    observation = PremarketObservation(
        ticker="NOVA",
        status="verified",
        premarket_high=8.2,
        premarket_low=7.8,
    )

    assert observation.is_usable is False


def _payload(*, timestamps, highs, lows, closes, previous_close=5.0):
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "chartPreviousClose": previous_close,
                        "exchangeTimezoneName": "America/New_York",
                        "currentTradingPeriod": {
                            "pre": {
                                "start": int(
                                    datetime(2026, 7, 13, 8, 0, tzinfo=UTC).timestamp()
                                ),
                                "end": int(
                                    datetime(2026, 7, 13, 13, 30, tzinfo=UTC).timestamp()
                                ),
                            }
                        },
                    },
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": closes,
                                "high": highs,
                                "low": lows,
                                "close": closes,
                                "volume": [1000 for _ in timestamps],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def _epoch(hour, minute):
    return int(datetime(2026, 7, 13, hour, minute, tzinfo=UTC).timestamp())


def _row(**overrides):
    row = {
        "ticker": "NOVA",
        "company": "Nova Research",
        "premarket_price": 8.0,
        "previous_close": "",
        "premarket_high": 8.0,
        "premarket_low": 8.0,
        "premarket_volume": 500_000,
        "dollar_volume": 4_000_000,
        "gap_pct": 60.0,
        "fixture_only": False,
        "manual_uploaded_data": False,
        "coverage_warning": (
            "previous_close_unavailable;premarket_range_unavailable_price_used;"
            "sec_risk_unverified"
        ),
    }
    row.update(overrides)
    return row


def test_chart_observation_is_point_in_time_and_excludes_future_bar():
    requested_at = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
    payload = _payload(
        timestamps=[_epoch(12, 55), _epoch(13, 0), _epoch(13, 10)],
        highs=[8.2, 8.4, 99.0],
        lows=[7.8, 7.7, 0.01],
        closes=[8.0, 8.1, 50.0],
    )

    observation = observation_from_chart_payload(
        "NOVA",
        payload,
        requested_at=requested_at,
        max_age_seconds=1200,
    )

    assert observation.status == "verified"
    assert observation.premarket_high == 8.2
    assert observation.premarket_low == 7.8
    assert observation.previous_close == 5.0
    assert observation.bar_count == 1
    assert observation.observed_at == "2026-07-13T12:55:00+00:00"
    assert observation.bar_completed_at == "2026-07-13T12:56:00+00:00"
    assert observation.is_complete is True
    assert observation.is_usable is True


def test_enrichment_replaces_only_missing_plan_facts_with_sourced_observation(tmp_path):
    payload = _payload(
        timestamps=[_epoch(12, 55), _epoch(13, 0)],
        highs=[8.2, 8.4],
        lows=[7.8, 7.7],
        closes=[8.0, 8.1],
    )

    result = enrich_premarket_rows(
        [_row()],
        config=ScannerConfig(
            premarket_enrichment_max_candidates=5,
            premarket_enrichment_max_age_seconds=1200,
        ),
        requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        fetcher=lambda _ticker, _config: payload,
        out_dir=tmp_path,
    )
    row = result["rows"][0]

    assert result["summary"]["status"] == "complete"
    assert row["premarket_high"] == 8.2
    assert row["premarket_low"] == 7.8
    assert row["previous_close"] == 5.0
    assert row["gap_pct"] == 60.0
    assert row["premarket_range_source"] == "yahoo_finance_chart"
    assert row["enrichment_status"] == "verified"
    assert "premarket_range_unavailable_price_used" not in row["coverage_warning"]
    assert "previous_close_unavailable" not in row["coverage_warning"]
    assert "sec_risk_unverified" in row["coverage_warning"]
    assert (tmp_path / "premarket_snapshot.csv").exists()
    assert (tmp_path / "observations.json").exists()


def test_stale_observation_keeps_missing_truth_ineligible():
    payload = _payload(
        timestamps=[_epoch(12, 0)],
        highs=[8.4],
        lows=[7.7],
        closes=[8.0],
    )

    result = enrich_premarket_rows(
        [_row()],
        config=ScannerConfig(premarket_enrichment_max_age_seconds=300),
        requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        fetcher=lambda _ticker, _config: payload,
    )
    row = result["rows"][0]

    assert result["summary"]["status"] == "unavailable"
    assert row["enrichment_status"] == "stale_observation"
    assert row["premarket_high"] == 8.0
    assert row["premarket_low"] == 8.0
    assert "premarket_range_unavailable_price_used" in row["coverage_warning"]


def test_fixture_rows_are_never_enriched_as_live_data():
    called = False

    def fetcher(_ticker, _config):
        nonlocal called
        called = True
        return {}

    result = enrich_premarket_rows(
        [_row(fixture_only=True)],
        config=ScannerConfig(),
        fetcher=fetcher,
    )

    assert called is False
    assert result["summary"]["status"] == "no_eligible_candidates"
    assert result["rows"][0]["enrichment_status"] == "not_selected"


def test_alpaca_enrichment_owns_price_range_volume_and_excludes_future_bar():
    requested_at = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
    bars = [
        {
            "ticker": "NOVA",
            "timestamp": "2026-07-13T12:55:00Z",
            "high": 8.2,
            "low": 7.8,
            "close": 8.0,
            "volume": 1000,
        },
        {
            "ticker": "NOVA",
            "timestamp": "2026-07-13T13:00:00Z",
            "high": 8.4,
            "low": 7.7,
            "close": 8.1,
            "volume": 1500,
        },
        {
            "ticker": "NOVA",
            "timestamp": "2026-07-13T13:10:00Z",
            "high": 99.0,
            "low": 0.01,
            "close": 50.0,
            "volume": 999999,
        },
    ]

    observation = observation_from_alpaca_bars(
        "NOVA",
        bars,
        previous_close=5.0,
        requested_at=requested_at,
        max_age_seconds=1200,
        feed="iex",
    )

    assert observation.status == "verified"
    assert observation.premarket_high == 8.2
    assert observation.premarket_low == 7.8
    assert observation.latest_price == 8.0
    assert observation.premarket_volume == 1000
    assert observation.observed_at == "2026-07-13T12:55:00+00:00"
    assert observation.bar_completed_at == "2026-07-13T12:56:00+00:00"
    assert observation.is_complete is True
    assert observation.is_usable is True
    assert observation.source == "alpaca_market_data_iex"


def test_alphaops_alpaca_enrichment_replaces_public_page_price_facts():
    requested_at = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)

    class FakeAlpacaProvider:
        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            assert symbols == ["NOVA"]
            assert config.alpaca_data_feed == "iex"
            return [
                {
                    "ticker": "NOVA",
                    "timestamp": "2026-07-13T12:55:00Z",
                    "high": 8.2,
                    "low": 7.8,
                    "close": 8.0,
                    "volume": 1000,
                },
                {
                    "ticker": "NOVA",
                    "timestamp": "2026-07-13T13:00:00Z",
                    "high": 8.4,
                    "low": 7.7,
                    "close": 8.1,
                    "volume": 1500,
                },
            ]

        def get_premarket_snapshot(self, symbols, config):
            return [SimpleNamespace(ticker="NOVA", previous_close=5.0)]

    result = enrich_premarket_rows(
        [_row(premarket_price=9.0, premarket_volume=1)],
        config=ScannerConfig(
            alpaca_data_feed="iex",
            premarket_enrichment_max_candidates=5,
            premarket_enrichment_max_age_seconds=1200,
        ),
        requested_at=requested_at,
        source="alpaca",
        alpaca_provider=FakeAlpacaProvider(),
    )
    row = result["rows"][0]

    assert result["summary"]["source"] == "alpaca_market_data_iex"
    assert row["premarket_price"] == 8.0
    assert row["premarket_high"] == 8.2
    assert row["premarket_low"] == 7.8
    assert row["previous_close"] == 5.0
    assert row["premarket_volume"] == 1000
    assert row["dollar_volume"] == 8000.0
    assert row["gap_pct"] == 60.0
    assert row["premarket_range_source"] == "alpaca_market_data_iex"
    assert row["enrichment_is_complete"] is True
    assert row["enrichment_was_fallback"] is False


def test_alpaca_primary_uses_explicit_provenance_labeled_yahoo_fallback():
    requested_at = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
    yahoo_payload = _payload(
        timestamps=[_epoch(12, 55), _epoch(13, 0)],
        highs=[8.2, 8.4],
        lows=[7.8, 7.7],
        closes=[8.0, 8.1],
    )

    class PartialAlpacaProvider:
        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            return [
                {
                    "ticker": ticker,
                    "timestamp": "2026-07-13T12:55:00Z",
                    "high": 8.2,
                    "low": 7.8,
                    "close": 8.0,
                    "volume": 1000,
                }
                for ticker in symbols
                if ticker != "NOVA"
            ]

        def get_premarket_snapshot(self, symbols, config):
            return [SimpleNamespace(ticker=ticker, previous_close=5.0) for ticker in symbols]

    fallback_calls = []

    def fetcher(ticker, _config):
        fallback_calls.append(ticker)
        return yahoo_payload

    result = enrich_premarket_rows(
        [
            _row(ticker="NOVA"),
            _row(ticker="ALFA"),
            _row(ticker="BETA"),
            _row(ticker="GAMM"),
        ],
        config=ScannerConfig(
            alpaca_data_feed="iex",
            premarket_enrichment_max_candidates=5,
            premarket_enrichment_max_age_seconds=1200,
        ),
        requested_at=requested_at,
        source="alpaca",
        alpaca_provider=PartialAlpacaProvider(),
        allow_yahoo_fallback=True,
        fetcher=fetcher,
    )
    rows = {row["ticker"]: row for row in result["rows"]}
    row = rows["NOVA"]

    assert fallback_calls == ["NOVA"]
    assert result["summary"]["source"] == "alpaca_market_data_iex"
    assert result["summary"]["secondary_source"] == "yahoo_finance_chart"
    assert result["summary"]["secondary_fallback_count"] == 1
    assert result["summary"]["secondary_fallback_ratio"] == 0.25
    assert result["summary"]["secondary_fallback_candidate_count"] == 1
    assert result["summary"]["secondary_fallback_candidate_ratio"] == 0.25
    assert result["summary"]["secondary_fallback_ceiling_ratio"] == 0.25
    assert result["summary"]["secondary_fallback_status"] == "applied"
    assert result["summary"]["verified_by_source"] == {
        "alpaca_market_data_iex": 3,
        "yahoo_finance_chart": 1,
    }
    assert row["premarket_range_source"] == "yahoo_finance_chart"
    assert row["enrichment_status"] == "verified"
    assert row["enrichment_primary_source"] == "alpaca_market_data_iex"
    assert row["enrichment_fallback_status"] == "applied"
    assert row["enrichment_fallback_source"] == "yahoo_finance_chart"
    assert row["enrichment_was_fallback"] is True
    assert rows["ALFA"]["enrichment_fallback_status"] == "not_needed"


def test_alpaca_100_percent_symbol_fallback_aborts_without_calling_yahoo():
    class EmptyAlpacaProvider:
        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            return []

        def get_premarket_snapshot(self, symbols, config):
            return [SimpleNamespace(ticker=ticker, previous_close=5.0) for ticker in symbols]

    yahoo_called = False

    def fetcher(_ticker, _config):
        nonlocal yahoo_called
        yahoo_called = True
        return {}

    with pytest.raises(DataProviderError, match="cycle was aborted"):
        enrich_premarket_rows(
            [_row()],
            config=ScannerConfig(
                alpaca_data_feed="iex",
                premarket_enrichment_max_candidates=5,
                premarket_enrichment_max_age_seconds=1200,
            ),
            requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
            source="alpaca",
            alpaca_provider=EmptyAlpacaProvider(),
            allow_yahoo_fallback=True,
            fetcher=fetcher,
        )

    assert yahoo_called is False


def test_alpaca_credential_failure_fails_closed_without_calling_yahoo():
    class CredentialFailureProvider:
        def validate_credentials(self):
            raise DataProviderError("Missing Alpaca credentials")

    yahoo_called = False

    def fetcher(_ticker, _config):
        nonlocal yahoo_called
        yahoo_called = True
        return {}

    with pytest.raises(DataProviderError, match="Systemic Alpaca premarket"):
        enrich_premarket_rows(
            [_row()],
            config=ScannerConfig(),
            requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
            source="alpaca",
            alpaca_provider=CredentialFailureProvider(),
            allow_yahoo_fallback=True,
            fetcher=fetcher,
        )

    assert yahoo_called is False


def test_alpaca_http_failure_fails_closed_without_calling_yahoo():
    class HttpFailureProvider:
        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            raise DataProviderError("Alpaca market data request failed with HTTP 503")

    yahoo_called = False

    def fetcher(_ticker, _config):
        nonlocal yahoo_called
        yahoo_called = True
        return {}

    with pytest.raises(DataProviderError, match="Systemic Alpaca premarket"):
        enrich_premarket_rows(
            [_row()],
            config=ScannerConfig(),
            requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
            source="alpaca",
            alpaca_provider=HttpFailureProvider(),
            allow_yahoo_fallback=True,
            fetcher=fetcher,
        )

    assert yahoo_called is False


def test_alpaca_network_failure_fails_closed_without_calling_yahoo():
    class NetworkFailureProvider:
        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            raise OSError("network unavailable")

    yahoo_called = False

    def fetcher(_ticker, _config):
        nonlocal yahoo_called
        yahoo_called = True
        return {}

    with pytest.raises(DataProviderError, match="Systemic Alpaca premarket"):
        enrich_premarket_rows(
            [_row()],
            config=ScannerConfig(),
            requested_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
            source="alpaca",
            alpaca_provider=NetworkFailureProvider(),
            allow_yahoo_fallback=True,
            fetcher=fetcher,
        )

    assert yahoo_called is False
