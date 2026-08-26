import json
from pathlib import Path

from intraday_scanner.performance.strategy_miss_attribution import (
    attribute_strategy_misses,
)
from intraday_scanner.services.daily_strategy_learning_service import (
    AttributionReportAnalyzer,
    run_daily_strategy_learning,
)
from intraday_scanner.v2.strategies import build_strategy_catalog


class FixtureAnalyzer:
    def analyze(self, strategy, context):
        assert context.market_date == "2026-08-20"
        return {
            "status": "ANALYZED",
            "evidence_contract": "fixture-miss-analysis-v1",
            "outcomes": [
                {"market_date": "2026-08-20", "status": "UNRESOLVED", "return_pct": 4.0},
                {"market_date": "2026-08-20", "status": "COMPLETE_SOURCED"},
                {"market_date": "2026-08-21", "status": "COMPLETE_SOURCED", "return_pct": 9.0},
            ],
            "misses": [{"market_date": "2026-08-20", "root_cause": "ranking_capacity"}],
            "proposals": [
                {
                    "hypothesis": f"one controlled change for {strategy.strategy_id}",
                    "controlled_change": {"field": "ranking_weight", "delta": 0.1},
                    "sample_size": 12,
                    "applied": True,
                }
            ],
        }


def test_daily_learning_is_catalog_complete_safe_and_idempotent(tmp_path: Path) -> None:
    first = run_daily_strategy_learning(
        market_date="2026-08-20",
        cutoff="2026-08-20T22:00:00+00:00",
        source_identity="fixture-source:2026-08-20",
        code_sha="fixture-code-sha",
        out_dir=tmp_path,
        analyzer=FixtureAnalyzer(),
    )
    receipt_path = Path(first["receipt_path"])
    proposal_path = Path(first["proposals_path"])
    receipt_bytes = receipt_path.read_bytes()
    proposals = json.loads(proposal_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert first["status"] == "complete"
    assert first["idempotent_reused"] is False
    assert first["strategy_count"] == len(build_strategy_catalog())
    assert receipt["strategy_count"] == len(build_strategy_catalog())
    assert receipt["daily_fit_performed"] is False
    assert receipt["challenger_evaluation_performed"] is False
    assert receipt["champion_mutated"] is False
    assert receipt["research_only"] is True
    assert receipt["automatic_policy_change"] is False
    assert receipt["automatic_promotion"] is False
    assert receipt["broker_execution_enabled"] is False
    assert receipt["missing_outcomes_are_zero"] is False
    assert receipt["same_day_unresolved_excluded"] is True
    assert all(
        item["strategy_id"] and item["strategy_version"] for item in receipt["catalog"]
    )
    assert all(item["applied"] is False for item in proposals["proposals"])
    assert all(item["status"] == "PROPOSED_NOT_APPLIED" for item in proposals["proposals"])
    assert all(item["research_only"] is True for item in proposals["proposals"])
    assert all(item["broker_execution_enabled"] is False for item in proposals["proposals"])

    retained = receipt["strategy_evidence"][0]["evidence"]
    assert retained["counts"]["unresolved_outcomes_excluded"] == 1
    assert retained["counts"]["future_evidence_excluded"] == 1
    assert retained["counts"]["outcomes_without_return_excluded_from_return_metrics"] == 1
    assert not any(
        outcome.get("return_pct") == 0
        for strategy in receipt["strategy_evidence"]
        for outcome in strategy["evidence"]["outcomes"]
    )

    second = run_daily_strategy_learning(
        market_date="2026-08-20",
        cutoff="2026-08-20T22:00:00+00:00",
        source_identity="fixture-source:2026-08-20",
        code_sha="fixture-code-sha",
        out_dir=tmp_path,
        analyzer=FixtureAnalyzer(),
    )
    assert second["run_id"] == first["run_id"]
    assert second["idempotent_reused"] is True
    assert receipt_path.read_bytes() == receipt_bytes


def test_daily_learning_retry_reuses_hash_valid_frozen_artifacts_without_reanalysis(
    tmp_path: Path,
) -> None:
    first = run_daily_strategy_learning(
        market_date="2026-08-20",
        cutoff="2026-08-20T22:00:00+00:00",
        source_identity="fixture-source:2026-08-20",
        code_sha="fixture-code-sha",
        out_dir=tmp_path,
        analyzer=FixtureAnalyzer(),
    )

    class RetryMustNotAnalyze:
        def analyze(self, strategy, context):
            raise AssertionError("a retry must reuse the frozen hash-valid artifact")

    second = run_daily_strategy_learning(
        market_date="2026-08-20",
        cutoff="2026-08-20T22:00:00+00:00",
        source_identity="fixture-source:2026-08-20",
        code_sha="fixture-code-sha",
        out_dir=tmp_path,
        analyzer=RetryMustNotAnalyze(),
    )

    assert second["run_id"] == first["run_id"]
    assert second["idempotent_reused"] is True


def test_daily_learning_retry_rejects_tampered_frozen_artifact(tmp_path: Path) -> None:
    first = run_daily_strategy_learning(
        market_date="2026-08-20",
        cutoff="2026-08-20T22:00:00+00:00",
        source_identity="fixture-source:2026-08-20",
        code_sha="fixture-code-sha",
        out_dir=tmp_path,
        analyzer=FixtureAnalyzer(),
    )
    receipt_path = Path(first["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["code_sha"] = "tampered"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    try:
        run_daily_strategy_learning(
            market_date="2026-08-20",
            cutoff="2026-08-20T22:00:00+00:00",
            source_identity="fixture-source:2026-08-20",
            code_sha="fixture-code-sha",
            out_dir=tmp_path,
            analyzer=FixtureAnalyzer(),
        )
    except ValueError as exc:
        assert "hash mismatch" in str(exc)
    else:
        raise AssertionError("a tampered immutable receipt was reused")


def test_daily_learning_rejects_unfrozen_inputs(tmp_path: Path) -> None:
    for kwargs in (
        {"market_date": "2026-08-20", "cutoff": "2026-08-20T22:00:00", "source_identity": "x"},
        {"market_date": "2026-08-20", "cutoff": "2026-08-20T22:00:00+00:00", "source_identity": ""},
    ):
        try:
            run_daily_strategy_learning(
                **kwargs,
                code_sha="sha",
                out_dir=tmp_path,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("unfrozen daily-learning input was accepted")


def test_attribution_adapter_keeps_only_closed_rows_as_outcomes(tmp_path: Path) -> None:
    rows = [
        {
            "record_id": "benchmark",
            "market_date": "2026-08-20",
            "cohort": "shadow_challenger",
            "strategy_id": "benchmark_buy_hold_equal_weight",
            "strategy_version": "v1.0",
            "record_status": "realized",
            "return_pct": 1.0,
            "open_position_count": 0,
        },
        {
            "record_id": "no-trade",
            "market_date": "2026-08-20",
            "cohort": "shadow_challenger",
            "strategy_id": "ts_momentum_sma_atr",
            "strategy_version": "v1.0",
            "record_status": "no_trade",
            "return_pct": 0.0,
            "open_position_count": 0,
        },
        {
            "record_id": "closed-loss",
            "market_date": "2026-08-20",
            "cohort": "shadow_challenger",
            "strategy_id": "ts_momentum_sma_atr",
            "strategy_version": "v1.0",
            "record_status": "realized",
            "return_pct": -0.5,
            "open_position_count": 0,
        },
    ]
    report = attribute_strategy_misses(rows)
    result = run_daily_strategy_learning(
        market_date="2026-08-20",
        cutoff="2026-08-20T22:00:00+00:00",
        source_identity="fixture-attribution",
        code_sha="fixture-code-sha",
        out_dir=tmp_path,
        analyzer=AttributionReportAnalyzer(report),
    )
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    evidence = next(
        item["evidence"]
        for item in receipt["strategy_evidence"]
        if item["strategy_id"] == "ts_momentum_sma_atr"
    )

    assert [row["record_id"] for row in evidence["outcomes"]] == ["closed-loss"]
    assert {row["record_id"] for row in evidence["misses"]} == {
        "closed-loss",
        "no-trade",
    }
    assert all(row["state"] == "closed" for row in evidence["outcomes"])
    assert result["proposal_count"] >= 2


def test_attribution_adapter_quarantines_provisional_closed_rows() -> None:
    report = attribute_strategy_misses(
        [
            {
                "record_id": "aggregate",
                "market_date": "2026-08-20",
                "cohort": "official_forward_paper",
                "strategy_id": "ts_momentum_sma_atr",
                "strategy_version": "v1.0",
                "record_status": "realized",
                "return_pct": -2.0,
                "fill_truth_status": "missing_committed_fill_truth",
                "fill_id": "fill-without-commit",
            }
        ]
    )
    strategy = next(
        item
        for item in build_strategy_catalog()
        if item.strategy_id == "ts_momentum_sma_atr" and item.version == "v1.0"
    )
    result = AttributionReportAnalyzer(report).analyze(
        strategy,
        type("Context", (), {})(),
    )
    assert result["outcomes"] == []
    assert len(result["quarantined_closed"]) == 1
    assert result["quarantined_closed"][0]["status"] == "CLOSED_PROVISIONAL"
    assert result["misses"][0]["classification"] == "closed_provisional"


def _decision_condition(condition_id: str, status: str, **fields: object) -> dict[str, object]:
    return {"condition_id": condition_id, "status": status, **fields}


def _decision_receipt(
    *,
    strategy_id: str,
    strategy_version: str,
    pick_tier: str,
    research_pick_eligible: bool,
    paper_entry_eligible: bool,
    outcome_state: str,
    condition_results: list[dict[str, object]],
    all_blocking_failures: tuple[str, ...] = (),
    disclosed_gaps: tuple[str, ...] = (),
    contradicted_claims: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "pick_tier": pick_tier,
        "research_pick_eligible": research_pick_eligible,
        "paper_entry_eligible": paper_entry_eligible,
        "outcome_state": outcome_state,
        "condition_results": condition_results,
        "all_blocking_failures": list(all_blocking_failures),
        "disclosed_gaps": list(disclosed_gaps),
        "contradicted_claims": contradicted_claims or [],
    }


def test_daily_learning_aggregates_decision_receipts_without_self_modification(
    tmp_path: Path,
) -> None:
    receipts = [
        _decision_receipt(
            strategy_id="ts_momentum_sma_atr",
            strategy_version="v1.0",
            pick_tier="QUALIFIED_PICK",
            research_pick_eligible=True,
            paper_entry_eligible=True,
            outcome_state="WIN",
            condition_results=[
                _decision_condition(
                    "offering_or_dilution",
                    "RESOLVED_FROM_SOURCE",
                    resolver_id="strategy_gap_resolver",
                ),
                _decision_condition("trend_regime", "PASS"),
            ],
            contradicted_claims=[
                {"condition_id": "offering_or_dilution", "authoritative": True}
            ],
        ),
        _decision_receipt(
            strategy_id="ts_momentum_sma_atr",
            strategy_version="v1.0",
            pick_tier="PICK_WITH_DISCLOSED_GAPS",
            research_pick_eligible=True,
            paper_entry_eligible=False,
            outcome_state="LOSS",
            condition_results=[_decision_condition("catalyst_identified", "MISSING_DISCLOSED")],
            disclosed_gaps=("catalyst_identified",),
        ),
        _decision_receipt(
            strategy_id="ts_momentum_sma_atr",
            strategy_version="v1.0",
            pick_tier="BLOCKED_DATA",
            research_pick_eligible=False,
            paper_entry_eligible=False,
            outcome_state="WIN",
            condition_results=[_decision_condition("point_in_time_ohlcv", "FAIL")],
            all_blocking_failures=("point_in_time_ohlcv",),
        ),
        _decision_receipt(
            strategy_id="ts_momentum_sma_atr",
            strategy_version="v1.1",
            pick_tier="CONDITIONAL_PICK",
            research_pick_eligible=True,
            paper_entry_eligible=False,
            outcome_state="LOSS",
            condition_results=[
                _decision_condition("borrow_or_locate_verified", "MISSING_DISCLOSED")
            ],
            disclosed_gaps=("borrow_or_locate_verified",),
        ),
        _decision_receipt(
            strategy_id="ts_momentum_sma_atr",
            strategy_version="v1.0",
            pick_tier="BLOCKED_DATA",
            research_pick_eligible=False,
            paper_entry_eligible=False,
            outcome_state="MISSING_OUTCOME",
            condition_results=[_decision_condition("point_in_time_ohlcv", "FAIL")],
            all_blocking_failures=("point_in_time_ohlcv",),
        ),
        _decision_receipt(
            strategy_id="cross_sectional_relative_strength",
            strategy_version="v1.0",
            pick_tier="BLOCKED_SAFETY",
            research_pick_eligible=False,
            paper_entry_eligible=False,
            outcome_state="WIN",
            condition_results=[_decision_condition("reward_risk_at_least_1_50", "FAIL")],
            all_blocking_failures=("reward_risk_at_least_1_50",),
        ),
        _decision_receipt(
            strategy_id="gap_up_continuation",
            strategy_version="v1.0",
            pick_tier="CONDITIONAL_PICK",
            research_pick_eligible=True,
            paper_entry_eligible=False,
            outcome_state="WIN",
            condition_results=[_decision_condition("corporate_action_basis", "MISSING_DISCLOSED")],
            disclosed_gaps=("corporate_action_basis",),
        ),
    ]

    result = run_daily_strategy_learning(
        market_date="2026-08-22",
        cutoff="2026-08-22T22:00:00+00:00",
        source_identity="fixture-decision-receipts:2026-08-22",
        code_sha="fixture-code-sha",
        out_dir=tmp_path,
        decision_receipts=receipts,
    )
    artifact = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    learning = artifact["decision_receipt_learning"]

    assert learning["receipt_count"] == 7
    assert learning["tier_counts"] == {
        "BLOCKED_DATA": 2,
        "BLOCKED_SAFETY": 1,
        "CONDITIONAL_PICK": 2,
        "PICK_WITH_DISCLOSED_GAPS": 1,
        "QUALIFIED_PICK": 1,
    }
    assert learning["outcome_state_counts"] == {
        "LOSS": 2,
        "MISSING_OUTCOME": 1,
        "WIN": 4,
    }

    strategies = {
        (row["strategy_id"], row["strategy_version"]): row
        for row in learning["strategies"]
    }
    assert strategies[("ts_momentum_sma_atr", "v1.0")] == {
        "strategy_id": "ts_momentum_sma_atr",
        "strategy_version": "v1.0",
        "receipt_count": 4,
        "tier_counts": {
            "BLOCKED_DATA": 2,
            "PICK_WITH_DISCLOSED_GAPS": 1,
            "QUALIFIED_PICK": 1,
        },
        "outcome_state_counts": {"LOSS": 1, "MISSING_OUTCOME": 1, "WIN": 2},
        "research_pick_eligible_count": 2,
        "paper_entry_eligible_count": 1,
    }
    assert strategies[("ts_momentum_sma_atr", "v1.1")]["receipt_count"] == 1

    observations = {
        (
            row["strategy_id"],
            row["strategy_version"],
            row["condition_id"],
            row["condition_status"],
            row["pick_tier"],
            row["research_pick_eligible"],
            row["paper_entry_eligible"],
            row["outcome_state"],
        ): row
        for row in learning["condition_observations"]
    }
    assert observations[
        (
            "ts_momentum_sma_atr",
            "v1.0",
            "offering_or_dilution",
            "RESOLVED_FROM_SOURCE",
            "QUALIFIED_PICK",
            True,
            True,
            "WIN",
        )
    ]["ai_resolved_count"] == 1
    assert observations[
        (
            "ts_momentum_sma_atr",
            "v1.0",
            "catalyst_identified",
            "MISSING_DISCLOSED",
            "PICK_WITH_DISCLOSED_GAPS",
            True,
            False,
            "LOSS",
        )
    ]["disclosed_gap_count"] == 1

    assert learning["conditions_most_frequently_blocking"][0] == {
        "strategy_id": "ts_momentum_sma_atr",
        "strategy_version": "v1.0",
        "condition_id": "point_in_time_ohlcv",
        "blocking_candidate_count": 2,
    }
    assert {
        (
            row["strategy_id"],
            row["strategy_version"],
            row["condition_id"],
            row["blocking_candidate_count"],
        )
        for row in learning["conditions_most_frequently_blocking"]
    } == {
        ("ts_momentum_sma_atr", "v1.0", "point_in_time_ohlcv", 2),
        ("cross_sectional_relative_strength", "v1.0", "reward_risk_at_least_1_50", 1),
    }

    assert learning["ai_resolvable_gaps_successfully_resolved"] == [
        {
            "strategy_id": "ts_momentum_sma_atr",
            "strategy_version": "v1.0",
            "condition_id": "offering_or_dilution",
            "resolved_count": 1,
        }
    ]
    assert {
        (row["condition_id"], row["outcome_state"], row["count"])
        for row in learning["disclosed_gap_outcomes"]
    } == {
        ("borrow_or_locate_verified", "LOSS", 1),
        ("catalyst_identified", "LOSS", 1),
        ("corporate_action_basis", "WIN", 1),
    }
    assert {
        (row["condition_id"], row["eventual_winner_count"])
        for row in learning["conditions_that_excluded_eventual_winners"]
    } == {
        ("point_in_time_ohlcv", 1),
        ("reward_risk_at_least_1_50", 1),
    }
    assert learning["ai_claims_later_contradicted"] == [
        {
            "strategy_id": "ts_momentum_sma_atr",
            "strategy_version": "v1.0",
            "condition_id": "offering_or_dilution",
            "authoritative_contradiction_count": 1,
        }
    ]

    safety_flags = (
        "research_only",
        "automatic_policy_change",
        "automatic_promotion",
        "broker_execution_enabled",
        "missing_outcomes_are_zero",
    )
    assert {flag: learning[flag] for flag in safety_flags} == {
        "research_only": True,
        "automatic_policy_change": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "missing_outcomes_are_zero": False,
    }
    assert {flag: artifact[flag] for flag in safety_flags} == {
        "research_only": True,
        "automatic_policy_change": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "missing_outcomes_are_zero": False,
    }
