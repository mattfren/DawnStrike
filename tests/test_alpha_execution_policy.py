from intraday_scanner.alpha.execution_policy import (
    ExecutionPolicyInput,
    evaluate_execution_policy,
)
from intraday_scanner.risk.portfolio import PortfolioRiskSnapshot


def _portfolio_snapshot() -> PortfolioRiskSnapshot:
    return PortfolioRiskSnapshot.from_mappings(
        equity=100_000.0,
        daily_realized_pnl=0.0,
        daily_unrealized_pnl=0.0,
        peak_equity=100_000.0,
        as_of="2026-07-29T14:00:00+00:00",
        metadata_complete=True,
    )


def _portfolio_policy_input(**overrides: object) -> ExecutionPolicyInput:
    values: dict[str, object] = {
        "ticker": "NOVA",
        "decision_time": "2026-07-29T14:00:00+00:00",
        "expected_exit_time": "2026-07-29T15:00:00+00:00",
        "session_close_time": "16:00",
        "equity_cents": 100_000,
        "entry_price": 10.0,
        "stop_price": 9.9,
        "proposed_notional_cents": 2_500,
        "daily_realized_loss_cents": 0,
        "ticker_notional_cents": 0,
        "correlated_position_count": 0,
        "halt_status": "clear",
        "corporate_action_status": "clear",
        "sec_risk_status": "clear",
        "source_quality_status": "verified",
        "spread_bps": 25.0,
        "portfolio_snapshot": _portfolio_snapshot(),
        "quantity": 25,
        "price_observed_at": "2026-07-29T14:00:00+00:00",
        "metadata_complete": True,
    }
    values.update(overrides)
    return ExecutionPolicyInput(**values)


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


def test_execution_policy_keeps_legacy_safety_gates_with_portfolio_context() -> None:
    cases = (
        ("halt_status", "halted", "halt_status_blocked"),
        ("spread_bps", None, "spread_unknown"),
        ("corporate_action_status", "blocked", "corporate_action_status_blocked"),
    )
    for field, value, expected_reason in cases:
        decision = evaluate_execution_policy(_portfolio_policy_input(**{field: value}))
        assert decision.allowed_for_paper is False
        assert expected_reason in decision.reasons
