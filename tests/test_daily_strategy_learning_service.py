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
