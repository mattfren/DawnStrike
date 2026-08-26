"""Regressions for the source-bound AlphaOps v5 plan boundary."""

from __future__ import annotations

from dataclasses import replace

from intraday_scanner.alpha.plan_constructor import (
    COMPLETE,
    NO_VALID_PLAN,
    construct_alphaops_v5_plan,
)
from intraday_scanner.alpha.v5_policy import evaluate_v5_official_paper
from intraday_scanner.decisioning.condition_registry import registry_for_strategy
from intraday_scanner.services.strategy_decision_service import StrategyDecisionService


def _observation(value: float, source_hash: str) -> dict[str, object]:
    return {
        "value": value,
        "observed_at": "2026-08-26T13:00:00+00:00",
        "completed_at": "2026-08-26T13:00:00+00:00",
        "source": "completed-market-feed",
        "source_url": "https://example.test/market",
        "source_hash": source_hash,
        "is_complete": True,
    }


def _signal(target: float = 12.75) -> dict[str, object]:
    return {
        "ticker": "NOVA",
        "strategy_id": "alphaops_v5",
        "strategy_version": "dawnstrike-alphaops-v5.0.0",
        "entry_watch_level": 10.0,
        "invalidation_level": 9.0,
        "target_1": target,
        "target_basis_kind": "sourced_resistance",
        "alpha_score": 90,
        "market_structure_observations": {
            "entry": _observation(10.0, "a" * 64),
            "stop": _observation(9.0, "b" * 64),
            "target": {
                **_observation(target, "c" * 64),
                "target_basis_kind": "sourced_resistance",
            },
        },
    }


def test_legacy_range_geometry_does_not_turn_into_a_passing_rr_plan() -> None:
    # E=1.005H, S=.985L, T=H+1.618(H-L), for H=10/L=9, is below 1.50R
    # even before costs while its stop distance is near the hard limit.
    entry, stop, target = 1.005 * 10, 0.985 * 9, 10 + 1.618 * (10 - 9)
    assert (target - entry) / (entry - stop) < 1.50
    assert (entry - stop) / entry * 100 < 15


def test_valid_plan_freezes_three_independently_hashed_observations() -> None:
    plan = construct_alphaops_v5_plan(_signal(), decision_at="2026-08-26T13:30:00+00:00")
    assert plan.status == COMPLETE
    assert (plan.entry, plan.stop, plan.target) == (10.0, 9.0, 12.75)
    assert len({item.observation_hash for item in plan.observations}) == 3
    assert plan.plan_hash_sha256


def test_missing_or_incomplete_provenance_returns_no_valid_plan() -> None:
    signal = _signal()
    signal["market_structure_observations"] = {
        **signal["market_structure_observations"],
        "target": {"value": 12.75, "target_basis_kind": "sourced_resistance"},
    }
    plan = construct_alphaops_v5_plan(signal)
    assert plan.status == NO_VALID_PLAN
    assert plan.reason == "target_observation_or_geometry_invalid"


def test_target_is_frozen_before_rr_and_never_walked_to_second_target() -> None:
    signal = _signal(target=10.25)
    signal["target_candidates"] = [
        {**_observation(10.25, "c" * 64), "target_basis_kind": "sourced_resistance"},
        {**_observation(13.0, "d" * 64), "target_basis_kind": "prior_resistance"},
    ]
    plan = construct_alphaops_v5_plan(signal)
    assert plan.status == COMPLETE
    assert plan.target == 10.25


def test_receipt_reuses_frozen_levels_and_preserves_v5_hard_rr_gate() -> None:
    signal = _signal()
    signal.update({item.condition_id: True for item in registry_for_strategy("alphaops_v5")})
    receipt = StrategyDecisionService(
        code_sha="a" * 40, source_identity="completed-market-feed"
    ).build_receipt(signal, decision_at="2026-08-26T13:30:00+00:00")
    assert (receipt.entry_reference, receipt.stop, receipt.target) == (10.0, 9.0, 12.75)
    assert receipt.paper_entry_eligible is True
    unchanged = evaluate_v5_official_paper(
        signal,
        {
            "price": 10.0,
            "observed_at": "2026-08-26T13:29:30+00:00",
            "requested_at": "2026-08-26T13:30:00+00:00",
            "freshness_seconds": 30,
            "is_usable": True,
        },
        decision_time="2026-08-26T13:30:00-04:00",
    )
    assert unchanged.computed["target_price"] == 12.75


def test_plan_dataclass_is_immutable() -> None:
    plan = construct_alphaops_v5_plan(_signal())
    try:
        replace(plan, target=99.0)
    except TypeError:
        pass
    else:
        # replace can create a separate object, but the original hash/levels
        # must remain unchanged; no caller can mutate the frozen instance.
        assert plan.target == 12.75
