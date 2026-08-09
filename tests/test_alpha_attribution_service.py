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
    no_trade = next(row for row in report["daily"] if row["market_date"] == "2026-08-04")
    assert no_trade["status"] == "NO_TRADE"
    assert no_trade["net_pnl"] is None
    assert no_trade["average_net_return_pct"] is None
    assert report["promotion_status"] == "operator_review_required_not_promoted"
    assert report["broker_execution_enabled"] is False
    assert report["diagnostic_attribution"]["single_trade_status"] == (
        "unexplained_within_predeclared_model_distribution"
    )
    assert report["diagnostic_attribution"]["aggregate"]["status"] == (
        "NOT_EVALUABLE_PENDING_PROTOCOL_APPROVAL"
    )


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


def test_cross_version_attribution_keeps_streams_and_missing_truth_distinct() -> None:
    report = build_alpha_attribution_report(
        signals=[
            {
                "signal_id": "v5-signal",
                "ticker": "V5A",
                "source": "sourced_fixture",
                "raw_payload_json": {"catalyst_category": "earnings"},
            }
        ],
        selections=[{"signal_id": "v5-signal", "decision": "clean_edge"}],
        evaluations=[],
        trades=[
            {
                "trade_id": "v5-trade",
                "signal_id": "v5-signal",
                "market_date": "2026-08-03",
                "ticker": "V5A",
                "strategy_id": "alphaops_v5",
                "cohort": "official_telegram",
                "net_pnl": 10.0,
                "net_return_pct": 1.0,
                "exit_reason": "target_1",
            }
        ],
        attempts=[],
        intents=[],
        v6_decisions=[
            {
                "decision_id": "v6-selected",
                "market_date": "2026-08-03",
                "ticker": "V6A",
                "action": "SHADOW_WATCH",
                "source_summary": {"status": "complete"},
                "feature_vector": {
                    "feature_json": {
                        "liquidity_execution": {"premarket_dollar_volume": 6_000_000},
                        "catalyst": {"sourced": True},
                    }
                },
            },
            {
                "decision_id": "v6-reject",
                "market_date": "2026-08-03",
                "ticker": "V6R",
                "action": "SHADOW_REJECTED_POLICY",
                "safety_vetoes": ["spread_too_wide"],
                "source_summary": {"status": "complete"},
                "rejected_sampling": {"included": True, "inclusion_probability": 0.2},
            },
        ],
        v6_outcomes=[
            {
                "decision_id": "v6-selected",
                "outcome_status": "COMPLETE_SOURCED",
                "learning_eligible": True,
                "net_excess_return_pct": -2.0,
                "activation_status": "ACTIVATED",
                "first_touch": "stop",
            },
            {
                "decision_id": "v6-reject",
                "outcome_status": "TERMINAL_MISSING",
                "learning_eligible": False,
            },
        ],
        paper_ops_rows=[
            {
                "record_id": "paper-good",
                "date": "2026-08-03",
                "record_status": "accepted",
                "strategy_id": "paper_v1",
                "return_pct": 0.5,
                "net_pnl": 5.0,
            },
            {
                "record_id": "paper-bad",
                "date": "2026-08-03",
                "record_status": "quarantined",
                "strategy_id": "paper_v1",
                "return_pct": None,
                "net_pnl": None,
            },
        ],
        paper_ops_issues=[{"code": "missing_paper_ops_cost_component"}],
        generated_at="2026-08-04T21:00:00+00:00",
    )

    cross = report["cross_version_attribution"]
    streams = {row["bucket"] for row in cross["category_breakdowns"]["evidence_stream"]}
    assert streams == {
        "ALPHAOPS_V5",
        "ALPHAOPS_V6",
        "ALPHAOPS_V6_SAMPLED_REJECT",
        "PAPEROPS",
    }
    assert cross["return_eligible_count"] == 3
    assert cross["return_missing_count"] == 2
    assert cross["missing_truth_is_zero"] is False
    assert cross["paper_ops_issue_count"] == 1
    assert {
        "universe_identity_corporate_action",
        "sampled_reject_regret",
        "regime_quality",
        "liquidity_capacity",
        "stop_invalidation_geometry",
        "target_exit_logic",
        "sizing_concentration",
        "tail_loss",
        "outcome_reconciliation_quality",
    } <= set(cross["category_breakdowns"])
    selected_tail = next(
        row
        for row in cross["category_breakdowns"]["tail_loss"]
        if row["bucket"] == "loss_above_tail_threshold"
    )
    assert selected_tail["mean_after_cost_return_pct"] == -2.0
    assert selected_tail["mean_benchmark_excess_return_pct"] == -2.0
    assert selected_tail["coverage_pct"] == 100.0
    assert "activation_counts" in selected_tail
    assert "stop_target_close_path_counts" in selected_tail
    assert "source_lineage_coverage_pct" in selected_tail
    source_failures = {row["bucket"] for row in cross["source_data_failures"]}
    assert "terminal_missing_source_outcome" in source_failures
    assert "quarantined_paperops_source" in source_failures
