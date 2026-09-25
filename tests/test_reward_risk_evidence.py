"""Regression tests for the reward:risk evidence regression.

The volatility-aware stop fixed a real defect (a 93% gapper produced a 35.4%
stop) but introduced a second one: because the stop and the first target are
both derived from the SAME observed premarket range, the reward:risk ratio
became arithmetic rather than evidence, and the 1.50R floor stopped binding.

Measured over 5,605 real premarket geometries drawn from 537 sessions of SIP
minute bars:

    stop basis          max_distance_cap 3024 | premarket_range_fraction 2409
                        min_distance_floor 172
    R:R now             median 3.459  p5 3.093  stdev 1.964
    R:R band-bound      min 3.070  max 3.197  stdev 0.0296   <- pinned
    R:R legacy stop     median 1.506  stdev 0.120
    below the 1.50R floor:  now 13/5605 (0.23%)   legacy 2664/5605 (47.53%)

These tests pin that behaviour so it cannot regress silently again.
"""

from __future__ import annotations

import dataclasses

import pytest

from intraday_scanner.config import ScannerConfig
from intraday_scanner.scoring import (
    FIRST_TARGET_RANGE_EXTENSION,
    REWARD_RISK_BASIS_PINNED,
    REWARD_RISK_BASIS_STRUCTURE,
    REWARD_RISK_BASIS_VOLATILITY,
    _reward_risk_evidence,
    _volatility_aware_stop,
)


@pytest.fixture
def config() -> ScannerConfig:
    return ScannerConfig()


def _plan(high: float, low: float, config: ScannerConfig):
    """Build the same entry/stop/target the scorer builds."""

    entry = high * 1.005
    observed_range = max(high - low, 0.0)
    stop, basis = _volatility_aware_stop(
        entry=entry, premarket_low=low, observed_range=observed_range, config=config
    )
    target = high + observed_range * FIRST_TARGET_RANGE_EXTENSION
    return entry, stop, target, basis


# --------------------------------------------------------------------------
# the regression itself
# --------------------------------------------------------------------------


def test_band_bound_reward_risk_is_pinned_and_labelled_as_such(config):
    """A range-fraction stop yields a ratio that is arithmetic, not evidence."""

    entry, stop, target, basis = _plan(10.00, 9.00, config)
    assert basis["stop_basis_kind"] == "premarket_range_fraction"

    evidence = _reward_risk_evidence(
        entry=entry, stop=stop, target=target, stop_basis_kind=basis["stop_basis_kind"]
    )
    assert evidence["reward_risk_basis"] == REWARD_RISK_BASIS_PINNED
    assert evidence["reward_risk_is_evidence"] is False
    # The measured band across 5,605 real geometries.
    assert 3.05 <= evidence["reward_risk_ratio_computed"] <= 3.20


@pytest.mark.parametrize(
    "high,low",
    [
        (10.0, 9.2),  # an 8% premarket range
        (10.0, 9.0),  # 10%
        (10.0, 8.8),  # 12%
        (10.0, 8.0),  # 20%
        (48.0, 43.2),  # a different price decade entirely
        (3.4, 3.06),  # a sub-$5 name
    ],
)
def test_the_pinned_ratio_does_not_vary_with_the_setup(high, low, config):
    """Setups spanning $3 to $48 and 8%-20% ranges give the same ratio.

    This is the defect in one assertion: the number carries no information
    about which of these setups is better.
    """

    entry, stop, target, basis = _plan(high, low, config)
    assert basis["stop_basis_kind"] == "premarket_range_fraction"
    ratio = _reward_risk_evidence(
        entry=entry, stop=stop, target=target, stop_basis_kind=basis["stop_basis_kind"]
    )["reward_risk_ratio_computed"]
    assert ratio == pytest.approx(3.15, abs=0.07)


def test_structural_stop_still_carries_information(config):
    """When the premarket low is the tighter level the ratio is real evidence."""

    # A very narrow range: the structural low sits above the volatility band.
    entry, stop, target, basis = _plan(10.00, 9.98, config)
    assert basis["stop_basis_kind"] == "premarket_low_tighter_than_volatility_band"
    evidence = _reward_risk_evidence(
        entry=entry, stop=stop, target=target, stop_basis_kind=basis["stop_basis_kind"]
    )
    assert evidence["reward_risk_basis"] == REWARD_RISK_BASIS_STRUCTURE
    assert evidence["reward_risk_is_evidence"] is True


def test_capped_stop_is_labelled_a_volatility_proxy(config):
    """A wide gapper hits the 12% loss cap; the ratio then tracks volatility."""

    entry, stop, target, basis = _plan(10.00, 5.00, config)
    assert basis["stop_basis_kind"] == "max_distance_cap"
    evidence = _reward_risk_evidence(
        entry=entry, stop=stop, target=target, stop_basis_kind=basis["stop_basis_kind"]
    )
    assert evidence["reward_risk_basis"] == REWARD_RISK_BASIS_VOLATILITY
    assert evidence["reward_risk_is_evidence"] is False


# --------------------------------------------------------------------------
# a correctly-failing setup
# --------------------------------------------------------------------------


def test_a_capped_stop_sits_inside_the_observed_volatility(config):
    """The correctly-failing case: R:R passes 1.50R while the plan is unsound.

    A 50% premarket range wants a 25% stop; the 12% loss cap grants less than
    half of it. The stop is inside the noise the premarket just demonstrated,
    so it is expected to be tripped - yet reward:risk reads a healthy 6.4R.
    Nothing in the ratio reveals this, which is why coverage is reported.
    """

    entry, stop, target, basis = _plan(10.00, 5.00, config)
    ratio = (target - entry) / (entry - stop)

    assert ratio > 1.50  # the floor is satisfied ...
    assert basis["stop_inside_observed_volatility"] is True  # ... and it should not be
    assert basis["stop_volatility_coverage"] < 0.5
    assert basis["stop_distance_pct"] == pytest.approx(12.0, abs=0.01)


def test_a_healthy_plan_covers_the_volatility_it_observed(config):
    """The contrasting pass: the stop covers the full band the range asked for."""

    _, _, _, basis = _plan(10.00, 9.00, config)
    assert basis["stop_inside_observed_volatility"] is False
    assert basis["stop_volatility_coverage"] == pytest.approx(1.0, abs=0.01)


# --------------------------------------------------------------------------
# invariants the fix must not break
# --------------------------------------------------------------------------


def test_the_loss_cap_still_holds_for_the_gapper_that_motivated_it(config):
    """The 93% gapper that produced a 35.4% stop stays inside the 12% cap."""

    _, _, _, basis = _plan(19.30, 10.00, config)
    assert basis["stop_distance_pct"] <= config.max_stop_distance_pct + 1e-9


def test_the_floor_is_unchanged(config):
    """No threshold was lowered as part of this fix."""

    assert config.min_stop_distance_pct == 3.0
    assert config.max_stop_distance_pct == 12.0
    assert config.stop_range_fraction == 0.5


def test_coverage_is_absent_rather_than_wrong_without_a_range(config):
    _, basis = _volatility_aware_stop(
        entry=10.0, premarket_low=9.9, observed_range=0.0, config=config
    )
    assert basis["stop_volatility_coverage"] is None
    assert basis["stop_inside_observed_volatility"] is False


def test_evidence_flag_survives_a_config_with_a_different_fraction():
    """The label follows the basis, not a hardcoded ratio."""

    config = dataclasses.replace(ScannerConfig(), stop_range_fraction=0.25)
    # A wide enough range that a quarter of it still clears the 3% floor.
    entry, stop, target, basis = _plan(10.00, 7.00, config)
    assert basis["stop_basis_kind"] == "premarket_range_fraction"
    evidence = _reward_risk_evidence(
        entry=entry, stop=stop, target=target, stop_basis_kind=basis["stop_basis_kind"]
    )
    # A different fraction moves the pinned value but not its pinned-ness.
    assert evidence["reward_risk_is_evidence"] is False
    assert evidence["reward_risk_ratio_computed"] > 6.0
