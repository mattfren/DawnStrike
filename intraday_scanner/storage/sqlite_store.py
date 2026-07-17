"""SQLite storage adapter."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from intraday_scanner.errors import StorageError
from intraday_scanner.models import ScanResult


class SQLiteScanStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS scan_runs (
                        id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        config_json TEXT NOT NULL,
                        summary_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS candidates (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        rank INTEGER NOT NULL,
                        ticker TEXT NOT NULL,
                        score REAL NOT NULL,
                        is_avoid INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(run_id) REFERENCES scan_runs(id)
                    );
                    CREATE TABLE IF NOT EXISTS snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        as_of_timestamp TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(run_id) REFERENCES scan_runs(id)
                    );
                    CREATE TABLE IF NOT EXISTS raw_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        as_of_timestamp TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(run_id) REFERENCES scan_runs(id)
                    );
                    CREATE TABLE IF NOT EXISTS ranked_candidates (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        rank INTEGER NOT NULL,
                        ticker TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(run_id) REFERENCES scan_runs(id)
                    );
                    CREATE TABLE IF NOT EXISTS top_explosive (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        rank INTEGER NOT NULL,
                        ticker TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(run_id) REFERENCES scan_runs(id)
                    );
                    CREATE TABLE IF NOT EXISTS avoid_list (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        rank INTEGER NOT NULL,
                        ticker TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(run_id) REFERENCES scan_runs(id)
                    );
                    CREATE TABLE IF NOT EXISTS paper_audit_trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT,
                        ticker TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS paper_audit_summary (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS notifications_sent (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_key TEXT NOT NULL UNIQUE,
                        run_id TEXT,
                        ticker TEXT,
                        channel TEXT NOT NULL,
                        sent_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS setup_monitor_checks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT,
                        ticker TEXT NOT NULL,
                        status TEXT NOT NULL,
                        checked_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS recommendation_theses (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        rank INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS monitor_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT,
                        ticker TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS alerts_sent (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        alert_key TEXT NOT NULL UNIQUE,
                        run_id TEXT,
                        ticker TEXT,
                        event_type TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        sent_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS performance_daily (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        report_date TEXT NOT NULL,
                        run_id TEXT,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS performance_cumulative (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS provider_health (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        provider TEXT NOT NULL,
                        status TEXT NOT NULL,
                        checked_at TEXT NOT NULL,
                        detail TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS web_fetch_runs (
                        id TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        source_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        url TEXT,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS web_fetch_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        status TEXT NOT NULL,
                        row_count INTEGER NOT NULL,
                        artifact_path TEXT,
                        failure_reason TEXT,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS source_health (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source TEXT NOT NULL,
                        status TEXT NOT NULL,
                        checked_at TEXT NOT NULL,
                        detail TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS raw_source_artifacts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        artifact_kind TEXT NOT NULL,
                        path TEXT NOT NULL,
                        content_type TEXT,
                        byte_count INTEGER NOT NULL,
                        sha256 TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS normalized_source_rows (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        as_of_timestamp TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS halt_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_key TEXT NOT NULL UNIQUE,
                        ticker TEXT NOT NULL,
                        event_time TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS sec_risk_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_key TEXT NOT NULL UNIQUE,
                        ticker TEXT NOT NULL,
                        filed_at TEXT NOT NULL,
                        form_type TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS ai_research_runs (
                        id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS ai_research_outputs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        classification TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS ai_data_warnings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        ticker TEXT,
                        warning TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS manual_snapshot_uploads (
                        id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        input_path TEXT NOT NULL,
                        output_path TEXT NOT NULL,
                        row_count INTEGER NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS manual_snapshot_rows (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        upload_id TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        as_of_timestamp TEXT NOT NULL,
                        raw_json TEXT NOT NULL,
                        normalized_json TEXT NOT NULL,
                        FOREIGN KEY(upload_id) REFERENCES manual_snapshot_uploads(id)
                    );
                    CREATE TABLE IF NOT EXISTS manual_outcomes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        outcome_key TEXT NOT NULL UNIQUE,
                        scan_id TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        recommendation_timestamp TEXT NOT NULL,
                        uploaded_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS manual_audit_trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scan_id TEXT,
                        ticker TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS manual_audit_summary (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS intelligence_outcomes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT,
                        ticker TEXT NOT NULL,
                        evaluated_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_intelligence_outcomes_run_ticker
                    ON intelligence_outcomes(run_id, ticker);
                    CREATE TABLE IF NOT EXISTS intelligence_outcome_summary (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS shadow_reports (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS screener_automation_runs (
                        id TEXT PRIMARY KEY,
                        file_hash TEXT NOT NULL UNIQUE,
                        input_path TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        raw_archive_path TEXT,
                        normalized_path TEXT,
                        out_dir TEXT,
                        scan_run_id TEXT,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS automation_runs (
                        id TEXT PRIMARY KEY,
                        run_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        out_dir TEXT,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS alpha_feature_vectors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scan_id TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        model_version TEXT NOT NULL,
                        config_hash TEXT NOT NULL,
                        feature_json TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_alpha_features_scan_ticker
                    ON alpha_feature_vectors(scan_id, ticker);
                    CREATE TABLE IF NOT EXISTS alpha_signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        signal_key TEXT NOT NULL UNIQUE,
                        scan_id TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        rank INTEGER NOT NULL,
                        timestamp TEXT NOT NULL,
                        alpha_score REAL NOT NULL,
                        edge_bucket TEXT NOT NULL,
                        confidence_bucket TEXT NOT NULL,
                        can_alert INTEGER NOT NULL,
                        no_trade_reason TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_alpha_signals_scan_rank
                    ON alpha_signals(scan_id, rank);
                    CREATE TABLE IF NOT EXISTS alpha_outcome_labels (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        label_key TEXT NOT NULL UNIQUE,
                        scan_id TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS alpha_learning_runs (
                        id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        summary_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS alpha_source_reliability (
                        source TEXT PRIMARY KEY,
                        updated_at TEXT NOT NULL,
                        runs INTEGER NOT NULL,
                        rows_returned INTEGER NOT NULL,
                        rows_normalized INTEGER NOT NULL,
                        rows_rejected INTEGER NOT NULL,
                        stale_count INTEGER NOT NULL,
                        missing_critical_count INTEGER NOT NULL,
                        outcome_count INTEGER NOT NULL,
                        winner_count INTEGER NOT NULL,
                        reliability_score REAL NOT NULL,
                        summary_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS alpha_setup_memory (
                        setup_key TEXT PRIMARY KEY,
                        updated_at TEXT NOT NULL,
                        sample_size INTEGER NOT NULL,
                        avg_return_pct REAL NOT NULL,
                        median_return_pct REAL NOT NULL,
                        win_rate_pct REAL NOT NULL,
                        max_drawdown_pct REAL NOT NULL,
                        outlier_dependency REAL NOT NULL,
                        summary_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS historical_signals (
                        signal_id TEXT PRIMARY KEY,
                        scan_id TEXT,
                        alpha_signal_id TEXT,
                        generated_at TEXT NOT NULL,
                        market_date TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        company TEXT,
                        rank INTEGER,
                        source TEXT,
                        source_url TEXT,
                        source_confidence REAL,
                        data_source_kind TEXT,
                        model_version TEXT,
                        config_hash TEXT,
                        primary_setup TEXT,
                        setup_grade TEXT,
                        signal_label TEXT NOT NULL,
                        entry_watch_level REAL,
                        entry_trigger_type TEXT,
                        entry_condition TEXT,
                        confirmation_condition TEXT,
                        exit_line REAL,
                        invalidation_level REAL,
                        target_1 REAL,
                        target_2 REAL,
                        risk_flags_json TEXT NOT NULL,
                        avoid_reasons_json TEXT NOT NULL,
                        catalyst_summary TEXT,
                        telegram_event_key TEXT,
                        was_alerted INTEGER NOT NULL DEFAULT 0,
                        no_trade_reason TEXT,
                        raw_payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_historical_signals_day_rank
                    ON historical_signals(market_date, rank);
                    CREATE INDEX IF NOT EXISTS idx_historical_signals_scan_ticker
                    ON historical_signals(scan_id, ticker);
                    CREATE TABLE IF NOT EXISTS signal_events (
                        event_id TEXT PRIMARY KEY,
                        signal_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        event_timestamp TEXT NOT NULL,
                        event_price REAL,
                        source TEXT,
                        notes TEXT,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(signal_id) REFERENCES historical_signals(signal_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_signal_events_signal_time
                    ON signal_events(signal_id, event_timestamp);
                    CREATE TABLE IF NOT EXISTS signal_outcomes (
                        signal_id TEXT PRIMARY KEY,
                        market_date TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        outcome_source TEXT NOT NULL,
                        entry_time TEXT,
                        entry_price REAL,
                        price_1m REAL,
                        price_5m REAL,
                        price_15m REAL,
                        lunch_price REAL,
                        close_price REAL,
                        high_after_entry REAL,
                        low_after_entry REAL,
                        halted INTEGER,
                        notes TEXT,
                        imported_at TEXT NOT NULL,
                        validated_against_signal_timestamp INTEGER NOT NULL,
                        outcome_status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(signal_id) REFERENCES historical_signals(signal_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_signal_outcomes_day_ticker
                    ON signal_outcomes(market_date, ticker);
                    CREATE TABLE IF NOT EXISTS price_observations (
                        observation_id TEXT PRIMARY KEY,
                        signal_id TEXT,
                        market_date TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        requested_at TEXT NOT NULL,
                        observed_at TEXT,
                        price REAL,
                        price_type TEXT NOT NULL,
                        source TEXT NOT NULL,
                        source_kind TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        provider_status TEXT NOT NULL,
                        freshness_seconds INTEGER,
                        tolerance_seconds INTEGER NOT NULL,
                        is_usable INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS trade_intents (
                        intent_id TEXT PRIMARY KEY,
                        signal_id TEXT,
                        market_date TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        lifecycle_state TEXT NOT NULL,
                        action TEXT NOT NULL,
                        decision_time TEXT NOT NULL,
                        decision_price REAL,
                        trigger_price REAL,
                        stop_price REAL,
                        target_price REAL,
                        quantity REAL,
                        notional REAL,
                        risk_amount REAL,
                        reason TEXT NOT NULL,
                        blocked_reason TEXT,
                        source_observation_id TEXT,
                        notification_event_key TEXT,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_trade_intents_day_ticker
                    ON trade_intents(market_date, ticker, decision_time);
                    CREATE INDEX IF NOT EXISTS idx_trade_intents_signal
                    ON trade_intents(signal_id, decision_time);
                    CREATE TABLE IF NOT EXISTS paper_positions (
                        position_id TEXT PRIMARY KEY,
                        signal_id TEXT,
                        market_date TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        status TEXT NOT NULL,
                        quantity REAL NOT NULL,
                        entry_intent_id TEXT,
                        exit_intent_id TEXT,
                        opened_at TEXT,
                        closed_at TEXT,
                        entry_price REAL,
                        exit_price REAL,
                        stop_price REAL,
                        target_price REAL,
                        notional REAL,
                        realized_pnl REAL,
                        realized_return_pct REAL,
                        max_favorable_excursion REAL,
                        max_adverse_excursion REAL,
                        updated_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_paper_positions_day_status
                    ON paper_positions(market_date, status, ticker);
                    CREATE TABLE IF NOT EXISTS paper_trade_fills (
                        fill_id TEXT PRIMARY KEY,
                        position_id TEXT NOT NULL,
                        intent_id TEXT NOT NULL,
                        signal_id TEXT,
                        market_date TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        side TEXT NOT NULL,
                        fill_time TEXT NOT NULL,
                        fill_price REAL NOT NULL,
                        quantity REAL NOT NULL,
                        gross_notional REAL NOT NULL,
                        slippage_bps REAL NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_paper_fills_day_ticker
                    ON paper_trade_fills(market_date, ticker, fill_time);
                    CREATE TABLE IF NOT EXISTS signal_return_attribution (
                        attribution_id TEXT PRIMARY KEY,
                        signal_id TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        market_date TEXT NOT NULL,
                        entry_policy TEXT NOT NULL,
                        exit_policy TEXT NOT NULL,
                        entry_price REAL,
                        exit_price REAL,
                        return_pct REAL,
                        max_favorable_excursion REAL,
                        max_adverse_excursion REAL,
                        drawdown_pct REAL,
                        hit_target_1 INTEGER,
                        hit_target_2 INTEGER,
                        hit_invalidation INTEGER,
                        trigger_activated INTEGER,
                        audit_status TEXT NOT NULL,
                        scenario_or_recommended TEXT NOT NULL,
                        calculated_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(signal_id) REFERENCES historical_signals(signal_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_signal_return_attr_day_rank
                    ON signal_return_attribution(market_date, ticker);
                    CREATE TABLE IF NOT EXISTS daily_signal_performance (
                        market_date TEXT PRIMARY KEY,
                        signal_count INTEGER NOT NULL,
                        alerted_count INTEGER NOT NULL,
                        no_trade_count INTEGER NOT NULL,
                        audited_count INTEGER NOT NULL,
                        missing_outcome_count INTEGER NOT NULL,
                        top1_return REAL,
                        top3_return REAL,
                        top5_return REAL,
                        top1_close_return REAL,
                        top3_close_return REAL,
                        top5_close_return REAL,
                        top1_lunch_return REAL,
                        top3_lunch_return REAL,
                        top5_lunch_return REAL,
                        best_pick_return REAL,
                        worst_pick_return REAL,
                        max_drawdown REAL,
                        hit_rate REAL,
                        outcome_coverage_pct REAL,
                        evidence_status TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS prediction_runs (
                        prediction_run_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        market_date TEXT NOT NULL,
                        model_version TEXT NOT NULL,
                        config_hash TEXT NOT NULL,
                        prediction_mode TEXT NOT NULL,
                        training_start_date TEXT,
                        training_end_date TEXT,
                        test_start_date TEXT,
                        test_end_date TEXT,
                        sample_size INTEGER NOT NULL,
                        real_day_count INTEGER NOT NULL,
                        outcome_label_count INTEGER NOT NULL,
                        data_quality_summary_json TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_prediction_runs_date
                    ON prediction_runs(market_date, created_at);
                    CREATE TABLE IF NOT EXISTS candidate_predictions (
                        prediction_id TEXT PRIMARY KEY,
                        prediction_run_id TEXT NOT NULL,
                        signal_id TEXT,
                        scan_id TEXT,
                        market_date TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        rank INTEGER,
                        primary_setup TEXT,
                        source TEXT,
                        source_confidence REAL,
                        alpha_score REAL,
                        launch_bucket TEXT,
                        probability_positive_1m REAL,
                        probability_positive_5m REAL,
                        probability_positive_15m REAL,
                        probability_positive_lunch REAL,
                        probability_positive_close REAL,
                        probability_hit_target_1 REAL,
                        probability_hit_target_2 REAL,
                        probability_hit_invalidation REAL,
                        expected_return_1m REAL,
                        expected_return_5m REAL,
                        expected_return_15m REAL,
                        expected_return_lunch REAL,
                        expected_return_close REAL,
                        expected_max_drawdown REAL,
                        expected_mfe REAL,
                        expected_mae REAL,
                        expected_value_score REAL,
                        uncertainty_bucket TEXT,
                        confidence_interval_low REAL,
                        confidence_interval_high REAL,
                        calibration_bucket TEXT,
                        prediction_status TEXT NOT NULL,
                        no_alert_reason TEXT,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(prediction_run_id) REFERENCES prediction_runs(prediction_run_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_candidate_predictions_run_rank
                    ON candidate_predictions(prediction_run_id, rank);
                    CREATE INDEX IF NOT EXISTS idx_candidate_predictions_date_ticker
                    ON candidate_predictions(market_date, ticker);
                    CREATE TABLE IF NOT EXISTS prediction_calibration (
                        calibration_id TEXT PRIMARY KEY,
                        prediction_run_id TEXT,
                        model_version TEXT NOT NULL,
                        evaluated_at TEXT NOT NULL,
                        horizon TEXT NOT NULL,
                        sample_size INTEGER NOT NULL,
                        brier_score REAL,
                        calibration_error REAL,
                        average_predicted_probability REAL,
                        actual_hit_rate REAL,
                        bucket_json TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_prediction_calibration_model
                    ON prediction_calibration(model_version, evaluated_at);
                    CREATE TABLE IF NOT EXISTS portfolio_expectancy (
                        id TEXT PRIMARY KEY,
                        prediction_run_id TEXT NOT NULL,
                        market_date TEXT NOT NULL,
                        portfolio_type TEXT NOT NULL,
                        expected_return_1m REAL,
                        expected_return_5m REAL,
                        expected_return_15m REAL,
                        expected_return_lunch REAL,
                        expected_return_close REAL,
                        expected_drawdown REAL,
                        probability_positive REAL,
                        uncertainty_bucket TEXT,
                        sample_size INTEGER NOT NULL,
                        evidence_status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(prediction_run_id) REFERENCES prediction_runs(prediction_run_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_portfolio_expectancy_run
                    ON portfolio_expectancy(prediction_run_id, portfolio_type);
                    CREATE TABLE IF NOT EXISTS daily_market_movers (
                        mover_id TEXT PRIMARY KEY,
                        market_date TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        company TEXT,
                        rank INTEGER,
                        price REAL,
                        change_pct REAL,
                        volume REAL,
                        dollar_volume REAL,
                        high REAL,
                        low REAL,
                        open REAL,
                        close REAL,
                        source TEXT,
                        source_url TEXT,
                        source_confidence REAL,
                        extracted_at TEXT NOT NULL,
                        data_quality TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_daily_market_movers_date_rank
                    ON daily_market_movers(market_date, rank);
                    CREATE INDEX IF NOT EXISTS idx_daily_market_movers_date_ticker
                    ON daily_market_movers(market_date, ticker);
                    CREATE TABLE IF NOT EXISTS daily_review_runs (
                        review_id TEXT PRIMARY KEY,
                        market_date TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        source_status TEXT NOT NULL,
                        mover_count INTEGER NOT NULL,
                        signal_count INTEGER NOT NULL,
                        matched_pick_count INTEGER NOT NULL,
                        missed_winner_count INTEGER NOT NULL,
                        false_positive_count INTEGER NOT NULL,
                        correct_avoid_count INTEGER NOT NULL,
                        missing_outcome_count INTEGER NOT NULL,
                        review_status TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_daily_review_runs_date
                    ON daily_review_runs(market_date, created_at);
                    CREATE TABLE IF NOT EXISTS daily_review_items (
                        item_id TEXT PRIMARY KEY,
                        review_id TEXT NOT NULL,
                        market_date TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        category TEXT NOT NULL,
                        dawnstrike_rank INTEGER,
                        mover_rank INTEGER,
                        alpha_score REAL,
                        setup TEXT,
                        source TEXT,
                        catalyst_category TEXT,
                        risk_flags_json TEXT NOT NULL,
                        avoid_reasons_json TEXT NOT NULL,
                        why_picked TEXT,
                        why_missed TEXT,
                        what_happened TEXT,
                        lesson TEXT,
                        backfeed_action TEXT,
                        return_1m REAL,
                        return_5m REAL,
                        return_15m REAL,
                        return_lunch REAL,
                        return_close REAL,
                        high_opportunity_return REAL,
                        drawdown_pct REAL,
                        miss_reason TEXT,
                        missed_at_stage TEXT,
                        missed_feature_gap TEXT,
                        could_have_been_caught INTEGER,
                        catchability TEXT,
                        failure_reason TEXT,
                        success_reason TEXT,
                        review_grade TEXT,
                        audit_status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(review_id) REFERENCES daily_review_runs(review_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_daily_review_items_run
                    ON daily_review_items(review_id, category);
                    CREATE INDEX IF NOT EXISTS idx_daily_review_items_date_ticker
                    ON daily_review_items(market_date, ticker);
                    CREATE TABLE IF NOT EXISTS learning_backfeed_events (
                        event_id TEXT PRIMARY KEY,
                        review_id TEXT NOT NULL,
                        market_date TEXT NOT NULL,
                        ticker TEXT,
                        event_type TEXT NOT NULL,
                        target TEXT NOT NULL,
                        before_value REAL,
                        suggested_adjustment REAL,
                        confidence REAL NOT NULL,
                        sample_size INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        applied INTEGER NOT NULL,
                        applied_at TEXT,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(review_id) REFERENCES daily_review_runs(review_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_learning_backfeed_review
                    ON learning_backfeed_events(review_id, event_type);
                    """
                )
                _ensure_columns(
                    connection,
                    "daily_review_items",
                    {
                        "miss_reason": "TEXT",
                        "missed_at_stage": "TEXT",
                        "missed_feature_gap": "TEXT",
                        "could_have_been_caught": "INTEGER",
                        "catchability": "TEXT",
                        "failure_reason": "TEXT",
                        "success_reason": "TEXT",
                        "review_grade": "TEXT",
                    },
                )
                _ensure_columns(
                    connection,
                    "price_observations",
                    {
                        "observation_id": "TEXT",
                        "signal_id": "TEXT",
                        "market_date": "TEXT NOT NULL DEFAULT ''",
                        "ticker": "TEXT NOT NULL DEFAULT ''",
                        "requested_at": "TEXT NOT NULL DEFAULT ''",
                        "observed_at": "TEXT",
                        "price": "REAL",
                        "price_type": "TEXT NOT NULL DEFAULT 'last_bar_close'",
                        "source": "TEXT NOT NULL DEFAULT ''",
                        "source_kind": "TEXT NOT NULL DEFAULT ''",
                        "provider": "TEXT NOT NULL DEFAULT ''",
                        "provider_status": "TEXT NOT NULL DEFAULT ''",
                        "freshness_seconds": "INTEGER",
                        "tolerance_seconds": "INTEGER NOT NULL DEFAULT 0",
                        "is_usable": "INTEGER NOT NULL DEFAULT 0",
                        "created_at": "TEXT NOT NULL DEFAULT ''",
                        "payload_json": "TEXT NOT NULL DEFAULT '{}'",
                    },
                )
                connection.executescript(
                    """
                    CREATE INDEX IF NOT EXISTS idx_price_observations_date_ticker
                    ON price_observations(market_date, ticker, observed_at);
                    CREATE INDEX IF NOT EXISTS idx_price_observations_signal
                    ON price_observations(signal_id, observed_at);
                    CREATE INDEX IF NOT EXISTS idx_trade_intents_day_ticker
                    ON trade_intents(market_date, ticker, decision_time);
                    CREATE INDEX IF NOT EXISTS idx_trade_intents_signal
                    ON trade_intents(signal_id, decision_time);
                    CREATE INDEX IF NOT EXISTS idx_paper_positions_day_status
                    ON paper_positions(market_date, status, ticker);
                    CREATE INDEX IF NOT EXISTS idx_paper_fills_day_ticker
                    ON paper_trade_fills(market_date, ticker, fill_time);
                    """
                )
                from intraday_scanner.storage.migrations import run_migrations

                run_migrations(connection)
        except sqlite3.Error as exc:
            raise StorageError(f"Could not initialize SQLite store: {exc}") from exc

    def persist_scan_result(self, result: ScanResult) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO scan_runs
                    (id, created_at, source, config_json, summary_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        result.run_id,
                        result.created_at,
                        str(result.config.get("provider", "unknown")),
                        json.dumps(result.config, sort_keys=True),
                        json.dumps(result.summary(), sort_keys=True),
                    ),
                )
                connection.execute("DELETE FROM candidates WHERE run_id = ?", (result.run_id,))
                connection.execute("DELETE FROM snapshots WHERE run_id = ?", (result.run_id,))
                connection.execute("DELETE FROM raw_snapshots WHERE run_id = ?", (result.run_id,))
                connection.execute(
                    "DELETE FROM ranked_candidates WHERE run_id = ?", (result.run_id,)
                )
                connection.execute("DELETE FROM top_explosive WHERE run_id = ?", (result.run_id,))
                connection.execute("DELETE FROM avoid_list WHERE run_id = ?", (result.run_id,))
                for candidate in result.all_candidates:
                    payload = candidate.to_dict()
                    snapshot_payload = candidate.snapshot.to_dict()
                    connection.execute(
                        """
                        INSERT INTO candidates
                        (run_id, rank, ticker, score, is_avoid, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            result.run_id,
                            candidate.rank,
                            candidate.ticker,
                            candidate.score,
                            int(candidate.is_avoid),
                            json.dumps(payload, sort_keys=True),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO snapshots
                        (run_id, ticker, as_of_timestamp, payload_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            result.run_id,
                            candidate.ticker,
                            candidate.snapshot.as_of_timestamp,
                            json.dumps(snapshot_payload, sort_keys=True),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO raw_snapshots
                        (run_id, ticker, as_of_timestamp, payload_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            result.run_id,
                            candidate.ticker,
                            candidate.snapshot.as_of_timestamp,
                            json.dumps(snapshot_payload, sort_keys=True),
                        ),
                    )
                for candidate in result.ranked_candidates:
                    payload = candidate.to_dict()
                    connection.execute(
                        """
                        INSERT INTO ranked_candidates (run_id, rank, ticker, payload_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            result.run_id,
                            candidate.rank,
                            candidate.ticker,
                            json.dumps(payload, sort_keys=True),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO recommendation_theses
                        (run_id, ticker, rank, created_at, payload_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            result.run_id,
                            candidate.ticker,
                            candidate.rank,
                            result.created_at,
                            json.dumps(_recommendation_payload(payload, result), sort_keys=True),
                        ),
                    )
                for candidate in result.top_explosive:
                    connection.execute(
                        """
                        INSERT INTO top_explosive (run_id, rank, ticker, payload_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            result.run_id,
                            candidate.rank,
                            candidate.ticker,
                            json.dumps(candidate.to_dict(), sort_keys=True),
                        ),
                    )
                for candidate in result.avoid_list:
                    connection.execute(
                        """
                        INSERT INTO avoid_list (run_id, rank, ticker, payload_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            result.run_id,
                            candidate.rank,
                            candidate.ticker,
                            json.dumps(candidate.to_dict(), sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist scan result: {exc}") from exc

    def persist_paper_audit(
        self, summary: dict[str, Any], trades: list[dict[str, Any]], run_id: str | None = None
    ) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                for trade in trades:
                    connection.execute(
                        """
                        INSERT INTO paper_audit_trades (run_id, ticker, payload_json)
                        VALUES (?, ?, ?)
                        """,
                        (run_id, str(trade.get("ticker", "")), json.dumps(trade, sort_keys=True)),
                    )
                connection.execute(
                    """
                    INSERT INTO paper_audit_summary (run_id, created_at, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    (
                        run_id,
                        str(summary.get("created_at", "")),
                        json.dumps(summary, sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist paper audit: {exc}") from exc

    def load_latest_scan(self) -> dict[str, object] | None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                run = connection.execute(
                    "SELECT * FROM scan_runs ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if run is None:
                    return None
                run_id = str(run["id"])
                candidates = self._load_payloads(connection, "ranked_candidates", run_id)
                top = self._load_payloads(connection, "top_explosive", run_id)
                avoid = self._load_payloads(connection, "avoid_list", run_id)
                return {
                    "run_id": run_id,
                    "summary": json.loads(str(run["summary_json"])),
                    "config": json.loads(str(run["config_json"])),
                    "ranked_candidates": candidates,
                    "top_explosive": top,
                    "avoid_list": avoid,
                }
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load latest scan: {exc}") from exc

    def load_scan(self, run_id: str) -> dict[str, object] | None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                run = connection.execute(
                    "SELECT * FROM scan_runs WHERE id = ? LIMIT 1", (run_id,)
                ).fetchone()
                if run is None:
                    return None
                candidates = self._load_payloads(connection, "ranked_candidates", run_id)
                top = self._load_payloads(connection, "top_explosive", run_id)
                avoid = self._load_payloads(connection, "avoid_list", run_id)
                return {
                    "run_id": run_id,
                    "summary": json.loads(str(run["summary_json"])),
                    "config": json.loads(str(run["config_json"])),
                    "ranked_candidates": candidates,
                    "top_explosive": top,
                    "avoid_list": avoid,
                }
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load scan: {exc}") from exc

    def load_scan_history(self, limit: int = 50) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT id, created_at, source, summary_json
                    FROM scan_runs
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [
                    {
                        "run_id": str(row["id"]),
                        "created_at": str(row["created_at"]),
                        "source": str(row["source"]),
                        **json.loads(str(row["summary_json"])),
                    }
                    for row in rows
                ]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load scan history: {exc}") from exc

    def persist_monitor_checks(
        self, rows: list[dict[str, Any]], run_id: str | None = None
    ) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                for row in rows:
                    connection.execute(
                        """
                        INSERT INTO setup_monitor_checks
                        (run_id, ticker, status, checked_at, payload_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            str(row.get("ticker", "")),
                            str(row.get("status", "")),
                            str(row.get("checked_at", "")),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist setup monitor checks: {exc}") from exc

    def persist_monitor_events(
        self, rows: list[dict[str, Any]], run_id: str | None = None
    ) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                for row in rows:
                    connection.execute(
                        """
                        INSERT INTO monitor_events
                        (run_id, ticker, event_type, severity, created_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            str(row.get("ticker", "")),
                            str(row.get("event_type", "")),
                            str(row.get("severity", "")),
                            str(row.get("created_at", "")),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist monitor events: {exc}") from exc

    def load_recent_monitor_events(self, limit: int = 50) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM monitor_events
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load monitor events: {exc}") from exc

    def load_latest_monitor_checks(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                latest = connection.execute(
                    """
                    SELECT checked_at
                    FROM setup_monitor_checks
                    ORDER BY checked_at DESC
                    LIMIT 1
                    """
                ).fetchone()
                if latest is None:
                    return []
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM setup_monitor_checks
                    WHERE checked_at = ?
                    ORDER BY
                        CASE status
                            WHEN 'confirming' THEN 0
                            WHEN 'watching' THEN 1
                            WHEN 'extended' THEN 2
                            WHEN 'fading' THEN 3
                            WHEN 'invalidated' THEN 4
                            ELSE 5
                        END,
                        ticker ASC
                    LIMIT ?
                    """,
                    (str(latest["checked_at"]), limit),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load setup monitor checks: {exc}") from exc

    def has_notification(self, event_key: str) -> bool:
        self.initialize()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT 1 FROM notifications_sent WHERE event_key = ? LIMIT 1",
                    (event_key,),
                ).fetchone()
                return row is not None
        except sqlite3.Error as exc:
            raise StorageError(f"Could not check notification state: {exc}") from exc

    def record_notification(
        self,
        *,
        event_key: str,
        channel: str,
        payload: dict[str, Any],
        run_id: str | None = None,
        ticker: str | None = None,
    ) -> bool:
        self.initialize()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO notifications_sent
                    (event_key, run_id, ticker, channel, sent_at, payload_json)
                    VALUES (?, ?, ?, ?, datetime('now'), ?)
                    """,
                    (
                        event_key,
                        run_id,
                        ticker,
                        channel,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise StorageError(f"Could not record notification: {exc}") from exc

    def load_recent_notifications(self, limit: int = 50) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT event_key, run_id, ticker, channel, sent_at, payload_json
                    FROM notifications_sent
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                notifications = []
                for row in rows:
                    payload = json.loads(str(row["payload_json"]))
                    notifications.append(
                        {
                            "event_key": str(row["event_key"]),
                            "run_id": str(row["run_id"] or ""),
                            "ticker": str(row["ticker"] or ""),
                            "channel": str(row["channel"]),
                            "sent_at": str(row["sent_at"]),
                            **payload,
                        }
                    )
                return notifications
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load notifications: {exc}") from exc

    def load_notification(self, event_key: str) -> dict[str, Any] | None:
        """Load one exact channel-qualified notification record."""

        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    """
                    SELECT event_key, run_id, ticker, channel, sent_at, payload_json
                    FROM notifications_sent
                    WHERE event_key = ?
                    LIMIT 1
                    """,
                    (event_key,),
                ).fetchone()
                if row is None:
                    return None
                payload = _json_value(row["payload_json"])
                return {
                    "event_key": str(row["event_key"]),
                    "run_id": str(row["run_id"] or ""),
                    "ticker": str(row["ticker"] or ""),
                    "channel": str(row["channel"]),
                    "sent_at": str(row["sent_at"]),
                    **(payload if isinstance(payload, dict) else {}),
                }
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load notification: {exc}") from exc

    def persist_strategy_versions(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Persist immutable strategy definitions used to make selections."""

        self.initialize()
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    strategy_id = str(row.get("strategy_id") or "").strip()
                    strategy_version = str(row.get("strategy_version") or "").strip()
                    registered_at = str(row.get("registered_at") or "").strip()
                    if not strategy_id or not strategy_version or not registered_at:
                        skipped += 1
                        continue
                    definition = row.get("definition_json") or row.get("definition") or {}
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO strategy_versions
                        (strategy_id, strategy_version, registered_at, definition_json,
                         payload_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            strategy_id,
                            strategy_version,
                            registered_at,
                            json.dumps(definition, sort_keys=True),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
            return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist strategy versions: {exc}") from exc

    def load_strategy_versions(
        self,
        *,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if strategy_id:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        if strategy_version:
            clauses.append("strategy_version = ?")
            params.append(strategy_version)
        query = "SELECT * FROM strategy_versions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY registered_at DESC, strategy_id ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [
                    {
                        "strategy_id": str(row["strategy_id"]),
                        "strategy_version": str(row["strategy_version"]),
                        "registered_at": str(row["registered_at"]),
                        "definition_json": _json_value(row["definition_json"]),
                        "payload_json": _json_value(row["payload_json"]),
                    }
                    for row in rows
                ]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load strategy versions: {exc}") from exc

    def persist_signal_selections(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Persist an immutable, exact set of selected signal identities."""

        self.initialize()
        inserted = 0
        skipped = 0
        required = (
            "selection_id",
            "scan_id",
            "signal_id",
            "strategy_id",
            "strategy_version",
            "cohort",
            "decision",
            "selected_at",
            "event_key",
            "body_sha256",
        )
        try:
            with self._connect() as connection:
                for row in rows:
                    if any(not str(row.get(field) or "").strip() for field in required):
                        skipped += 1
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO signal_selections
                        (selection_id, scan_id, signal_id, ticker, rank, strategy_id,
                         strategy_version, cohort, decision, selected_at, event_key,
                         body_sha256, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row["selection_id"]),
                            str(row["scan_id"]),
                            str(row["signal_id"]),
                            str(row.get("ticker") or "").upper(),
                            _int_or_none(row.get("rank")),
                            str(row["strategy_id"]),
                            str(row["strategy_version"]),
                            str(row["cohort"]),
                            str(row["decision"]),
                            str(row["selected_at"]),
                            str(row["event_key"]),
                            str(row["body_sha256"]),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
            return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist signal selections: {exc}") from exc

    def load_signal_selections(
        self,
        *,
        scan_id: str | None = None,
        signal_id: str | None = None,
        event_key: str | None = None,
        strategy_id: str | None = None,
        cohort: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("scan_id", scan_id),
            ("signal_id", signal_id),
            ("event_key", event_key),
            ("strategy_id", strategy_id),
            ("cohort", cohort),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        query = "SELECT * FROM signal_selections"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY selected_at DESC, COALESCE(rank, 999999) ASC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_selection_identity_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load signal selections: {exc}") from exc

    def persist_notification_deliveries(
        self,
        rows: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Persist exact per-signal notification membership and delivery truth.

        A later dedupe observation never downgrades a membership already proven
        delivered. Reusing an event identity with a different body hash is rejected.
        """

        self.initialize()
        inserted = 0
        updated = 0
        skipped = 0
        required = (
            "membership_id",
            "scan_id",
            "signal_id",
            "strategy_id",
            "strategy_version",
            "cohort",
            "decision",
            "selected_at",
            "event_key",
            "channel",
            "delivery_status",
            "attempted_at",
            "body_sha256",
        )
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                for row in rows:
                    if any(not str(row.get(field) or "").strip() for field in required):
                        skipped += 1
                        continue
                    event_key = str(row["event_key"])
                    channel = str(row["channel"])
                    signal_id = str(row["signal_id"])
                    body_sha256 = str(row["body_sha256"])
                    existing = connection.execute(
                        """
                        SELECT * FROM notification_delivery_memberships
                        WHERE event_key = ? AND channel = ? AND signal_id = ?
                        LIMIT 1
                        """,
                        (event_key, channel, signal_id),
                    ).fetchone()
                    payload = dict(row.get("payload_json") or row)
                    incoming_status = str(row["delivery_status"])
                    if existing is not None:
                        if str(existing["body_sha256"]) != body_sha256:
                            raise StorageError(
                                "Notification event identity was reused with a different "
                                "body hash: "
                                f"{event_key}"
                            )
                        existing_status = str(existing["delivery_status"])
                        delivery_status = (
                            existing_status
                            if existing_status in {"delivered", "delivered_legacy"}
                            else incoming_status
                        )
                        delivered_at = str(
                            existing["delivered_at"] or row.get("delivered_at") or ""
                        )
                        payload["latest_attempt_status"] = incoming_status
                        connection.execute(
                            """
                            UPDATE notification_delivery_memberships
                            SET delivery_status = ?, attempted_at = ?, delivered_at = ?,
                                payload_json = ?
                            WHERE event_key = ? AND channel = ? AND signal_id = ?
                            """,
                            (
                                delivery_status,
                                str(row["attempted_at"]),
                                delivered_at,
                                json.dumps(payload, sort_keys=True),
                                event_key,
                                channel,
                                signal_id,
                            ),
                        )
                        updated += 1
                        continue
                    connection.execute(
                        """
                        INSERT INTO notification_delivery_memberships
                        (membership_id, selection_id, scan_id, signal_id, ticker,
                         strategy_id, strategy_version, cohort, decision, selected_at,
                         event_key, channel, delivery_status, attempted_at, delivered_at,
                         body_sha256, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row["membership_id"]),
                            str(row.get("selection_id") or ""),
                            str(row["scan_id"]),
                            signal_id,
                            str(row.get("ticker") or "").upper(),
                            str(row["strategy_id"]),
                            str(row["strategy_version"]),
                            str(row["cohort"]),
                            str(row["decision"]),
                            str(row["selected_at"]),
                            event_key,
                            channel,
                            incoming_status,
                            str(row["attempted_at"]),
                            str(row.get("delivered_at") or ""),
                            body_sha256,
                            json.dumps(payload, sort_keys=True),
                        ),
                    )
                    inserted += 1
            return {"inserted": inserted, "updated": updated, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist notification deliveries: {exc}") from exc

    def load_notification_deliveries(
        self,
        *,
        scan_id: str | None = None,
        signal_id: str | None = None,
        event_key: str | None = None,
        channel: str | None = None,
        cohort: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("scan_id", scan_id),
            ("signal_id", signal_id),
            ("event_key", event_key),
            ("channel", channel),
            ("cohort", cohort),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        query = "SELECT * FROM notification_delivery_memberships"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY attempted_at DESC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_notification_delivery_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load notification deliveries: {exc}") from exc

    def persist_strategy_reconciliation(
        self,
        *,
        evaluations: list[dict[str, Any]],
        paper_trades: list[dict[str, Any]],
        learning_labels: list[dict[str, Any]],
        scorecards: list[dict[str, Any]],
    ) -> dict[str, dict[str, int]]:
        """Atomically upsert one sourced strategy-reconciliation batch."""

        self.initialize()
        stats = {
            "evaluations": {"inserted": 0, "updated": 0},
            "trades": {"inserted": 0, "updated": 0, "deleted": 0},
            "learning_labels": {"inserted": 0, "updated": 0, "deleted": 0},
            "scorecards": {"inserted": 0, "updated": 0},
        }
        try:
            with self._connect() as connection:
                # The batch is a complete snapshot for every included evaluation.
                # Purge prior derived rows that are no longer present before the
                # upserts.  This shares the transaction with all writes, so a failed
                # rerun cannot leave an evaluation corrected while its old return
                # trade/label survives (or vice versa).
                expected_trade_ids_by_selection: dict[str, set[str]] = {}
                for row in paper_trades:
                    selection_id = str(row.get("selection_id") or "")
                    trade_id = str(row.get("trade_id") or "")
                    if selection_id and trade_id:
                        expected_trade_ids_by_selection.setdefault(
                            selection_id, set()
                        ).add(trade_id)
                expected_label_ids_by_evaluation: dict[str, set[str]] = {}
                for row in learning_labels:
                    evaluation_id = str(row.get("evaluation_id") or "")
                    label_id = str(row.get("label_id") or "")
                    if evaluation_id and label_id:
                        expected_label_ids_by_evaluation.setdefault(
                            evaluation_id, set()
                        ).add(label_id)
                for evaluation in evaluations:
                    evaluation_id = str(evaluation.get("evaluation_id") or "")
                    selection_id = str(evaluation.get("selection_id") or "")
                    if selection_id:
                        expected_trade_ids = expected_trade_ids_by_selection.get(
                            selection_id, set()
                        )
                        existing_trade_ids = {
                            str(row[0])
                            for row in connection.execute(
                                "SELECT trade_id FROM strategy_paper_trades "
                                "WHERE selection_id = ?",
                                (selection_id,),
                            ).fetchall()
                        }
                        stale_trade_ids = existing_trade_ids - expected_trade_ids
                        for trade_id in stale_trade_ids:
                            connection.execute(
                                "DELETE FROM strategy_paper_trades WHERE trade_id = ?",
                                (trade_id,),
                            )
                        stats["trades"]["deleted"] += len(stale_trade_ids)
                    if evaluation_id:
                        expected_label_ids = expected_label_ids_by_evaluation.get(
                            evaluation_id, set()
                        )
                        existing_label_ids = {
                            str(row[0])
                            for row in connection.execute(
                                "SELECT label_id FROM strategy_learning_labels "
                                "WHERE evaluation_id = ?",
                                (evaluation_id,),
                            ).fetchall()
                        }
                        stale_label_ids = existing_label_ids - expected_label_ids
                        for label_id in stale_label_ids:
                            connection.execute(
                                "DELETE FROM strategy_learning_labels WHERE label_id = ?",
                                (label_id,),
                            )
                        stats["learning_labels"]["deleted"] += len(stale_label_ids)
                for row in evaluations:
                    evaluation_id = str(row.get("evaluation_id") or "")
                    if not evaluation_id:
                        continue
                    existed = connection.execute(
                        "SELECT 1 FROM strategy_evaluations WHERE evaluation_id = ?",
                        (evaluation_id,),
                    ).fetchone() is not None
                    connection.execute(
                        """
                        INSERT INTO strategy_evaluations
                        (evaluation_id, selection_id, signal_id, market_date, ticker,
                         strategy_id, strategy_version, cohort, terminal_state,
                         reconciliation_status, activated, filled, closed, net_return_pct,
                         source_bar_hash_sha256, execution_policy_version, reconciled_at,
                         payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(evaluation_id) DO UPDATE SET
                            terminal_state = excluded.terminal_state,
                            reconciliation_status = excluded.reconciliation_status,
                            activated = excluded.activated,
                            filled = excluded.filled,
                            closed = excluded.closed,
                            net_return_pct = excluded.net_return_pct,
                            source_bar_hash_sha256 = excluded.source_bar_hash_sha256,
                            reconciled_at = excluded.reconciled_at,
                            payload_json = excluded.payload_json
                        """,
                        (
                            evaluation_id,
                            str(row.get("selection_id") or ""),
                            str(row.get("signal_id") or ""),
                            str(row.get("market_date") or "")[:10],
                            str(row.get("ticker") or "").upper(),
                            str(row.get("strategy_id") or ""),
                            str(row.get("strategy_version") or ""),
                            str(row.get("cohort") or ""),
                            str(row.get("terminal_state") or ""),
                            str(row.get("reconciliation_status") or ""),
                            _bool_or_none(row.get("activated")),
                            1 if row.get("filled") else 0,
                            1 if row.get("closed") else 0,
                            _float_or_none(row.get("net_return_pct")),
                            str(row.get("source_bar_hash_sha256") or ""),
                            str(row.get("execution_policy_version") or ""),
                            str(row.get("reconciled_at") or ""),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    stats["evaluations"]["updated" if existed else "inserted"] += 1
                for row in paper_trades:
                    trade_id = str(row.get("trade_id") or "")
                    if not trade_id:
                        continue
                    existed = connection.execute(
                        "SELECT 1 FROM strategy_paper_trades WHERE trade_id = ?",
                        (trade_id,),
                    ).fetchone() is not None
                    connection.execute(
                        """
                        INSERT INTO strategy_paper_trades
                        (trade_id, selection_id, signal_id, market_date, ticker,
                         strategy_id, strategy_version, cohort, direction, decision_time,
                         entry_time, entry_fill_price, exit_time, exit_fill_price,
                         exit_reason, quantity, notional, net_pnl, net_return_pct,
                         r_multiple, fees, slippage_cost, source_bar_hash_sha256,
                         execution_policy_version, created_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(trade_id) DO UPDATE SET
                            entry_time = excluded.entry_time,
                            entry_fill_price = excluded.entry_fill_price,
                            exit_time = excluded.exit_time,
                            exit_fill_price = excluded.exit_fill_price,
                            exit_reason = excluded.exit_reason,
                            quantity = excluded.quantity,
                            notional = excluded.notional,
                            net_pnl = excluded.net_pnl,
                            net_return_pct = excluded.net_return_pct,
                            r_multiple = excluded.r_multiple,
                            fees = excluded.fees,
                            slippage_cost = excluded.slippage_cost,
                            source_bar_hash_sha256 = excluded.source_bar_hash_sha256,
                            payload_json = excluded.payload_json
                        """,
                        (
                            trade_id,
                            str(row.get("selection_id") or ""),
                            str(row.get("signal_id") or ""),
                            str(row.get("market_date") or "")[:10],
                            str(row.get("ticker") or "").upper(),
                            str(row.get("strategy_id") or ""),
                            str(row.get("strategy_version") or ""),
                            str(row.get("cohort") or ""),
                            str(row.get("direction") or ""),
                            str(row.get("decision_time") or ""),
                            str(row.get("entry_time") or ""),
                            float(row.get("entry_fill_price") or 0.0),
                            str(row.get("exit_time") or ""),
                            float(row.get("exit_fill_price") or 0.0),
                            str(row.get("exit_reason") or ""),
                            float(row.get("quantity") or 0.0),
                            float(row.get("notional") or 0.0),
                            float(row.get("net_pnl") or 0.0),
                            float(row.get("net_return_pct") or 0.0),
                            _float_or_none(row.get("r_multiple")),
                            float(row.get("fees") or 0.0),
                            float(row.get("slippage_cost") or 0.0),
                            str(row.get("source_bar_hash_sha256") or ""),
                            str(row.get("execution_policy_version") or ""),
                            str(row.get("created_at") or ""),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    stats["trades"]["updated" if existed else "inserted"] += 1
                for row in learning_labels:
                    label_id = str(row.get("label_id") or "")
                    if not label_id:
                        continue
                    existed = connection.execute(
                        "SELECT 1 FROM strategy_learning_labels WHERE label_id = ?",
                        (label_id,),
                    ).fetchone() is not None
                    connection.execute(
                        """
                        INSERT INTO strategy_learning_labels
                        (label_id, evaluation_id, signal_id, market_date, ticker,
                         strategy_id, strategy_version, cohort, label_family, label_value,
                         r_multiple, eligible, exclusion_reason, source_bar_hash_sha256,
                         created_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(label_id) DO UPDATE SET
                            label_value = excluded.label_value,
                            r_multiple = excluded.r_multiple,
                            eligible = excluded.eligible,
                            exclusion_reason = excluded.exclusion_reason,
                            source_bar_hash_sha256 = excluded.source_bar_hash_sha256,
                            payload_json = excluded.payload_json
                        """,
                        (
                            label_id,
                            str(row.get("evaluation_id") or ""),
                            str(row.get("signal_id") or ""),
                            str(row.get("market_date") or "")[:10],
                            str(row.get("ticker") or "").upper(),
                            str(row.get("strategy_id") or ""),
                            str(row.get("strategy_version") or ""),
                            str(row.get("cohort") or ""),
                            str(row.get("label_family") or ""),
                            _float_or_none(row.get("label_value")),
                            _float_or_none(row.get("r_multiple")),
                            1 if row.get("eligible") else 0,
                            str(row.get("exclusion_reason") or ""),
                            str(row.get("source_bar_hash_sha256") or ""),
                            str(row.get("created_at") or ""),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    stats["learning_labels"]["updated" if existed else "inserted"] += 1
                for row in scorecards:
                    scorecard_id = str(row.get("scorecard_id") or "")
                    if not scorecard_id:
                        continue
                    existed = connection.execute(
                        "SELECT 1 FROM daily_strategy_scorecards WHERE scorecard_id = ?",
                        (scorecard_id,),
                    ).fetchone() is not None
                    connection.execute(
                        """
                        INSERT INTO daily_strategy_scorecards
                        (scorecard_id, market_date, strategy_id, strategy_version, cohort,
                         execution_policy_version, selected_count, delivered_count,
                         resolved_count, triggered_count, not_triggered_count, filled_count,
                         closed_count, unresolved_count, wins, losses, flats,
                         activation_rate_pct, win_rate_pct, average_net_return_pct, net_pnl,
                         return_on_allocated_capital_pct, average_r, expectancy_r,
                         profit_factor, fees, slippage_cost, reconciliation_status,
                         created_at, session_status, no_trade_count, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(scorecard_id) DO UPDATE SET
                            selected_count = excluded.selected_count,
                            delivered_count = excluded.delivered_count,
                            resolved_count = excluded.resolved_count,
                            triggered_count = excluded.triggered_count,
                            not_triggered_count = excluded.not_triggered_count,
                            filled_count = excluded.filled_count,
                            closed_count = excluded.closed_count,
                            unresolved_count = excluded.unresolved_count,
                            wins = excluded.wins,
                            losses = excluded.losses,
                            flats = excluded.flats,
                            activation_rate_pct = excluded.activation_rate_pct,
                            win_rate_pct = excluded.win_rate_pct,
                            average_net_return_pct = excluded.average_net_return_pct,
                            net_pnl = excluded.net_pnl,
                            return_on_allocated_capital_pct =
                                excluded.return_on_allocated_capital_pct,
                            average_r = excluded.average_r,
                            expectancy_r = excluded.expectancy_r,
                            profit_factor = excluded.profit_factor,
                            fees = excluded.fees,
                            slippage_cost = excluded.slippage_cost,
                            reconciliation_status = excluded.reconciliation_status,
                            created_at = excluded.created_at,
                            session_status = excluded.session_status,
                            no_trade_count = excluded.no_trade_count,
                            payload_json = excluded.payload_json
                        """,
                        (
                            scorecard_id,
                            str(row.get("market_date") or "")[:10],
                            str(row.get("strategy_id") or ""),
                            str(row.get("strategy_version") or ""),
                            str(row.get("cohort") or ""),
                            str(row.get("execution_policy_version") or ""),
                            int(row.get("selected_count") or 0),
                            int(row.get("delivered_count") or 0),
                            int(row.get("resolved_count") or 0),
                            int(row.get("triggered_count") or 0),
                            int(row.get("not_triggered_count") or 0),
                            int(row.get("filled_count") or 0),
                            int(row.get("closed_count") or 0),
                            int(row.get("unresolved_count") or 0),
                            int(row.get("wins") or 0),
                            int(row.get("losses") or 0),
                            int(row.get("flats") or 0),
                            _float_or_none(row.get("activation_rate_pct")),
                            _float_or_none(row.get("win_rate_pct")),
                            _float_or_none(row.get("average_net_return_pct")),
                            float(row.get("net_pnl") or 0.0),
                            _float_or_none(row.get("return_on_allocated_capital_pct")),
                            _float_or_none(row.get("average_r")),
                            _float_or_none(row.get("expectancy_r")),
                            _float_or_none(row.get("profit_factor")),
                            float(row.get("fees") or 0.0),
                            float(row.get("slippage_cost") or 0.0),
                            str(row.get("reconciliation_status") or ""),
                            str(row.get("created_at") or ""),
                            str(row.get("session_status") or "unknown"),
                            int(row.get("no_trade_count") or 0),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    stats["scorecards"]["updated" if existed else "inserted"] += 1
            return stats
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist strategy reconciliation: {exc}") from exc

    def load_strategy_evaluations(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        strategy_id: str | None = None,
        cohort: str | None = None,
        limit: int = 50_000,
    ) -> list[dict[str, Any]]:
        return self._load_strategy_rows(
            "strategy_evaluations",
            start=start,
            end=end,
            strategy_id=strategy_id,
            cohort=cohort,
            limit=limit,
        )

    def load_strategy_paper_trades(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        strategy_id: str | None = None,
        cohort: str | None = None,
        limit: int = 50_000,
    ) -> list[dict[str, Any]]:
        return self._load_strategy_rows(
            "strategy_paper_trades",
            start=start,
            end=end,
            strategy_id=strategy_id,
            cohort=cohort,
            limit=limit,
        )

    def load_strategy_learning_labels(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        strategy_id: str | None = None,
        cohort: str | None = None,
        limit: int = 50_000,
    ) -> list[dict[str, Any]]:
        return self._load_strategy_rows(
            "strategy_learning_labels",
            start=start,
            end=end,
            strategy_id=strategy_id,
            cohort=cohort,
            limit=limit,
        )

    def load_daily_strategy_scorecards(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        strategy_id: str | None = None,
        cohort: str | None = None,
        limit: int = 50_000,
    ) -> list[dict[str, Any]]:
        return self._load_strategy_rows(
            "daily_strategy_scorecards",
            start=start,
            end=end,
            strategy_id=strategy_id,
            cohort=cohort,
            limit=limit,
        )

    def _load_strategy_rows(
        self,
        table: str,
        *,
        start: str | None,
        end: str | None,
        strategy_id: str | None,
        cohort: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        allowed = {
            "strategy_evaluations",
            "strategy_paper_trades",
            "strategy_learning_labels",
            "daily_strategy_scorecards",
        }
        if table not in allowed:
            raise StorageError(f"Unsupported strategy table: {table}")
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("market_date >= ?")
            params.append(start[:10])
        if end:
            clauses.append("market_date <= ?")
            params.append(end[:10])
        if strategy_id:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        if cohort:
            clauses.append("cohort = ?")
            params.append(cohort)
        query = f"SELECT * FROM {table}"  # noqa: S608
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC, strategy_id ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load strategy rows from {table}: {exc}") from exc

    def record_alert(
        self,
        *,
        alert_key: str,
        event_type: str,
        severity: str,
        payload: dict[str, Any],
        run_id: str | None = None,
        ticker: str | None = None,
    ) -> bool:
        self.initialize()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO alerts_sent
                    (alert_key, run_id, ticker, event_type, severity, sent_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, datetime('now'), ?)
                    """,
                    (
                        alert_key,
                        run_id,
                        ticker,
                        event_type,
                        severity,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise StorageError(f"Could not record alert: {exc}") from exc

    def load_recent_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT alert_key, run_id, ticker, event_type, severity, sent_at, payload_json
                    FROM alerts_sent
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                alerts = []
                for row in rows:
                    payload = json.loads(str(row["payload_json"]))
                    alerts.append(
                        {
                            "alert_key": str(row["alert_key"]),
                            "run_id": str(row["run_id"] or ""),
                            "ticker": str(row["ticker"] or ""),
                            "event_type": str(row["event_type"]),
                            "severity": str(row["severity"]),
                            "sent_at": str(row["sent_at"]),
                            **payload,
                        }
                    )
                return alerts
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load alerts: {exc}") from exc

    def load_recommendation_theses(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM recommendation_theses
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load recommendation theses: {exc}") from exc

    def load_paper_audit_trades(self, limit: int = 1000) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM paper_audit_trades
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load paper audit trades: {exc}") from exc

    def load_latest_paper_audit_summary(self) -> dict[str, Any] | None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM paper_audit_summary
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                return json.loads(str(row["payload_json"])) if row else None
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load paper audit summary: {exc}") from exc

    def persist_performance_report(self, report: dict[str, Any]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO performance_daily (report_date, run_id, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    (
                        str(report.get("report_date", "")),
                        str(report.get("run_id") or ""),
                        json.dumps(report, sort_keys=True),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO performance_cumulative (created_at, payload_json)
                    VALUES (datetime('now'), ?)
                    """,
                    (json.dumps(report, sort_keys=True),),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist performance report: {exc}") from exc

    def load_latest_performance_report(self) -> dict[str, Any] | None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM performance_cumulative
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                return json.loads(str(row["payload_json"])) if row else None
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load performance report: {exc}") from exc

    def record_provider_health(
        self, provider: str, status: str, checked_at: str, detail: str = ""
    ) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO provider_health (provider, status, checked_at, detail)
                    VALUES (?, ?, ?, ?)
                    """,
                    (provider, status, checked_at, detail),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not record provider health: {exc}") from exc

    def load_provider_health(self, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT provider, status, checked_at, detail
                    FROM provider_health
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load provider health: {exc}") from exc

    def persist_web_fetch_run(self, payload: dict[str, Any]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO web_fetch_runs
                    (id, source, source_type, status, started_at, completed_at, url,
                     payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(payload.get("run_id", "")),
                        str(payload.get("source", "")),
                        str(payload.get("source_type", "")),
                        str(payload.get("status", "")),
                        str(payload.get("started_at", "")),
                        str(payload.get("completed_at", "")),
                        str(payload.get("url", "")),
                        json.dumps(payload, sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist web fetch run: {exc}") from exc

    def persist_web_fetch_result(self, payload: dict[str, Any]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO web_fetch_results
                    (run_id, source, status, row_count, artifact_path, failure_reason,
                     payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(payload.get("run_id", "")),
                        str(payload.get("source", "")),
                        str(payload.get("status", "")),
                        int(payload.get("row_count") or 0),
                        str(payload.get("artifact_path", "")),
                        str(payload.get("failure_reason", "")),
                        json.dumps(payload, sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist web fetch result: {exc}") from exc

    def record_source_health(
        self,
        source: str,
        status: str,
        checked_at: str,
        detail: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO source_health
                    (source, status, checked_at, detail, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        source,
                        status,
                        checked_at,
                        detail,
                        json.dumps(payload or {}, sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not record source health: {exc}") from exc

    def persist_raw_source_artifact(self, payload: dict[str, Any]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO raw_source_artifacts
                    (run_id, source, artifact_kind, path, content_type, byte_count,
                     sha256, created_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(payload.get("run_id", "")),
                        str(payload.get("source", "")),
                        str(payload.get("artifact_kind", "")),
                        str(payload.get("path", "")),
                        str(payload.get("content_type", "")),
                        int(payload.get("byte_count") or 0),
                        str(payload.get("sha256", "")),
                        str(payload.get("created_at", "")),
                        json.dumps(dict(payload.get("metadata") or {}), sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist raw source artifact: {exc}") from exc

    def persist_normalized_source_rows(
        self, run_id: str, source: str, rows: list[dict[str, Any]]
    ) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                for row in rows:
                    connection.execute(
                        """
                        INSERT INTO normalized_source_rows
                        (run_id, source, ticker, as_of_timestamp, payload_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            source,
                            str(row.get("ticker", "")),
                            str(row.get("as_of_timestamp", "")),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist normalized source rows: {exc}") from exc

    def load_web_fetch_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM web_fetch_runs
                    ORDER BY started_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load web fetch runs: {exc}") from exc

    def load_web_fetch_results(self, limit: int = 50) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM web_fetch_results
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load web fetch results: {exc}") from exc

    def load_source_health(self, limit: int = 50) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT source, status, checked_at, detail, payload_json
                    FROM source_health
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [
                    {
                        "source": str(row["source"]),
                        "status": str(row["status"]),
                        "checked_at": str(row["checked_at"]),
                        "detail": str(row["detail"]),
                        **json.loads(str(row["payload_json"])),
                    }
                    for row in rows
                ]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load source health: {exc}") from exc

    def load_raw_source_artifacts(self, limit: int = 50) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT run_id, source, artifact_kind, path, content_type, byte_count,
                           sha256, created_at, metadata_json
                    FROM raw_source_artifacts
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [
                    {
                        "run_id": str(row["run_id"]),
                        "source": str(row["source"]),
                        "artifact_kind": str(row["artifact_kind"]),
                        "path": str(row["path"]),
                        "content_type": str(row["content_type"] or ""),
                        "byte_count": int(row["byte_count"]),
                        "sha256": str(row["sha256"]),
                        "created_at": str(row["created_at"]),
                        "metadata": json.loads(str(row["metadata_json"])),
                    }
                    for row in rows
                ]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load raw source artifacts: {exc}") from exc

    def load_normalized_source_rows(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM normalized_source_rows
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load normalized source rows: {exc}") from exc

    def persist_halt_events(self, events: list[dict[str, Any]]) -> dict[str, int]:
        self.initialize()
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for event in events:
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO halt_events
                        (event_key, ticker, event_time, status, payload_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            str(event.get("event_key", "")),
                            str(event.get("ticker", "")),
                            str(event.get("event_time", "")),
                            str(event.get("status", "")),
                            json.dumps(event, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
                return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist halt events: {exc}") from exc

    def load_halt_events(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM halt_events
                    ORDER BY event_time DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load halt events: {exc}") from exc

    def persist_sec_risk_events(self, events: list[dict[str, Any]]) -> dict[str, int]:
        self.initialize()
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for event in events:
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO sec_risk_events
                        (event_key, ticker, filed_at, form_type, severity, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(event.get("event_key", "")),
                            str(event.get("ticker", "")),
                            str(event.get("filed_at", "")),
                            str(event.get("form_type", "")),
                            str(event.get("severity", "")),
                            json.dumps(event, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
                return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist SEC risk events: {exc}") from exc

    def load_sec_risk_events(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM sec_risk_events
                    ORDER BY filed_at DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load SEC risk events: {exc}") from exc

    def persist_ai_research(
        self,
        run: dict[str, Any],
        outputs: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
    ) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO ai_research_runs
                    (id, mode, status, started_at, completed_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(run.get("run_id", "")),
                        str(run.get("mode", "")),
                        str(run.get("status", "")),
                        str(run.get("started_at", "")),
                        str(run.get("completed_at", "")),
                        json.dumps(run, sort_keys=True),
                    ),
                )
                for output in outputs:
                    connection.execute(
                        """
                        INSERT INTO ai_research_outputs
                        (run_id, ticker, classification, payload_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            str(run.get("run_id", "")),
                            str(output.get("ticker", "")),
                            str(output.get("classification", "")),
                            json.dumps(output, sort_keys=True),
                        ),
                    )
                for warning in warnings:
                    connection.execute(
                        """
                        INSERT INTO ai_data_warnings
                        (run_id, ticker, warning, created_at, payload_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            str(run.get("run_id", "")),
                            str(warning.get("ticker", "")),
                            str(warning.get("warning", "")),
                            str(warning.get("created_at", "")),
                            json.dumps(warning, sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist AI research output: {exc}") from exc

    def load_ai_research_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM ai_research_runs
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load AI research runs: {exc}") from exc

    def load_ai_research_outputs(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM ai_research_outputs
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load AI research outputs: {exc}") from exc

    def load_ai_data_warnings(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM ai_data_warnings
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load AI data warnings: {exc}") from exc

    def persist_manual_snapshot_upload(
        self,
        *,
        upload_id: str,
        created_at: str,
        input_path: str,
        output_path: str,
        raw_rows: list[dict[str, Any]],
        normalized_rows: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO manual_snapshot_uploads
                    (id, created_at, input_path, output_path, row_count, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        upload_id,
                        created_at,
                        input_path,
                        output_path,
                        len(normalized_rows),
                        json.dumps(summary, sort_keys=True),
                    ),
                )
                connection.execute(
                    "DELETE FROM manual_snapshot_rows WHERE upload_id = ?", (upload_id,)
                )
                for raw, normalized in zip(raw_rows, normalized_rows, strict=False):
                    connection.execute(
                        """
                        INSERT INTO manual_snapshot_rows
                        (upload_id, ticker, as_of_timestamp, raw_json, normalized_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            upload_id,
                            str(normalized.get("ticker", "")),
                            str(normalized.get("as_of_timestamp", "")),
                            json.dumps(raw, sort_keys=True),
                            json.dumps(normalized, sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist manual snapshot upload: {exc}") from exc

    def load_manual_snapshot_uploads(self, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT id, created_at, input_path, output_path, row_count, payload_json
                    FROM manual_snapshot_uploads
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [
                    {
                        "upload_id": str(row["id"]),
                        "created_at": str(row["created_at"]),
                        "input_path": str(row["input_path"]),
                        "output_path": str(row["output_path"]),
                        "row_count": int(row["row_count"]),
                        **json.loads(str(row["payload_json"])),
                    }
                    for row in rows
                ]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load manual snapshot uploads: {exc}") from exc

    def persist_manual_outcomes(
        self, rows: list[dict[str, Any]], *, replace: bool = False
    ) -> dict[str, int]:
        self.initialize()
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    key = str(row.get("outcome_key", ""))
                    if replace:
                        connection.execute(
                            "DELETE FROM manual_outcomes WHERE outcome_key = ?", (key,)
                        )
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO manual_outcomes
                        (outcome_key, scan_id, ticker, recommendation_timestamp,
                         uploaded_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            key,
                            str(row.get("scan_id", "")),
                            str(row.get("ticker", "")),
                            str(row.get("recommendation_timestamp", "")),
                            str(row.get("uploaded_at", "")),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
                return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist manual outcomes: {exc}") from exc

    def load_manual_outcomes(self, limit: int = 1000) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM manual_outcomes
                    ORDER BY uploaded_at DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load manual outcomes: {exc}") from exc

    def persist_manual_audit(
        self, summary: dict[str, Any], trades: list[dict[str, Any]]
    ) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                for trade in trades:
                    connection.execute(
                        """
                        INSERT INTO manual_audit_trades (scan_id, ticker, payload_json)
                        VALUES (?, ?, ?)
                        """,
                        (
                            str(trade.get("scan_id", "")),
                            str(trade.get("ticker", "")),
                            json.dumps(trade, sort_keys=True),
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO manual_audit_summary (created_at, payload_json)
                    VALUES (?, ?)
                    """,
                    (
                        str(summary.get("created_at", "")),
                        json.dumps(summary, sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist manual audit: {exc}") from exc

    def load_manual_audit_trades(self, limit: int = 1000) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM manual_audit_trades
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load manual audit trades: {exc}") from exc

    def load_latest_manual_audit_summary(self) -> dict[str, Any] | None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM manual_audit_summary
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                return json.loads(str(row["payload_json"])) if row else None
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load manual audit summary: {exc}") from exc

    def persist_intelligence_outcomes(
        self,
        summary: dict[str, Any],
        rows: list[dict[str, Any]],
        *,
        run_id: str | None = None,
    ) -> None:
        self.initialize()
        resolved_run_id = run_id or str(summary.get("run_id") or "")
        try:
            with self._connect() as connection:
                for row in rows:
                    connection.execute(
                        """
                        INSERT INTO intelligence_outcomes
                        (run_id, ticker, evaluated_at, payload_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            resolved_run_id,
                            str(row.get("ticker", "")),
                            str(row.get("evaluated_at", "")),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO intelligence_outcome_summary
                    (run_id, created_at, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    (
                        resolved_run_id,
                        str(summary.get("created_at", "")),
                        json.dumps(summary, sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist intelligence outcomes: {exc}") from exc

    def load_intelligence_outcomes(self, limit: int = 1000) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM intelligence_outcomes
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load intelligence outcomes: {exc}") from exc

    def load_latest_intelligence_outcome_summary(self) -> dict[str, Any] | None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM intelligence_outcome_summary
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                return json.loads(str(row["payload_json"])) if row else None
        except sqlite3.Error as exc:
            raise StorageError(
                f"Could not load intelligence outcome summary: {exc}"
            ) from exc

    def persist_shadow_report(self, report: dict[str, Any]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO shadow_reports (created_at, payload_json)
                    VALUES (?, ?)
                    """,
                    (str(report.get("created_at", "")), json.dumps(report, sort_keys=True)),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist shadow report: {exc}") from exc

    def load_latest_shadow_report(self) -> dict[str, Any] | None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM shadow_reports
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                return json.loads(str(row["payload_json"])) if row else None
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load shadow report: {exc}") from exc

    def has_screener_file_hash(self, file_hash: str) -> bool:
        self.initialize()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT 1
                    FROM screener_automation_runs
                    WHERE file_hash = ?
                    LIMIT 1
                    """,
                    (file_hash,),
                ).fetchone()
                return row is not None
        except sqlite3.Error as exc:
            raise StorageError(f"Could not check screener file hash: {exc}") from exc

    def persist_screener_automation_run(self, payload: dict[str, Any]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO screener_automation_runs
                    (id, file_hash, input_path, status, started_at, completed_at,
                     raw_archive_path, normalized_path, out_dir, scan_run_id, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(payload.get("run_id", "")),
                        str(payload.get("file_hash", "")),
                        str(payload.get("input_path", "")),
                        str(payload.get("status", "")),
                        str(payload.get("started_at", "")),
                        str(payload.get("completed_at", "")),
                        str(payload.get("raw_archive_path", "")),
                        str(payload.get("normalized_path", "")),
                        str(payload.get("out_dir", "")),
                        str(payload.get("scan_run_id", "")),
                        json.dumps(payload, sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist screener automation run: {exc}") from exc

    def load_screener_automation_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM screener_automation_runs
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load screener automation runs: {exc}") from exc

    def persist_automation_run(self, payload: dict[str, Any]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO automation_runs
                    (id, run_type, status, started_at, completed_at, out_dir, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(payload.get("run_id", "")),
                        str(payload.get("run_type", "")),
                        str(payload.get("status", "")),
                        str(payload.get("started_at", "")),
                        str(payload.get("completed_at", "")),
                        str(payload.get("out_dir", "")),
                        json.dumps(payload, sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist automation run: {exc}") from exc

    def load_automation_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM automation_runs
                    ORDER BY started_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load automation runs: {exc}") from exc

    def persist_alpha_feature_vectors(self, rows: list[dict[str, Any]]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                for row in rows:
                    connection.execute(
                        """
                        INSERT INTO alpha_feature_vectors
                        (scan_id, ticker, timestamp, model_version, config_hash,
                         feature_json, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("scan_id", "")),
                            str(row.get("ticker", "")),
                            str(row.get("timestamp", "")),
                            str(row.get("model_version", "")),
                            str(row.get("config_hash", "")),
                            json.dumps(row.get("feature_json") or {}, sort_keys=True),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist AlphaOps feature vectors: {exc}") from exc

    def load_alpha_feature_vectors(
        self,
        *,
        scan_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                if scan_id:
                    rows = connection.execute(
                        """
                        SELECT payload_json
                        FROM alpha_feature_vectors
                        WHERE scan_id = ?
                        ORDER BY id DESC
                        LIMIT ?
                        """,
                        (scan_id, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT payload_json
                        FROM alpha_feature_vectors
                        ORDER BY id DESC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load AlphaOps feature vectors: {exc}") from exc

    def persist_alpha_signals(self, rows: list[dict[str, Any]], *, replace: bool = True) -> None:
        self.initialize()
        statement = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        try:
            with self._connect() as connection:
                for row in rows:
                    signal_key = str(
                        row.get("signal_key")
                        or f"{row.get('scan_id')}:{row.get('rank')}:{row.get('ticker')}"
                    )
                    connection.execute(
                        f"""
                        {statement} INTO alpha_signals
                        (signal_key, scan_id, ticker, rank, timestamp, alpha_score,
                         edge_bucket, confidence_bucket, can_alert, no_trade_reason,
                         payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,  # noqa: S608
                        (
                            signal_key,
                            str(row.get("scan_id", "")),
                            str(row.get("ticker", "")),
                            int(float(row.get("rank") or 0)),
                            str(row.get("timestamp") or row.get("as_of_timestamp") or ""),
                            float(row.get("alpha_score") or 0.0),
                            str(row.get("edge_bucket") or ""),
                            str(row.get("confidence_bucket") or ""),
                            1 if row.get("can_alert") else 0,
                            str(row.get("no_trade_reason") or ""),
                            json.dumps({**row, "signal_key": signal_key}, sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist AlphaOps signals: {exc}") from exc

    def load_alpha_signals(
        self,
        *,
        scan_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                if scan_id:
                    rows = connection.execute(
                        """
                        SELECT payload_json
                        FROM alpha_signals
                        WHERE scan_id = ?
                        ORDER BY rank ASC
                        LIMIT ?
                        """,
                        (scan_id, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT payload_json
                        FROM alpha_signals
                        ORDER BY timestamp DESC, rank ASC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load AlphaOps signals: {exc}") from exc

    def persist_alpha_outcome_labels(self, rows: list[dict[str, Any]]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                for row in rows:
                    label_key = str(
                        row.get("label_key")
                        or f"{row.get('scan_id')}:{row.get('ticker')}:{row.get('created_at', '')}"
                    )
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO alpha_outcome_labels
                        (label_key, scan_id, ticker, created_at, payload_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            label_key,
                            str(row.get("scan_id", "")),
                            str(row.get("ticker", "")),
                            str(row.get("created_at", "")),
                            json.dumps({**row, "label_key": label_key}, sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist AlphaOps outcome labels: {exc}") from exc

    def replace_alpha_production_outcome_labels(
        self,
        rows: list[dict[str, Any]],
    ) -> None:
        """Atomically replace only canonical production-learning label copies.

        Legacy/manual labels remain queryable for historical dashboards, but cannot
        survive as stale production labels after a reconciliation correction.
        """

        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM alpha_outcome_labels WHERE label_key LIKE ?",
                    ("strategy_learning:%",),
                )
                for row in rows:
                    label_key = str(row.get("label_key") or "")
                    if not label_key.startswith("strategy_learning:"):
                        raise StorageError(
                            "Production Alpha label keys must use strategy_learning:."
                        )
                    connection.execute(
                        """
                        INSERT INTO alpha_outcome_labels
                        (label_key, scan_id, ticker, created_at, payload_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            label_key,
                            str(row.get("scan_id") or ""),
                            str(row.get("ticker") or ""),
                            str(row.get("created_at") or ""),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(
                f"Could not replace AlphaOps production outcome labels: {exc}"
            ) from exc

    def load_alpha_outcome_labels(self, limit: int = 5000) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json
                    FROM alpha_outcome_labels
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load AlphaOps outcome labels: {exc}") from exc

    def persist_alpha_learning_run(self, payload: dict[str, Any]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO alpha_learning_runs
                    (id, created_at, status, summary_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        str(payload.get("run_id", "")),
                        str(payload.get("created_at", "")),
                        str(payload.get("status", "")),
                        json.dumps(payload, sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist AlphaOps learning run: {exc}") from exc

    def load_alpha_learning_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT summary_json
                    FROM alpha_learning_runs
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["summary_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load AlphaOps learning runs: {exc}") from exc

    def persist_alpha_source_reliability(self, rows: list[dict[str, Any]]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                for row in rows:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO alpha_source_reliability
                        (source, updated_at, runs, rows_returned, rows_normalized,
                         rows_rejected, stale_count, missing_critical_count,
                         outcome_count, winner_count, reliability_score, summary_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("source", "")),
                            str(row.get("updated_at", "")),
                            int(row.get("runs") or 0),
                            int(row.get("rows_returned") or 0),
                            int(row.get("rows_normalized") or 0),
                            int(row.get("rows_rejected") or 0),
                            int(row.get("stale_count") or 0),
                            int(row.get("missing_critical_count") or 0),
                            int(row.get("outcome_count") or 0),
                            int(row.get("winner_count") or 0),
                            float(row.get("reliability_score") or 0.0),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist AlphaOps source reliability: {exc}") from exc

    def load_alpha_source_reliability(self) -> dict[str, dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT source, summary_json
                    FROM alpha_source_reliability
                    ORDER BY source ASC
                    """
                ).fetchall()
                return {
                    str(row["source"]): json.loads(str(row["summary_json"])) for row in rows
                }
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load AlphaOps source reliability: {exc}") from exc

    def persist_alpha_setup_memory(self, rows: list[dict[str, Any]]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                for row in rows:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO alpha_setup_memory
                        (setup_key, updated_at, sample_size, avg_return_pct,
                         median_return_pct, win_rate_pct, max_drawdown_pct,
                         outlier_dependency, summary_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("setup_key", "")),
                            str(row.get("updated_at", "")),
                            int(row.get("sample_size") or 0),
                            float(row.get("avg_return_pct") or 0.0),
                            float(row.get("median_return_pct") or 0.0),
                            float(row.get("win_rate_pct") or 0.0),
                            float(row.get("max_drawdown_pct") or 0.0),
                            float(row.get("outlier_dependency") or 0.0),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist AlphaOps setup memory: {exc}") from exc

    def load_alpha_setup_memory(self) -> dict[str, dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT setup_key, summary_json
                    FROM alpha_setup_memory
                    ORDER BY setup_key ASC
                    """
                ).fetchall()
                return {
                    str(row["setup_key"]): json.loads(str(row["summary_json"])) for row in rows
                }
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load AlphaOps setup memory: {exc}") from exc

    def persist_historical_signals(
        self,
        rows: list[dict[str, Any]],
        *,
        replace: bool = True,
    ) -> dict[str, int]:
        self.initialize()
        statement = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    signal_id = str(row.get("signal_id") or "")
                    if not signal_id:
                        continue
                    cursor = connection.execute(
                        f"""
                        {statement} INTO historical_signals
                        (signal_id, scan_id, alpha_signal_id, generated_at, market_date,
                         ticker, company, rank, source, source_url, source_confidence,
                         data_source_kind, model_version, config_hash, primary_setup,
                         setup_grade, signal_label, entry_watch_level, entry_trigger_type,
                         entry_condition, confirmation_condition, exit_line,
                         invalidation_level, target_1, target_2, risk_flags_json,
                         avoid_reasons_json, catalyst_summary, telegram_event_key,
                         was_alerted, no_trade_reason, raw_payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,  # noqa: S608
                        (
                            signal_id,
                            str(row.get("scan_id") or ""),
                            str(row.get("alpha_signal_id") or ""),
                            str(row.get("generated_at") or ""),
                            str(row.get("market_date") or ""),
                            str(row.get("ticker") or ""),
                            str(row.get("company") or ""),
                            _int_or_none(row.get("rank")),
                            str(row.get("source") or ""),
                            str(row.get("source_url") or ""),
                            _float_or_none(row.get("source_confidence")),
                            str(row.get("data_source_kind") or ""),
                            str(row.get("model_version") or ""),
                            str(row.get("config_hash") or ""),
                            str(row.get("primary_setup") or ""),
                            str(row.get("setup_grade") or ""),
                            str(row.get("signal_label") or ""),
                            _float_or_none(row.get("entry_watch_level")),
                            str(row.get("entry_trigger_type") or ""),
                            str(row.get("entry_condition") or ""),
                            str(row.get("confirmation_condition") or ""),
                            _float_or_none(row.get("exit_line")),
                            _float_or_none(row.get("invalidation_level")),
                            _float_or_none(row.get("target_1")),
                            _float_or_none(row.get("target_2")),
                            json.dumps(row.get("risk_flags_json") or [], sort_keys=True),
                            json.dumps(row.get("avoid_reasons_json") or [], sort_keys=True),
                            str(row.get("catalyst_summary") or ""),
                            str(row.get("telegram_event_key") or ""),
                            1 if row.get("was_alerted") else 0,
                            str(row.get("no_trade_reason") or ""),
                            json.dumps(row.get("raw_payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
                return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist historical signals: {exc}") from exc

    def load_historical_signals(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        market_date: str | None = None,
        scan_id: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date)
        if start:
            clauses.append("market_date >= ?")
            params.append(start)
        if end:
            clauses.append("market_date <= ?")
            params.append(end)
        if scan_id:
            clauses.append("scan_id = ?")
            params.append(scan_id)
        query = """
            SELECT *
            FROM historical_signals
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC, COALESCE(rank, 999999) ASC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_historical_signal_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load historical signals: {exc}") from exc

    def link_historical_signal_notification(
        self,
        *,
        scan_id: str,
        telegram_event_key: str,
        was_alerted: bool,
        signal_ids: Iterable[str] | None = None,
    ) -> int:
        self.initialize()
        selected_ids = (
            sorted({str(signal_id) for signal_id in signal_ids if str(signal_id).strip()})
            if signal_ids is not None
            else None
        )
        if selected_ids == []:
            return 0
        try:
            with self._connect() as connection:
                if selected_ids is None:
                    cursor = connection.execute(
                        """
                        UPDATE historical_signals
                        SET telegram_event_key = ?, was_alerted = ?
                        WHERE scan_id = ?
                        """,
                        (telegram_event_key, 1 if was_alerted else 0, scan_id),
                    )
                else:
                    placeholders = ",".join("?" for _ in selected_ids)
                    cursor = connection.execute(
                        f"""
                        UPDATE historical_signals
                        SET telegram_event_key = ?, was_alerted = ?
                        WHERE scan_id = ? AND signal_id IN ({placeholders})
                        """,  # noqa: S608
                        (
                            telegram_event_key,
                            1 if was_alerted else 0,
                            scan_id,
                            *selected_ids,
                        ),
                    )
                return int(cursor.rowcount or 0)
        except sqlite3.Error as exc:
            raise StorageError(f"Could not link historical signal notification: {exc}") from exc

    def persist_signal_events(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        self.initialize()
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    event_id = str(row.get("event_id") or "")
                    signal_id = str(row.get("signal_id") or "")
                    if not event_id or not signal_id:
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO signal_events
                        (event_id, signal_id, event_type, event_timestamp, event_price,
                         source, notes, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            signal_id,
                            str(row.get("event_type") or ""),
                            str(row.get("event_timestamp") or ""),
                            _float_or_none(row.get("event_price")),
                            str(row.get("source") or ""),
                            str(row.get("notes") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
                return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist signal events: {exc}") from exc

    def load_signal_events(
        self,
        *,
        signal_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if signal_id:
            clauses.append("signal_id = ?")
            params.append(signal_id)
        if start:
            clauses.append("event_timestamp >= ?")
            params.append(start)
        if end:
            clauses.append("event_timestamp <= ?")
            params.append(end)
        query = "SELECT * FROM signal_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY event_timestamp ASC, event_id ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load signal events: {exc}") from exc

    def persist_signal_outcomes(
        self,
        rows: list[dict[str, Any]],
        *,
        replace: bool = False,
    ) -> dict[str, int]:
        self.initialize()
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    signal_id = str(row.get("signal_id") or "")
                    if not signal_id:
                        continue
                    if replace:
                        connection.execute(
                            "DELETE FROM signal_outcomes WHERE signal_id = ?", (signal_id,)
                        )
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO signal_outcomes
                        (signal_id, market_date, ticker, outcome_source, entry_time,
                         entry_price, price_1m, price_5m, price_15m, lunch_price,
                         close_price, high_after_entry, low_after_entry, halted, notes,
                         imported_at, validated_against_signal_timestamp, outcome_status,
                         payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            signal_id,
                            str(row.get("market_date") or row.get("date") or ""),
                            str(row.get("ticker") or ""),
                            str(row.get("outcome_source") or row.get("source") or ""),
                            str(row.get("entry_time") or ""),
                            _float_or_none(row.get("entry_price")),
                            _float_or_none(row.get("price_1m")),
                            _float_or_none(row.get("price_5m")),
                            _float_or_none(row.get("price_15m")),
                            _float_or_none(row.get("lunch_price")),
                            _float_or_none(row.get("close_price")),
                            _float_or_none(row.get("high_after_entry")),
                            _float_or_none(row.get("low_after_entry")),
                            _bool_or_none(row.get("halted")),
                            str(row.get("notes") or ""),
                            str(row.get("imported_at") or ""),
                            1 if row.get("validated_against_signal_timestamp") else 0,
                            str(row.get("outcome_status") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
                return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist signal outcomes: {exc}") from exc

    def persist_signal_outcomes_with_events(
        self,
        outcome_rows: list[dict[str, Any]],
        event_rows: list[dict[str, Any]],
        *,
        replace: bool = False,
    ) -> dict[str, dict[str, int]]:
        """Persist outcome evidence and its audit events in one transaction."""

        self.initialize()
        outcome_stats = {"inserted": 0, "skipped": 0}
        event_stats = {"inserted": 0, "skipped": 0}
        try:
            with self._connect() as connection:
                for row in outcome_rows:
                    signal_id = str(row.get("signal_id") or "")
                    if not signal_id:
                        continue
                    if replace:
                        connection.execute(
                            "DELETE FROM signal_outcomes WHERE signal_id = ?", (signal_id,)
                        )
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO signal_outcomes
                        (signal_id, market_date, ticker, outcome_source, entry_time,
                         entry_price, price_1m, price_5m, price_15m, lunch_price,
                         close_price, high_after_entry, low_after_entry, halted, notes,
                         imported_at, validated_against_signal_timestamp, outcome_status,
                         payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            signal_id,
                            str(row.get("market_date") or row.get("date") or ""),
                            str(row.get("ticker") or ""),
                            str(row.get("outcome_source") or row.get("source") or ""),
                            str(row.get("entry_time") or ""),
                            _float_or_none(row.get("entry_price")),
                            _float_or_none(row.get("price_1m")),
                            _float_or_none(row.get("price_5m")),
                            _float_or_none(row.get("price_15m")),
                            _float_or_none(row.get("lunch_price")),
                            _float_or_none(row.get("close_price")),
                            _float_or_none(row.get("high_after_entry")),
                            _float_or_none(row.get("low_after_entry")),
                            _bool_or_none(row.get("halted")),
                            str(row.get("notes") or ""),
                            str(row.get("imported_at") or ""),
                            1 if row.get("validated_against_signal_timestamp") else 0,
                            str(row.get("outcome_status") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        outcome_stats["inserted"] += 1
                    else:
                        outcome_stats["skipped"] += 1
                for row in event_rows:
                    event_id = str(row.get("event_id") or "")
                    signal_id = str(row.get("signal_id") or "")
                    if not event_id or not signal_id:
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO signal_events
                        (event_id, signal_id, event_type, event_timestamp, event_price,
                         source, notes, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            signal_id,
                            str(row.get("event_type") or ""),
                            str(row.get("event_timestamp") or ""),
                            _float_or_none(row.get("event_price")),
                            str(row.get("source") or ""),
                            str(row.get("notes") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        event_stats["inserted"] += 1
                    else:
                        event_stats["skipped"] += 1
            return {"outcomes": outcome_stats, "events": event_stats}
        except sqlite3.Error as exc:
            raise StorageError(
                f"Could not atomically persist signal outcomes and events: {exc}"
            ) from exc

    def load_signal_outcomes(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        signal_id: str | None = None,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("market_date >= ?")
            params.append(start)
        if end:
            clauses.append("market_date <= ?")
            params.append(end)
        if signal_id:
            clauses.append("signal_id = ?")
            params.append(signal_id)
        query = "SELECT * FROM signal_outcomes"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load signal outcomes: {exc}") from exc

    def persist_price_observations(
        self,
        rows: list[dict[str, Any]],
        *,
        replace: bool = True,
    ) -> dict[str, int]:
        self.initialize()
        statement = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    observation_id = str(row.get("observation_id") or "")
                    ticker = str(row.get("ticker") or "").upper()
                    market_date = str(row.get("market_date") or "")[:10]
                    requested_at = str(row.get("requested_at") or "")
                    if not observation_id or not ticker or not market_date or not requested_at:
                        continue
                    cursor = connection.execute(
                        f"""
                        {statement} INTO price_observations
                        (observation_id, signal_id, market_date, ticker, requested_at,
                         observed_at, price, price_type, source, source_kind, provider,
                         provider_status, freshness_seconds, tolerance_seconds, is_usable,
                         created_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,  # noqa: S608
                        (
                            observation_id,
                            str(row.get("signal_id") or ""),
                            market_date,
                            ticker,
                            requested_at,
                            str(row.get("observed_at") or ""),
                            _float_or_none(row.get("price")),
                            str(row.get("price_type") or "last_bar_close"),
                            str(row.get("source") or ""),
                            str(row.get("source_kind") or ""),
                            str(row.get("provider") or ""),
                            str(row.get("provider_status") or ""),
                            _int_or_none(row.get("freshness_seconds")),
                            int(_int_or_none(row.get("tolerance_seconds")) or 0),
                            1 if row.get("is_usable") else 0,
                            str(row.get("created_at") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
                return {"inserted": inserted, "skipped": skipped, "row_count": len(rows)}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist price observations: {exc}") from exc

    def load_price_observations(
        self,
        *,
        market_date: str | None = None,
        ticker: str | None = None,
        signal_id: str | None = None,
        usable_only: bool = False,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date[:10])
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker.upper())
        if signal_id:
            clauses.append("signal_id = ?")
            params.append(signal_id)
        if usable_only:
            clauses.append("is_usable = 1")
        query = "SELECT * FROM price_observations"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY observed_at DESC, requested_at DESC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load price observations: {exc}") from exc

    def persist_trade_intents(
        self,
        rows: list[dict[str, Any]],
        *,
        replace: bool = False,
    ) -> dict[str, int]:
        self.initialize()
        statement = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    intent_id = str(row.get("intent_id") or "")
                    ticker = str(row.get("ticker") or "").upper()
                    market_date = str(row.get("market_date") or "")[:10]
                    if not intent_id or not ticker or not market_date:
                        continue
                    cursor = connection.execute(
                        f"""
                        {statement} INTO trade_intents
                        (intent_id, signal_id, market_date, ticker, mode, lifecycle_state,
                         action, decision_time, decision_price, trigger_price, stop_price,
                         target_price, quantity, notional, risk_amount, reason,
                         blocked_reason, source_observation_id, notification_event_key,
                         created_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,  # noqa: S608
                        (
                            intent_id,
                            str(row.get("signal_id") or ""),
                            market_date,
                            ticker,
                            str(row.get("mode") or ""),
                            str(row.get("lifecycle_state") or ""),
                            str(row.get("action") or ""),
                            str(row.get("decision_time") or ""),
                            _float_or_none(row.get("decision_price")),
                            _float_or_none(row.get("trigger_price")),
                            _float_or_none(row.get("stop_price")),
                            _float_or_none(row.get("target_price")),
                            _float_or_none(row.get("quantity")),
                            _float_or_none(row.get("notional")),
                            _float_or_none(row.get("risk_amount")),
                            str(row.get("reason") or ""),
                            str(row.get("blocked_reason") or ""),
                            str(row.get("source_observation_id") or ""),
                            str(row.get("notification_event_key") or ""),
                            str(row.get("created_at") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
                return {"inserted": inserted, "skipped": skipped, "row_count": len(rows)}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist trade intents: {exc}") from exc

    def persist_trade_watcher_lifecycle(
        self,
        *,
        intents: list[dict[str, Any]],
        paper_positions: list[dict[str, Any]],
        paper_fills: list[dict[str, Any]],
        signal_events: list[dict[str, Any]],
    ) -> dict[str, dict[str, int]]:
        """Persist one paper-watcher lifecycle batch in one transaction.

        The conflict behavior intentionally matches the focused persistence
        methods used by the watcher previously: intents, fills, and events are
        idempotent inserts while positions are replaceable lifecycle snapshots.
        """

        self.initialize()
        intent_stats = {"inserted": 0, "skipped": 0, "row_count": len(intents)}
        position_stats = {"inserted": 0, "row_count": len(paper_positions)}
        fill_stats = {"inserted": 0, "skipped": 0, "row_count": len(paper_fills)}
        event_stats = {"inserted": 0, "skipped": 0}
        try:
            with self._connect() as connection:
                for row in intents:
                    intent_id = str(row.get("intent_id") or "")
                    ticker = str(row.get("ticker") or "").upper()
                    market_date = str(row.get("market_date") or "")[:10]
                    if not intent_id or not ticker or not market_date:
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO trade_intents
                        (intent_id, signal_id, market_date, ticker, mode, lifecycle_state,
                         action, decision_time, decision_price, trigger_price, stop_price,
                         target_price, quantity, notional, risk_amount, reason,
                         blocked_reason, source_observation_id, notification_event_key,
                         created_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            intent_id,
                            str(row.get("signal_id") or ""),
                            market_date,
                            ticker,
                            str(row.get("mode") or ""),
                            str(row.get("lifecycle_state") or ""),
                            str(row.get("action") or ""),
                            str(row.get("decision_time") or ""),
                            _float_or_none(row.get("decision_price")),
                            _float_or_none(row.get("trigger_price")),
                            _float_or_none(row.get("stop_price")),
                            _float_or_none(row.get("target_price")),
                            _float_or_none(row.get("quantity")),
                            _float_or_none(row.get("notional")),
                            _float_or_none(row.get("risk_amount")),
                            str(row.get("reason") or ""),
                            str(row.get("blocked_reason") or ""),
                            str(row.get("source_observation_id") or ""),
                            str(row.get("notification_event_key") or ""),
                            str(row.get("created_at") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        intent_stats["inserted"] += 1
                    else:
                        intent_stats["skipped"] += 1

                for row in paper_positions:
                    position_id = str(row.get("position_id") or "")
                    ticker = str(row.get("ticker") or "").upper()
                    market_date = str(row.get("market_date") or "")[:10]
                    if not position_id or not ticker or not market_date:
                        continue
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO paper_positions
                        (position_id, signal_id, market_date, ticker, status, quantity,
                         entry_intent_id, exit_intent_id, opened_at, closed_at, entry_price,
                         exit_price, stop_price, target_price, notional, realized_pnl,
                         realized_return_pct, max_favorable_excursion,
                         max_adverse_excursion, updated_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            position_id,
                            str(row.get("signal_id") or ""),
                            market_date,
                            ticker,
                            str(row.get("status") or ""),
                            float(row.get("quantity") or 0.0),
                            str(row.get("entry_intent_id") or ""),
                            str(row.get("exit_intent_id") or ""),
                            str(row.get("opened_at") or ""),
                            str(row.get("closed_at") or ""),
                            _float_or_none(row.get("entry_price")),
                            _float_or_none(row.get("exit_price")),
                            _float_or_none(row.get("stop_price")),
                            _float_or_none(row.get("target_price")),
                            _float_or_none(row.get("notional")),
                            _float_or_none(row.get("realized_pnl")),
                            _float_or_none(row.get("realized_return_pct")),
                            _float_or_none(row.get("max_favorable_excursion")),
                            _float_or_none(row.get("max_adverse_excursion")),
                            str(row.get("updated_at") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    position_stats["inserted"] += 1

                for row in paper_fills:
                    fill_id = str(row.get("fill_id") or "")
                    position_id = str(row.get("position_id") or "")
                    intent_id = str(row.get("intent_id") or "")
                    ticker = str(row.get("ticker") or "").upper()
                    market_date = str(row.get("market_date") or "")[:10]
                    if not fill_id or not position_id or not intent_id or not ticker:
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO paper_trade_fills
                        (fill_id, position_id, intent_id, signal_id, market_date, ticker,
                         side, fill_time, fill_price, quantity, gross_notional,
                         slippage_bps, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            fill_id,
                            position_id,
                            intent_id,
                            str(row.get("signal_id") or ""),
                            market_date,
                            ticker,
                            str(row.get("side") or ""),
                            str(row.get("fill_time") or ""),
                            float(row.get("fill_price") or 0.0),
                            float(row.get("quantity") or 0.0),
                            float(row.get("gross_notional") or 0.0),
                            float(row.get("slippage_bps") or 0.0),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        fill_stats["inserted"] += 1
                    else:
                        fill_stats["skipped"] += 1

                for row in signal_events:
                    event_id = str(row.get("event_id") or "")
                    signal_id = str(row.get("signal_id") or "")
                    if not event_id or not signal_id:
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO signal_events
                        (event_id, signal_id, event_type, event_timestamp, event_price,
                         source, notes, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            signal_id,
                            str(row.get("event_type") or ""),
                            str(row.get("event_timestamp") or ""),
                            _float_or_none(row.get("event_price")),
                            str(row.get("source") or ""),
                            str(row.get("notes") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        event_stats["inserted"] += 1
                    else:
                        event_stats["skipped"] += 1
        except sqlite3.Error as exc:
            raise StorageError(
                f"Could not atomically persist trade watcher lifecycle: {exc}"
            ) from exc
        return {
            "intents": intent_stats,
            "paper_positions": position_stats,
            "paper_fills": fill_stats,
            "signal_events": event_stats,
        }

    def load_trade_intents(
        self,
        *,
        market_date: str | None = None,
        ticker: str | None = None,
        signal_id: str | None = None,
        action: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date[:10])
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker.upper())
        if signal_id:
            clauses.append("signal_id = ?")
            params.append(signal_id)
        if action:
            clauses.append("action = ?")
            params.append(action)
        query = "SELECT * FROM trade_intents"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY decision_time DESC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load trade intents: {exc}") from exc

    def persist_paper_positions(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        self.initialize()
        inserted = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    position_id = str(row.get("position_id") or "")
                    ticker = str(row.get("ticker") or "").upper()
                    market_date = str(row.get("market_date") or "")[:10]
                    if not position_id or not ticker or not market_date:
                        continue
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO paper_positions
                        (position_id, signal_id, market_date, ticker, status, quantity,
                         entry_intent_id, exit_intent_id, opened_at, closed_at, entry_price,
                         exit_price, stop_price, target_price, notional, realized_pnl,
                         realized_return_pct, max_favorable_excursion,
                         max_adverse_excursion, updated_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            position_id,
                            str(row.get("signal_id") or ""),
                            market_date,
                            ticker,
                            str(row.get("status") or ""),
                            float(row.get("quantity") or 0.0),
                            str(row.get("entry_intent_id") or ""),
                            str(row.get("exit_intent_id") or ""),
                            str(row.get("opened_at") or ""),
                            str(row.get("closed_at") or ""),
                            _float_or_none(row.get("entry_price")),
                            _float_or_none(row.get("exit_price")),
                            _float_or_none(row.get("stop_price")),
                            _float_or_none(row.get("target_price")),
                            _float_or_none(row.get("notional")),
                            _float_or_none(row.get("realized_pnl")),
                            _float_or_none(row.get("realized_return_pct")),
                            _float_or_none(row.get("max_favorable_excursion")),
                            _float_or_none(row.get("max_adverse_excursion")),
                            str(row.get("updated_at") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    inserted += 1
                return {"inserted": inserted, "row_count": len(rows)}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist paper positions: {exc}") from exc

    def load_paper_positions(
        self,
        *,
        market_date: str | None = None,
        ticker: str | None = None,
        signal_id: str | None = None,
        status: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date[:10])
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker.upper())
        if signal_id:
            clauses.append("signal_id = ?")
            params.append(signal_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        query = "SELECT * FROM paper_positions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC, updated_at DESC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load paper positions: {exc}") from exc

    def persist_paper_trade_fills(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        self.initialize()
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    fill_id = str(row.get("fill_id") or "")
                    position_id = str(row.get("position_id") or "")
                    intent_id = str(row.get("intent_id") or "")
                    ticker = str(row.get("ticker") or "").upper()
                    market_date = str(row.get("market_date") or "")[:10]
                    if not fill_id or not position_id or not intent_id or not ticker:
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO paper_trade_fills
                        (fill_id, position_id, intent_id, signal_id, market_date, ticker,
                         side, fill_time, fill_price, quantity, gross_notional,
                         slippage_bps, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            fill_id,
                            position_id,
                            intent_id,
                            str(row.get("signal_id") or ""),
                            market_date,
                            ticker,
                            str(row.get("side") or ""),
                            str(row.get("fill_time") or ""),
                            float(row.get("fill_price") or 0.0),
                            float(row.get("quantity") or 0.0),
                            float(row.get("gross_notional") or 0.0),
                            float(row.get("slippage_bps") or 0.0),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
                return {"inserted": inserted, "skipped": skipped, "row_count": len(rows)}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist paper trade fills: {exc}") from exc

    def load_paper_trade_fills(
        self,
        *,
        market_date: str | None = None,
        ticker: str | None = None,
        signal_id: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date[:10])
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker.upper())
        if signal_id:
            clauses.append("signal_id = ?")
            params.append(signal_id)
        query = "SELECT * FROM paper_trade_fills"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY fill_time DESC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load paper trade fills: {exc}") from exc

    def persist_signal_return_attribution(
        self,
        rows: list[dict[str, Any]],
        *,
        replace: bool = True,
    ) -> dict[str, int]:
        self.initialize()
        inserted = 0
        skipped = 0
        signal_ids = sorted(
            {str(row.get("signal_id") or "") for row in rows if row.get("signal_id")}
        )
        try:
            with self._connect() as connection:
                if replace:
                    for signal_id in signal_ids:
                        connection.execute(
                            "DELETE FROM signal_return_attribution WHERE signal_id = ?",
                            (signal_id,),
                        )
                for row in rows:
                    attribution_id = str(row.get("attribution_id") or "")
                    signal_id = str(row.get("signal_id") or "")
                    if not attribution_id or not signal_id:
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO signal_return_attribution
                        (attribution_id, signal_id, ticker, market_date, entry_policy,
                         exit_policy, entry_price, exit_price, return_pct,
                         max_favorable_excursion, max_adverse_excursion, drawdown_pct,
                         hit_target_1, hit_target_2, hit_invalidation,
                         trigger_activated, audit_status, scenario_or_recommended,
                         calculated_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            attribution_id,
                            signal_id,
                            str(row.get("ticker") or ""),
                            str(row.get("market_date") or ""),
                            str(row.get("entry_policy") or ""),
                            str(row.get("exit_policy") or ""),
                            _float_or_none(row.get("entry_price")),
                            _float_or_none(row.get("exit_price")),
                            _float_or_none(row.get("return_pct")),
                            _float_or_none(row.get("max_favorable_excursion")),
                            _float_or_none(row.get("max_adverse_excursion")),
                            _float_or_none(row.get("drawdown_pct")),
                            _bool_or_none(row.get("hit_target_1")),
                            _bool_or_none(row.get("hit_target_2")),
                            _bool_or_none(row.get("hit_invalidation")),
                            _bool_or_none(row.get("trigger_activated")),
                            str(row.get("audit_status") or ""),
                            str(row.get("scenario_or_recommended") or ""),
                            str(row.get("calculated_at") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
                return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist signal return attribution: {exc}") from exc

    def load_signal_return_attribution(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 50000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("market_date >= ?")
            params.append(start)
        if end:
            clauses.append("market_date <= ?")
            params.append(end)
        query = "SELECT * FROM signal_return_attribution"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC, ticker ASC, entry_policy ASC, exit_policy ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load signal return attribution: {exc}") from exc

    def persist_daily_signal_performance(self, rows: list[dict[str, Any]]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                for row in rows:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO daily_signal_performance
                        (market_date, signal_count, alerted_count, no_trade_count,
                         audited_count, missing_outcome_count, top1_return, top3_return,
                         top5_return, top1_close_return, top3_close_return,
                         top5_close_return, top1_lunch_return, top3_lunch_return,
                         top5_lunch_return, best_pick_return, worst_pick_return,
                         max_drawdown, hit_rate, outcome_coverage_pct, evidence_status,
                         payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("market_date") or ""),
                            int(row.get("signal_count") or 0),
                            int(row.get("alerted_count") or 0),
                            int(row.get("no_trade_count") or 0),
                            int(row.get("audited_count") or 0),
                            int(row.get("missing_outcome_count") or 0),
                            _float_or_none(row.get("top1_return")),
                            _float_or_none(row.get("top3_return")),
                            _float_or_none(row.get("top5_return")),
                            _float_or_none(row.get("top1_close_return")),
                            _float_or_none(row.get("top3_close_return")),
                            _float_or_none(row.get("top5_close_return")),
                            _float_or_none(row.get("top1_lunch_return")),
                            _float_or_none(row.get("top3_lunch_return")),
                            _float_or_none(row.get("top5_lunch_return")),
                            _float_or_none(row.get("best_pick_return")),
                            _float_or_none(row.get("worst_pick_return")),
                            _float_or_none(row.get("max_drawdown")),
                            _float_or_none(row.get("hit_rate")),
                            _float_or_none(row.get("outcome_coverage_pct")),
                            str(row.get("evidence_status") or ""),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist daily signal performance: {exc}") from exc

    def load_daily_signal_performance(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("market_date >= ?")
            params.append(start)
        if end:
            clauses.append("market_date <= ?")
            params.append(end)
        query = "SELECT * FROM daily_signal_performance"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load daily signal performance: {exc}") from exc

    def persist_prediction_run(
        self,
        run: dict[str, Any],
        predictions: list[dict[str, Any]],
        calibration: list[dict[str, Any]] | None = None,
        portfolio: list[dict[str, Any]] | None = None,
        *,
        replace: bool = True,
    ) -> dict[str, int]:
        self.initialize()
        run_id = str(run.get("prediction_run_id") or "")
        market_date = str(run.get("market_date") or "")
        if not run_id or not market_date:
            raise StorageError("Prediction run requires prediction_run_id and market_date.")
        calibration = list(calibration or [])
        portfolio = list(portfolio or [])
        try:
            with self._connect() as connection:
                if replace:
                    existing = connection.execute(
                        """
                        SELECT prediction_run_id
                        FROM prediction_runs
                        WHERE market_date = ? AND model_version = ?
                        """,
                        (market_date, str(run.get("model_version") or "")),
                    ).fetchall()
                    for old_run in [str(row[0]) for row in existing]:
                        connection.execute(
                            "DELETE FROM candidate_predictions WHERE prediction_run_id = ?",
                            (old_run,),
                        )
                        connection.execute(
                            "DELETE FROM prediction_calibration WHERE prediction_run_id = ?",
                            (old_run,),
                        )
                        connection.execute(
                            "DELETE FROM portfolio_expectancy WHERE prediction_run_id = ?",
                            (old_run,),
                        )
                    connection.execute(
                        """
                        DELETE FROM prediction_runs
                        WHERE market_date = ? AND model_version = ?
                        """,
                        (market_date, str(run.get("model_version") or "")),
                    )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO prediction_runs
                    (prediction_run_id, created_at, market_date, model_version, config_hash,
                     prediction_mode, training_start_date, training_end_date, test_start_date,
                     test_end_date, sample_size, real_day_count, outcome_label_count,
                     data_quality_summary_json, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(run.get("created_at") or ""),
                        market_date,
                        str(run.get("model_version") or ""),
                        str(run.get("config_hash") or ""),
                        str(run.get("prediction_mode") or ""),
                        str(run.get("training_start_date") or ""),
                        str(run.get("training_end_date") or ""),
                        str(run.get("test_start_date") or ""),
                        str(run.get("test_end_date") or ""),
                        int(run.get("sample_size") or 0),
                        int(run.get("real_day_count") or 0),
                        int(run.get("outcome_label_count") or 0),
                        json.dumps(
                            run.get("data_quality_summary_json")
                            or run.get("data_quality_summary")
                            or {},
                            sort_keys=True,
                        ),
                        json.dumps(run.get("payload_json") or run, sort_keys=True),
                    ),
                )
                prediction_count = 0
                for row in predictions:
                    prediction_id = str(row.get("prediction_id") or "")
                    if not prediction_id:
                        continue
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO candidate_predictions
                        (prediction_id, prediction_run_id, signal_id, scan_id, market_date,
                         ticker, rank, primary_setup, source, source_confidence,
                         alpha_score, launch_bucket, probability_positive_1m,
                         probability_positive_5m, probability_positive_15m,
                         probability_positive_lunch, probability_positive_close,
                         probability_hit_target_1, probability_hit_target_2,
                         probability_hit_invalidation, expected_return_1m,
                         expected_return_5m, expected_return_15m, expected_return_lunch,
                         expected_return_close, expected_max_drawdown, expected_mfe,
                         expected_mae, expected_value_score, uncertainty_bucket,
                         confidence_interval_low, confidence_interval_high,
                         calibration_bucket, prediction_status, no_alert_reason, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            prediction_id,
                            run_id,
                            str(row.get("signal_id") or ""),
                            str(row.get("scan_id") or ""),
                            str(row.get("market_date") or market_date),
                            str(row.get("ticker") or "").upper(),
                            _int_or_none(row.get("rank")),
                            str(row.get("primary_setup") or ""),
                            str(row.get("source") or ""),
                            _float_or_none(row.get("source_confidence")),
                            _float_or_none(row.get("alpha_score")),
                            str(row.get("launch_bucket") or ""),
                            _float_or_none(row.get("probability_positive_1m")),
                            _float_or_none(row.get("probability_positive_5m")),
                            _float_or_none(row.get("probability_positive_15m")),
                            _float_or_none(row.get("probability_positive_lunch")),
                            _float_or_none(row.get("probability_positive_close")),
                            _float_or_none(row.get("probability_hit_target_1")),
                            _float_or_none(row.get("probability_hit_target_2")),
                            _float_or_none(row.get("probability_hit_invalidation")),
                            _float_or_none(row.get("expected_return_1m")),
                            _float_or_none(row.get("expected_return_5m")),
                            _float_or_none(row.get("expected_return_15m")),
                            _float_or_none(row.get("expected_return_lunch")),
                            _float_or_none(row.get("expected_return_close")),
                            _float_or_none(row.get("expected_max_drawdown")),
                            _float_or_none(row.get("expected_mfe")),
                            _float_or_none(row.get("expected_mae")),
                            _float_or_none(row.get("expected_value_score")),
                            str(row.get("uncertainty_bucket") or ""),
                            _float_or_none(row.get("confidence_interval_low")),
                            _float_or_none(row.get("confidence_interval_high")),
                            str(row.get("calibration_bucket") or ""),
                            str(row.get("prediction_status") or ""),
                            str(row.get("no_alert_reason") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    prediction_count += 1
                calibration_count = 0
                for row in calibration:
                    calibration_id = str(row.get("calibration_id") or "")
                    if not calibration_id:
                        continue
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO prediction_calibration
                        (calibration_id, prediction_run_id, model_version, evaluated_at,
                         horizon, sample_size, brier_score, calibration_error,
                         average_predicted_probability, actual_hit_rate, bucket_json,
                         payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            calibration_id,
                            str(row.get("prediction_run_id") or run_id),
                            str(row.get("model_version") or run.get("model_version") or ""),
                            str(row.get("evaluated_at") or ""),
                            str(row.get("horizon") or ""),
                            int(row.get("sample_size") or 0),
                            _float_or_none(row.get("brier_score")),
                            _float_or_none(row.get("calibration_error")),
                            _float_or_none(row.get("average_predicted_probability")),
                            _float_or_none(row.get("actual_hit_rate")),
                            json.dumps(row.get("bucket_json") or {}, sort_keys=True),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    calibration_count += 1
                portfolio_count = 0
                for row in portfolio:
                    row_id = str(row.get("id") or "")
                    if not row_id:
                        continue
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO portfolio_expectancy
                        (id, prediction_run_id, market_date, portfolio_type,
                         expected_return_1m, expected_return_5m, expected_return_15m,
                         expected_return_lunch, expected_return_close, expected_drawdown,
                         probability_positive, uncertainty_bucket, sample_size,
                         evidence_status, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row_id,
                            run_id,
                            str(row.get("market_date") or market_date),
                            str(row.get("portfolio_type") or ""),
                            _float_or_none(row.get("expected_return_1m")),
                            _float_or_none(row.get("expected_return_5m")),
                            _float_or_none(row.get("expected_return_15m")),
                            _float_or_none(row.get("expected_return_lunch")),
                            _float_or_none(row.get("expected_return_close")),
                            _float_or_none(row.get("expected_drawdown")),
                            _float_or_none(row.get("probability_positive")),
                            str(row.get("uncertainty_bucket") or ""),
                            int(row.get("sample_size") or 0),
                            str(row.get("evidence_status") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    portfolio_count += 1
                return {
                    "runs": 1,
                    "predictions": prediction_count,
                    "calibration": calibration_count,
                    "portfolio": portfolio_count,
                }
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist prediction run: {exc}") from exc

    def load_prediction_runs(
        self,
        *,
        market_date: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date)
        query = "SELECT * FROM prediction_runs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_prediction_run_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load prediction runs: {exc}") from exc

    def load_candidate_predictions(
        self,
        *,
        prediction_run_id: str | None = None,
        market_date: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if prediction_run_id:
            clauses.append("prediction_run_id = ?")
            params.append(prediction_run_id)
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date)
        query = "SELECT * FROM candidate_predictions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC, COALESCE(rank, 999999), ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load candidate predictions: {exc}") from exc

    def load_prediction_calibration(
        self,
        *,
        prediction_run_id: str | None = None,
        model_version: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if prediction_run_id:
            clauses.append("prediction_run_id = ?")
            params.append(prediction_run_id)
        if model_version:
            clauses.append("model_version = ?")
            params.append(model_version)
        query = "SELECT * FROM prediction_calibration"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY evaluated_at DESC, horizon ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_calibration_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load prediction calibration: {exc}") from exc

    def load_portfolio_expectancy(
        self,
        *,
        prediction_run_id: str | None = None,
        market_date: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if prediction_run_id:
            clauses.append("prediction_run_id = ?")
            params.append(prediction_run_id)
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date)
        query = "SELECT * FROM portfolio_expectancy"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC, portfolio_type ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load portfolio expectancy: {exc}") from exc

    def persist_daily_market_movers(
        self,
        rows: list[dict[str, Any]],
        *,
        market_date: str,
        replace: bool = True,
    ) -> dict[str, int]:
        self.initialize()
        inserted = 0
        try:
            with self._connect() as connection:
                if replace:
                    connection.execute(
                        "DELETE FROM daily_market_movers WHERE market_date = ?",
                        (market_date,),
                    )
                for row in rows:
                    cursor = connection.execute(
                        """
                        INSERT OR REPLACE INTO daily_market_movers
                        (mover_id, market_date, ticker, company, rank, price,
                         change_pct, volume, dollar_volume, high, low, open, close,
                         source, source_url, source_confidence, extracted_at,
                         data_quality, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("mover_id") or ""),
                            str(row.get("market_date") or row.get("date") or market_date),
                            str(row.get("ticker") or "").upper(),
                            str(row.get("company") or ""),
                            _int_or_none(row.get("rank")),
                            _float_or_none(row.get("price")),
                            _float_or_none(row.get("change_pct")),
                            _float_or_none(row.get("volume")),
                            _float_or_none(row.get("dollar_volume")),
                            _float_or_none(row.get("high")),
                            _float_or_none(row.get("low")),
                            _float_or_none(row.get("open")),
                            _float_or_none(row.get("close")),
                            str(row.get("source") or ""),
                            str(row.get("source_url") or ""),
                            _float_or_none(row.get("source_confidence")),
                            str(row.get("extracted_at") or ""),
                            str(row.get("data_quality") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                return {"inserted": inserted, "row_count": len(rows)}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist daily market movers: {exc}") from exc

    def load_daily_market_movers(
        self,
        *,
        market_date: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date)
        if start:
            clauses.append("market_date >= ?")
            params.append(start)
        if end:
            clauses.append("market_date <= ?")
            params.append(end)
        query = "SELECT * FROM daily_market_movers"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC, COALESCE(rank, 999999) ASC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load daily market movers: {exc}") from exc

    def persist_daily_review(
        self,
        run: dict[str, Any],
        items: list[dict[str, Any]],
        events: list[dict[str, Any]],
        *,
        replace: bool = True,
    ) -> dict[str, int]:
        self.initialize()
        review_id = str(run.get("review_id") or "")
        market_date = str(run.get("market_date") or "")
        if not review_id or not market_date:
            raise StorageError("Daily review run requires review_id and market_date.")
        try:
            with self._connect() as connection:
                if replace:
                    existing = connection.execute(
                        "SELECT review_id FROM daily_review_runs WHERE market_date = ?",
                        (market_date,),
                    ).fetchall()
                    existing_ids = [str(row[0]) for row in existing]
                    for old_review_id in existing_ids:
                        connection.execute(
                            "DELETE FROM learning_backfeed_events WHERE review_id = ?",
                            (old_review_id,),
                        )
                        connection.execute(
                            "DELETE FROM daily_review_items WHERE review_id = ?",
                            (old_review_id,),
                        )
                    connection.execute(
                        "DELETE FROM daily_review_runs WHERE market_date = ?",
                        (market_date,),
                    )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO daily_review_runs
                    (review_id, market_date, created_at, source_status, mover_count,
                     signal_count, matched_pick_count, missed_winner_count,
                     false_positive_count, correct_avoid_count, missing_outcome_count,
                     review_status, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_id,
                        market_date,
                        str(run.get("created_at") or ""),
                        str(run.get("source_status") or ""),
                        int(run.get("mover_count") or 0),
                        int(run.get("signal_count") or 0),
                        int(run.get("matched_pick_count") or 0),
                        int(run.get("missed_winner_count") or 0),
                        int(run.get("false_positive_count") or 0),
                        int(run.get("correct_avoid_count") or 0),
                        int(run.get("missing_outcome_count") or 0),
                        str(run.get("review_status") or ""),
                        json.dumps(run.get("payload_json") or run, sort_keys=True),
                    ),
                )
                item_count = 0
                for item in items:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO daily_review_items
                        (item_id, review_id, market_date, ticker, category,
                         dawnstrike_rank, mover_rank, alpha_score, setup, source,
                         catalyst_category, risk_flags_json, avoid_reasons_json,
                         why_picked, why_missed, what_happened, lesson,
                         backfeed_action, return_1m, return_5m, return_15m,
                         return_lunch, return_close, high_opportunity_return,
                         drawdown_pct, miss_reason, missed_at_stage,
                         missed_feature_gap, could_have_been_caught, catchability,
                         failure_reason, success_reason, review_grade, audit_status,
                         payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(item.get("item_id") or ""),
                            review_id,
                            str(item.get("market_date") or market_date),
                            str(item.get("ticker") or "").upper(),
                            str(item.get("category") or ""),
                            _int_or_none(item.get("dawnstrike_rank")),
                            _int_or_none(item.get("mover_rank")),
                            _float_or_none(item.get("alpha_score")),
                            str(item.get("setup") or ""),
                            str(item.get("source") or ""),
                            str(item.get("catalyst_category") or ""),
                            json.dumps(item.get("risk_flags_json") or [], sort_keys=True),
                            json.dumps(item.get("avoid_reasons_json") or [], sort_keys=True),
                            str(item.get("why_picked") or ""),
                            str(item.get("why_missed") or ""),
                            str(item.get("what_happened") or ""),
                            str(item.get("lesson") or ""),
                            str(item.get("backfeed_action") or ""),
                            _float_or_none(item.get("return_1m")),
                            _float_or_none(item.get("return_5m")),
                            _float_or_none(item.get("return_15m")),
                            _float_or_none(item.get("return_lunch")),
                            _float_or_none(item.get("return_close")),
                            _float_or_none(item.get("high_opportunity_return")),
                            _float_or_none(item.get("drawdown_pct")),
                            str(item.get("miss_reason") or ""),
                            str(item.get("missed_at_stage") or ""),
                            str(item.get("missed_feature_gap") or ""),
                            _bool_or_none(item.get("could_have_been_caught")),
                            str(item.get("catchability") or ""),
                            str(item.get("failure_reason") or ""),
                            str(item.get("success_reason") or ""),
                            str(item.get("review_grade") or ""),
                            str(item.get("audit_status") or ""),
                            json.dumps(item.get("payload_json") or item, sort_keys=True),
                        ),
                    )
                    item_count += 1
                event_count = 0
                for event in events:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO learning_backfeed_events
                        (event_id, review_id, market_date, ticker, event_type, target,
                         before_value, suggested_adjustment, confidence, sample_size,
                         reason, applied, applied_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(event.get("event_id") or ""),
                            review_id,
                            str(event.get("market_date") or market_date),
                            str(event.get("ticker") or "").upper(),
                            str(event.get("event_type") or ""),
                            str(event.get("target") or ""),
                            _float_or_none(event.get("before_value")),
                            _float_or_none(event.get("suggested_adjustment")),
                            float(event.get("confidence") or 0.0),
                            int(event.get("sample_size") or 0),
                            str(event.get("reason") or ""),
                            1 if event.get("applied") else 0,
                            str(event.get("applied_at") or ""),
                            json.dumps(event.get("payload_json") or event, sort_keys=True),
                        ),
                    )
                    event_count += 1
                return {"runs": 1, "items": item_count, "events": event_count}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist daily review: {exc}") from exc

    def load_daily_review_runs(
        self,
        *,
        market_date: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date)
        query = "SELECT * FROM daily_review_runs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC, created_at DESC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load daily review runs: {exc}") from exc

    def load_daily_review_items(
        self,
        *,
        review_id: str | None = None,
        market_date: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if review_id:
            clauses.append("review_id = ?")
            params.append(review_id)
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date)
        query = "SELECT * FROM daily_review_items"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC, COALESCE(mover_rank, 999999), ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load daily review items: {exc}") from exc

    def load_learning_backfeed_events(
        self,
        *,
        review_id: str | None = None,
        market_date: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if review_id:
            clauses.append("review_id = ?")
            params.append(review_id)
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date)
        query = "SELECT * FROM learning_backfeed_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC, event_type ASC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load learning backfeed events: {exc}") from exc

    def replace_learning_backfeed_events(
        self,
        *,
        review_id: str,
        events: list[dict[str, Any]],
    ) -> dict[str, int]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM learning_backfeed_events WHERE review_id = ?",
                    (review_id,),
                )
                inserted = 0
                for event in events:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO learning_backfeed_events
                        (event_id, review_id, market_date, ticker, event_type, target,
                         before_value, suggested_adjustment, confidence, sample_size,
                         reason, applied, applied_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(event.get("event_id") or ""),
                            review_id,
                            str(event.get("market_date") or ""),
                            str(event.get("ticker") or "").upper(),
                            str(event.get("event_type") or ""),
                            str(event.get("target") or ""),
                            _float_or_none(event.get("before_value")),
                            _float_or_none(event.get("suggested_adjustment")),
                            _float_or_none(event.get("confidence")),
                            _int_or_none(event.get("sample_size")),
                            str(event.get("reason") or ""),
                            1 if event.get("applied") else 0,
                            str(event.get("applied_at") or ""),
                            json.dumps(event.get("payload_json") or event, sort_keys=True),
                        ),
                    )
                    inserted += 1
                return {"inserted": inserted, "row_count": len(events)}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not replace learning backfeed events: {exc}") from exc

    def _load_payloads(
        self, connection: sqlite3.Connection, table: str, run_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            f"SELECT payload_json FROM {table} WHERE run_id = ? ORDER BY rank ASC",  # noqa: S608
            (run_id,),
        ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)


def _historical_signal_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_value(row["raw_payload_json"])
    return {
        "signal_id": str(row["signal_id"]),
        "scan_id": str(row["scan_id"] or ""),
        "alpha_signal_id": str(row["alpha_signal_id"] or ""),
        "generated_at": str(row["generated_at"] or ""),
        "market_date": str(row["market_date"] or ""),
        "ticker": str(row["ticker"] or ""),
        "company": str(row["company"] or ""),
        "rank": row["rank"],
        "source": str(row["source"] or ""),
        "source_url": str(row["source_url"] or ""),
        "source_confidence": row["source_confidence"],
        "data_source_kind": str(row["data_source_kind"] or ""),
        "model_version": str(row["model_version"] or ""),
        "config_hash": str(row["config_hash"] or ""),
        "primary_setup": str(row["primary_setup"] or ""),
        "setup_grade": str(row["setup_grade"] or ""),
        "signal_label": str(row["signal_label"] or ""),
        "entry_watch_level": row["entry_watch_level"],
        "entry_trigger_type": str(row["entry_trigger_type"] or ""),
        "entry_condition": str(row["entry_condition"] or ""),
        "confirmation_condition": str(row["confirmation_condition"] or ""),
        "exit_line": row["exit_line"],
        "invalidation_level": row["invalidation_level"],
        "target_1": row["target_1"],
        "target_2": row["target_2"],
        "risk_flags_json": _json_value(row["risk_flags_json"], default=[]),
        "avoid_reasons_json": _json_value(row["avoid_reasons_json"], default=[]),
        "catalyst_summary": str(row["catalyst_summary"] or ""),
        "telegram_event_key": str(row["telegram_event_key"] or ""),
        "was_alerted": bool(row["was_alerted"]),
        "no_trade_reason": str(row["no_trade_reason"] or ""),
        "raw_payload_json": payload,
    }


def _selection_identity_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "selection_id": str(row["selection_id"]),
        "scan_id": str(row["scan_id"]),
        "signal_id": str(row["signal_id"]),
        "ticker": str(row["ticker"]),
        "rank": row["rank"],
        "strategy_id": str(row["strategy_id"]),
        "strategy_version": str(row["strategy_version"]),
        "cohort": str(row["cohort"]),
        "decision": str(row["decision"]),
        "selected_at": str(row["selected_at"]),
        "event_key": str(row["event_key"]),
        "body_sha256": str(row["body_sha256"]),
        "payload_json": _json_value(row["payload_json"]),
    }


def _notification_delivery_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "membership_id": str(row["membership_id"]),
        "selection_id": str(row["selection_id"] or ""),
        "scan_id": str(row["scan_id"]),
        "signal_id": str(row["signal_id"]),
        "ticker": str(row["ticker"]),
        "strategy_id": str(row["strategy_id"]),
        "strategy_version": str(row["strategy_version"]),
        "cohort": str(row["cohort"]),
        "decision": str(row["decision"]),
        "selected_at": str(row["selected_at"]),
        "event_key": str(row["event_key"]),
        "channel": str(row["channel"]),
        "delivery_status": str(row["delivery_status"]),
        "attempted_at": str(row["attempted_at"]),
        "delivered_at": str(row["delivered_at"] or ""),
        "body_sha256": str(row["body_sha256"]),
        "payload_json": _json_value(row["payload_json"]),
    }


def _json_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_value(row["payload_json"])
    merged = {key: row[key] for key in row.keys() if key != "payload_json"}
    if isinstance(payload, dict):
        merged.update(payload)
    return merged


def _prediction_run_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_value(row["payload_json"])
    data_quality = _json_value(row["data_quality_summary_json"])
    merged = {
        key: row[key]
        for key in row.keys()
        if key not in {"payload_json", "data_quality_summary_json"}
    }
    merged["data_quality_summary"] = data_quality
    if isinstance(payload, dict):
        merged.update(payload)
        merged["data_quality_summary"] = payload.get("data_quality_summary", data_quality)
    return merged


def _calibration_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_value(row["payload_json"])
    bucket = _json_value(row["bucket_json"])
    merged = {
        key: row[key]
        for key in row.keys()
        if key not in {"payload_json", "bucket_json"}
    }
    merged["bucket_json"] = bucket
    if isinstance(payload, dict):
        merged.update(payload)
        merged["bucket_json"] = payload.get("bucket_json", bucket)
    return merged


def _ensure_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()  # noqa: S608
    }
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")  # noqa: S608


def _json_value(value: Any, *, default: Any | None = None) -> Any:
    if default is None:
        default = {}
    if value in {None, ""}:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _float_or_none(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    text = str(value).replace("$", "").replace(",", "").replace("%", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    parsed = _float_or_none(value)
    return int(parsed) if parsed is not None else None


def _bool_or_none(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y"}:
        return 1
    if text in {"false", "f", "0", "no", "n"}:
        return 0
    return None


def _recommendation_payload(row: dict[str, Any], result: ScanResult) -> dict[str, Any]:
    return {
        "scan_id": result.run_id,
        "timestamp": row.get("as_of_timestamp") or result.created_at,
        "source_as_of_timestamp": row.get("as_of_timestamp") or "",
        "recorded_at": result.created_at,
        "rank": row.get("rank"),
        "ticker": row.get("ticker"),
        "score": row.get("score"),
        "component_scores": row.get("score_breakdown"),
        "total_score": row.get("total_score"),
        "explosive_score": row.get("explosive_score"),
        "tradability_score": row.get("tradability_score"),
        "catalyst_score": row.get("catalyst_score"),
        "risk_score": row.get("risk_score"),
        "expected_return_bucket": row.get("expected_return_bucket"),
        "confidence_bucket": row.get("confidence_bucket"),
        "model_version": row.get("model_version") or row.get("equation_version"),
        "config_hash": row.get("config_hash"),
        "thesis": _thesis(row),
        "catalyst_summary": row.get("catalyst_headline") or "No catalyst headline available.",
        "catalyst_tier": row.get("catalyst_tier") or "",
        "catalyst_category": row.get("catalyst_category") or "",
        "catalyst_quality_summary": row.get("catalyst_summary") or "",
        "catalyst_url": row.get("catalyst_url") or "",
        "action": row.get("action") or "",
        "classification": row.get("classification") or "",
        "predicted_action": row.get("predicted_action") or "",
        "entry_trigger": row.get("entry_trigger") or "",
        "confirmation_needed": row.get("confirmation_needed"),
        "invalidation": row.get("invalidation") or "",
        "target_1": row.get("target_1") or "",
        "target_2": row.get("target_2") or "",
        "risk_level": row.get("risk_level") or "",
        "premarket_structure": row.get("premarket_structure") or "",
        "structure_notes": row.get("structure_notes") or "",
        "float_rotation": row.get("float_rotation") or "",
        "float_rotation_label": row.get("float_rotation_label") or "",
        "do_not_enter_if": row.get("do_not_enter_if") or "",
        "data_confidence_score": row.get("data_confidence_score"),
        "data_warnings": row.get("data_warnings") or "",
        "field_sources": row.get("field_sources") or "",
        "risk_flags": row.get("risk_flags") or "",
        "breakout_trigger": row.get("breakout_trigger"),
        "pullback_zone_low": _pullback_part(row.get("pullback_zone"), 0),
        "pullback_zone_high": _pullback_part(row.get("pullback_zone"), 1),
        "invalidation_level": row.get("invalidation_level"),
        "first_target": row.get("first_target"),
        "stretch_target": row.get("stretch_target"),
        "exit_bias": row.get("best_exit_bias"),
        "confidence_level": row.get("setup_grade"),
        "data_quality_score": row.get("data_quality_score"),
        "source_lineage": row.get("source_lineage"),
        "source_confidence": row.get("source_confidence"),
        "stale_data_flag": row.get("stale_data_flag"),
    }


def _thesis(row: dict[str, Any]) -> str:
    return (
        f"{row.get('ticker')} ranked #{row.get('rank')} with score {row.get('score')}. "
        f"Watch {row.get('breakout_trigger')}, invalidation {row.get('invalidation_level')}, "
        f"first target {row.get('first_target')}."
    )


def _pullback_part(value: Any, index: int) -> str:
    parts = str(value or "").split("-", 1)
    if len(parts) != 2:
        return ""
    return parts[index].strip()
