from __future__ import annotations

from intraday_scanner.services.alpha_attribution_service import (
    build_alpha_attribution_report,
)


def test_causal_attribution_separates_expectation_observation_and_missing_truth() -> None:
    signals = [
        {
            "signal_id": "sig-win",
            "ticker": "WIN",
            "primary_setup": "breakout",
            "gap_pct": 25.0,
            "float_shares": 4_000_000,
            "source_confidence": 90,
            "raw_payload_json": {
                "expected_win_probability": 0.60,
                "actual_after_cost_reward_risk": 1.8,
                "catalyst_category": "earnings",
                "market_regime": "risk_on",
                "sector_regime": "strong",
                "premarket_dollar_volume": 2_000_000,
            },
        },
        {
            "signal_id": "sig-loss",
            "ticker": "LOSS",
            "primary_setup": "breakout",
            "gap_pct": 55.0,
            "float_shares": 30_000_000,
            "source_confidence": 70,
            "raw_payload_json": {
                "expected_win_probability": 0.40,
                "actual_after_cost_reward_risk": 1.5,
                "catalyst_category": "unknown",
                "market_regime": "risk_off",
                "sector_regime": "weak",
                "premarket_dollar_volume": 500_000,
            },
        },
    ]
    selections = [
        {
            "signal_id": "sig-win",
            "selected_at": "2026-07-31T13:00:00Z",
            "market_date": "2026-07-31",
            "ticker": "WIN",
            "strategy_id": "alphaops_v5",
            "cohort": "official_telegram",
            "decision": "clean_edge",
        },
        {
            "signal_id": "sig-loss",
            "selected_at": "2026-08-03T13:00:00Z",
            "market_date": "2026-08-03",
            "ticker": "LOSS",
            "strategy_id": "alphaops_v5",
            "cohort": "official_telegram",
            "decision": "clean_edge",
        },
        {
            "signal_id": "no-trade",
            "selected_at": "2026-08-04T13:00:00Z",
            "market_date": "2026-08-04",
            "ticker": "NO_TRADE",
            "strategy_id": "alphaops_v5",
            "cohort": "official_telegram",
            "decision": "no_trade",
        },
    ]
    evaluations = [
        {
            "signal_id": "sig-win",
            "market_date": "2026-07-31",
            "strategy_id": "alphaops_v5",
            "terminal_state": "filled_and_closed",
            "filled": True,
        },
        {
            "signal_id": "sig-loss",
            "market_date": "2026-08-03",
            "strategy_id": "alphaops_v5",
            "terminal_state": "filled_and_closed",
            "filled": True,
        },
        {
            "signal_id": "blocked",
            "market_date": "2026-08-03",
            "strategy_id": "alphaops_v5",
            "terminal_state": "research_only_policy_blocked",
            "filled": False,
        },
    ]
    trades = [
        {
            "trade_id": "trade-win",
            "signal_id": "sig-win",
            "market_date": "2026-07-31",
            "ticker": "WIN",
            "strategy_id": "alphaops_v5",
            "cohort": "official_telegram",
            "net_pnl": 100.0,
            "gross_pnl": 110.0,
            "net_return_pct": 1.0,
            "r_multiple": 1.0,
            "exit_reason": "target_1",
        },
        {
            "trade_id": "trade-loss",
            "signal_id": "sig-loss",
            "market_date": "2026-08-03",
            "ticker": "LOSS",
            "strategy_id": "alphaops_v5",
            "cohort": "official_telegram",
            "net_pnl": -300.0,
            "gross_pnl": -280.0,
            "net_return_pct": -3.0,
            "r_multiple": -1.0,
            "exit_reason": "invalidation",
        },
    ]
    attempts = [
        {
            "signal_id": "sig-win",
            "market_date": "2026-07-31",
            "status": "resolved",
        },
        {
            "signal_id": "sig-loss",
            "market_date": "2026-08-03",
            "status": "resolved",
        },
        {
            "signal_id": "missing",
            "market_date": "2026-08-03",
            "status": "terminal_missing",
        },
    ]
    intents = [
        {
            "signal_id": "sig-win",
            "market_date": "2026-07-31",
            "action": "ENTER_LONG",
        },
        {
            "signal_id": "blocked",
            "market_date": "2026-08-03",
            "action": "STAND_DOWN",
            "blocked_reason": "gap_regime_outside_policy",
        },
    ]

    report = build_alpha_attribution_report(
        signals=signals,
        selections=selections,
        evaluations=evaluations,
        trades=trades,
        attempts=attempts,
        intents=intents,
        generated_at="2026-08-04T21:00:00+00:00",
    )

    official = report["official"]
    assert official["trade_count"] == 2
    assert official["net_pnl"] == -200.0
    assert official["observed_hit_rate_pct"] == 50.0
    assert official["expected_hit_rate_pct"] == 50.0
    assert official["observed_r_multiple"] == 0.0
    assert official["expected_r_multiple"] == 1.65
    assert report["loss_concentration"]["largest_loss_share_pct"] == 100.0
    assert report["outcome_coverage"]["coverage_pct"] == 66.6667
    assert report["outcome_coverage"]["missing_is_zero"] is False
    no_trade = next(
        row for row in report["daily"] if row["market_date"] == "2026-08-04"
    )
    assert no_trade["status"] == "NO_TRADE"
    assert no_trade["net_pnl"] is None
    assert no_trade["average_net_return_pct"] is None
    assert report["promotion_status"] == "operator_review_required_not_promoted"
    assert report["broker_execution_enabled"] is False


def test_empty_attribution_uses_null_not_zero() -> None:
    report = build_alpha_attribution_report(
        signals=[],
        selections=[],
        evaluations=[],
        trades=[],
        attempts=[],
        intents=[],
        generated_at="2026-08-04T21:00:00+00:00",
    )

    assert report["status"] == "no_evidence"
    assert report["official"]["net_pnl"] is None
    assert report["official"]["observed_hit_rate_pct"] is None
    assert report["outcome_coverage"]["coverage_pct"] is None
