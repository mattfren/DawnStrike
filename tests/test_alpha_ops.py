import json
from pathlib import Path

import pytest

from intraday_scanner.alpha.alert_gate import (
    _price,
    _reward_risk_ratio,
    _stop_distance_pct,
    _volume,
    apply_alert_gate,
)
from intraday_scanner.alpha.alpha_model import AlphaModel
from intraday_scanner.alpha.edge_calibrator import (
    calibrate_edge,
    outlier_warning,
    shrink_empirical_mean,
)
from intraday_scanner.alpha.feature_factory import build_feature_vector
from intraday_scanner.alpha.no_trade_filter import evaluate_no_trade
from intraday_scanner.alpha.outcome_labeler import label_outcome
from intraday_scanner.alpha.performance_truth import build_truth_report
from intraday_scanner.alpha.risk_governor import evaluate_risk
from intraday_scanner.alpha.setup_memory import build_setup_memory
from intraday_scanner.cli import main
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.notifiers import NotificationEvent
from intraday_scanner.notifiers.telegram_formatter import (
    format_alpha_monitor,
    format_alpha_no_trade,
    format_alpha_summary,
    format_alpha_watch,
)
from intraday_scanner.services.alpha_cycle_service import (
    _historical_publication_rows,
    _persist_research_radar_selections,
    _radar_monitor_signals,
    _research_radar,
    alpha_monitor,
)
from intraday_scanner.services.luna_research_slate_service import (
    build_ranked_research_slate,
)
from intraday_scanner.services.signal_review_service import (
    monitor_alpha_signals,
    review_alpha_signals,
)
from intraday_scanner.services.source_reliability_service import build_source_reliability
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

FIXTURE_CONFIG = Path("tests/fixtures/web_sources_fixture.yaml")


def _candidate(**overrides):
    base = {
        "rank": 1,
        "ticker": "NOVA",
        "score": 88.0,
        "total_score": 88.0,
        "explosive_score": 82.0,
        "catalyst_score": 70.0,
        "premarket_price": 5.25,
        "previous_close": 2.80,
        "premarket_high": 5.35,
        "premarket_low": 4.80,
        "premarket_volume": 1_200_000,
        "dollar_volume": 6_300_000,
        "gap_pct": 87.5,
        "spread_pct": 1.2,
        "float_rotation_pct": 25.0,
        "range_position_pct": 72.0,
        "liquidity_tier": "high_liquidity",
        "setup_grade": "A",
        "risk_flags": "",
        "avoid_reasons": "",
        "source": "fixture_public_table",
        "preferred_source": "fixture_public_table",
        "source_confidence": 82,
        "source_count": 2,
        "data_source_kind": "fixture",
        "stale_data_flag": False,
        "conflict_flags": "",
        "has_news": True,
        "catalyst_headline": "Positive Phase 2 update",
        "catalyst_category": "biotech",
        "breakout_trigger": 5.40,
        "invalidation_level": 4.85,
        "first_target": 6.25,
        "best_exit_bias": "trail_to_close",
    }
    base.update(overrides)
    return base


def test_alpha_feature_vector_has_required_groups():
    vector = build_feature_vector(
        _candidate(),
        scan_id="scan-1",
        timestamp="2026-06-21T12:00:00+00:00",
        source_summary={"rows_normalized": 4},
        source_reliability={"fixture_public_table": {"reliability_score": 91}},
    )

    assert vector["scan_id"] == "scan-1"
    assert vector["ticker"] == "NOVA"
    assert vector["model_version"].startswith("dawnstrike-alphaops-v4")
    assert set(vector["feature_json"]) == {
        "price_momentum",
        "liquidity_execution",
        "source_data_quality",
        "catalyst",
        "risk",
        "structure",
        "playbook_setup",
    }
    assert vector["feature_json"]["source_data_quality"]["source_reliability_score"] == 91
    price = vector["feature_json"]["price_momentum"]
    source = vector["feature_json"]["source_data_quality"]
    catalyst = vector["feature_json"]["catalyst"]
    structure = vector["feature_json"]["structure"]
    playbook = vector["feature_json"]["playbook_setup"]
    assert price["price_bucket"] == "small_cap_range"
    assert price["mega_gap_flag"] is False
    assert price["price_near_high"] is True
    assert source["public_url_unverified_flag"] is False
    assert catalyst["fda_biotech_flag"] is True
    assert structure["squeeze_structure_score"] >= 0
    assert playbook["primary_setup"] == "biotech_catalyst"


def test_risk_governor_hard_avoids_block_alerts():
    decision = evaluate_risk(_candidate(current_halt=True, risk_flags="current_halt"))

    assert decision.can_alert is False
    assert "current_halt" in decision.hard_avoid_reasons
    assert "current_halt" in decision.avoid_reasons


@pytest.mark.parametrize("invalid", (0.0, -1.0, float("nan"), float("inf")))
def test_risk_governor_candidate_price_and_volume_own_precedence(invalid: float) -> None:
    price_decision = evaluate_risk(
        _candidate(premarket_price=invalid), {"premarket_price": 5.25}
    )
    volume_decision = evaluate_risk(
        _candidate(premarket_volume=invalid), {"premarket_volume": 1_200_000}
    )
    assert "missing_price" in price_decision.hard_avoid_reasons
    assert "zero_volume" in volume_decision.hard_avoid_reasons


def test_risk_governor_null_blank_primary_controls_fall_back_to_features() -> None:
    for missing in (None, "", "   "):
        decision = evaluate_risk(
            _candidate(premarket_price=missing, premarket_volume=missing),
            {"premarket_price": 5.25, "premarket_volume": 1_200_000},
        )
        assert "missing_price" not in decision.hard_avoid_reasons
        assert "zero_volume" not in decision.hard_avoid_reasons


def test_alert_gate_price_volume_and_plan_aliases_are_presence_ordered() -> None:
    assert _price({"premarket_price": 0, "price": 10}) == 0
    assert _price({"premarket_price": None, "price": 10}) == 10
    assert _volume({"premarket_volume": 0, "volume": 100}) is None
    assert _volume({"premarket_volume": None, "volume": 100}) == 100
    assert _reward_risk_ratio(
        {"reward_risk_ratio": 0, "entry_trigger": 10, "target_1": 20, "invalidation": 5}
    ) == 0
    assert _reward_risk_ratio(
        {"entry_trigger": 0, "premarket_price": 10, "target_1": 20, "invalidation": 5}
    ) is None
    assert _stop_distance_pct(
        {"entry_trigger": 0, "premarket_price": 10, "invalidation": 5}
    ) is None
    assert _stop_distance_pct(
        {"entry_trigger": None, "premarket_price": 10, "invalidation": 5}
    ) == 50.0


@pytest.mark.parametrize(
    "overrides",
    (
        {"premarket_price": 0.0, "price": 10.0},
        {"premarket_volume": 0.0, "volume": 100.0},
        {"entry_trigger": 0.0, "breakout_trigger": 5.4},
    ),
)
def test_alert_gate_rejects_invalid_primary_alias_without_fallback(
    overrides: dict[str, object],
) -> None:
    gated = apply_alert_gate({**_candidate(), **overrides, "can_alert": True})

    assert gated["alert_gate_status"] == "BLOCKED"
    assert gated["can_alert"] is False


def test_no_trade_filter_allows_no_clean_edge():
    decision = evaluate_no_trade([
        {"ticker": "HALT", "can_alert": False, "alpha_score": 0, "no_trade_reason": "current_halt"}
    ])

    assert decision.no_trade is True
    assert "current halt" in decision.reason
    assert "Do not force" in decision.next_action


def test_no_trade_filter_does_not_call_sparse_provider_coverage_no_edge():
    source_summary = {
        "status": "success",
        "premarket_enrichment": {
            "selected_count": 9,
            "verified_count": 2,
            "secondary_fallback_status": "ceiling_exceeded_not_applied",
        },
    }

    decision = evaluate_no_trade([], source_summary=source_summary)

    assert decision.no_trade is True
    assert "2 of 9" in decision.reason
    assert "market no-edge" in decision.next_action


def test_no_trade_filter_quarantines_low_confidence_uncalibrated_fallback():
    signal = _candidate(
        alpha_score=40.4,
        edge_bucket="LOW",
        confidence_bucket="INSUFFICIENT_SAMPLE",
        source_confidence=27.5,
        risk_score=70,
        risk_flags="missing_previous_close;unknown_float;public_url_unverified",
        can_alert=True,
        no_trade_reason="",
    )

    decision = evaluate_no_trade([signal], source_summary={"status": "success"})
    review = review_alpha_signals([signal], source_summary={"status": "success"})

    assert decision.no_trade is True
    assert decision.decision_tier == "no_trade"
    assert decision.fallback_count == 0
    assert review["watchlist"] == []
    assert review["blocked"][0]["ticker"] == "NOVA"
    assert review["plain_read"].startswith("No clean edge today")


def test_review_does_not_reintroduce_a_candidate_below_the_fallback_floor():
    signal = _candidate(
        alpha_score=40.0,
        edge_bucket="MEDIUM",
        confidence_bucket="MEDIUM",
        source_confidence=90.0,
        risk_score=90.0,
        can_alert=True,
        no_trade_reason="",
        alert_gate_status="PASS",
        manual_confirmation_required=False,
    )

    decision = evaluate_no_trade([signal], source_summary={"status": "success"})
    review = review_alpha_signals([signal], source_summary={"status": "success"})

    assert decision.no_trade is True
    assert decision.fallback_count == 0
    assert review["watchlist"] == []
    assert review["blocked"][0]["ticker"] == "NOVA"


def test_alpha_model_uses_insufficient_sample_fallback_under_20_days():
    candidate = _candidate()
    vector = build_feature_vector(candidate, scan_id="scan-1", timestamp="now")

    scored = AlphaModel().score_candidates(
        [candidate],
        [vector],
        historical_outcomes=[],
        real_shadow_days=7,
    )

    assert scored[0]["expectancy_status"] == "INSUFFICIENT_SAMPLE"
    assert scored[0]["confidence_bucket"] == "INSUFFICIENT_SAMPLE"
    assert scored[0]["can_alert"] is True
    assert scored[0]["alpha_score"] > 0
    assert scored[0]["source_reliability_adjustment"] == 0
    assert scored[0]["ml_score_used"] is False


def test_source_reliability_changes_alpha_score():
    weak = _candidate(source_confidence=82)
    strong = _candidate(source_confidence=82)
    weak_vector = build_feature_vector(
        weak,
        scan_id="scan-1",
        timestamp="now",
        source_reliability={"fixture_public_table": {"reliability_score": 20}},
    )
    strong_vector = build_feature_vector(
        strong,
        scan_id="scan-1",
        timestamp="now",
        source_reliability={"fixture_public_table": {"reliability_score": 95}},
    )

    weak_score = AlphaModel().score_candidates([weak], [weak_vector], real_shadow_days=7)[0]
    strong_score = AlphaModel().score_candidates([strong], [strong_vector], real_shadow_days=7)[0]

    assert weak_score["source_reliability_adjustment"] < 0
    assert strong_score["source_reliability_adjustment"] > 0
    assert strong_score["alpha_score"] > weak_score["alpha_score"]


def test_offline_ml_only_activates_when_it_beats_rule_baseline():
    outcomes = []
    for day in range(1, 31):
        for index in range(4):
            score = 40 + index * 10
            outcomes.append({
                "date": f"2026-05-{day:02d}",
                "score": score,
                "risk_score": 80,
                "source_reliability_score": 90,
                "source_confidence": 85,
                "gap_pct": 50,
                "dollar_volume": 1_000_000,
                "spread_pct": 1,
                "catalyst_confidence": 0.8,
                "close_return_pct": score / 10,
            })
    candidate = _candidate(score=80)
    vector = build_feature_vector(candidate, scan_id="scan-1", timestamp="now")

    scored = AlphaModel().score_candidates(
        [candidate],
        [vector],
        historical_outcomes=outcomes,
        real_shadow_days=30,
    )[0]

    assert scored["ml_status"] == "ml_beats_baseline"
    assert scored["ml_score_used"] is True
    assert scored["ml_evaluation"]["split"] == "date_ordered_70_30"


def test_empirical_prior_shrinkage_and_outlier_warning():
    assert shrink_empirical_mean(bucket_mean=20, bucket_count=2, global_mean=2) < 5
    report = calibrate_edge(
        bucket_rows=[{"high_after_entry_return": 12}, {"high_after_entry_return": -2}],
        global_rows=[{"high_after_entry_return": 2}] * 40,
        real_shadow_days=22,
    )

    assert report["mode"] == "empirical_shrinkage"
    assert report["sample_size"] == 2
    assert outlier_warning([
        {"high_after_entry_return": 50},
        {"high_after_entry_return": 1},
        {"high_after_entry_return": 1},
    ])["outlier_dependent"] is True


def test_source_reliability_and_setup_memory_update():
    reliability = build_source_reliability(
        {
            "attempts": [
                {
                    "source": "fixture_public_table",
                    "status": "success",
                    "rows_extracted": 4,
                    "rows_normalized": 4,
                    "rows_rejected": 0,
                }
            ]
        },
        outcomes=[{"source": "fixture_public_table", "winner_close": True}],
    )
    memory = build_setup_memory([
        {"setup_key": "grade:A", "high_after_entry_return": 10, "low_after_entry_drawdown": -2},
        {"setup_key": "grade:A", "high_after_entry_return": -1, "low_after_entry_drawdown": -4},
    ])

    assert reliability[0]["reliability_score"] > 50
    assert memory["grade:A"]["sample_size"] == 2
    assert memory["grade:A"]["win_rate_pct"] == 50


def test_performance_truth_reports_alpha_buckets_and_warnings():
    rows = [
        {
            "rank": 1,
            "ticker": "NOVA",
            "edge_bucket": "HIGH",
            "score_decile": 9,
            "setup_key": "grade:A",
            "source": "fixture",
            "catalyst_category": "biotech",
            "risk_flags": "none",
            "high_after_entry_return": 10,
            "low_after_entry_drawdown": -2,
            "data_source_kind": "manual",
        },
        {
            "rank": 2,
            "ticker": "RIFT",
            "edge_bucket": "LOW",
            "score_decile": 4,
            "setup_key": "grade:D",
            "source": "fixture",
            "catalyst_category": "none",
            "risk_flags": "wide_spread",
            "high_after_entry_return": -3,
            "low_after_entry_drawdown": -8,
            "data_source_kind": "manual",
        },
    ]

    report = build_truth_report(rows, real_days_collected=2)

    assert report["max_drawdown_pct"] == -8
    assert "HIGH" in report["alpha_bucket_performance"]
    assert report["best_worst_setup"]["best"]["bucket"] == "grade:A"
    assert "fewer_than_20_real_days" in report["evidence_warnings"]
    assert report["can_claim_success"] is False


def test_truth_report_keeps_missing_returns_null() -> None:
    report = build_truth_report(
        [{"rank": 1, "ticker": "MISS", "missing_outcome_high": True}],
        real_days_collected=0,
    )

    assert report["average_return_pct"] is None
    assert report["median_return_pct"] is None
    assert report["win_rate_pct"] is None
    assert report["max_drawdown_pct"] is None
    assert report["observed_return_count"] == 0
    assert report["missing_return_count"] == 1


def test_outcome_labeler_uses_entry_not_future_high_for_winners():
    label = label_outcome(
        _candidate(scan_id="scan-1", breakout_trigger=5.0, first_target=6.0),
        {"ticker": "NOVA", "entry": 5.0, "price_5m": 5.5, "high": 6.2, "low": 4.8},
    )

    assert label["winner_5m"] is True
    assert label["high_after_entry_return"] == 24.0
    assert label["failed_fast"] is True
    assert label["squeeze_candidate"] is True


def test_alpha_telegram_messages_are_secret_free():
    secret_token = "test-token-do-not-print"
    signal = _candidate(alpha_score=81, edge_bucket="HIGH", can_alert=True)

    text = format_alpha_watch(signals=[signal], edge_label="HIGH")
    no_trade = format_alpha_no_trade(reason="low source confidence", next_action="wait")
    summary = format_alpha_summary({"truth_report": {"real_days_collected": 7}})

    assert "Dawnstrike Alpha Watch" in text
    assert "No clean edge today" in no_trade
    for heading in (
        "OFFICIAL PAPER CANDIDATES",
        "RESEARCH WATCHLIST",
        "NO TRADE / BLOCKED REASONS",
    ):
        assert heading in text
        assert heading in no_trade
    assert "insufficient sample" in summary
    assert secret_token not in text + no_trade + summary


def test_manual_monitor_no_price_source_dedupes_without_spam(tmp_path):
    store = SQLiteScanStore(tmp_path / "alpha.sqlite")
    store.persist_alpha_signals([
        {
            **_candidate(alpha_score=76, edge_bucket="MEDIUM", can_alert=True),
            "scan_id": "scan-1",
            "timestamp": "2026-06-21T12:00:00+00:00",
            "signal_key": "scan-1:1:NOVA",
        }
    ])

    first = alpha_monitor(db_path=tmp_path / "alpha.sqlite", dry_run=True)
    second = alpha_monitor(db_path=tmp_path / "alpha.sqlite", dry_run=True)

    assert first["status"] == "manual_monitor_required"
    assert first["notification_stats"]["sent"] == 1
    assert second["notification_stats"]["skipped"] == 1
    assert "MANUAL REVIEW" in format_alpha_monitor(monitor_alpha_signals([], current_prices={}))


def test_research_radar_uses_independent_stretch_target_and_tracks_lifecycle():
    signal = _candidate(
        signal_key="scan-radar:1:NOVA",
        gap_pct=8.0,
        spread_pct=0.8,
        dollar_volume=8_000_000,
        source_confidence=90.0,
        source_quality_status="VERIFIED",
        halt_status="CLEAR",
        sec_risk_status="CLEAR",
        corporate_action_status="CLEAR",
        hard_avoid_reasons=[],
        entry_trigger=10.0,
        invalidation=9.5,
        target_1=10.6,
        target_2=11.0,
    )

    radar = _research_radar([signal])

    assert len(radar) == 1
    assert radar[0]["radar_target"] == 11.0
    assert radar[0]["radar_target_role"] == "stretch_range_extension"
    selection = {
        "signal_id": signal["signal_key"],
        "payload_json": {"signal": radar[0]},
    }
    monitored = _radar_monitor_signals([signal], [selection])
    assert monitored[0]["target_1"] == 11.0

    assert monitor_alpha_signals(monitored, current_prices={"NOVA": 9.8})["events"][0][
        "label"
    ] == "WAITING FOR TRIGGER"
    assert monitor_alpha_signals(monitored, current_prices={"NOVA": 10.1})["events"][0][
        "label"
    ] == "ENTRY TRIGGERED"
    assert monitor_alpha_signals(monitored, current_prices={"NOVA": 11.0})["events"][0][
        "label"
    ] == "TARGET HIT"
    assert monitor_alpha_signals(monitored, current_prices={"NOVA": 9.4})["events"][0][
        "label"
    ] == "SETUP INVALIDATED"
    assert monitor_alpha_signals(
        monitored,
        current_prices={"NOVA": 10.1},
        current_quotes={},
    )["events"][0]["label"] == "WAITING FOR LIQUID QUOTE"
    assert monitor_alpha_signals(
        monitored,
        current_prices={"NOVA": 10.1},
        current_quotes={
            "NOVA": {"bid": 9.8, "ask": 10.3, "spread_pct": 4.98, "is_usable": True}
        },
    )["events"][0]["label"] == "SPREAD TOO WIDE"
    assert monitor_alpha_signals(
        monitored,
        current_prices={"NOVA": 10.1},
        current_quotes={
            "NOVA": {"bid": 10.08, "ask": 10.12, "spread_pct": 0.4, "is_usable": True}
        },
    )["events"][0]["label"] == "ENTRY TRIGGERED"


def test_radar_monitor_rehydrates_exact_cross_scan_frozen_slate_signal():
    slate = build_ranked_research_slate(
        [{"ticker": "NOVA", "signal_id": "scan-old:1:NOVA", "target_1": 11.0}],
        generated_at="2026-08-26T13:00:00+00:00",
        market_date="2026-08-26",
        scan_id="scan-old",
    )
    frozen_signal = slate["rows"][0]
    selection = {
        "selection_id": "selection-frozen",
        "scan_id": "scan-retry",
        "source_scan_id": "scan-old",
        "scan_lineage_status": "GOVERNED_DAILY_FREEZE_REUSE",
        "signal_id": "scan-old:1:NOVA",
        "ticker": "NOVA",
        "cohort": "research_radar",
        "payload_json": {
            "signal": frozen_signal,
            "frozen_slate_lineage": {
                "schema_version": "dawnstrike.luna.frozen_slate_selection_lineage.v1",
                "slate_id": slate["slate_id"],
                "slate_content_hash_sha256": slate["content_hash_sha256"],
                "frozen_source_scan_id": "scan-old",
                "current_scan_id": "scan-retry",
                "reuse_status": "GOVERNED_DAILY_FREEZE_REUSE",
            },
            "frozen_ranked_research_slate": slate,
        },
    }

    monitored = _radar_monitor_signals([], [selection])

    assert len(monitored) == 1
    assert monitored[0]["signal_id"] == "scan-old:1:NOVA"
    assert monitored[0]["monitor_cohort"] == "research_radar"


def test_retry_materializes_exact_frozen_rows_before_delivery():
    current = {"ticker": "CURRENT", "signal_key": "scan-retry:1:CURRENT"}
    frozen = {"ticker": "FROZEN", "signal_key": "scan-old:1:FROZEN"}

    rows = _historical_publication_rows([current], [frozen, current])

    assert [row["ticker"] for row in rows] == ["CURRENT", "FROZEN"]


def test_radar_monitor_fails_closed_for_unbound_cross_scan_selection():
    selection = {
        "scan_id": "scan-retry",
        "source_scan_id": "scan-old",
        "signal_id": "scan-old:1:NOVA",
        "ticker": "NOVA",
        "payload_json": {"signal": {"signal_id": "scan-old:1:NOVA", "ticker": "NOVA"}},
    }

    with pytest.raises(SnapshotValidationError, match="frozen-slate lineage"):
        _radar_monitor_signals([], [selection])


def test_empty_authoritative_slate_has_no_radar_notification_or_selection_rows(tmp_path):
    slate = build_ranked_research_slate(
        [],
        target=5,
        data_eligible=False,
        generated_at="2026-08-26T13:00:00+00:00",
        market_date="2026-08-26",
        scan_id="scan-empty",
    )
    event = NotificationEvent(
        event_key="alphaops:scan-empty:alpha_no_trade",
        title="Dawnstrike Alpha Check",
        body="No clean edge today.",
        channel_hint="alpha_no_trade",
    )
    store = SQLiteScanStore(tmp_path / "empty-slate.sqlite")

    rows, stats = _persist_research_radar_selections(
        store,
        scan_id="scan-empty",
        radar=list(slate["rows"]),
        slate=slate,
        selected_at="2026-08-26T13:00:00+00:00",
        event=event,
    )
    body = format_alpha_no_trade(
        reason="DATA_UNAVAILABLE",
        next_action="wait",
        research_signals=[],
        research_total=0,
    )

    assert slate["published_count"] == 0
    assert rows == []
    assert stats["inserted"] == 0
    assert store.load_signal_selections(cohort="research_radar") == []
    assert "Research slate: 0 of 0 shown" in body
    assert "No clean edge today." in body
    assert "No safe/current Tier 1 research rows were available: DATA_UNAVAILABLE" in body
    assert "1.5R research floor" not in body


def test_alpha_cycle_cli_fixture_persists_research_only_outputs(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    db_path = tmp_path / "alpha.sqlite"
    out_dir = tmp_path / "alpha"

    exit_code = main([
        "alpha-cycle",
        "--config",
        str(FIXTURE_CONFIG),
        "--db-path",
        str(db_path),
        "--out-dir",
        str(out_dir),
        "--notify",
        "console",
        "--dry-run",
    ])
    status_code = main(["alpha-status", "--db-path", str(db_path)])
    report_code = main([
        "alpha-report",
        "--db-path",
        str(db_path),
        "--out-dir",
        str(out_dir / "report"),
    ])

    status = SQLiteScanStore(db_path).load_alpha_signals(limit=10)
    store = SQLiteScanStore(db_path)
    slate = json.loads((out_dir / "ranked_research_slate.json").read_text(encoding="utf-8"))
    notification = next(
        item
        for item in store.load_recent_notifications()
        if item["event_key"].endswith(":alpha_no_trade:console")
    )
    assert exit_code == 0
    assert status_code == 0
    assert report_code == 0
    assert status
    assert all("buy" not in str(row).lower() and "sell" not in str(row).lower() for row in status)
    assert (out_dir / "alpha_cycle.json").exists()
    assert (out_dir / "report" / "alpha_report.json").exists()
    assert (
        f"Research slate: {slate['published_count']} of {slate['target_count']} shown"
        in notification["body"]
    )
    assert notification["body"].count("Slate shortfall reason:") == (
        1 if slate["slate_shortfall_reason"] else 0
    )
