"""The prior-day-resistance target must never publish a target below the entry.

`_attach_authenticated_alpaca_structure` binds the AlphaOps v5 legs to completed
Alpaca observations and uses the prior session's high as ``target_1`` under the
basis ``prior_day_resistance``.  A prior-day high is only *resistance* while it
sits above the entry.  On a gap up - the entire premise of this product - the
premarket high has already cleared yesterday's high, so an unguarded assignment
publishes a first target below both the entry and the stop.

Three such rows existed in the live operational database:

    LIDR 2026-09-01  entry 1.8090  target_1 1.17
    GPRO 2026-09-01  entry 1.5879  target_1 0.88
    PPBT 2026-09-02  entry 3.0150  target_1 1.68

The geometry below reproduces the LIDR row exactly.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from intraday_scanner.services.alpha_cycle_service import (
    _attach_authenticated_alpaca_structure,
)

SOURCE = "alpaca_market_data_iex"
SOURCE_URL = "https://data.alpaca.markets/v2/stocks/bars"
TICKER = "LIDR"

# 2026-08-26 is a regular trading Wednesday; 12:55Z is 08:55 ET, inside the
# 04:00-09:29 ET premarket window the reconciler requires.
DECISION_AT = datetime(2026, 8, 26, 13, 0, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 8, 26, 12, 55, tzinfo=timezone.utc)
COMPLETED_AT = OBSERVED_AT + timedelta(minutes=1)
PRIOR_DAY = "2026-08-25"


def _authenticated_row(
    *, premarket_high: float, premarket_low: float, prior_high: float
) -> dict[str, Any]:
    """Build a fully source-authenticated AlphaOps v5 row.

    Every hash, reconciliation and session check in the production path is
    satisfied, so the only thing under test is the target geometry.
    """

    premarket_raw = {
        "ticker": TICKER,
        "feed": "iex",
        "requested_at": DECISION_AT.isoformat(),
        "bars": [
            {
                "ticker": TICKER,
                "timestamp": OBSERVED_AT.isoformat(),
                "high": premarket_high,
                "low": premarket_low,
                "close": premarket_low,
                "volume": 500_000,
            }
        ],
    }
    premarket_raw_json = json.dumps(premarket_raw, sort_keys=True, separators=(",", ":"))
    premarket_hash = hashlib.sha256(premarket_raw_json.encode()).hexdigest()

    prior_raw = {
        "ticker": TICKER,
        "timestamp": f"{PRIOR_DAY}T00:00:00+00:00",
        "high": prior_high,
        "bar": {"t": f"{PRIOR_DAY}T00:00:00Z", "h": prior_high},
    }
    prior_raw_json = json.dumps(prior_raw, sort_keys=True, separators=(",", ":"))
    prior_hash = hashlib.sha256(prior_raw_json.encode()).hexdigest()

    observation_payload = {
        "ticker": TICKER,
        "status": "verified",
        "premarket_high": premarket_high,
        "premarket_low": premarket_low,
        "observed_at": OBSERVED_AT.isoformat(),
        "bar_completed_at": COMPLETED_AT.isoformat(),
        "is_complete": True,
        "source": SOURCE,
        "source_url": SOURCE_URL,
    }
    observation_payload_json = json.dumps(
        observation_payload, sort_keys=True, separators=(",", ":")
    )

    return {
        "ticker": TICKER,
        "strategy_id": "alphaops_v5",
        "premarket_high": premarket_high,
        "premarket_low": premarket_low,
        "premarket_range_source": SOURCE,
        "premarket_range_source_url": SOURCE_URL,
        "premarket_raw_payload_json": premarket_raw_json,
        "premarket_source_hash_sha256": premarket_hash,
        "enrichment_primary_source": SOURCE,
        "enrichment_range_source": SOURCE,
        "enrichment_status": "verified",
        "enrichment_is_complete": True,
        "enrichment_was_fallback": False,
        "enrichment_observed_at": OBSERVED_AT.isoformat(),
        "enrichment_bar_completed_at": COMPLETED_AT.isoformat(),
        "enrichment_observation_sha256": hashlib.sha256(
            observation_payload_json.encode()
        ).hexdigest(),
        "enrichment_observation_payload_json": observation_payload_json,
        "prior_daily_high": prior_high,
        "prior_daily_high_observed_at": f"{PRIOR_DAY}T00:00:00+00:00",
        "prior_daily_high_completed_at": f"{PRIOR_DAY}T00:00:00+00:00",
        "prior_daily_high_completion_semantics": "availability_boundary",
        "prior_daily_high_source": SOURCE,
        "prior_daily_high_source_url": SOURCE_URL,
        "prior_daily_high_source_hash": prior_hash,
        "prior_daily_high_raw_payload_json": prior_raw_json,
    }


def test_prior_day_high_above_entry_is_bound_as_the_target() -> None:
    """Control: when yesterday's high is genuine overhead resistance, use it."""

    row = _authenticated_row(premarket_high=10.0, premarket_low=9.0, prior_high=12.75)

    result = _attach_authenticated_alpaca_structure(row, decision_at=DECISION_AT.isoformat())

    assert result["target_1"] == 12.75
    assert result["target_basis_kind"] == "prior_day_resistance"
    assert result["entry_watch_level"] == 10.0
    assert result["invalidation_level"] == 9.0
    assert result["target_1"] > result["entry_watch_level"] > result["invalidation_level"]


def test_gap_up_never_publishes_a_target_below_the_entry() -> None:
    """The live LIDR geometry: yesterday's high is already cleared premarket."""

    row = _authenticated_row(premarket_high=1.809, premarket_low=1.60, prior_high=1.17)

    result = _attach_authenticated_alpaca_structure(row, decision_at=DECISION_AT.isoformat())

    # The structure must be refused outright rather than published inverted, so
    # the plan constructor emits NO_VALID_PLAN exactly as it does for an absent
    # observation.
    assert "market_structure_observations" not in result
    assert result.get("target_1") != 1.17
    if result.get("target_1") is not None and result.get("entry_watch_level") is not None:
        assert result["target_1"] > result["entry_watch_level"]


def test_prior_day_high_equal_to_the_entry_is_not_resistance() -> None:
    """A target exactly at the entry carries zero reward and must be refused."""

    row = _authenticated_row(premarket_high=5.0, premarket_low=4.5, prior_high=5.0)

    result = _attach_authenticated_alpaca_structure(row, decision_at=DECISION_AT.isoformat())

    assert "market_structure_observations" not in result
