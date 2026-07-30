from intraday_scanner.alpha.execution_policy import (
    ExecutionPolicyInput,
    evaluate_execution_policy,
)


def test_execution_policy_blocks_unknown_holding_window() -> None:
    decision = evaluate_execution_policy(
        ExecutionPolicyInput(
            ticker="NOVA",
            decision_time="2026-07-29T14:00:00+00:00",
            expected_exit_time=None,
            session_close_time="16:00",
            equity_cents=100_000,
            entry_price=10.0,
            stop_price=9.9,
            proposed_notional_cents=10_000,
            daily_realized_loss_cents=0,
            ticker_notional_cents=0,
            correlated_position_count=0,
            halt_status="clear",
            corporate_action_status="clear",
            sec_risk_status="clear",
            source_quality_status="verified",
            spread_bps=25.0,
        )
    )
    assert decision.allowed_for_paper is False
    assert "holding_window_unknown" in decision.reasons
