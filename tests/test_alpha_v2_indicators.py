from __future__ import annotations

from datetime import datetime, timedelta, timezone

from intraday_scanner.v2.data import MarketBar
from intraday_scanner.v2.indicators.core import (
    prior_sma,
    rolling_zscore,
    session_vwap,
)

UTC = timezone.utc


def _bar(index: int, session: str, close: float, volume: int) -> MarketBar:
    return MarketBar(
        symbol="NOVA",
        timestamp=datetime(2026, 8, 3, 14, 0, tzinfo=UTC) + timedelta(minutes=index),
        open=close,
        high=close + 0.1,
        low=close - 0.1,
        close=close,
        volume=volume,
        exchange_session_id=session,
    )


def test_prior_sma_and_zscore_exclude_current_observation_from_window() -> None:
    assert prior_sma([1.0, 2.0, 3.0], 2) == [None, None, 1.5]
    scores = rolling_zscore([1.0, 1.0, 1.0, 3.0], 3)
    assert scores[:3] == [None, None, None]
    assert scores[3] == 0.0


def test_session_vwap_resets_on_session_identity() -> None:
    bars = (
        _bar(0, "XNYS:2026-08-03", 10.0, 100),
        _bar(1, "XNYS:2026-08-03", 12.0, 100),
        _bar(2, "XNYS:2026-08-04", 20.0, 100),
    )

    values = session_vwap(bars)

    assert values == [10.0, 11.0, 20.0]
