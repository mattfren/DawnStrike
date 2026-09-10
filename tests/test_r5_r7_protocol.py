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
    bootstrap_paired_session_returns,
    compare_v5_baseline_challenger,
    run_r7_weekly_controller,
    run_v5_admission,
    simulate_causal_fill_lifecycle,
    replay_account_twr,
)
from tests.test_alphaops_intraday_adapter import _signal as _v5_signal_fixture


def _bars() -> list[dict]:
    rows = []
    for minute in range(15):
        hour, minute_in_hour = divmod(9 * 60 + 30 + minute, 60)
        stamp = f"2026-01-02T{hour:02d}:{minute_in_hour:02d}:00-05:00"
        rows.append({"event_at": stamp, "available_at": stamp.replace(":00-05:00", ":01-05:00"), "ingested_at": stamp.replace(":00-05:00", ":02-05:00"), "open": 101, "high": 102, "low": 100, "close": 101.1})
    rows.append({"event_at": "2026-01-02T10:00:00-05:00", "available_at": "2026-01-02T10:00:01-05:00", "ingested_at": "2026-01-02T10:00:02-05:00", "ask": 102.2, "bid": 102.1, "high": 103, "low": 101.7, "close": 102.5})
    return rows


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
        close_at="2026-01-02T16:00:00-05:00",
        expected_opening_bars=15,
    )
    assert signal["status"] == "SIGNAL"
    assert signal["target_price"] > signal["entry_price"]
    assert signal["same_bar_fill"] is False
    assert signal["hindsight_range_data"] is False
    bad = evaluate_gap_orb15_signal(
        bars=_bars(), prior_close=100.0, corporate_action_valid=False,
        close_at="2026-01-02T16:00:00-05:00",
        expected_opening_bars=15,
    )
    assert bad["status"] == "REJECTED"
    assert bad["reason"] == "corporate_action_prior_close_unverified"


def test_twr_requires_flow_timing_and_preserves_missing_sessions() -> None:
    report = replay_account_twr(
        sessions=[
            {"session_id": "s1", "starting_equity_cents": 10000000, "ending_equity_cents": 10100000, "external_flow_cents": 0, "cash_flow_timing": "start", "fees": 10, "turnover": 20000, "average_gross_exposure": .2, "trade_count": 1},
            {"session_id": "s2", "starting_equity_cents": 10100000, "ending_equity_cents": 10150000, "external_flow_cents": 100000, "cash_flow_timing": "start", "fees": 5, "turnover": 0, "average_gross_exposure": 0, "trade_count": 0},
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


def test_causal_lifecycle_records_partial_fill_and_close_exit() -> None:
    decision = {
        "feature_event_at": "2026-01-02T09:44:00-05:00",
        "feature_available_at": "2026-01-02T09:44:01-05:00",
        "feature_ingested_at": "2026-01-02T09:44:02-05:00",
        "decision_at": "2026-01-02T09:45:00-05:00",
    }
    signal = {
        "symbol": "AAA", "direction": "long", "notional": 5.0, "open_risk_pct": .10,
        "sector_theme": "tech", "entry_price": 102.2, "stop_price": 100.0,
        "target_price": 108.8, "maximum_exit_at": "2026-01-02T10:30:00-05:00",
    }
    manifest = {"status": "COMPLETE", "session_id": "XNYS:2026-01-02:regular", "universe_manifest_sha256": "a" * 64, "source_config_sha256": "b" * 64, "raw_events_sha256": "c" * 64}
    result = simulate_causal_fill_lifecycle(
        manifest=manifest, decision=decision, signal=signal,
        quotes=[{"event_at": "2026-01-02T10:00:01-05:00", "provider_available_at": "2026-01-02T10:00:02-05:00", "ingested_at": "2026-01-02T10:00:03-05:00", "ask": 102.2, "bid": 102.1, "fill_quantity": 3}],
        path_events=[{"event_at": "2026-01-02T10:30:00-05:00", "provider_available_at": "2026-01-02T10:30:00-05:00", "ingested_at": "2026-01-02T10:30:00-05:00", "close": 102.0, "high": 102.5, "low": 101.5}],
        portfolio={"gross_pct": 0, "net_pct": 0, "open_risk_pct": 0, "sector_theme_pct": 0, "daily_loss_pct": 0, "drawdown_pct": 0, "concurrent_positions": 0, "entries": 0},
        wrapper={"max_symbol_notional_pct": 10, "gross_exposure_pct": 30, "net_exposure_pct": 30, "sector_theme_exposure_pct": 20, "aggregate_open_risk_pct": .75, "daily_loss_stop_pct": 1, "drawdown_stop_pct": 8, "max_concurrent_positions": 3, "max_entries_per_session": 5},
        desired_quantity=5,
        v5_signal=_v5_signal_fixture(),
        v5_observation={"price": 10.05, "observed_at": "2026-08-03T14:00:00+00:00", "requested_at": "2026-08-03T14:00:00+00:00", "freshness_seconds": 0, "is_usable": True},
    )
    assert result["status"] == "COMPLETE"
    assert result["quantity"] == 3
    assert result["position_flat"] is True
    assert result["cost_model_version"] == "alphaops-v5-cost-model-50bps-0.005ps"
    assert result["journal_sha256"]
    assert result["total_cost"] > 0
    assert result["net_pnl"] < result["gross_pnl"]
    assert result["net_pnl"] < 0
    assert result["financial_pass"] is False
    assert any(row["kind"] == "v5_admission" and row["payload"]["eligible_for_official_paper"] for row in result["journal"])


def test_profitable_after_cost_control_and_v5_comparator_parity() -> None:
    decision = {"feature_event_at": "2026-01-02T09:44:00-05:00", "feature_available_at": "2026-01-02T09:44:01-05:00", "feature_ingested_at": "2026-01-02T09:44:02-05:00", "decision_at": "2026-01-02T09:45:00-05:00"}
    signal = {"symbol": "AAA", "direction": "long", "notional": 5.0, "open_risk_pct": .10, "sector_theme": "tech", "entry_price": 102.2, "stop_price": 100.0, "target_price": 108.8, "maximum_exit_at": "2026-01-02T10:30:00-05:00"}
    manifest = {"status": "COMPLETE", "session_id": "XNYS:2026-01-02:regular", "universe_manifest_sha256": "a" * 64, "source_config_sha256": "b" * 64, "raw_events_sha256": "c" * 64}
    result = simulate_causal_fill_lifecycle(
        manifest=manifest, decision=decision, signal=signal,
        quotes=[{"event_at": "2026-01-02T10:00:01-05:00", "provider_available_at": "2026-01-02T10:00:02-05:00", "ingested_at": "2026-01-02T10:00:03-05:00", "ask": 102.2, "bid": 102.1, "fill_quantity": 3}],
        path_events=[{"event_at": "2026-01-02T10:01:00-05:00", "provider_available_at": "2026-01-02T10:01:01-05:00", "ingested_at": "2026-01-02T10:01:02-05:00", "close": 110.0, "high": 110.0, "low": 105.0}],
        portfolio={"gross_pct": 0, "net_pct": 0, "open_risk_pct": 0, "sector_theme_pct": 0, "daily_loss_pct": 0, "drawdown_pct": 0, "concurrent_positions": 0, "entries": 0},
        wrapper={"max_symbol_notional_pct": 10, "gross_exposure_pct": 30, "net_exposure_pct": 30, "sector_theme_exposure_pct": 20, "aggregate_open_risk_pct": .75, "daily_loss_stop_pct": 1, "drawdown_stop_pct": 8, "max_concurrent_positions": 3, "max_entries_per_session": 5},
        desired_quantity=3, v5_signal=_v5_signal_fixture(), v5_observation={"price": 10.05, "observed_at": "2026-08-03T14:00:00+00:00", "requested_at": "2026-08-03T14:00:00+00:00", "freshness_seconds": 0, "is_usable": True},
    )
    assert result["net_pnl"] > 0
    comparison = compare_v5_baseline_challenger(signal=_v5_signal_fixture(), observation={"price": 10.05, "observed_at": "2026-08-03T14:00:00+00:00", "requested_at": "2026-08-03T14:00:00+00:00", "freshness_seconds": 0, "is_usable": True})
    assert comparison["equal_input_risk_cost"] is True
    assert comparison["baseline"]["eligible_for_official_paper"] is True
    assert comparison["challenger"]["eligible_for_official_paper"] is True


def test_lifecycle_rejects_overrisk_halt_and_same_bar_ambiguity() -> None:
    decision = {"feature_event_at": "2026-01-02T09:44:00-05:00", "feature_available_at": "2026-01-02T09:44:01-05:00", "feature_ingested_at": "2026-01-02T09:44:02-05:00", "decision_at": "2026-01-02T09:45:00-05:00"}
    signal = {"symbol": "AAA", "direction": "long", "notional": 5.0, "open_risk_pct": .10, "sector_theme": "tech", "entry_price": 102.2, "stop_price": 100.0, "target_price": 108.8, "maximum_exit_at": "2026-01-02T10:30:00-05:00"}
    manifest = {"status": "COMPLETE", "session_id": "XNYS:2026-01-02:regular", "universe_manifest_sha256": "a" * 64, "source_config_sha256": "b" * 64, "raw_events_sha256": "c" * 64}
    common = dict(manifest=manifest, decision=decision, signal=signal, quotes=[{"event_at": "2026-01-02T10:00:01-05:00", "provider_available_at": "2026-01-02T10:00:02-05:00", "ingested_at": "2026-01-02T10:00:03-05:00", "ask": 102.2, "bid": 102.1}], desired_quantity=1, wrapper={"max_symbol_notional_pct": 10, "gross_exposure_pct": 30, "net_exposure_pct": 30, "sector_theme_exposure_pct": 20, "aggregate_open_risk_pct": .75, "daily_loss_stop_pct": 1, "drawdown_stop_pct": 8, "max_concurrent_positions": 3, "max_entries_per_session": 5})
    over = simulate_causal_fill_lifecycle(**common, path_events=[], portfolio={"gross_pct": 29, "net_pct": 0, "open_risk_pct": 0, "sector_theme_pct": 0, "daily_loss_pct": 0, "drawdown_pct": 0, "concurrent_positions": 0, "entries": 0})
    assert over["status"] == "REJECTED"
    halted = simulate_causal_fill_lifecycle(**common, path_events=[{"event_at": "2026-01-02T10:01:00-05:00", "provider_available_at": "2026-01-02T10:01:00-05:00", "ingested_at": "2026-01-02T10:01:00-05:00", "halt": True}], portfolio={"gross_pct": 0, "net_pct": 0, "open_risk_pct": 0, "sector_theme_pct": 0, "daily_loss_pct": 0, "drawdown_pct": 0, "concurrent_positions": 0, "entries": 0})
    assert halted["status"] == "CENSORED"
    ambiguous = simulate_causal_fill_lifecycle(**common, path_events=[{"event_at": "2026-01-02T10:01:00-05:00", "provider_available_at": "2026-01-02T10:01:00-05:00", "ingested_at": "2026-01-02T10:01:00-05:00", "high": 109, "low": 99}], portfolio={"gross_pct": 0, "net_pct": 0, "open_risk_pct": 0, "sector_theme_pct": 0, "daily_loss_pct": 0, "drawdown_pct": 0, "concurrent_positions": 0, "entries": 0})
    assert ambiguous["status"] == "AMBIGUOUS"


def test_future_quote_and_v5_trace_fail_closed() -> None:
    bars = _bars()
    bars[-1]["available_at"] = "2026-01-02T09:59:00-05:00"
    rejected = evaluate_gap_orb15_signal(bars=bars, prior_close=100.0, corporate_action_valid=True, close_at="2026-01-02T16:00:00-05:00")
    assert rejected["reason"] == "source_availability_or_ingestion_invalid"
    trace = run_v5_admission(signal={"ticker": "AAA", "strategy_id": "alphaops_v5", "strategy_version": "dawnstrike-alphaops-v5.0.0"}, observation={"price": 100, "observed_at": "2026-08-03T14:00:00+00:00"})
    assert trace["eligible_for_official_paper"] is False
    assert trace["action"]


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


def test_fixed_block_bootstrap_is_derived_from_paired_sessions() -> None:
    report = bootstrap_paired_session_returns(
        challenger_returns=[.01, .02, -.01, .00, .03, .01, .00, -.02, .02, .01],
        baseline_returns=[.00, .01, -.01, -.01, .01, .00, .00, -.01, .01, .00],
        block_length=5,
        resamples=100,
        seed=27029,
    )
    assert report["status"] == "COMPLETE"
    assert report["complete_block_count"] == 2
    assert report["resamples"] == 100
    assert report["one_sided_lower_bound_95"] <= report["observed_mean_daily_log_return"]
    waiting = bootstrap_paired_session_returns(challenger_returns=[.01], baseline_returns=[.00], block_length=5)
    assert waiting["status"] == "WAITING"


def test_r7_controller_invokes_existing_learner_and_income_sequence_is_executable() -> None:
    protocol = build_research_protocol()
    rows = []
    for index in range(100):
        day = index + 1
        rows.append({
            "decision_id": f"r7-{index}", "market_date": f"2026-{(day - 1) // 28 + 1:02d}-{(day - 1) % 28 + 1:02d}",
            "source_artifact_hash_sha256": "a" * 64,
            "feature_event_at": f"2026-{(day - 1) // 28 + 1:02d}-{(day - 1) % 28 + 1:02d}T13:30:00+00:00",
            "feature_available_at": f"2026-{(day - 1) // 28 + 1:02d}-{(day - 1) % 28 + 1:02d}T13:31:00+00:00",
            "feature_ingested_at": f"2026-{(day - 1) // 28 + 1:02d}-{(day - 1) % 28 + 1:02d}T13:32:00+00:00",
            "decision_at": f"2026-{(day - 1) // 28 + 1:02d}-{(day - 1) % 28 + 1:02d}T13:33:00+00:00",
        })
    controller = run_r7_weekly_controller(dataset={"dataset_hash_sha256": "b" * 64, "feature_schema_version": "fixture-v1", "rows": rows}, protocol=protocol, code_sha="c" * 40)
    assert controller["status"] in {"RETAIN_WAITING_MARKET_EVIDENCE", "RETAIN_FROZEN_CHAMPION"}
    assert controller["training_receipt"]["status"] == "NOT_TRAINED_INSUFFICIENT_LABELS"
    income = build_income_illustration(illustrative_capital=100000, annual_return_assumption=.01, tax_rate=.2, annual_withdrawal_rate=.02, annual_cost_rate=.01, reserve_months=6, decay_rate=.1, period_returns=[.20, -.20, .20, -.20], fixed_withdrawal=500)
    assert len(income["forward_replay"]["periods"]) == 4
    assert income["sequence_difference"] != 0
    assert income["capacity_status"] == "UNKNOWN_NO_CAPACITY_EVIDENCE"
