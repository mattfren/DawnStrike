from pathlib import Path

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
from intraday_scanner.notifiers.telegram_formatter import (
    format_alpha_monitor,
    format_alpha_no_trade,
    format_alpha_summary,
    format_alpha_watch,
)
from intraday_scanner.services.alpha_cycle_service import alpha_monitor
from intraday_scanner.services.signal_review_service import monitor_alpha_signals
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


def test_no_trade_filter_allows_no_clean_edge():
    decision = evaluate_no_trade([
        {"ticker": "HALT", "can_alert": False, "alpha_score": 0, "no_trade_reason": "current_halt"}
    ])

    assert decision.no_trade is True
    assert "current halt" in decision.reason
    assert "Do not force" in decision.next_action


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
    assert exit_code == 0
    assert status_code == 0
    assert report_code == 0
    assert status
    assert all("buy" not in str(row).lower() and "sell" not in str(row).lower() for row in status)
    assert (out_dir / "alpha_cycle.json").exists()
    assert (out_dir / "report" / "alpha_report.json").exists()
