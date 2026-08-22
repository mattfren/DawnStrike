from datetime import date

from intraday_scanner.services.strategy_challenger_backtest_service import (
    build_strategy_challenger_backtest_report,
)
from intraday_scanner.v2.data import build_synthetic_ohlcv_dataset


def test_all_strategies_and_challengers_are_compared_research_only() -> None:
    dataset = build_synthetic_ohlcv_dataset(
        end_date=date(2026, 8, 21),
        trading_days=140,
    )
    report = build_strategy_challenger_backtest_report(
        dataset,
        source_manifest={"snapshot_id": "fixture-snapshot", "validation_status": "fixture"},
        code_sha="fixture-code-sha",
    )

    assert report["strategy_count"] == 11
    assert report["challenger_count"] == 9
    assert len(report["strategies"]) == 11
    assert report["research_only"] is True
    assert report["promotion_eligible"] is False
    assert report["automatic_policy_change"] is False
    assert report["automatic_promotion"] is False
    assert report["broker_execution_enabled"] is False
    assert report["missing_outcomes_are_zero"] is False
    assert report["evidence_boundary"] == "latest_snapshot_retrospective_not_forward"
    assert len(report["report_sha256"]) == 64

    by_id = {row["strategy_id"]: row for row in report["strategies"]}
    assert by_id["benchmark_buy_hold_equal_weight"]["comparison_status"] == (
        "COMPARATOR_ONLY"
    )
    assert by_id["benchmark_buy_hold_equal_weight"]["champion"]["trade_count"] > 0
    assert by_id["cash_no_trade_baseline"]["champion"]["trade_count"] == 0
    assert by_id["failed_breakout_reversal_short"]["comparison_status"] == (
        "NOT_EVALUABLE_NO_CHALLENGER_TRADES"
    )
    assert by_id["failed_breakout_reversal_short"]["metric_delta"] is None
    assert by_id["failed_breakout_reversal_short"]["gate_telemetry"][
        "eligible_count"
    ] == 0
