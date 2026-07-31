from datetime import datetime, timezone

from intraday_scanner.config import ScannerConfig
from intraday_scanner.services.premarket_enrichment_service import (
    enrich_premarket_rows,
    observation_from_chart_payload,
)

UTC = timezone.utc


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
    assert observation.premarket_high == 8.4
    assert observation.premarket_low == 7.7
    assert observation.previous_close == 5.0
    assert observation.bar_count == 2


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
    assert row["premarket_high"] == 8.4
    assert row["premarket_low"] == 7.7
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
