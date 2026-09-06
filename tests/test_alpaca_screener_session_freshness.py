"""The authenticated screener must be usable at the hour the product runs.

Alpaca republishes its screener lanes once per session, stamped after the
close. The provider held those lanes to the same 300-second window it uses for
an intraday price observation, so a premarket cycle - always reading the prior
session's stamp - failed every single day with
``STALE_MOVER_DISCOVERY_SOURCE``. Live evidence from 2026-09-03 premarket and
again on 2026-09-05:

    most_actives  last_updated=2026-09-04T23:59:00Z  age=103,731s (28.8h)
    movers        last_updated=2026-09-04T23:59:00Z  age=103,731s (28.8h)

With the only authenticated source failing, every candidate came from scraped
web tables carrying ``source_confidence`` 29.5-42 against an alert floor of 80
and ``source_quality_status='LIMITED'`` - two hard blocks the alert gate can
never clear. The pipeline could not produce a single alertable signal.

The screener lanes are *discovery*: they choose which symbols to look at. Every
price on the emitted row comes from ``get_premarket_snapshot``, fetched moments
earlier. So the lanes are bound to the session they must come from, and the row
keeps its own observation time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import SnapshotRow
from intraday_scanner.providers import alpaca_screener_provider
from intraday_scanner.providers.alpaca_screener_provider import AlpacaScreenerProvider

# Published calendar: Thu 2026-09-03 and Fri 2026-09-04 are sessions,
# Mon 2026-09-07 is Labor Day, Tue 2026-09-08 is the next session.
PRIOR_CLOSE_STAMP = "2026-09-03T23:59:00Z"
FRIDAY_CLOSE_STAMP = "2026-09-04T23:59:00Z"
FRIDAY_PREMARKET = "2026-09-04T12:35:00+00:00"
TUESDAY_PREMARKET = "2026-09-08T12:35:00+00:00"


class _FakeMarketData:
    """Screener lanes stamped at a session close; snapshot observed just now."""

    def __init__(self, lane_stamp: str, snapshot_stamp: str) -> None:
        self.lane_stamp = lane_stamp
        self.snapshot_stamp = snapshot_stamp

    def validate_credentials(self) -> None:
        return None

    def _request_json(self, path: str, params: Any, config: Any) -> dict[str, Any]:
        if path.endswith("most-actives"):
            return {
                "most_actives": [{"symbol": "NOVA"}],
                "last_updated": self.lane_stamp,
            }
        return {
            "gainers": [{"symbol": "NOVA"}],
            "losers": [],
            "last_updated": self.lane_stamp,
        }

    def get_premarket_snapshot(self, symbols: list[str], config: Any) -> list[SnapshotRow]:
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
                    "spread_pct": 0.5,
                    "float_shares": "",
                    "market_cap": "",
                    "short_float_pct": "",
                    "has_news": False,
                    "catalyst_headline": "",
                    "catalyst_url": "",
                    "current_halt": False,
                    "recent_offering": False,
                    "reverse_split_90d": False,
                    "source": "alpaca",
                    "as_of_timestamp": self.snapshot_stamp,
                }
            )
        ]


def _provider(monkeypatch, *, lane_stamp: str, now: str) -> AlpacaScreenerProvider:
    monkeypatch.setattr(alpaca_screener_provider, "utc_now_iso", lambda: now)
    provider = AlpacaScreenerProvider(
        ScannerConfig(
            alpaca_api_key_id="key",  # pragma: allowlist secret
            alpaca_api_secret_key="secret",  # pragma: allowlist secret
        )
    )
    provider.market_data = _FakeMarketData(lane_stamp, now)  # type: ignore[assignment]
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
            }
        ],
    )
    return provider


def test_premarket_collection_accepts_the_prior_session_discovery_stamp(monkeypatch):
    """The exact live failure: 12.6-hour-old lanes during a premarket cycle."""

    provider = _provider(monkeypatch, lane_stamp=PRIOR_CLOSE_STAMP, now=FRIDAY_PREMARKET)

    result = provider.collect()

    assert result["status"] == "success", result.get("failure_reason")
    assert [row["ticker"] for row in result["rows"]] == ["NOVA"]
    ages = result["source_timestamp_age_seconds"]
    assert min(ages.values()) > 12 * 60 * 60, (
        "the lanes must genuinely be hours old, or this test proves nothing"
    )
    assert result["source_timestamp_status"] == "SESSION_BOUND"


def test_collection_survives_a_long_holiday_weekend(monkeypatch):
    """Friday's close is the last completed session before Tuesday premarket.

    A fixed 24-hour age window would reject this; the calendar-derived floor
    accepts it because no session has closed in between.
    """

    provider = _provider(monkeypatch, lane_stamp=FRIDAY_CLOSE_STAMP, now=TUESDAY_PREMARKET)

    result = provider.collect()

    assert result["status"] == "success", result.get("failure_reason")
    ages = result["source_timestamp_age_seconds"]
    assert min(ages.values()) > 3 * 24 * 60 * 60, "expected a >3-day gap across Labor Day"


def test_lane_older_than_the_last_completed_close_is_still_refused(monkeypatch):
    """A genuinely stalled vendor feed must still fail closed."""

    provider = _provider(
        monkeypatch,
        lane_stamp="2026-09-02T23:59:00Z",  # Wednesday close - a session behind
        now=FRIDAY_PREMARKET,
    )

    with pytest.raises(DataProviderError, match="predate the last completed session close"):
        provider.collect()


def test_lane_stamped_ahead_of_the_snapshot_is_refused(monkeypatch):
    provider = _provider(
        monkeypatch,
        lane_stamp="2026-09-04T18:00:00Z",  # hours ahead of the 12:35Z reference
        now=FRIDAY_PREMARKET,
    )

    with pytest.raises(DataProviderError, match="stamped ahead of the requested snapshot"):
        provider.collect()


def test_row_carries_its_own_observation_time_not_the_discovery_stamp(monkeypatch):
    """``source_timestamp`` is what every downstream freshness check reads.

    Stamping it with the once-a-session discovery time made a quote fetched
    seconds ago look 12 hours old to ``scoring._is_stale`` and to the core
    universe freshness projection.
    """

    provider = _provider(monkeypatch, lane_stamp=PRIOR_CLOSE_STAMP, now=FRIDAY_PREMARKET)

    row = provider.collect()["rows"][0]

    assert row["source_timestamp"] != PRIOR_CLOSE_STAMP
    observed = datetime.fromisoformat(str(row["source_timestamp"]).replace("Z", "+00:00"))
    reference = datetime.fromisoformat(FRIDAY_PREMARKET.replace("Z", "+00:00"))
    assert abs((observed - reference).total_seconds()) < 1.0


def test_authenticated_rows_still_declare_verified_source_quality(monkeypatch):
    """Relaxing the discovery window must not weaken what the row claims."""

    provider = _provider(monkeypatch, lane_stamp=PRIOR_CLOSE_STAMP, now=FRIDAY_PREMARKET)

    row = provider.collect()["rows"][0]

    assert row["source_quality_status"] == "VERIFIED"
    assert row["data_source_kind"] == "alpaca_api"
    assert row["extraction_mode"] == "authenticated_api"
    # Halt and SEC evidence is still unverified here; the enrichment providers
    # own those fields and the alert gate still blocks until they are clear.
    assert row["halt_status"] == "UNKNOWN"
    assert row["sec_risk_status"] == "UNKNOWN"


def test_research_only_boundary_is_unchanged(monkeypatch):
    provider = _provider(monkeypatch, lane_stamp=PRIOR_CLOSE_STAMP, now=FRIDAY_PREMARKET)

    result = provider.collect()

    assert result["research_only"] is True
    assert result["broker_execution_enabled"] is False


@pytest.mark.parametrize(
    "stamp",
    ["", "not-a-timestamp", "2026-09-03T23:59:00"],
    ids=["missing", "invalid", "timezone-naive"],
)
def test_unusable_lane_timestamps_still_fail_closed(monkeypatch, stamp: str):
    provider = _provider(monkeypatch, lane_stamp=stamp, now=FRIDAY_PREMARKET)

    with pytest.raises(DataProviderError, match="STALE_MOVER_DISCOVERY_SOURCE"):
        provider.collect()
