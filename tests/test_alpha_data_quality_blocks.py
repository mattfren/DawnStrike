from intraday_scanner.risk.policy import RiskInput, evaluate_risk


def test_unknown_critical_quality_inputs_fail_closed() -> None:
    decision = evaluate_risk(
        RiskInput(
            ticker="NOVA",
            decision_time="2026-07-29T14:00:00+00:00",
            equity_cents=100_000,
            entry_price=10.0,
            stop_price=9.9,
            proposed_notional_cents=1_000,
            daily_realized_loss_cents=0,
            ticker_notional_cents=0,
            correlated_position_count=0,
            halt_status=None,
            corporate_action_status=None,
            sec_risk_status=None,
            source_quality_status=None,
            spread_bps=None,
        )
    )
    assert decision.allowed_for_paper is False
    assert decision.action == "SHADOW_BLOCK"
