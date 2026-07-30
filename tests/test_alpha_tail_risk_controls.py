from intraday_scanner.risk.policy import RiskInput, _within_eod_window, evaluate_risk


def test_daily_loss_limit_blocks_new_paper_entry() -> None:
    decision = evaluate_risk(
        RiskInput(
            ticker="NOVA",
            decision_time="2026-07-29T14:00:00+00:00",
            equity_cents=100_000,
            entry_price=10.0,
            stop_price=9.9,
            proposed_notional_cents=1_000,
            daily_realized_loss_cents=-1_001,
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
    assert "daily_loss_exceeds_1_pct" in decision.reasons


def test_eod_window_converts_offset_bearing_time_to_chicago() -> None:
    assert _within_eod_window("2026-07-29T20:00:00+00:00") is False
    assert _within_eod_window("2026-07-29T20:30:00+00:00") is True
