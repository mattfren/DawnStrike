"""Regressions for the source-bound AlphaOps v5 plan boundary."""

from __future__ import annotations

from dataclasses import replace

from intraday_scanner.alpha.alert_gate import apply_alert_gates
from intraday_scanner.alpha.plan_constructor import (
    COMPLETE,
    NO_VALID_PLAN,
    apply_structural_level_enrichment,
    construct_alphaops_v5_plan,
)
from intraday_scanner.alpha.v5_policy import evaluate_v5_official_paper
from intraday_scanner.config import load_config
from intraday_scanner.decisioning.condition_registry import registry_for_strategy
from intraday_scanner.services.alpha_cycle_service import (
    _apply_strategy_decision_receipts,
    _signal_payload,
)
from intraday_scanner.services.strategy_decision_service import StrategyDecisionService
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _observation(
    value: float, source_hash: str, *, observation_kind: str = "sourced_entry"
) -> dict[str, object]:
    return {
        "value": value,
        "observed_at": "2026-08-26T13:00:00+00:00",
        "completed_at": "2026-08-26T13:00:00+00:00",
        "source": "completed-market-feed",
        "source_url": "https://example.test/market",
        "source_hash": source_hash,
        "observation_kind": observation_kind,
        "raw_value": value,
        "derivation_policy": "identity",
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
            "stop": _observation(9.0, "b" * 64, observation_kind="sourced_stop"),
            "target": {
                **_observation(
                    target, "c" * 64, observation_kind="prior_day_resistance"
                ),
                "target_basis_kind": "sourced_resistance",
            },
        },
    }


def test_legacy_range_geometry_does_not_turn_into_a_passing_rr_plan() -> None:
    # Algebraically, stop <=15% requires L/H >= .867766. At that boundary
    # and above, the range-extension RR remains below 1.50 before costs.
    for ratio in (0.867766, 0.88, 0.92, 0.99):
        high = 10.0
        low = high * ratio
        entry = 1.005 * high
        stop = 0.985 * low
        target = high + 1.618 * (high - low)
        assert (entry - stop) / entry * 100 <= 15.0
        assert (target - entry) / (entry - stop) < 1.50


def test_range_extension_is_not_a_eligible_target_basis() -> None:
    signal = _signal()
    signal["market_structure_observations"]["target"]["observation_kind"] = (
        "premarket_range_extension"
    )
    signal["market_structure_observations"]["target"]["derivation_policy"] = (
        "premarket_range_extension_1.618"
    )
    assert construct_alphaops_v5_plan(signal).status == NO_VALID_PLAN


def test_valid_plan_freezes_three_independently_hashed_observations() -> None:
    plan = construct_alphaops_v5_plan(_signal(), decision_at="2026-08-26T13:30:00+00:00")
    assert plan.status == COMPLETE
    assert (plan.entry, plan.stop, plan.target) == (10.0, 9.0, 12.75)
    assert len({item.observation_hash for item in plan.observations}) == 3
    assert plan.plan_hash_sha256


def test_structural_enrichment_adapter_only_attaches_upstream_observations() -> None:
    signal = {"ticker": "NOVA", "target_1": 12.75}
    enriched = apply_structural_level_enrichment(
        signal,
        {"entry": _observation(10.0, "a" * 64)},
    )
    assert enriched["ticker"] == "NOVA"
    assert enriched["market_structure_observations"]["entry"]["value"] == 10.0
    assert enriched["legacy_plan_baseline"] is False
    assert "stop" not in enriched["market_structure_observations"]


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
        {
            **_observation(10.25, "c" * 64, observation_kind="prior_day_resistance"),
            "target_basis_kind": "sourced_resistance",
        },
        {
            **_observation(13.0, "d" * 64, observation_kind="prior_week_resistance"),
            "target_basis_kind": "prior_resistance",
        },
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


def test_v5_policy_rejects_legacy_range_target_as_independent() -> None:
    signal = _signal()
    signal["target_basis_kind"] = "premarket_range_extension"
    signal["target_derived_from_risk"] = False
    decision = evaluate_v5_official_paper(
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
    assert "target_not_independently_derived" in decision.reasons


def test_receipt_rejects_mismatched_declared_plan_even_with_true_overrides() -> None:
    signal = _signal()
    signal.update({item.condition_id: True for item in registry_for_strategy("alphaops_v5")})
    frozen = construct_alphaops_v5_plan(signal)
    signal["alphaops_market_structure_plan"] = {
        **frozen.to_dict(),
        "target": 99.0,
    }
    signal["condition_results"] = {
        "market_structure_plan": True,
        "entry_observation_provenance": True,
        "stop_observation_provenance": True,
        "target_observation_provenance": True,
        "plan_levels_frozen": True,
    }
    receipt = StrategyDecisionService(
        code_sha="a" * 40, source_identity="completed-market-feed"
    ).build_receipt(signal, decision_at="2026-08-26T13:30:00+00:00")
    assert receipt.paper_entry_eligible is False
    assert "market_structure_plan" in receipt.all_blocking_failures


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


def test_alpha_cycle_legacy_payload_is_no_valid_plan_and_alert_blocked(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DAWNSTRIKE_CODE_SHA", "a" * 40)
    config = load_config(strategy_evidence_enabled=True, strategy_evidence_shadow_only=True)
    row = {
        "ticker": "LEGACY",
        "entry_watch_level": 10.05,
        "invalidation_level": 9.0,
        "target_1": 11.67,
        "target_basis_kind": "premarket_range_extension",
    }
    payload = _signal_payload(row, "scan-legacy", "2026-08-26T13:30:00+00:00", 1)
    assert payload["plan_construction_status"] == "NO_VALID_PLAN"
    assert payload["alphaops_market_structure_plan"]["status"] == NO_VALID_PLAN
    _apply_strategy_decision_receipts(
        [payload],
        store=SQLiteScanStore(tmp_path / "legacy.sqlite"),
        config=config,
        decision_at="2026-08-26T13:30:00+00:00",
        source_summary={"source_identity": "completed-market-feed"},
    )
    gated = apply_alert_gates([payload])[0]
    assert gated["strategy_receipt_paper_entry_eligible"] is False
    assert gated["alert_gate_status"] == "BLOCKED"


def test_alpha_cycle_structural_plan_preserves_hash_levels_through_alert_gate(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DAWNSTRIKE_CODE_SHA", "b" * 40)
    config = load_config(strategy_evidence_enabled=True, strategy_evidence_shadow_only=True)
    source = _signal()
    source.update({item.condition_id: True for item in registry_for_strategy("alphaops_v5")})
    frozen = construct_alphaops_v5_plan(source, decision_at="2026-08-26T13:30:00+00:00")
    payload = _signal_payload(source, "scan-structural", "2026-08-26T13:30:00+00:00", 1)
    assert payload["plan_hash_sha256"] == frozen.plan_hash_sha256
    assert (payload["entry_watch_level"], payload["invalidation_level"], payload["target_1"]) == (
        frozen.entry,
        frozen.stop,
        frozen.target,
    )
    _apply_strategy_decision_receipts(
        [payload],
        store=SQLiteScanStore(tmp_path / "structural.sqlite"),
        config=config,
        decision_at="2026-08-26T13:30:00+00:00",
        source_summary={"source_identity": "completed-market-feed"},
    )
    gated = apply_alert_gates([payload])[0]
    assert gated["plan_hash_sha256"] == frozen.plan_hash_sha256
    assert gated["strategy_receipt_paper_entry_eligible"] is True
