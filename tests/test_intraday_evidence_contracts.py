from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from intraday_scanner.v2.data import MarketBar
from intraday_scanner.v2.data_truth.intraday import (
    IntradayBar,
    IntradayCoverageReceipt,
    IntradayCoverageStatus,
    IntradaySourceMetadata,
    PriceAdjustmentBasis,
)

UTC = timezone.utc


def _source(
    *,
    request_start: datetime = datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
    request_end: datetime = datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
) -> IntradaySourceMetadata:
    return IntradaySourceMetadata(
        provider="fixture-provider",
        feed="historical-trades",
        entitlement="research",
        exchange_session_id="XNYS:2026-11-27:regular-early-close",
        request_start=request_start,
        request_end=request_end,
        fetched_at=datetime(2026, 11, 27, 18, 1, tzinfo=UTC),
        code_sha="code-sha",
        raw_artifact_hash_sha256="raw-sha",
        normalized_artifact_hash_sha256="normalized-sha",
        retention_status="retained",
    )


def _bar() -> IntradayBar:
    return IntradayBar(
        symbol="TST",
        exchange_session_id="XNYS:2026-11-27:regular-early-close",
        timestamp=datetime(2026, 11, 27, 17, 59, tzinfo=UTC),
        open_price=Decimal("10.00"),
        high_price=Decimal("10.50"),
        low_price=Decimal("9.90"),
        close_price=Decimal("10.40"),
        volume=1000,
        vwap=Decimal("10.20"),
        price_adjustment_basis=PriceAdjustmentBasis.UNADJUSTED,
        source_metadata=_source(),
    )


def test_intraday_contract_round_trip_preserves_utc_and_early_close_identity() -> None:
    bar = _bar()

    restored = IntradayBar.from_json(bar.to_json())

    assert restored == bar
    assert restored.timestamp.tzinfo == UTC
    assert restored.exchange_session_id.endswith("regular-early-close")
    assert restored.source_metadata.request_start.hour == 14


def test_intraday_contract_rejects_non_utc_timestamp() -> None:
    with pytest.raises(ValueError, match="must be UTC"):
        IntradayBar(
            **{
                **_bar().__dict__,
                "timestamp": datetime.fromisoformat("2026-11-27T12:59:00-05:00"),
            }
        )


def test_dst_transition_has_distinct_utc_instants() -> None:
    spring_before = datetime(2026, 3, 8, 6, 59, tzinfo=UTC)
    spring_after = datetime(2026, 3, 8, 7, 1, tzinfo=UTC)

    assert spring_before < spring_after
    assert _source(request_start=spring_before, request_end=spring_after).to_json()


def test_contract_is_frozen_and_serialization_is_deterministic() -> None:
    bar = _bar()

    with pytest.raises(FrozenInstanceError):
        bar.close_price = Decimal("11.00")  # type: ignore[misc]
    assert bar.to_json() == bar.to_json()


def test_coverage_statuses_are_source_data_statuses_only() -> None:
    receipt = IntradayCoverageReceipt(
        coverage_receipt_id="coverage-1",
        provider="fixture-provider",
        feed="historical-trades",
        entitlement="research",
        symbol="TST",
        market_date="2026-11-27",
        exchange_session_id="XNYS:2026-11-27:regular-early-close",
        request_start=_source().request_start,
        request_end=_source().request_end,
        status=IntradayCoverageStatus.KNOWN_HALT_GAPS,
        source_metadata=_source(),
        reason="halt interval is retained separately",
    )

    assert receipt.status.value == "KNOWN_HALT_GAPS"
    assert not any("strategy" in status.value.lower() for status in IntradayCoverageStatus)


def test_existing_daily_market_bar_constructor_remains_compatible() -> None:
    bar = MarketBar(
        symbol="TST",
        timestamp=datetime(2026, 1, 2, 21, 0, tzinfo=UTC),
        open=10.0,
        high=11.0,
        low=9.0,
        close=10.5,
        volume=100,
    )

    assert bar.vwap is None
    assert bar.price_adjustment_basis == "unknown"
