from __future__ import annotations

from intraday_scanner.research.r5_r7_protocol import (
    FIXED_PANEL,
    LEGACY_REJECTED_STRATEGY,
    build_income_illustration,
    build_observer_controller,
    build_research_protocol,
    evaluate_confirmation_summary,
    evaluate_controller_update,
    evaluate_gap_orb15_signal,
    replay_account_twr,
)


def _bars() -> list[dict]:
    return [
        {"event_at": "2026-01-02T09:30:00+00:00", "available_at": "2026-01-02T09:30:01+00:00", "open": 101, "high": 102, "low": 100, "close": 101.1},
        {"event_at": "2026-01-02T09:35:00+00:00", "available_at": "2026-01-02T09:35:01+00:00", "open": 101.1, "high": 102, "low": 100.8, "close": 101.3},
        {"event_at": "2026-01-02T09:40:00+00:00", "available_at": "2026-01-02T09:40:01+00:00", "open": 101.3, "high": 102.2, "low": 101, "close": 101.8},
        {"event_at": "2026-01-02T10:00:00+00:00", "available_at": "2026-01-02T10:00:02+00:00", "open": 101.8, "high": 103, "low": 101.7, "close": 102.5, "executable_ask": 102.2},
    ]


def test_protocol_freezes_new_scopes_and_rejects_legacy() -> None:
    protocol = build_research_protocol()
    assert protocol["outcome_inspection_started"] is False
    assert protocol["legacy_strategy_rejection"] == LEGACY_REJECTED_STRATEGY
    assert protocol["scopes"]["panel_orb15_continuation_research_v1"]["fixed_panel"] == list(FIXED_PANEL)
    assert protocol["preregistration"]["bootstrap"] == {"block_length_sessions": 5, "resamples": 10000, "seed": 27029}
    assert protocol["cost"]["adverse_slippage_bps_each_leg"] == 50.0
    assert "top_contributor_concentration" in protocol["negative_controls"]
    assert protocol["protocol_hash_sha256"]


def test_orb_signal_requires_causal_break_and_close_deadline() -> None:
    signal = evaluate_gap_orb15_signal(
        bars=_bars(), prior_close=100.0, corporate_action_valid=True,
        close_at="2026-01-02T16:00:00+00:00",
    )
    assert signal["status"] == "SIGNAL"
    assert signal["target_price"] > signal["entry_price"]
    assert signal["same_bar_fill"] is False
    assert signal["hindsight_range_data"] is False
    bad = evaluate_gap_orb15_signal(
        bars=_bars(), prior_close=100.0, corporate_action_valid=False,
        close_at="2026-01-02T16:00:00+00:00",
    )
    assert bad["status"] == "REJECTED"
    assert bad["reason"] == "corporate_action_prior_close_unverified"


def test_twr_requires_flow_timing_and_preserves_missing_sessions() -> None:
    report = replay_account_twr(
        sessions=[
            {"session_id": "s1", "starting_equity": 100000, "external_flow_before": 1000, "external_flow_after": 0, "ending_equity_after_fees": 101000, "fees": 10, "turnover": 20000, "average_gross_exposure": .2, "trade_count": 1},
            {"session_id": "s2", "starting_equity": 101000, "external_flow_before": 0, "external_flow_after": 500, "ending_equity_after_fees": 100500, "fees": 5, "turnover": 0, "average_gross_exposure": 0, "trade_count": 0},
            {"session_id": "missed"},
        ]
    )
    assert report["status"] == "PARTIAL_MISSING_SESSIONS"
    assert report["missing_sessions"] == ["missed"]
    assert report["valid_no_trade_sessions"] == 1
    assert report["mfe_used"] is False
    assert report["price_optimism_used"] is False


def test_observer_retains_champion_and_rejects_unsafe_update() -> None:
    protocol = build_research_protocol()
    controller = build_observer_controller(protocol=protocol, eligible_session_count=0)
    assert controller["status"] == "RETAIN_WAITING_MARKET_EVIDENCE"
    result = evaluate_controller_update(
        controller=controller,
        candidate={"protocol_hash_sha256": "bad", "cost_status": "UNKNOWN", "broker_execution_enabled": True},
    )
    assert result["status"] == "RETAIN_FROZEN_CHAMPION"
    assert "protocol_hash_mismatch" in result["reasons"]
    assert "broker_promotion_forbidden" in result["reasons"]
    income = build_income_illustration(
        illustrative_capital=250000, annual_return_assumption=.05, tax_rate=.25,
        annual_withdrawal_rate=.03, annual_cost_rate=.01, reserve_months=12, decay_rate=.02,
    )
    assert income["status"] == "ILLUSTRATIVE_ONLY"
    assert income["personal_capital_commitment"] is None
    assert income["retirement_date"] is None


def test_confirmation_summary_waits_for_real_market_evidence() -> None:
    protocol = build_research_protocol()
    report = evaluate_confirmation_summary(
        protocol=protocol,
        scope="panel_orb15_continuation_research_v1",
        common_complete_sessions=10,
        challenger_trade_count=4,
        baseline_trade_count=4,
        paired_mean_daily_log_return=None,
        lower_bound_one_sided=None,
        drawdown_worsening_pp=None,
        critical_coverage_clear=False,
        cost_valid=False,
        capacity_valid=False,
    )
    assert report["status"] == "WAITING_MARKET_EVIDENCE"
    assert report["economic_pass"] is False
    assert report["promotion_eligible"] is False
