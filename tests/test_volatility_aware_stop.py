"""The stop must bound a single trade's loss without inventing a looser one.

Pinning the stop to the full premarket low produced a 35.4% stop on a real 93%
gapper - outside the alert gate's 15% policy and the mechanism behind the worst
recorded single loss. The stop is now a fraction of observed volatility, floored
against noise and capped so one trade's loss stays bounded.
"""

from __future__ import annotations

import pytest

from intraday_scanner.config import ConfigError, load_config
from intraday_scanner.scoring import _volatility_aware_stop


def _cfg(**overrides):
    return load_config().with_overrides(**overrides)


def _distance_pct(entry: float, stop: float) -> float:
    return (entry - stop) / entry * 100


def test_wide_range_gapper_is_capped_not_pinned_to_the_low() -> None:
    """The real BIAF geometry: a 93% gapper with a very wide premarket range."""

    cfg = _cfg()
    entry = 14.673
    # premarket low 9.618 -> the old rule produced a 35.4% stop.
    stop, basis = _volatility_aware_stop(
        entry=entry, premarket_low=9.618, observed_range=4.98, config=cfg
    )

    assert basis["stop_basis_kind"] == "max_distance_cap"
    assert _distance_pct(entry, stop) == pytest.approx(cfg.max_stop_distance_pct, abs=0.01)
    # Decisively tighter than the structural low it replaced.
    assert stop > 9.618 * 0.985


@pytest.mark.parametrize(
    ("entry", "low", "rng"),
    [
        (14.673, 9.618, 4.98),   # 93% gapper
        (1.81, 1.43, 0.38),      # small-cap
        (3.02, 1.86, 1.16),      # wide-range small-cap
        (100.0, 92.0, 8.0),      # higher-priced name
    ],
)
def test_stop_distance_always_within_policy_bounds(entry: float, low: float, rng: float) -> None:
    """No input may produce a stop outside the configured band."""

    cfg = _cfg()
    stop, _ = _volatility_aware_stop(
        entry=entry, premarket_low=low, observed_range=rng, config=cfg
    )

    assert 0 < stop < entry
    distance = _distance_pct(entry, stop)
    # The structural low can be tighter than the floor, which is allowed; the
    # cap is the loss-bounding guarantee and must never be exceeded.
    assert distance <= cfg.max_stop_distance_pct + 1e-6


def test_a_tighter_premarket_low_is_respected_over_a_looser_band() -> None:
    """Observed structure wins when it is tighter; never invent a wider stop."""

    cfg = _cfg()
    entry = 10.0
    # A 2% range would imply a 1% stop, but the low sits only 0.5% below entry.
    stop, basis = _volatility_aware_stop(
        entry=entry, premarket_low=10.0, observed_range=0.2, config=cfg
    )

    assert basis["stop_basis_kind"] == "premarket_low_tighter_than_volatility_band"
    assert stop == pytest.approx(9.85, abs=0.001)


def test_degenerate_range_falls_back_to_the_floor_not_a_zero_stop() -> None:
    """A high == low snapshot must not yield a zero-width (instant-stop) trade."""

    cfg = _cfg()
    entry = 5.0
    stop, basis = _volatility_aware_stop(
        entry=entry, premarket_low=0.0, observed_range=0.0, config=cfg
    )

    assert stop < entry
    assert _distance_pct(entry, stop) == pytest.approx(cfg.min_stop_distance_pct, abs=0.01)
    assert basis["stop_basis_kind"] == "min_distance_floor_no_range"


def test_tightening_the_stop_raises_reward_risk_against_a_fixed_target() -> None:
    """Loss shrinks and reward:risk grows - the target is untouched."""

    cfg = _cfg()
    entry, low, rng = 14.673, 9.618, 4.98
    target = 22.66  # structural range-extension target, unchanged by this policy

    old_stop = low * 0.985
    new_stop, _ = _volatility_aware_stop(
        entry=entry, premarket_low=low, observed_range=rng, config=cfg
    )

    old_rr = (target - entry) / (entry - old_stop)
    new_rr = (target - entry) / (entry - new_stop)

    assert _distance_pct(entry, new_stop) < _distance_pct(entry, old_stop) / 2
    assert new_rr > old_rr * 2
    assert new_rr >= 1.5


def test_stop_bounds_are_validated() -> None:
    with pytest.raises(ConfigError):
        _cfg(max_stop_distance_pct=1.0, min_stop_distance_pct=3.0)
    with pytest.raises(ConfigError):
        _cfg(stop_range_fraction=0.0)
    with pytest.raises(ConfigError):
        _cfg(stop_range_fraction=1.5)


def test_basis_records_provenance_for_review() -> None:
    cfg = _cfg()
    _, basis = _volatility_aware_stop(
        entry=14.673, premarket_low=9.618, observed_range=4.98, config=cfg
    )

    for key in (
        "stop_basis_kind",
        "stop_distance_pct",
        "stop_range_fraction",
        "stop_min_distance_pct",
        "stop_max_distance_pct",
        "stop_structural_low",
        "stop_policy_version",
    ):
        assert key in basis
