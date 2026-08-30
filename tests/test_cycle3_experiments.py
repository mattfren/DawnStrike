from __future__ import annotations

import pytest

from intraday_scanner.alpha import cycle3_experiments as cycle3
from intraday_scanner.v2.paper_ops.fleet_allocator import FleetCandidate
from intraday_scanner.v2.paper_ops.position_management import BorrowAvailability
from intraday_scanner.v2.strategies import Direction


def _hashes() -> cycle3.Cycle3EvidenceHashes:
    return cycle3.Cycle3EvidenceHashes(
        source_hash_sha256="a" * 64,
        config_hash_sha256="b" * 64,
        code_sha="c" * 40,
        window_hash_sha256="d" * 64,
        evidence_hash_sha256="e" * 64,
    )


def _candidate(name: str, *, short: bool = False) -> FleetCandidate:
    return FleetCandidate(
        candidate_id=name,
        strategy_id="strategy-" + name,
        strategy_version="v1",
        symbol=name,
        asset_type="stock",
        direction=Direction.SHORT if short else Direction.LONG,
        score=90.0,
        risk_amount=100.0,
        notional=1_000.0,
        correlation_group="technology",
        individual_account_decision="accepted",
        borrow=(
            BorrowAvailability(
                status="verified_available",
                located_at="2026-08-29T13:00:00Z",
                borrow_cost_bps_per_session=20.0,
                source_ref="borrow:test",
            )
            if short
            else None
        ),
    )


def _correlation(*names: str) -> dict[str, dict[str, object]]:
    return {
        name: {
            "candidate_id": name,
            "correlation_group": "technology",
            "correlation_value": 0.25,
            "status": "verified",
            "as_of": "2026-08-29T12:00:00Z",
            "decision_timestamp": "2026-08-29T13:00:00Z",
            "source_ref": "corr:test",
        }
        for name in names
    }


def test_paired_fleet_is_order_invariant_cash_retaining_and_non_mutating() -> None:
    candidates = [_candidate("AAA"), _candidate("BBB")]
    kwargs = {
        "market_date": "2026-08-29",
        "official_account_bytes": {"strategy-AAA": b"before"},
        "post_run_official_account_bytes": {"strategy-AAA": b"before"},
        "evidence": _hashes(),
        "correlation_truth_by_candidate": _correlation("AAA", "BBB"),
        "common_candidate_ids": ["AAA", "BBB"],
        "official_candidate_ids": ["AAA", "BBB"],
        "candidate_market_date_by_id": {"AAA": "2026-08-29", "BBB": "2026-08-29"},
        "starting_cash": 1_500.0,
        "window_start": "2026-08-29T13:00:00Z",
        "window_end": "2026-08-29T21:00:00Z",
    }
    first = cycle3.build_paired_counterfactual_shadow_receipt(
        candidates=candidates, **kwargs
    )
    second = cycle3.build_paired_counterfactual_shadow_receipt(
        candidates=list(reversed(candidates)), **kwargs
    )
    assert first["common_candidate_identity_hash_sha256"] == second[
        "common_candidate_identity_hash_sha256"
    ]
    assert first["cash"]["cash_retained"] == 500.0
    assert first["individual_strategy_accounts_mutated"] is False
    assert first["counterfactual_return_truth"] is None
    assert cycle3.validate_cycle3_receipt(first) is True
    tampered = {**first, "cash": {**first["cash"], "cash_retained": 0.0}}
    assert cycle3.validate_cycle3_receipt(tampered) is False


def test_paired_fleet_rejects_stale_correlation_and_account_tampering() -> None:
    candidate = _candidate("AAA")
    base = {
        "market_date": "2026-08-29",
        "candidates": [candidate],
        "official_account_bytes": {"account": b"before"},
        "post_run_official_account_bytes": {"account": b"before"},
        "evidence": _hashes(),
        "correlation_truth_by_candidate": {
            "AAA": {
                "status": "verified",
                "candidate_id": "AAA",
                "correlation_group": "technology",
                "correlation_value": 0.25,
                "as_of": "2026-08-28T13:00:00Z",
                "decision_timestamp": "2026-08-29T13:00:00Z",
                "source_ref": "corr:test",
            }
        },
        "window_start": "2026-08-29T13:00:00Z",
        "window_end": "2026-08-29T21:00:00Z",
        "common_candidate_ids": ["AAA"],
        "official_candidate_ids": ["AAA"],
        "candidate_market_date_by_id": {"AAA": "2026-08-29"},
    }
    with pytest.raises(ValueError, match="stale"):
        cycle3.build_paired_counterfactual_shadow_receipt(**base)
    with pytest.raises(ValueError, match="bytes changed"):
        cycle3.build_paired_counterfactual_shadow_receipt(
            **{
                **base,
                "correlation_truth_by_candidate": _correlation("AAA"),
                "post_run_official_account_bytes": {"account": b"changed"},
                "window_start": "2026-08-29T13:00:00Z",
                "window_end": "2026-08-29T21:00:00Z",
            }
        )


def test_fleet_policy_and_chicago_window_edges_fail_closed() -> None:
    candidate = _candidate("AAA")
    kwargs = {
        "market_date": "2026-08-29",
        "candidates": [candidate],
        "official_account_bytes": {"account": b"same"},
        "post_run_official_account_bytes": {"account": b"same"},
        "evidence": _hashes(),
        "correlation_truth_by_candidate": _correlation("AAA"),
        "common_candidate_ids": ["AAA"],
        "official_candidate_ids": ["AAA"],
        "candidate_market_date_by_id": {"AAA": "2026-08-29"},
        "window_start": "2026-08-30T00:30:00Z",  # 19:30 CT on Aug 29
        "window_end": "2026-08-30T02:00:00Z",  # 21:00 CT on Aug 29
    }
    receipt = cycle3.build_paired_counterfactual_shadow_receipt(**kwargs)
    assert receipt["frozen_window"]["market_date_timezone"] == "America/Chicago"
    zero_cash = cycle3.build_paired_counterfactual_shadow_receipt(
        **{**kwargs, "starting_cash": 0.0}
    )
    assert zero_cash["status"] == "NOT_EVALUABLE_NONPOSITIVE_CASH"
    assert zero_cash["allocation"]["selected"] == []
    assert zero_cash["allocation"]["diagnostics"]["selected_count"] == 0
    with pytest.raises(ValueError, match="policy flags"):
        cycle3.build_paired_counterfactual_shadow_receipt(
            **kwargs,
            policy=cycle3.FleetAllocatorPolicy(research_only=False),
        )
    with pytest.raises(ValueError, match="identity"):
        cycle3.build_paired_counterfactual_shadow_receipt(
            **{**kwargs, "common_candidate_ids": ["other"]},
        )


def test_strict_date_and_correlation_range() -> None:
    with pytest.raises(ValueError):
        cycle3._parse_date("2026-08-29 trailing")
    bad = _correlation("AAA")
    bad["AAA"]["correlation_value"] = 1.1
    with pytest.raises(ValueError, match="between"):
        cycle3.build_paired_counterfactual_shadow_receipt(
            **{
                "market_date": "2026-08-29",
                "candidates": [_candidate("AAA")],
                "official_account_bytes": {"account": b"same"},
                "post_run_official_account_bytes": {"account": b"same"},
                "evidence": _hashes(),
                "correlation_truth_by_candidate": bad,
                "common_candidate_ids": ["AAA"],
                "official_candidate_ids": ["AAA"],
                "candidate_market_date_by_id": {"AAA": "2026-08-29"},
                "starting_cash": 1_000.0,
                "window_start": "2026-08-29T13:00:00Z",
                "window_end": "2026-08-29T21:00:00Z",
            }
        )


def test_rejected_sampling_has_typed_reason_and_clustered_capped_ipw(monkeypatch) -> None:
    rows = cycle3.attach_typed_rejected_sampling(
        [
            {
                "action": "SHADOW_REJECTED_POLICY",
                "decision_id": "rejected-14",
                "market_date": "2026-08-29",
                "ticker": "CANDIDATE",
                "policy_rejection_reason": "not_ranked_by_frozen_v5_candidate_policy",
            }
        ],
        denominator=10,
        max_weight=5.0,
        config_hash_sha256="b" * 64,
        source_hash_sha256="a" * 64,
        code_sha="c" * 40,
        window_hash_sha256="d" * 64,
        evidence_hash_sha256="e" * 64,
        window_start="2026-08-29T13:00:00Z",
        window_end="2026-08-31T02:00:00Z",
    )
    sampled = rows[0]["rejected_attribution"]
    sampled["included"] = True
    sampled["inclusion_probability"] = 0.1
    rows[0]["market_date"] = "2026-08-29"
    rows[0]["rejected_attribution"]["authenticated_outcome"] = {
        "decision_id": "rejected-14",
        "candidate_id": "CANDIDATE",
        "market_date": "2026-08-29",
        "research_only": True,
        "broker_execution_enabled": False,
        "config_hash_sha256": "b" * 64,
        "source_hash_sha256": "a" * 64,
        "code_sha": "c" * 40,
        "window_hash_sha256": "d" * 64,
        "evidence_hash_sha256": "e" * 64,
        "sampling_policy_config_hash_sha256": cycle3.canonical_hash(
            {
                "denominator": 10,
                "max_weight": 5.0,
                "min_rows": 2,
                "min_market_sessions": 2,
                "window_start": "2026-08-29T13:00:00+00:00",
                "window_end": "2026-08-31T02:00:00+00:00",
            }
        ),
        "frozen_window": {
            "start": "2026-08-29T13:00:00+00:00",
            "end": "2026-08-31T02:00:00+00:00",
            "market_date_timezone": "America/Chicago",
        },
        "outcome_value": 2.0,
    }
    rows[0]["rejected_attribution"]["authenticated_outcome"][
        "return_payload_hash_sha256"
    ] = cycle3.canonical_hash(
        {
            key: value
            for key, value in rows[0]["rejected_attribution"]["authenticated_outcome"].items()
            if key != "return_payload_hash_sha256"
        }
    )
    sampled["sampling_receipt_hash_sha256"] = cycle3.canonical_hash(
        {key: value for key, value in sampled.items() if key != "sampling_receipt_hash_sha256"}
    )
    second = dict(rows[0])
    second["market_date"] = "2026-08-30"
    second["rejected_attribution"] = dict(rows[0]["rejected_attribution"])
    second["rejected_attribution"]["decision_id"] = "rejected-14"
    second["rejected_attribution"]["market_date"] = "2026-08-30"
    second["rejected_attribution"]["authenticated_outcome"] = dict(
        rows[0]["rejected_attribution"]["authenticated_outcome"]
    )
    second["rejected_attribution"]["authenticated_outcome"]["market_date"] = "2026-08-30"
    second["rejected_attribution"]["authenticated_outcome"]["outcome_value"] = 1.0
    second["rejected_attribution"]["authenticated_outcome"][
        "return_payload_hash_sha256"
    ] = cycle3.canonical_hash(
        {
            key: value
            for key, value in second["rejected_attribution"]["authenticated_outcome"].items()
            if key != "return_payload_hash_sha256"
        }
    )
    second["rejected_attribution"]["sampling_receipt_hash_sha256"] = cycle3.canonical_hash(
        {
            key: value
            for key, value in second["rejected_attribution"].items()
            if key != "sampling_receipt_hash_sha256"
        }
    )
    monkeypatch.setattr(
        cycle3.fill_truth,
        "has_authenticated_committed_fill_truth",
        lambda value: isinstance(value, dict) and value.get("outcome_value") in {1.0, 2.0},
    )
    result = cycle3.evaluate_rejected_candidate_attribution(
        [rows[0], second], evidence=_hashes(), max_weight=5.0
    )
    assert rows[0]["reason_code"] == "NOT_RANKED_BY_POLICY"
    assert result["status"] == "EVALUABLE_RESEARCH_ONLY"
    assert result["capped_weight_count"] == 2
    assert result["effective_sample_size"] == 2.0
    assert result["clustered_by_session"]["standard_error"] is not None
    reversed_result = cycle3.evaluate_rejected_candidate_attribution(
        [second, rows[0]], evidence=_hashes(), max_weight=5.0
    )
    assert result["input_attribution_set_hash_sha256"] == reversed_result[
        "input_attribution_set_hash_sha256"
    ]
    assert result == reversed_result
    assert cycle3.validate_cycle3_receipt(result) is True
    assert cycle3.validate_cycle3_receipt({**result, "estimand": 999.0}) is False


def test_rejected_attribution_missing_probability_or_outcome_is_null() -> None:
    result = cycle3.evaluate_rejected_candidate_attribution(
        [{"included": True, "market_date": "2026-08-29"}], evidence=_hashes()
    )
    assert result["estimand"] is None
    assert result["status"].startswith("NOT_EVALUABLE")
    assert result["missing_truth_is_zero"] is False
    assert cycle3.validate_cycle3_receipt(result) is True


def test_rejected_attribution_input_hash_includes_excluded_and_is_order_invariant() -> None:
    source_rows = [
        {
            "action": "SHADOW_REJECTED_POLICY",
            "decision_id": f"excluded-input-{index}",
            "market_date": "2026-08-29",
            "ticker": f"INPUT{index}",
            "policy_rejection_reason": "not_ranked_by_frozen_v5_candidate_policy",
        }
        for index in range(12)
    ]
    rows = cycle3.attach_typed_rejected_sampling(
        source_rows,
        denominator=10,
        config_hash_sha256="b" * 64,
        source_hash_sha256="a" * 64,
        code_sha="c" * 40,
        window_hash_sha256="d" * 64,
        evidence_hash_sha256="e" * 64,
        window_start="2026-08-29T13:00:00Z",
        window_end="2026-08-31T02:00:00Z",
    )
    assert any(
        row["rejected_attribution"]["included"] is True for row in rows
    )
    assert any(
        row["rejected_attribution"]["included"] is False for row in rows
    )
    result = cycle3.evaluate_rejected_candidate_attribution(rows, evidence=_hashes())
    expected = cycle3.canonical_hash(
        sorted(
            [row["rejected_attribution"] for row in rows],
            key=cycle3.canonical_hash,
        )
    )
    assert result["input_attribution_set_hash_sha256"] == expected
    reversed_result = cycle3.evaluate_rejected_candidate_attribution(
        list(reversed(rows)), evidence=_hashes()
    )
    assert result == reversed_result
    caller_mutated = [dict(row, outcome=999.0) for row in rows]
    caller_mutated_result = cycle3.evaluate_rejected_candidate_attribution(
        caller_mutated, evidence=_hashes()
    )
    assert (
        result["input_attribution_set_hash_sha256"]
        == caller_mutated_result["input_attribution_set_hash_sha256"]
    )


def test_rejected_attribution_ignores_caller_outcome_and_top_level_inclusion() -> None:
    result = cycle3.evaluate_rejected_candidate_attribution(
        [
            {
                "included": True,
                "outcome": 123.0,
                "rejected_attribution": {"included": False},
            }
        ],
        evidence=_hashes(),
    )
    assert result["estimand"] is None
    assert result["weighted_rows"] == 0


def test_scenario_prefilter_waits_for_trusted_fill_truth(monkeypatch) -> None:
    outcome = {
        "candidate_id": "scenario-1",
        "decision_id": "decision-1",
        "decision_at": "2026-08-29T13:00:00Z",
        "observation_policy": "scenario-prefilter-v1",
        "market_date": "2026-08-29",
        "evidence_hash_sha256": "e" * 64,
        "config_hash_sha256": "b" * 64,
        "source_hash_sha256": "a" * 64,
        "code_sha": "c" * 40,
        "window_hash_sha256": "d" * 64,
        "research_only": True,
        "broker_execution_enabled": False,
        "observation_policy_config_hash_sha256": cycle3.canonical_hash({"version": "v1"}),
        "open_to_close_return_pct": 99.0,
        "outcome_status": "COMPLETE_SOURCED",
        "opportunity_label": "positive",
        "outcome_value": 0.5,
    }
    outcome["return_payload_hash_sha256"] = cycle3.canonical_hash(outcome)
    receipt = cycle3.build_scenario_prefilter_observation_receipt(
        market_date="2026-08-29",
        observations=[
            {
                "candidate_id": "scenario-1",
                "decision_id": "decision-1",
                "decision_at": "2026-08-29T13:00:00Z",
                "observation_policy": "scenario-prefilter-v1",
                "prefilter_decision": "NOT_TRADE",
                "opportunity_label": "positive",
                "closed_paper_outcome": outcome,
            }
        ],
        evidence=_hashes(),
        scenario_config_hash_sha256="b" * 64,
        observation_policy_config={"version": "v1"},
        observation_policy_config_hash_sha256=cycle3.canonical_hash({"version": "v1"}),
    )
    assert receipt["false_negative_calibration_status"] == (
        "NOT_EVALUABLE_FILL_TRUTH_REQUIRED"
    )
    assert receipt["observations"][0]["false_negative_opportunity_label"] is None
    monkeypatch.setattr(
        cycle3.fill_truth,
        "has_authenticated_committed_fill_truth",
        lambda value: value is outcome,
    )
    authenticated = cycle3.build_scenario_prefilter_observation_receipt(
        market_date="2026-08-29",
        observations=[
            {
                "candidate_id": "scenario-1",
                "decision_id": "decision-1",
                "decision_at": "2026-08-29T13:00:00Z",
                "observation_policy": "scenario-prefilter-v1",
                "prefilter_decision": "NOT_TRADE",
                "closed_paper_outcome": outcome,
            }
        ],
        evidence=_hashes(),
        scenario_config_hash_sha256="b" * 64,
        observation_policy_config={"version": "v1"},
        observation_policy_config_hash_sha256=cycle3.canonical_hash({"version": "v1"}),
    )
    assert authenticated["false_negative_calibration_status"] == (
        "EVALUABLE_AFTER_TRUSTED_FILL_TRUTH"
    )
    assert authenticated["observations"][0]["return_pct"] is None
    assert authenticated["observations"][0]["official_pnl"] is None


def test_scenario_observation_requires_identity_and_policy() -> None:
    with pytest.raises(ValueError, match="frozen observation-policy"):
        cycle3.build_scenario_prefilter_observation_receipt(
            market_date="2026-08-29",
            observations=[{"candidate_id": "candidate-1"}],
            evidence=_hashes(),
            scenario_config_hash_sha256="b" * 64,
        )
