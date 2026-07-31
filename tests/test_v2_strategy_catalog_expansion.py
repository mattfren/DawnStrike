"""Regression coverage for additive daily research strategies."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.paper_ops import engine as paper_ops_engine
from intraday_scanner.v2.strategies import build_strategy_catalog
from intraday_scanner.v2.strategies.catalog import (
    build_strategy_catalog as build_legacy_strategy_catalog,
)
from intraday_scanner.v2.strategy_identity import strategy_semantics_fingerprint

_LEGACY_FINGERPRINTS = {
    "ts_momentum_sma_atr": "30585b86085f588041cdebca394fc8fe42aed8daf73a18aceb6e57bbf31bb602",
    "donchian_breakout_20_10": "7e00b23b67ae059f30671b3b1086096fa83c90cea9755a617bcf7dadfc4912f0",
    "cross_sectional_relative_strength": (
        "5eaeb6846dac04479212a72c7f6a04a1e44e91b9f5dcffa4024c141ecbaf6fe0"
    ),
    "pullback_reclaim_uptrend": "e13691fa30994163372d106f8ccd7b253b1417ec25b2179099c6906622740a3e",
    "volatility_contraction_breakout": (
        "045e4abcd4f86379d469fde5a30126684e4fb4727efba42726233b353c7f69c1"
    ),
    "failed_breakout_reversal_short": (
        "24d94ee059695bb8e47b7bf721c52f63bbeaf85fd8840756009656df37b5cec8"
    ),
    "bullish_fvg_continuation": "8dadc36ae35f119159fa811ac53396ec63983bffd3b43577bce4f5b4a4813ed7",
}
_NEW_IDS = ("gap_up_continuation", "gap_up_continuation_atr")


def _bars(*, gap_direction: str, future_spike: bool = False) -> tuple[MarketBar, ...]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows: list[MarketBar] = []
    for index in range(100):
        close = 100.0 + index * 0.10
        rows.append(
            MarketBar(
                symbol="AAA",
                timestamp=start + timedelta(days=index),
                open=close - 0.10,
                high=close + 0.50,
                low=close - 0.50,
                close=close,
                volume=1_000,
            )
        )
    previous_close = rows[-1].close
    if gap_direction == "up":
        signal_bar = MarketBar(
            "AAA",
            start + timedelta(days=100),
            previous_close * 1.01,
            previous_close * 1.025,
            previous_close * 1.005,
            previous_close * 1.022,
            2_000,
        )
    elif gap_direction == "down":
        signal_bar = MarketBar(
            "AAA",
            start + timedelta(days=100),
            previous_close * 0.985,
            previous_close * 1.020,
            previous_close * 0.980,
            previous_close * 1.015,
            2_000,
        )
    else:
        raise ValueError(gap_direction)
    rows.append(signal_bar)
    if future_spike:
        rows.append(
            MarketBar(
                "AAA",
                start + timedelta(days=101),
                500.0,
                510.0,
                490.0,
                505.0,
                9_999_999,
            )
        )
    return tuple(rows)


def _dataset(bars: tuple[MarketBar, ...]) -> MarketDataset:
    return MarketDataset(
        dataset_id="catalog-expansion-fixture",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={"AAA": bars},
    )


def _strategy(strategy_id: str):  # type: ignore[no-untyped-def]
    return next(item for item in build_strategy_catalog() if item.strategy_id == strategy_id)


def test_combined_catalog_is_additive_and_keeps_comparators_last() -> None:
    legacy = build_legacy_strategy_catalog()
    combined = build_strategy_catalog()

    assert len(legacy) == 9
    assert len(combined) == 11
    assert tuple(item.strategy_id for item in combined[:7]) == tuple(
        item.strategy_id for item in legacy[:7]
    )
    assert tuple(item.strategy_id for item in combined[7:9]) == _NEW_IDS
    assert tuple(item.status for item in combined[-2:]) == ("benchmark", "baseline")
    assert len({(item.strategy_id, item.version) for item in combined}) == len(combined)


def test_legacy_strategy_fingerprints_are_byte_stable() -> None:
    actual = {
        item.strategy_id: strategy_semantics_fingerprint(item)
        for item in build_strategy_catalog()
        if item.strategy_id in _LEGACY_FINGERPRINTS
    }
    assert actual == _LEGACY_FINGERPRINTS


@pytest.mark.parametrize("strategy_id", _NEW_IDS)
def test_new_strategies_are_isolated_forward_unvalidated_specs(strategy_id: str) -> None:
    strategy = _strategy(strategy_id)

    assert strategy.version == "v1.0"
    assert strategy.status == "experimental"
    assert strategy.compatible_timeframe == "1d"
    assert strategy.required_data_fields == ("open", "high", "low", "close", "volume")
    assert strategy.validation_status in {
        "retained_snapshot_screened_forward_validation_required",
        "retained_snapshot_grid_selected_forward_validation_required",
    }
    assert strategy.generate_signal.__module__.endswith(strategy_id)
    module_source = inspect.getsource(inspect.getmodule(strategy.generate_signal))
    assert "broker" not in module_source.lower()
    assert "order placement" not in module_source.lower()


@pytest.mark.parametrize("strategy_id", _NEW_IDS)
def test_new_strategy_triggers_without_reading_future_bars(strategy_id: str) -> None:
    gap_direction = "up"
    strategy = _strategy(strategy_id)
    prefix = _bars(gap_direction=gap_direction)
    extended = _bars(gap_direction=gap_direction, future_spike=True)

    prefix_signal = strategy.signal(_dataset(prefix), "AAA", prefix, 100)
    extended_signal = strategy.signal(_dataset(extended), "AAA", extended, 100)

    assert prefix_signal is not None
    assert extended_signal == prefix_signal
    assert prefix_signal.direction == "long"
    assert prefix_signal.entry_reference == pytest.approx(prefix[100].close)
    assert prefix_signal.stop < prefix_signal.entry_reference
    assert prefix_signal.target is not None
    assert prefix_signal.target > prefix_signal.entry_reference
    assert prefix_signal.reward_per_unit == pytest.approx(2.0 * prefix_signal.risk_per_unit)


def test_gap_up_rejects_weak_volume() -> None:
    up = list(_bars(gap_direction="up"))
    up[-1] = MarketBar(**{**up[-1].__dict__, "volume": 999})
    for strategy_id in _NEW_IDS:
        assert _strategy(strategy_id).signal(
            _dataset(tuple(up)), "AAA", tuple(up), 100
        ) is None


def test_paper_ops_init_adds_new_accounts_without_mutating_legacy_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "paper_ops"
    monkeypatch.setattr(
        paper_ops_engine,
        "build_strategy_catalog",
        build_legacy_strategy_catalog,
    )
    paper_ops_engine.init(output_root=root)

    monkeypatch.setattr(paper_ops_engine, "build_strategy_catalog", build_strategy_catalog)
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    registry = paper_ops_engine._strategy_registry(paths)
    accounts_payload = paper_ops_engine.read_json(paths.state / "paper_accounts.json", {})
    accounts = {
        str(row["strategy_id"]): row
        for row in accounts_payload.get("accounts", [])
    }

    expected_ids = {
        item.strategy_id
        for item in build_strategy_catalog()
        if item.status not in {"baseline", "benchmark"}
    }
    assert {str(row["strategy_id"]) for row in registry} == expected_ids
    assert set(accounts) == expected_ids
    assert all(
        float(accounts[strategy_id]["current_equity"]) == 100_000.0
        for strategy_id in _NEW_IDS
    )
    for mode in ("replay", "demo"):
        payload = paper_ops_engine.read_json(
            paths.state / f"{mode}_paper_accounts.json",
            {},
        )
        assert {
            str(row["strategy_id"])
            for row in payload.get("accounts", [])
        } == expected_ids


def test_registration_coverage_begins_on_first_eligible_session() -> None:
    entry: dict[str, object] = {
        "registered_at": "2026-07-16T20:01:00+00:00",
    }
    paper_ops_engine._ensure_registration_coverage(entry, artifact="fixture")
    assert entry["coverage_inception_date"] == "2026-07-17"

    entry["coverage_inception_date"] = "2026-07-16"
    with pytest.raises(ValueError, match="conflicts with registered_at"):
        paper_ops_engine._ensure_registration_coverage(entry, artifact="fixture")
