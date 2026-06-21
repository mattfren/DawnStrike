from __future__ import annotations

from dataclasses import replace

from intraday_scanner.cli import main
from intraday_scanner.config import ScannerConfig
from intraday_scanner.notifiers.telegram_formatter import format_morning_watchlist
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.scoring import score_snapshot, score_universe
from intraday_scanner.services.premarket_intelligence import (
    ACTION_AVOID,
    ACTION_NEEDS_CONFIRMATION,
    ACTION_OPENING_BREAKOUT,
    ACTION_WATCH_ONLY,
    classify_catalyst,
    evaluate_intelligence_outcomes,
    probability_summary,
    write_intelligence_outcome_outputs,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _rows_by_ticker():
    return {
        row.ticker: row
        for row in read_snapshot_csv("sample_data/premarket_snapshot_sample.csv")
    }


def test_catalyst_tiering_keywords_and_risk_flags():
    assert classify_catalyst("FDA approval for Phase 2 data").catalyst_tier == "A"
    assert classify_catalyst("Company announces product launch").catalyst_tier == "B"

    promoted = classify_catalyst("Sponsored paid promotion social media rumor")

    assert promoted.catalyst_tier == "C"
    assert promoted.catalyst_category == "sympathy_momentum"
    assert "paid_promotion_style" in promoted.catalyst_risk_flags
    assert classify_catalyst("", has_news=False).catalyst_summary == "No clear catalyst"
    assert classify_catalyst("ATM offering with warrants").catalyst_category == "dilution_risk"


def test_trade_classification_assigns_action_to_every_ticker():
    result = score_universe(
        read_snapshot_csv("sample_data/premarket_snapshot_sample.csv"), ScannerConfig()
    )
    rows = {candidate.ticker: candidate.to_dict() for candidate in result.all_candidates}

    assert all(row["action"] for row in rows.values())
    assert rows["NOVA"]["action"] == ACTION_OPENING_BREAKOUT
    assert rows["HALT"]["action"] == ACTION_AVOID
    assert rows["OFFER"]["action"] == ACTION_AVOID
    assert rows["MOON"]["action"] == ACTION_WATCH_ONLY


def test_float_rotation_structure_and_liquidity_risk_fields():
    rows = _rows_by_ticker()
    nova = score_snapshot(rows["NOVA"], ScannerConfig()).to_dict()
    wide = score_snapshot(rows["WIDE"], ScannerConfig()).to_dict()

    assert nova["float_rotation"] == 0.0833
    assert nova["float_rotation_label"] == "low pressure"
    assert nova["premarket_structure"] == "strong"
    assert "wide_spread" in wide["risk_flags"]
    assert wide["premarket_structure"] == "mixed"
    assert wide["action"] == ACTION_NEEDS_CONFIRMATION


def test_opening_plan_never_blind_buys_and_weak_setups_are_downgraded():
    rows = _rows_by_ticker()
    nova = score_snapshot(rows["NOVA"], ScannerConfig()).to_dict()
    thin = score_snapshot(rows["THIN"], ScannerConfig()).to_dict()

    combined = " ".join(
        str(nova.get(key, "")) for key in ("entry_trigger", "why_this_matters", "do_not_enter_if")
    ).lower()
    assert "buy now" not in combined
    assert "premarket buy" not in combined
    assert "confirmation" in str(nova["entry_trigger"]).lower()
    assert thin["action"] == ACTION_AVOID
    assert thin["entry_trigger"] == "Manual review only; setup is not eligible"


def test_missing_enrichment_fallback_and_data_quality_warnings():
    base = _rows_by_ticker()["NOVA"]
    missing = replace(
        base,
        ticker="MISS",
        float_shares=None,
        has_news=False,
        catalyst_headline="",
        coverage_warning="missing_float;missing_news",
        missing_enrichment_count=2,
    )

    scored = score_snapshot(missing, ScannerConfig()).to_dict()

    assert scored["float_rotation"] == ""
    assert scored["float_rotation_label"] == "unknown"
    assert "missing_float_data" in scored["data_warnings"]
    assert "weak_or_missing_catalyst" in scored["data_warnings"]
    assert scored["data_confidence_score"] < 100


def test_telegram_watchlist_uses_simple_emoji_operational_format():
    result = score_universe(
        read_snapshot_csv("sample_data/premarket_snapshot_sample.csv"), ScannerConfig()
    )
    body = format_morning_watchlist(
        ranked=[candidate.to_dict() for candidate in result.ranked_candidates],
        avoid=[candidate.to_dict() for candidate in result.avoid_list],
        source_summary={"attempts": []},
    )

    assert "🚀 Dawnstrike Watchlist" in body
    assert "1) NOVA" in body
    assert "🎯" in body
    assert "🛑" in body
    assert "🚫 Avoid:" in body
    assert "Plan:" not in body
    assert "Targets:" not in body
    assert "Avoid if:" not in body
    assert "buy now" not in body.lower()


def test_probability_summary_does_not_fake_sparse_history():
    sparse = probability_summary([{"target_1_hit": True}], min_samples=2)
    enough = probability_summary(
        [
            {
                "target_1_hit": True,
                "max_gain_after_trigger_pct": 10,
                "max_drawdown_after_trigger_pct": -3,
            },
            {
                "target_1_hit": False,
                "max_gain_after_trigger_pct": 2,
                "max_drawdown_after_trigger_pct": -6,
            },
        ],
        min_samples=2,
    )

    assert sparse["historical_win_rate"] == "insufficient sample size"
    assert enough["historical_win_rate"] == 50.0
    assert enough["similar_setup_count"] == 2


def test_scoring_shows_probability_when_enough_similar_history_exists():
    row = _rows_by_ticker()["NOVA"]
    history = [
        {
            "predicted_action": ACTION_OPENING_BREAKOUT,
            "catalyst_tier": "A",
            "premarket_structure": "strong",
            "risk_level": "low",
            "target_1_hit": index % 2 == 0,
            "target_2_hit": False,
            "max_gain_after_trigger_pct": 6,
            "max_drawdown_after_trigger_pct": -2,
        }
        for index in range(20)
    ]

    scored = score_snapshot(row, ScannerConfig(), historical_outcomes=history).to_dict()

    assert scored["similar_setup_count"] == 20
    assert scored["historical_win_rate"] == 50.0
    assert scored["average_max_gain"] == 6.0
    assert scored["average_drawdown"] == -2.0


def test_historical_outcome_evaluator_persists_classification_results(tmp_path):
    store = SQLiteScanStore(tmp_path / "scanner.sqlite")
    result = score_universe(
        read_snapshot_csv("sample_data/premarket_snapshot_sample.csv"), ScannerConfig()
    )
    store.persist_scan_result(result)
    run_id = result.run_id
    store.persist_manual_outcomes(
        [
            {
                "outcome_key": f"{run_id}:NOVA:2026-06-18:2026-06-18T09:45:00-04:00",
                "scan_id": run_id,
                "ticker": "NOVA",
                "recommendation_timestamp": result.created_at,
                "uploaded_at": "2026-06-18T16:05:00-04:00",
                "entry_time": "2026-06-18T09:45:00-04:00",
                "entry_price": "5.40",
                "close_price": "7.70",
                "high_after_entry": "8.10",
                "low_after_entry": "5.05",
                "source": "manual_outcome_upload",
            }
        ],
        replace=False,
    )

    evaluation = evaluate_intelligence_outcomes(store=store, run_id=run_id, persist=True)
    paths = write_intelligence_outcome_outputs(evaluation, tmp_path / "outcomes")
    stored_rows = store.load_intelligence_outcomes()
    stored_summary = store.load_latest_intelligence_outcome_summary()

    assert evaluation["summary"]["evaluated_count"] == 1
    assert evaluation["rows"][0]["ticker"] == "NOVA"
    assert evaluation["rows"][0]["classification"] == ACTION_OPENING_BREAKOUT
    assert evaluation["rows"][0]["breakout_triggered"] is True
    assert stored_rows[0]["actual_outcome"] in {"target_1_hit", "target_2_hit"}
    assert stored_summary is not None
    assert paths["rows"].exists()
    assert paths["summary"].exists()


def test_cli_evaluate_intelligence_outcomes_command(tmp_path, capsys):
    db_path = tmp_path / "scanner.sqlite"
    scan_out = tmp_path / "scan"
    eval_out = tmp_path / "eval"

    assert (
        main(
            [
                "scan",
                "--snapshot",
                "sample_data/premarket_snapshot_sample.csv",
                "--out-dir",
                str(scan_out),
                "--db-path",
                str(db_path),
                "--persist",
            ]
        )
        == 0
    )
    store = SQLiteScanStore(db_path)
    scan = store.load_latest_scan()
    assert scan is not None
    run_id = str(scan["run_id"])
    store.persist_manual_outcomes(
        [
            {
                "outcome_key": f"{run_id}:NOVA:2026-06-18:2026-06-18T09:45:00-04:00",
                "scan_id": run_id,
                "ticker": "NOVA",
                "recommendation_timestamp": scan["summary"]["created_at"],
                "uploaded_at": "2026-06-18T16:05:00-04:00",
                "entry_time": "2026-06-18T09:45:00-04:00",
                "entry_price": "5.40",
                "close_price": "7.70",
                "high_after_entry": "8.10",
                "low_after_entry": "5.05",
                "source": "manual_outcome_upload",
            }
        ],
        replace=False,
    )

    assert (
        main(
            [
                "evaluate-intelligence-outcomes",
                "--db-path",
                str(db_path),
                "--out-dir",
                str(eval_out),
                "--persist",
            ]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert '"evaluated_count": 1' in out
    assert (eval_out / "intelligence_outcomes.csv").exists()
    assert (eval_out / "intelligence_outcome_summary.json").exists()
