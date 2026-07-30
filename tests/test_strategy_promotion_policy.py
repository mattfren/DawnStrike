from intraday_scanner.alpha.promotion_policy import PromotionEvidence, evaluate_promotion


def test_promotion_waits_for_forward_evidence() -> None:
    decision = evaluate_promotion(
        PromotionEvidence(
            strategy_id="alphaops_v4",
            strategy_version="v1",
            cohort="official_forward_paper",
            real_market_days=5,
            closed_forward_trades=7,
            eligible_outcome_coverage_pct=100.0,
            net_expectancy_pct=-1.0,
            profit_factor=0.2,
            excess_return_vs_cash_pct=-1.0,
            excess_return_vs_benchmark_pct=None,
            max_forward_drawdown_pct=-47.0,
            max_gain_concentration_pct=50.0,
            max_loss_concentration_pct=100.0,
            walk_forward_positive=False,
            holdout_positive=False,
            slippage_stress_positive=False,
            no_lookahead_passed=True,
        )
    )
    assert decision.status == "WAITING_FOR_FORWARD_EVIDENCE"
    assert decision.action == "HOLD"
    assert "drawdown_worse_than_8_pct" in decision.vetoes
