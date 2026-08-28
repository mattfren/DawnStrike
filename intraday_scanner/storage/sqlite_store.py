"""SQLite storage adapter."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from intraday_scanner.errors import StorageError
from intraday_scanner.models import ScanResult
from intraday_scanner.scenario.contracts import (
    SCENARIO_FEATURE_SCHEMA_VERSION,
    SCENARIO_FORWARD_COHORT,
    SCENARIO_POLICY_VERSION,
    SCENARIO_STRATEGY_ID,
    canonical_hash,
)
from intraday_scanner.sql_safety import quote_sql_identifier, quote_sql_identifiers
from intraday_scanner.storage.read_only import connect_read_only
from intraday_scanner.storage.test_isolation import assert_test_database_isolated

_V6_PAYLOAD_TABLE_ORDERS = {
    "alpha_v6_experiments": "created_at",
    "alpha_v6_holdout_evaluations": "evaluated_at",
    "alpha_v6_datasets": "created_at",
    "alpha_v6_shadow_predictions": "generated_at",
    "alpha_v6_drift_reports": "created_at",
    "alpha_v6_promotion_reviews": "created_at",
    "alpha_v6_operational_receipts": "created_at",
}
_V6_SINGLE_PAYLOAD_COLUMNS = {
    "alpha_v6_drift_reports": {"drift_report_id", "created_at", "status", "payload_json"},
    "alpha_v6_promotion_reviews": {
        "review_id",
        "created_at",
        "status",
        "approved",
        "payload_json",
    },
    "alpha_v6_operational_receipts": {
        "receipt_id",
        "receipt_kind",
        "as_of_date",
        "created_at",
        "status",
        "input_hash_sha256",
        "payload_json",
    },
}


class SQLiteScanStore:
    def __init__(self, db_path: str | Path, *, read_only: bool = False):
        assert_test_database_isolated(db_path)
        self.db_path = Path(db_path)
        self.read_only = read_only

    def initialize(self) -> None:
        if self.read_only:
            with self._connect() as connection:
                connection.execute("SELECT 1")
            return
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
                    CREATE TABLE IF NOT EXISTS monitor_publication_receipts (
                        receipt_id TEXT PRIMARY KEY,
                        market_date TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        signal_id TEXT NOT NULL,
                        plan_hash_sha256 TEXT NOT NULL,
                        content_hash_sha256 TEXT NOT NULL UNIQUE,
                        publication_count INTEGER NOT NULL,
                        checked_at TEXT NOT NULL,
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
                        episode_id TEXT NOT NULL DEFAULT '',
                        strategy_id TEXT NOT NULL DEFAULT '',
                        account_id TEXT NOT NULL DEFAULT '',
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
                _ensure_columns(
                    connection,
                    "trade_intents",
                    {
                        "episode_id": "TEXT NOT NULL DEFAULT ''",
                        "strategy_id": "TEXT NOT NULL DEFAULT ''",
                        "account_id": "TEXT NOT NULL DEFAULT ''",
                    },
                )
                _backfill_trade_intent_identity(connection)
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
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_intents_episode_entry
                    ON trade_intents(market_date, account_id, strategy_id, episode_id)
                    WHERE episode_id <> ''
                      AND action IN ('ENTER_LONG', 'ENTER_SHORT');
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_intents_episode_entry_v2
                    ON trade_intents(market_date, account_id, strategy_id, episode_id)
                    WHERE episode_id <> ''
                      AND UPPER(TRIM(action)) IN ('ENTER_LONG', 'ENTER_SHORT');
                    CREATE INDEX IF NOT EXISTS idx_paper_positions_day_status
                    ON paper_positions(market_date, status, ticker);
                    CREATE INDEX IF NOT EXISTS idx_paper_fills_day_ticker
                    ON paper_trade_fills(market_date, ticker, fill_time);
                    CREATE TABLE IF NOT EXISTS scenario_news_items (
                        article_id TEXT PRIMARY KEY,
                        provider TEXT NOT NULL,
                        symbols_json TEXT NOT NULL,
                        headline TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        source TEXT NOT NULL,
                        author TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        fetched_at TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        timing_kind TEXT NOT NULL,
                        source_tier TEXT NOT NULL,
                        content_hash_sha256 TEXT NOT NULL,
                        source_lineage_hash_sha256 TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_scenario_news_created
                    ON scenario_news_items(created_at, timing_kind);
                    CREATE TABLE IF NOT EXISTS scenario_claim_extractions (
                        extraction_id TEXT PRIMARY KEY,
                        article_id TEXT NOT NULL,
                        model TEXT NOT NULL,
                        response_id TEXT NOT NULL,
                        prompt_version TEXT NOT NULL,
                        schema_version TEXT NOT NULL,
                        input_hash_sha256 TEXT NOT NULL,
                        output_hash_sha256 TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error_code TEXT NOT NULL,
                        usage_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        UNIQUE(article_id, model, prompt_version, schema_version, input_hash_sha256)
                    );
                    CREATE INDEX IF NOT EXISTS idx_scenario_extractions_article
                    ON scenario_claim_extractions(article_id, created_at);
                    CREATE TABLE IF NOT EXISTS scenario_decisions (
                        decision_id TEXT PRIMARY KEY,
                        article_id TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        market_date TEXT NOT NULL,
                        decision_at TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        directional_evidence_score REAL NOT NULL,
                        action TEXT NOT NULL,
                        calibration_status TEXT NOT NULL,
                        entry_trigger REAL,
                        invalidation_level REAL,
                        target_1 REAL,
                        time_stop TEXT NOT NULL,
                        source_tier TEXT NOT NULL,
                        source_lineage_hash_sha256 TEXT NOT NULL,
                        feature_hash_sha256 TEXT NOT NULL,
                        cohort TEXT NOT NULL,
                        policy_version TEXT NOT NULL,
                        feature_schema_version TEXT NOT NULL,
                        research_only INTEGER NOT NULL,
                        broker_execution_enabled INTEGER NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_scenario_decisions_date
                    ON scenario_decisions(market_date, cohort, ticker, decision_at);
                    CREATE TABLE IF NOT EXISTS scenario_events (
                        event_id TEXT PRIMARY KEY,
                        decision_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        event_timestamp TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS scenario_signal_links (
                        decision_id TEXT PRIMARY KEY,
                        signal_id TEXT,
                        scan_id TEXT,
                        paper_intent_id TEXT,
                        entry_intent_id TEXT,
                        exit_intent_id TEXT,
                        position_id TEXT,
                        entry_fill_id TEXT,
                        exit_fill_id TEXT,
                        paper_trade_id TEXT,
                        outcome_id TEXT,
                        cohort TEXT NOT NULL,
                        strategy_id TEXT NOT NULL,
                        strategy_version TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_scenario_links_signal
                    ON scenario_signal_links(signal_id, cohort);
                    CREATE TABLE IF NOT EXISTS scenario_model_registry (
                        model_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        policy_version TEXT NOT NULL,
                        feature_schema_version TEXT NOT NULL,
                        calibration_status TEXT NOT NULL,
                        sample_count INTEGER NOT NULL,
                        promotion_state TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS scenario_run_receipts (
                        run_id TEXT PRIMARY KEY,
                        run_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        error_code TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS scenario_daily_performance (
                        market_date TEXT NOT NULL,
                        cohort TEXT NOT NULL,
                        strategy_id TEXT NOT NULL,
                        policy_version TEXT NOT NULL,
                        signal_count INTEGER NOT NULL,
                        triggered_count INTEGER NOT NULL,
                        closed_eligible_count INTEGER NOT NULL,
                        open_count INTEGER NOT NULL,
                        missing_count INTEGER NOT NULL,
                        quarantined_count INTEGER NOT NULL,
                        gross_return_pct REAL,
                        modeled_after_cost_return_pct REAL,
                        benchmark_return_pct REAL,
                        excess_return_pct REAL,
                        hit_rate_pct REAL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY(market_date, cohort, strategy_id, policy_version)
                    );
                    CREATE TABLE IF NOT EXISTS scenario_replay_trades (
                        replay_trade_id TEXT PRIMARY KEY,
                        decision_id TEXT NOT NULL,
                        article_id TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        market_date TEXT NOT NULL,
                        entry_at TEXT,
                        entry_price REAL,
                        exit_at TEXT,
                        exit_price REAL,
                        outcome_status TEXT NOT NULL,
                        gross_return_pct REAL,
                        modeled_after_cost_return_pct REAL,
                        quarantine_reason TEXT NOT NULL,
                        source_bar_hash_sha256 TEXT NOT NULL,
                        source_quote_hash_sha256 TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_scenario_replay_date
                    ON scenario_replay_trades(market_date, ticker, outcome_status);
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

    def persist_monitor_checks(self, rows: list[dict[str, Any]], run_id: str | None = None) -> None:
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

    def persist_monitor_events(self, rows: list[dict[str, Any]], run_id: str | None = None) -> None:
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

    def persist_monitor_publication_receipts(
        self, rows: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Persist immutable watcher publication receipts without touching the slate."""

        self.initialize()
        inserted = 0
        reused = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    receipt_id = str(row.get("receipt_id") or "")
                    content_hash = str(row.get("content_hash_sha256") or "")
                    if not receipt_id or not content_hash:
                        continue
                    canonical_payload = json.dumps(row, sort_keys=True)
                    existing = connection.execute(
                        """
                        SELECT content_hash_sha256, payload_json
                        FROM monitor_publication_receipts WHERE receipt_id = ?
                        """,
                        (receipt_id,),
                    ).fetchone()
                    if existing is not None:
                        if (
                            str(existing[0]) != content_hash
                            or str(existing[1]) != canonical_payload
                        ):
                            raise StorageError(
                                "monitor publication receipt identity collision"
                            )
                        reused += 1
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO monitor_publication_receipts
                        (receipt_id, market_date, ticker, signal_id, plan_hash_sha256,
                         content_hash_sha256, publication_count, checked_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            receipt_id,
                            str(row.get("market_date") or ""),
                            str(row.get("ticker") or ""),
                            str(row.get("signal_id") or ""),
                            str(row.get("plan_hash_sha256") or ""),
                            content_hash,
                            int(row.get("publication_count") or 0),
                            str(row.get("checked_at") or ""),
                            canonical_payload,
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        # ``INSERT OR IGNORE`` can also be suppressed by the
                        # unique content hash (rather than by receipt_id).
                        # Resolve the stored identity explicitly so a
                        # divergent payload can never be reported as an
                        # idempotent reuse.
                        persisted = connection.execute(
                            """
                            SELECT content_hash_sha256, payload_json
                            FROM monitor_publication_receipts WHERE receipt_id = ?
                            """,
                            (receipt_id,),
                        ).fetchone()
                        if (
                            persisted is None
                            or str(persisted[0]) != content_hash
                            or str(persisted[1]) != canonical_payload
                        ):
                            raise StorageError(
                                "monitor publication receipt identity collision"
                            )
                        reused += 1
            return {"inserted": inserted, "reused": reused, "count": inserted + reused}
        except sqlite3.Error as exc:
            raise StorageError(
                f"Could not persist monitor publication receipts: {exc}"
            ) from exc

    def load_monitor_publication_receipts(
        self, *, market_date: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                if market_date:
                    rows = connection.execute(
                        """
                        SELECT payload_json FROM monitor_publication_receipts
                        WHERE market_date = ? ORDER BY checked_at DESC LIMIT ?
                        """,
                        (str(market_date)[:10], limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT payload_json FROM monitor_publication_receipts
                        ORDER BY checked_at DESC LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(
                f"Could not load monitor publication receipts: {exc}"
            ) from exc

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

    def record_notification_delivery(
        self,
        *,
        event_key: str,
        channel: str,
        payload: dict[str, Any],
        run_id: str | None = None,
        ticker: str | None = None,
    ) -> bool:
        """Persist an attempt while preserving a completed Telegram delivery."""

        self.initialize()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO notifications_sent
                    (event_key, run_id, ticker, channel, sent_at, payload_json)
                    VALUES (?, ?, ?, ?, datetime('now'), ?)
                    ON CONFLICT(event_key) DO UPDATE SET
                        run_id = excluded.run_id,
                        ticker = excluded.ticker,
                        channel = excluded.channel,
                        sent_at = excluded.sent_at,
                        payload_json = excluded.payload_json
                    WHERE notifications_sent.channel != 'telegram:sent'
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
            raise StorageError(f"Could not record notification delivery: {exc}") from exc

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

    def discard_dry_run_notification(self, event_key: str) -> bool:
        """Delete only a simulated notification so a later real send can proceed."""

        self.initialize()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload_json FROM notifications_sent WHERE event_key = ?",
                    (event_key,),
                ).fetchone()
                if row is None:
                    return False
                payload = _json_value(row[0])
                if not isinstance(payload, dict) or payload.get("dry_run") is not True:
                    return False
                cursor = connection.execute(
                    "DELETE FROM notifications_sent WHERE event_key = ?",
                    (event_key,),
                )
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise StorageError(f"Could not discard dry-run notification: {exc}") from exc

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

    def persist_signal_selections(
        self,
        rows: list[dict[str, Any]],
        *,
        require_exact: bool = False,
    ) -> dict[str, int]:
        """Persist an immutable, exact set of selected signal identities.

        ``require_exact`` is used by frozen radar persistence.  It validates
        duplicate identities and existing unique-key conflicts inside the same
        transaction as the inserts, so a rejected set cannot leave partial
        selection rows behind.
        """

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
                if require_exact:
                    connection.row_factory = sqlite3.Row
                if require_exact:
                    valid_rows: list[dict[str, Any]] = []
                    seen_selection_ids: set[str] = set()
                    seen_signal_keys: set[tuple[str, str, str, str]] = set()
                    for row in rows:
                        if any(not str(row.get(field) or "").strip() for field in required):
                            raise StorageError(
                                "Exact signal selection is missing required immutable identity"
                            )
                        selection_id = str(row["selection_id"])
                        signal_key = (
                            str(row["strategy_id"]),
                            str(row["strategy_version"]),
                            str(row["cohort"]),
                            str(row["signal_id"]),
                        )
                        if selection_id in seen_selection_ids or signal_key in seen_signal_keys:
                            raise StorageError(
                                "Exact signal selection set contains duplicate immutable identity"
                            )
                        seen_selection_ids.add(selection_id)
                        seen_signal_keys.add(signal_key)
                        valid_rows.append(row)
                    for row in valid_rows:
                        selection_id = str(row["selection_id"])
                        signal_key = (
                            str(row["strategy_id"]),
                            str(row["strategy_version"]),
                            str(row["cohort"]),
                            str(row["signal_id"]),
                        )
                        existing_by_id = connection.execute(
                            "SELECT * FROM signal_selections WHERE selection_id = ? LIMIT 1",
                            (selection_id,),
                        ).fetchone()
                        if existing_by_id is not None:
                            actual = _selection_identity_row(existing_by_id)
                            if _selection_semantics(actual) != _selection_semantics(row):
                                raise StorageError(
                                    "Exact signal selection identity conflicts with prior truth: "
                                    + selection_id
                                )
                        existing_by_signal = connection.execute(
                            """
                            SELECT * FROM signal_selections
                            WHERE strategy_id = ? AND strategy_version = ?
                              AND cohort = ? AND signal_id = ?
                            LIMIT 1
                            """,
                            signal_key,
                        ).fetchone()
                        if (
                            existing_by_signal is not None
                            and str(existing_by_signal["selection_id"]) != selection_id
                        ):
                            raise StorageError(
                                "Exact signal selection signal identity conflicts "
                                "with prior truth: "
                                + str(row["signal_id"])
                            )
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
                if require_exact:
                    for row in rows:
                        actual_row = connection.execute(
                            "SELECT * FROM signal_selections WHERE selection_id = ? LIMIT 1",
                            (str(row["selection_id"]),),
                        ).fetchone()
                        if (
                            actual_row is None
                            or _selection_semantics(_selection_identity_row(actual_row))
                            != _selection_semantics(row)
                        ):
                            raise StorageError(
                                "Exact signal selection persistence is incomplete: "
                                + str(row["selection_id"])
                            )
            return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist signal selections: {exc}") from exc

    def persist_official_signal_cohort(
        self,
        cohort_row: dict[str, Any],
        selections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically claim one date-level official cohort and its exact members.

        The unique date/strategy/cohort key is the morning-run concurrency lock.
        A retry may restate the exact same cohort, but it cannot replace the
        membership, message identity, or source-failure ``NO_TRADE`` sentinel.
        """

        self.initialize()
        required = (
            "official_cohort_id",
            "market_date",
            "strategy_id",
            "strategy_version",
            "cohort",
            "scan_id",
            "event_key",
            "body_sha256",
            "membership_sha256",
            "claimed_at",
        )
        if any(not str(cohort_row.get(field) or "").strip() for field in required):
            raise StorageError("Official strategy cohort lock is missing required truth")
        if not selections:
            raise StorageError("Official strategy cohort requires at least one member")
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO official_strategy_cohorts
                    (official_cohort_id, market_date, strategy_id, strategy_version,
                     cohort, scan_id, event_key, body_sha256, membership_sha256,
                     claimed_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(cohort_row["official_cohort_id"]),
                        str(cohort_row["market_date"])[:10],
                        str(cohort_row["strategy_id"]),
                        str(cohort_row["strategy_version"]),
                        str(cohort_row["cohort"]),
                        str(cohort_row["scan_id"]),
                        str(cohort_row["event_key"]),
                        str(cohort_row["body_sha256"]),
                        str(cohort_row["membership_sha256"]),
                        str(cohort_row["claimed_at"]),
                        json.dumps(cohort_row.get("payload_json") or cohort_row, sort_keys=True),
                    ),
                )
                official = connection.execute(
                    """
                    SELECT * FROM official_strategy_cohorts
                    WHERE market_date = ? AND strategy_id = ?
                      AND strategy_version = ? AND cohort = ?
                    LIMIT 1
                    """,
                    (
                        str(cohort_row["market_date"])[:10],
                        str(cohort_row["strategy_id"]),
                        str(cohort_row["strategy_version"]),
                        str(cohort_row["cohort"]),
                    ),
                ).fetchone()
                if official is None:
                    raise StorageError("Official strategy cohort lock was not persisted")
                immutable_fields = (
                    "official_cohort_id",
                    "market_date",
                    "strategy_id",
                    "strategy_version",
                    "cohort",
                    "scan_id",
                    "event_key",
                    "body_sha256",
                    "membership_sha256",
                )
                if any(
                    str(official[field]) != str(cohort_row[field]) for field in immutable_fields
                ):
                    raise StorageError(
                        "FROZEN_COHORT_CONFLICT: Official strategy cohort is already "
                        "frozen for this market date "
                        "with a different Telegram cohort"
                    )
                inserted_members = 0
                for row in selections:
                    member = connection.execute(
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
                    inserted_members += int(member.rowcount or 0)
                actual_members = connection.execute(
                    """
                    SELECT COUNT(*) FROM signal_selections
                    WHERE scan_id = ? AND strategy_id = ? AND strategy_version = ?
                      AND cohort = ? AND event_key = ?
                    """,
                    (
                        str(cohort_row["scan_id"]),
                        str(cohort_row["strategy_id"]),
                        str(cohort_row["strategy_version"]),
                        str(cohort_row["cohort"]),
                        str(cohort_row["event_key"]),
                    ),
                ).fetchone()
                if actual_members is None or int(actual_members[0]) != len(selections):
                    raise StorageError("Official strategy cohort membership is incomplete")
                return {
                    "claimed": bool(cursor.rowcount),
                    "inserted_members": inserted_members,
                    "official_cohort": _official_strategy_cohort_row(official),
                }
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist official strategy cohort: {exc}") from exc

    def load_official_strategy_cohort(
        self,
        *,
        market_date: str,
        strategy_id: str,
        strategy_version: str,
        cohort: str,
    ) -> dict[str, Any] | None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    """
                    SELECT * FROM official_strategy_cohorts
                    WHERE market_date = ? AND strategy_id = ?
                      AND strategy_version = ? AND cohort = ?
                    LIMIT 1
                    """,
                    (market_date[:10], strategy_id, strategy_version, cohort),
                ).fetchone()
                return _official_strategy_cohort_row(row) if row is not None else None
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load official strategy cohort: {exc}") from exc

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

    def load_scenario_selection_bindings(
        self,
        *,
        selection_ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        """Load Scenario selections with every exact link and joined decision parent."""

        selected_ids = sorted({str(value) for value in selection_ids if str(value).strip()})
        if not selected_ids:
            return []
        self.initialize()
        placeholders = ",".join("?" for _ in selected_ids)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                selections = connection.execute(
                    "SELECT * FROM signal_selections "
                    f"WHERE selection_id IN ({placeholders})",  # nosec B608
                    selected_ids,
                ).fetchall()
                signal_ids = sorted({str(row["signal_id"] or "") for row in selections})
                if not signal_ids:
                    return [
                        {"selection": _selection_identity_row(row), "links": [], "historical": None}
                        for row in selections
                    ]
                signal_placeholders = ",".join("?" for _ in signal_ids)
                historical = {
                    str(row["signal_id"]): _historical_signal_row(row)
                    for row in connection.execute(
                        "SELECT * FROM historical_signals "
                        f"WHERE signal_id IN ({signal_placeholders})",  # nosec B608
                        signal_ids,
                    ).fetchall()
                }
                joined = connection.execute(
                    """
                    SELECT
                        l.decision_id AS link_decision_id,
                        l.signal_id AS link_signal_id,
                        l.scan_id AS link_scan_id,
                        l.cohort AS link_cohort,
                        l.strategy_id AS link_strategy_id,
                        l.strategy_version AS link_strategy_version,
                        d.decision_id AS decision_id,
                        d.article_id AS decision_article_id,
                        d.ticker AS decision_ticker,
                        d.market_date AS decision_market_date,
                        d.decision_at AS decision_at,
                        d.event_type AS decision_event_type,
                        d.direction AS decision_direction,
                        d.directional_evidence_score AS decision_directional_evidence_score,
                        d.action AS decision_action,
                        d.calibration_status AS decision_calibration_status,
                        d.entry_trigger AS decision_entry_trigger,
                        d.invalidation_level AS decision_invalidation_level,
                        d.target_1 AS decision_target_1,
                        d.time_stop AS decision_time_stop,
                        d.source_tier AS decision_source_tier,
                        d.source_lineage_hash_sha256 AS decision_source_lineage_hash_sha256,
                        d.feature_hash_sha256 AS decision_feature_hash_sha256,
                        d.cohort AS decision_cohort,
                        d.policy_version AS decision_policy_version,
                        d.feature_schema_version AS decision_feature_schema_version,
                        d.research_only AS decision_research_only,
                        d.broker_execution_enabled AS decision_broker_execution_enabled,
                        d.payload_json AS decision_payload_json
                    FROM scenario_signal_links AS l
                    LEFT JOIN scenario_decisions AS d ON d.decision_id = l.decision_id
                    WHERE l.signal_id IN ("""
                    f"{signal_placeholders})",  # nosec B608
                    signal_ids,
                ).fetchall()
                links_by_signal: dict[str, list[dict[str, Any]]] = {}
                for row in joined:
                    signal_id = str(row["link_signal_id"] or "")
                    decision = None
                    if row["decision_id"] is not None:
                        decision = {
                            "decision_id": str(row["decision_id"] or ""),
                            "article_id": str(row["decision_article_id"] or ""),
                            "ticker": str(row["decision_ticker"] or "").upper(),
                            "market_date": str(row["decision_market_date"] or "")[:10],
                            "decision_at": str(row["decision_at"] or ""),
                            "event_type": str(row["decision_event_type"] or ""),
                            "direction": str(row["decision_direction"] or "").lower(),
                            "directional_evidence_score": _float_or_none(
                                row["decision_directional_evidence_score"]
                            ),
                            "action": str(row["decision_action"] or "").upper(),
                            "calibration_status": str(
                                row["decision_calibration_status"] or ""
                            ),
                            "entry_trigger": _float_or_none(row["decision_entry_trigger"]),
                            "invalidation_level": _float_or_none(
                                row["decision_invalidation_level"]
                            ),
                            "target_1": _float_or_none(row["decision_target_1"]),
                            "time_stop": str(row["decision_time_stop"] or "market_close"),
                            "source_tier": str(row["decision_source_tier"] or ""),
                            "source_lineage_hash_sha256": str(
                                row["decision_source_lineage_hash_sha256"] or ""
                            ),
                            "feature_hash_sha256": str(
                                row["decision_feature_hash_sha256"] or ""
                            ),
                            "cohort": str(row["decision_cohort"] or ""),
                            "policy_version": str(row["decision_policy_version"] or ""),
                            "feature_schema_version": str(
                                row["decision_feature_schema_version"] or ""
                            ),
                            "research_only": _scenario_bool(row["decision_research_only"]),
                            "broker_execution_enabled": _scenario_bool(
                                row["decision_broker_execution_enabled"]
                            ),
                        }
                        canonical_payload = _json_value(row["decision_payload_json"])
                        decision["_canonical_payload"] = (
                            canonical_payload if isinstance(canonical_payload, dict) else None
                        )
                        decision["_canonical_payload_matches"] = (
                            isinstance(canonical_payload, dict)
                            and _scenario_decision_projection(canonical_payload)
                            == _scenario_decision_projection(decision)
                        )
                    links_by_signal.setdefault(signal_id, []).append(
                        {
                            "decision_id": str(row["link_decision_id"] or ""),
                            "signal_id": signal_id,
                            "scan_id": str(row["link_scan_id"] or ""),
                            "cohort": str(row["link_cohort"] or ""),
                            "strategy_id": str(row["link_strategy_id"] or ""),
                            "strategy_version": str(row["link_strategy_version"] or ""),
                            "decision": decision,
                        }
                    )
                return [
                    {
                        "selection": _selection_identity_row(row),
                        "links": links_by_signal.get(str(row["signal_id"] or ""), []),
                        "historical": historical.get(str(row["signal_id"] or "")),
                    }
                    for row in selections
                ]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load Scenario selection bindings: {exc}") from exc

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
        immutable: bool = False,
    ) -> dict[str, dict[str, int]]:
        """Atomically persist one sourced strategy-reconciliation batch.

        ``immutable=True`` is the production AlphaOps truth path. Existing
        identities are only accepted when their non-volatile semantics are
        identical; changed bars or economics raise instead of overwriting
        historical evaluations, trades, labels, or scorecards.
        """

        self.initialize()
        stats = {
            "evaluations": {"inserted": 0, "updated": 0},
            "trades": {"inserted": 0, "updated": 0, "deleted": 0},
            "learning_labels": {"inserted": 0, "updated": 0, "deleted": 0},
            "scorecards": {"inserted": 0, "updated": 0},
        }
        try:
            with self._connect() as connection:
                if immutable:
                    evaluations = _immutable_new_rows(
                        connection,
                        table="strategy_evaluations",
                        identity_column="evaluation_id",
                        rows=evaluations,
                    )
                    paper_trades = _immutable_new_rows(
                        connection,
                        table="strategy_paper_trades",
                        identity_column="trade_id",
                        rows=paper_trades,
                    )
                    learning_labels = _immutable_new_rows(
                        connection,
                        table="strategy_learning_labels",
                        identity_column="label_id",
                        rows=learning_labels,
                    )
                    scorecards = _immutable_new_rows(
                        connection,
                        table="daily_strategy_scorecards",
                        identity_column="scorecard_id",
                        rows=scorecards,
                    )
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
                        expected_trade_ids_by_selection.setdefault(selection_id, set()).add(
                            trade_id
                        )
                expected_label_ids_by_evaluation: dict[str, set[str]] = {}
                for row in learning_labels:
                    evaluation_id = str(row.get("evaluation_id") or "")
                    label_id = str(row.get("label_id") or "")
                    if evaluation_id and label_id:
                        expected_label_ids_by_evaluation.setdefault(evaluation_id, set()).add(
                            label_id
                        )
                for evaluation in evaluations if not immutable else []:
                    evaluation_id = str(evaluation.get("evaluation_id") or "")
                    selection_id = str(evaluation.get("selection_id") or "")
                    if selection_id:
                        expected_trade_ids = expected_trade_ids_by_selection.get(
                            selection_id, set()
                        )
                        existing_trade_ids = {
                            str(row[0])
                            for row in connection.execute(
                                "SELECT trade_id FROM strategy_paper_trades WHERE selection_id = ?",
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
                    existed = (
                        connection.execute(
                            "SELECT 1 FROM strategy_evaluations WHERE evaluation_id = ?",
                            (evaluation_id,),
                        ).fetchone()
                        is not None
                    )
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
                    existed = (
                        connection.execute(
                            "SELECT 1 FROM strategy_paper_trades WHERE trade_id = ?",
                            (trade_id,),
                        ).fetchone()
                        is not None
                    )
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
                    existed = (
                        connection.execute(
                            "SELECT 1 FROM strategy_learning_labels WHERE label_id = ?",
                            (label_id,),
                        ).fetchone()
                        is not None
                    )
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
                    existed = (
                        connection.execute(
                            "SELECT 1 FROM daily_strategy_scorecards WHERE scorecard_id = ?",
                            (scorecard_id,),
                        ).fetchone()
                        is not None
                    )
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
        table_sql = quote_sql_identifier(table, allowed=allowed)
        # This public method composes only a fixed table allowlist.
        query = f"SELECT * FROM {table_sql}"  # nosec B608
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

    def persist_manual_audit(self, summary: dict[str, Any], trades: list[dict[str, Any]]) -> None:
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
            raise StorageError(f"Could not load intelligence outcome summary: {exc}") from exc

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
                        """,
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
                return {str(row["source"]): json.loads(str(row["summary_json"])) for row in rows}
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

    def replace_alpha_setup_memory(self, rows: list[dict[str, Any]]) -> None:
        """Atomically replace derived AlphaOps setup memory.

        Setup memory is a materialized view of canonical strategy-learning
        labels.  Deleting stale buckets in the same transaction prevents a
        corrected reconciliation from leaving obsolete evidence active.
        """

        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute("DELETE FROM alpha_setup_memory")
                for row in rows:
                    connection.execute(
                        """
                        INSERT INTO alpha_setup_memory
                        (setup_key, updated_at, sample_size, avg_return_pct,
                         median_return_pct, win_rate_pct, max_drawdown_pct,
                         outlier_dependency, summary_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("setup_key") or ""),
                            str(row.get("updated_at") or ""),
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
            raise StorageError(f"Could not replace AlphaOps setup memory: {exc}") from exc

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
                return {str(row["setup_key"]): json.loads(str(row["summary_json"])) for row in rows}
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
                        """,
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
                        """,  # nosec B608
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
                _validate_signal_parent_rows(connection, rows, require_market_identity=False)
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
                _validate_signal_parent_rows(connection, rows, require_market_identity=True)
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
        immutable: bool = False,
    ) -> dict[str, dict[str, int]]:
        """Persist outcome evidence and its audit events in one transaction."""

        self.initialize()
        outcome_stats = {"inserted": 0, "skipped": 0}
        event_stats = {"inserted": 0, "skipped": 0}
        try:
            with self._connect() as connection:
                _validate_signal_parent_rows(
                    connection, outcome_rows, require_market_identity=True
                )
                _validate_signal_parent_rows(connection, event_rows, require_market_identity=False)
                if immutable:
                    outcome_count = len(outcome_rows)
                    event_count = len(event_rows)
                    outcome_rows = _immutable_new_rows(
                        connection,
                        table="signal_outcomes",
                        identity_column="signal_id",
                        rows=outcome_rows,
                    )
                    event_rows = _immutable_new_rows(
                        connection,
                        table="signal_events",
                        identity_column="event_id",
                        rows=event_rows,
                    )
                    outcome_stats["skipped"] += outcome_count - len(outcome_rows)
                    event_stats["skipped"] += event_count - len(event_rows)
                    replace = False
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

    def persist_outcome_capture_attempts(
        self,
        rows: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Persist immutable, idempotent terminal and successful capture attempts."""

        self.initialize()
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    attempt_id = str(row.get("attempt_id") or "")
                    signal_id = str(row.get("signal_id") or "")
                    market_date = str(row.get("market_date") or "")[:10]
                    ticker = str(row.get("ticker") or "").upper()
                    if not attempt_id or not signal_id or not market_date or not ticker:
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO outcome_capture_attempts
                        (attempt_id, run_id, signal_id, market_date, ticker, status,
                         terminal, learning_eligible, provider_chain_json,
                         source_refs_json, source_bar_hash_sha256, attempted_at,
                         resolved_at, error_code, error_detail, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            attempt_id,
                            str(row.get("run_id") or ""),
                            signal_id,
                            market_date,
                            ticker,
                            str(row.get("status") or ""),
                            1 if row.get("terminal") else 0,
                            1 if row.get("learning_eligible") else 0,
                            json.dumps(row.get("provider_chain") or [], sort_keys=True),
                            json.dumps(row.get("source_refs") or [], sort_keys=True),
                            str(row.get("source_bar_hash_sha256") or ""),
                            str(row.get("attempted_at") or ""),
                            str(row.get("resolved_at") or ""),
                            str(row.get("error_code") or ""),
                            str(row.get("error_detail") or ""),
                            json.dumps(row.get("payload_json") or row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
            return {"inserted": inserted, "skipped": skipped, "row_count": len(rows)}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist outcome capture attempts: {exc}") from exc

    def load_outcome_capture_attempts(
        self,
        *,
        market_date: str | None = None,
        signal_id: str | None = None,
        status: str | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date[:10])
        if signal_id:
            clauses.append("signal_id = ?")
            params.append(signal_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        query = "SELECT * FROM outcome_capture_attempts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY attempted_at DESC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load outcome capture attempts: {exc}") from exc

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

    def load_price_observation_records(
        self,
        *,
        observation_id: str | None = None,
        market_date: str | None = None,
        ticker: str | None = None,
        signal_id: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Load immutable columns and JSON payload without merge precedence."""

        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("observation_id", observation_id),
            ("market_date", market_date[:10] if market_date else None),
            ("ticker", ticker.upper() if ticker else None),
            ("signal_id", signal_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        query = "SELECT * FROM price_observations"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY observed_at DESC, requested_at DESC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_raw_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load raw price observations: {exc}") from exc

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
                connection.row_factory = sqlite3.Row
                for row in rows:
                    intent_id = str(row.get("intent_id") or "")
                    ticker = str(row.get("ticker") or "").upper()
                    market_date = str(row.get("market_date") or "")[:10]
                    if not intent_id or not ticker or not market_date:
                        continue
                    if replace:
                        existing = connection.execute(
                            "SELECT * FROM trade_intents WHERE intent_id = ?",
                            (intent_id,),
                        ).fetchone()
                        if existing is not None:
                            if not _trade_intent_semantics_match(existing, row):
                                raise StorageError(
                                    "trade intent identity conflict: existing intent semantics "
                                    f"do not match {intent_id}"
                                )
                            skipped += 1
                            continue
                    insert_statement = (
                        "INSERT OR IGNORE"
                        if _trade_intent_episode_id(row)
                        else statement
                    )
                    cursor = connection.execute(
                        f"""
                        {insert_statement} INTO trade_intents
                        (intent_id, signal_id, market_date, ticker, episode_id, strategy_id,
                         account_id, mode, lifecycle_state,
                         action, decision_time, decision_price, trigger_price, stop_price,
                         target_price, quantity, notional, risk_amount, reason,
                         blocked_reason, source_observation_id, notification_event_key,
                         created_at, payload_json)
                        VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?
                        )
                        """,  # noqa: S608
                        (
                            intent_id,
                            str(row.get("signal_id") or ""),
                            market_date,
                            ticker,
                            _trade_intent_episode_id(row),
                            _trade_intent_strategy_id(row),
                            _trade_intent_account_id(row),
                            str(row.get("mode") or ""),
                            str(row.get("lifecycle_state") or ""),
                            _trade_intent_action(row),
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
                        existing = connection.execute(
                            "SELECT * FROM trade_intents WHERE intent_id = ?",
                            (intent_id,),
                        ).fetchone()
                        if existing is not None and not _trade_intent_semantics_match(
                            existing, row
                        ):
                            raise StorageError(
                                "trade intent identity conflict: existing intent semantics "
                                f"do not match {intent_id}"
                            )
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
        monitor_publication_receipts: list[dict[str, Any]] | None = None,
        portfolio_account_id: str = "",
        portfolio_market_date: str = "",
        max_open_positions: int | None = None,
        max_daily_entries: int | None = None,
    ) -> dict[str, Any]:
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
        monitor_rows = list(monitor_publication_receipts or [])
        monitor_stats = {"inserted": 0, "reused": 0, "count": 0}
        rejected_intents: dict[str, str] = {}
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.row_factory = sqlite3.Row
                # The watcher builds lifecycle rows before entering this
                # transaction.  Treat the intent insert as the durable claim:
                # an entry that loses the episode unique index must not be
                # allowed to materialize a position or fill from its stale
                # in-memory snapshot.  Exact intent-id retries remain
                # idempotently reusable for crash repair.
                admitted_intent_ids: set[str] = set()
                reserved_entry_count = 0
                reserved_entry_tickers: set[tuple[str, str]] = set()
                for row in intents:
                    intent_id = str(row.get("intent_id") or "")
                    ticker = str(row.get("ticker") or "").upper()
                    market_date = str(row.get("market_date") or "")[:10]
                    if not intent_id or not ticker or not market_date:
                        continue
                    existing_intent = connection.execute(
                        "SELECT intent_id FROM trade_intents WHERE intent_id = ?",
                        (intent_id,),
                    ).fetchone()
                    action = _trade_intent_action(row)
                    row_account_id = _trade_intent_account_id(row)
                    scoped_account_id = str(portfolio_account_id or "").strip()
                    if scoped_account_id and row_account_id != scoped_account_id:
                        raise StorageError(
                            "trade intent account conflicts with scoped portfolio: "
                            f"{intent_id}"
                        )
                    account_id = row_account_id or scoped_account_id
                    cap_date = str(portfolio_market_date or market_date)[:10]
                    if (
                        action in {"ENTER_LONG", "ENTER_SHORT"}
                        and (
                            cap_date != market_date
                            or not account_id
                            or account_id != "alphaops_v5_simulated"
                        )
                    ):
                        raise StorageError(
                            "trade intent account/date conflicts with paper portfolio: "
                            f"{intent_id}"
                        )
                    if existing_intent is None and action in {"ENTER_LONG", "ENTER_SHORT"}:
                        rejection = _entry_admission_rejection(
                            connection,
                            account_id=account_id,
                            market_date=cap_date,
                            ticker=ticker,
                            max_open_positions=max_open_positions,
                            max_daily_entries=max_daily_entries,
                            reserved_entry_count=reserved_entry_count,
                            reserved_entry_tickers=reserved_entry_tickers,
                        )
                        if rejection:
                            rejected_intents[intent_id] = rejection
                            intent_stats["skipped"] += 1
                            continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO trade_intents
                        (intent_id, signal_id, market_date, ticker, episode_id, strategy_id,
                         account_id, mode, lifecycle_state,
                         action, decision_time, decision_price, trigger_price, stop_price,
                         target_price, quantity, notional, risk_amount, reason,
                         blocked_reason, source_observation_id, notification_event_key,
                         created_at, payload_json)
                        VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            intent_id,
                            str(row.get("signal_id") or ""),
                            market_date,
                            ticker,
                            _trade_intent_episode_id(row),
                            _trade_intent_strategy_id(row),
                            account_id,
                            str(row.get("mode") or ""),
                            str(row.get("lifecycle_state") or ""),
                            _trade_intent_action(row),
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
                        admitted_intent_ids.add(intent_id)
                        if action in {"ENTER_LONG", "ENTER_SHORT"}:
                            reserved_entry_count += 1
                            reserved_entry_tickers.add((account_id, ticker))
                    else:
                        intent_stats["skipped"] += 1
                        existing = connection.execute(
                            "SELECT * FROM trade_intents WHERE intent_id = ?",
                            (intent_id,),
                        ).fetchone()
                        if existing is not None and not _trade_intent_semantics_match(
                            existing, row
                        ):
                            raise StorageError(
                                "trade intent identity conflict: existing intent semantics "
                                f"do not match {intent_id}"
                            )
                        if existing is not None:
                            admitted_intent_ids.add(intent_id)

                admitted_entry_intent_ids = {
                    str(row.get("intent_id") or "")
                    for row in intents
                    if str(row.get("intent_id") or "") in admitted_intent_ids
                    and _trade_intent_action(row)
                    in {"ENTER_LONG", "ENTER_SHORT"}
                }
                admitted_exit_intent_ids = {
                    str(row.get("intent_id") or "")
                    for row in intents
                    if str(row.get("intent_id") or "") in admitted_intent_ids
                    and _trade_intent_action(row) in {"EXIT_LONG", "EXIT_SHORT"}
                }
                admitted_intents_by_id = {
                    str(row.get("intent_id") or ""): row
                    for row in intents
                    if str(row.get("intent_id") or "") in admitted_intent_ids
                }
                for row in paper_positions:
                    position_id = str(row.get("position_id") or "")
                    ticker = str(row.get("ticker") or "").upper()
                    market_date = str(row.get("market_date") or "")[:10]
                    if not position_id or not ticker or not market_date:
                        continue
                    status = str(row.get("status") or "").upper()
                    if status not in {"OPEN", "PENDING", "CLOSED"}:
                        raise StorageError(
                            "paper position has unsupported lifecycle status: "
                            f"{position_id}"
                        )
                    lifecycle_intent_id = str(
                        row.get("entry_intent_id")
                        if status in {"OPEN", "PENDING"}
                        else row.get("exit_intent_id") or ""
                    )
                    if status in {"OPEN", "PENDING"}:
                        if lifecycle_intent_id not in admitted_entry_intent_ids:
                            continue
                    elif status == "CLOSED" and (
                        not lifecycle_intent_id
                        or lifecycle_intent_id not in admitted_exit_intent_ids
                    ):
                        # A closed snapshot is an exit side effect.  Do not
                        # admit unbound lifecycle truth through this watcher API.
                        raise StorageError(
                            "paper position close requires an admitted exit intent: "
                            f"{position_id}"
                        )
                    existing_position = connection.execute(
                        "SELECT * FROM paper_positions WHERE position_id = ?",
                        (position_id,),
                    ).fetchone()
                    bound_fills = [
                        fill
                        for fill in paper_fills
                        if str(fill.get("intent_id") or "") == lifecycle_intent_id
                        and str(fill.get("position_id") or "") == position_id
                    ]
                    bound_fill = bound_fills[0] if len(bound_fills) == 1 else None
                    if existing_position is None:
                        if status == "CLOSED":
                            raise StorageError(
                                "paper position close requires an existing open position: "
                                f"{position_id}"
                            )
                        if status in {"OPEN", "PENDING"} and not _valid_position_entry_fill(
                            row,
                            bound_fill,
                            entry_intent=admitted_intents_by_id.get(lifecycle_intent_id),
                        ):
                            raise StorageError(
                                "paper position entry requires one valid bound fill: "
                                f"{position_id}"
                            )
                    elif status == "CLOSED":
                        existing_status = str(existing_position["status"] or "").upper()
                        if existing_status == "CLOSED":
                            if not _lifecycle_semantics_match(
                                existing_position,
                                row,
                                keys=_PAPER_POSITION_SEMANTIC_KEYS,
                                numeric_keys=_PAPER_POSITION_NUMERIC_KEYS,
                            ):
                                raise StorageError(
                                    "paper position identity conflict: existing lifecycle "
                                    f"semantics do not match {position_id}"
                                )
                            # Exact CLOSED retries reuse immutable stored truth.
                            continue
                        if not _valid_position_close_transition(
                            existing_position,
                            row,
                            exit_intent=admitted_intents_by_id.get(lifecycle_intent_id),
                            bound_exit_fill=bound_fill,
                        ):
                            raise StorageError(
                                "paper position close violates transition/fill invariants: "
                                f"{position_id}"
                            )
                    elif existing_position is not None and not _lifecycle_semantics_match(
                        existing_position,
                        row,
                        keys=_PAPER_POSITION_SEMANTIC_KEYS,
                        numeric_keys=_PAPER_POSITION_NUMERIC_KEYS,
                    ):
                        raise StorageError(
                            "paper position identity conflict: existing lifecycle "
                            f"semantics do not match {position_id}"
                        )
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
                    if intent_id not in admitted_intent_ids:
                        continue
                    admitted_intent = admitted_intents_by_id.get(intent_id)
                    if admitted_intent is None or not _valid_intent_fill(
                        admitted_intent, row, position_id=position_id
                    ):
                        raise StorageError(
                            "paper fill is not bound to an admitted intent: " f"{fill_id}"
                        )
                    durable_position = connection.execute(
                        "SELECT * FROM paper_positions WHERE position_id = ?",
                        (position_id,),
                    ).fetchone()
                    if durable_position is None:
                        raise StorageError(
                            "paper fill requires a durable bound position: " f"{fill_id}"
                        )
                    position_status = str(durable_position["status"] or "").upper()
                    action = _trade_intent_action(admitted_intent)
                    bound_position_intent = str(
                        durable_position[
                            "entry_intent_id"
                            if action in {"ENTER_LONG", "ENTER_SHORT"}
                            else "exit_intent_id"
                        ]
                        or ""
                    )
                    expected_statuses = (
                        {"OPEN", "PENDING", "CLOSED"}
                        if action in {"ENTER_LONG", "ENTER_SHORT"}
                        else {"CLOSED"}
                    )
                    if (
                        bound_position_intent != intent_id
                        or position_status not in expected_statuses
                    ):
                        raise StorageError(
                            "paper fill does not match durable position lifecycle: "
                            f"{fill_id}"
                        )
                    claimed_fills = connection.execute(
                        "SELECT * FROM paper_trade_fills WHERE intent_id = ?",
                        (intent_id,),
                    ).fetchall()
                    for claimed_fill in claimed_fills:
                        if str(claimed_fill["fill_id"] or "") != fill_id:
                            raise StorageError(
                                "paper fill claim conflict: intent or position already "
                                f"owns a different fill for {fill_id}"
                            )
                    existing_fill = connection.execute(
                        "SELECT * FROM paper_trade_fills WHERE fill_id = ?",
                        (fill_id,),
                    ).fetchone()
                    if existing_fill is not None and not _lifecycle_semantics_match(
                        existing_fill,
                        row,
                        keys=_PAPER_FILL_SEMANTIC_KEYS,
                        numeric_keys=_PAPER_FILL_NUMERIC_KEYS,
                    ):
                        raise StorageError(
                            "paper fill identity conflict: existing lifecycle semantics "
                            f"do not match {fill_id}"
                        )
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

                monitor_stats = _persist_bound_monitor_receipts(
                    connection,
                    monitor_rows,
                    admitted_intents_by_id=admitted_intents_by_id,
                    candidate_fills=paper_fills,
                )

                admitted_signal_events: list[dict[str, Any]] = []
                for row in signal_events:
                    event_id = str(row.get("event_id") or "")
                    signal_id = str(row.get("signal_id") or "")
                    if not event_id or not signal_id:
                        continue
                    event_payload = row.get("payload_json")
                    bound_intent_id = (
                        str(event_payload.get("intent_id") or "").strip()
                        if isinstance(event_payload, dict)
                        else ""
                    )
                    if bound_intent_id and bound_intent_id not in admitted_intent_ids:
                        continue
                    if bound_intent_id:
                        bound_signal_id = str(
                            admitted_intents_by_id[bound_intent_id].get("signal_id") or ""
                        )
                        if bound_signal_id != signal_id:
                            raise StorageError(
                                "Signal event intent binding failed: "
                                f"{event_id} does not match {bound_intent_id}"
                            )
                    admitted_signal_events.append(row)

                # Lifecycle events are durable signal children too.  Filter
                # skipped intents first, then validate the admitted events
                # before any event insert so an orphan rolls back the batch.
                _validate_signal_parent_rows(
                    connection, admitted_signal_events, require_market_identity=False
                )
                for row in admitted_signal_events:
                    event_id = str(row.get("event_id") or "")
                    signal_id = str(row.get("signal_id") or "")
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
            "monitor_publication_receipts": monitor_stats,
            "rejected_intents": rejected_intents,
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
            clauses.append("UPPER(TRIM(action)) = ?")
            params.append(str(action).strip().upper())
        query = "SELECT * FROM trade_intents"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY decision_time DESC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_trade_intent_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load trade intents: {exc}") from exc

    def load_trade_intent_records(
        self,
        *,
        intent_id: str | None = None,
        market_date: str | None = None,
        ticker: str | None = None,
        signal_id: str | None = None,
        action: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Load immutable intent columns and JSON without payload overwrite."""

        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("intent_id", intent_id),
            ("market_date", market_date[:10] if market_date else None),
            ("ticker", ticker.upper() if ticker else None),
            ("signal_id", signal_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if action:
            clauses.append("UPPER(TRIM(action)) = ?")
            params.append(str(action).strip().upper())
        query = "SELECT * FROM trade_intents"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY decision_time DESC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_raw_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load raw trade intents: {exc}") from exc

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
        quarantine_manifest_path: str | Path | None = None,
    ) -> dict[str, int]:
        self.initialize()
        review_id = str(run.get("review_id") or "")
        market_date = str(run.get("market_date") or "")
        if not review_id or not market_date:
            raise StorageError("Daily review run requires review_id and market_date.")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                _assert_applied_backfeed_allowed(
                    events,
                    review_id=review_id,
                    quarantine_manifest_path=quarantine_manifest_path,
                    db_path=self.db_path,
                )
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
        quarantine_manifest_path: str | Path | None = None,
    ) -> dict[str, int]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                _assert_applied_backfeed_allowed(
                    events,
                    review_id=review_id,
                    quarantine_manifest_path=quarantine_manifest_path,
                    db_path=self.db_path,
                )
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

    def upsert_daily_run(self, row: dict[str, Any]) -> None:
        self.initialize()
        run_id = str(row.get("run_id") or "")
        if not run_id:
            raise StorageError("Daily run requires run_id")
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO daily_runs
                    (run_id, market_date, release_sha, runtime_root, state_root,
                     scheduler_version, strategy_versions_json, status, current_stage,
                     started_at, completed_at, last_attempted_at, failed_stage,
                     failure_reason, source_data_watermark, publication_timestamp,
                     deployed_source_sha, deployed_build_sha, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        status = excluded.status,
                        current_stage = excluded.current_stage,
                        completed_at = excluded.completed_at,
                        last_attempted_at = excluded.last_attempted_at,
                        failed_stage = excluded.failed_stage,
                        failure_reason = excluded.failure_reason,
                        source_data_watermark = excluded.source_data_watermark,
                        publication_timestamp = excluded.publication_timestamp,
                        deployed_source_sha = excluded.deployed_source_sha,
                        deployed_build_sha = excluded.deployed_build_sha,
                        payload_json = excluded.payload_json
                    """,
                    (
                        run_id,
                        str(row.get("market_date") or "")[:10],
                        str(row.get("release_sha") or ""),
                        str(row.get("runtime_root") or ""),
                        str(row.get("state_root") or ""),
                        str(row.get("scheduler_version") or ""),
                        json.dumps(row.get("strategy_versions") or {}, sort_keys=True),
                        str(row.get("status") or ""),
                        str(row.get("current_stage") or ""),
                        str(row.get("started_at") or ""),
                        str(row.get("completed_at") or ""),
                        str(row.get("last_attempted_at") or ""),
                        str(row.get("failed_stage") or ""),
                        str(row.get("failure_reason") or ""),
                        str(row.get("source_data_watermark") or ""),
                        str(row.get("publication_timestamp") or ""),
                        str(row.get("deployed_source_sha") or ""),
                        str(row.get("deployed_build_sha") or ""),
                        json.dumps(row.get("payload_json") or row, sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not upsert daily run: {exc}") from exc

    def persist_daily_run_stage(self, row: dict[str, Any]) -> bool:
        self.initialize()
        stage_event_id = str(row.get("stage_event_id") or "")
        run_id = str(row.get("run_id") or "")
        if not stage_event_id or not run_id:
            raise StorageError("Daily run stage requires stage_event_id and run_id")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO daily_run_stages
                    (stage_event_id, run_id, stage_name, attempt_no, status, required,
                     started_at, completed_at, exit_code, input_hash_sha256,
                     output_hash_sha256, source_data_watermark, error_code,
                     error_detail, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stage_event_id,
                        run_id,
                        str(row.get("stage_name") or ""),
                        int(row.get("attempt_no") or 0),
                        str(row.get("status") or ""),
                        1 if row.get("required") else 0,
                        str(row.get("started_at") or ""),
                        str(row.get("completed_at") or ""),
                        _int_or_none(row.get("exit_code")),
                        str(row.get("input_hash_sha256") or ""),
                        str(row.get("output_hash_sha256") or ""),
                        str(row.get("source_data_watermark") or ""),
                        str(row.get("error_code") or ""),
                        str(row.get("error_detail") or ""),
                        json.dumps(row.get("payload_json") or row, sort_keys=True),
                    ),
                )
                return bool(cursor.rowcount)
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist daily run stage: {exc}") from exc

    def persist_alpha_v6_decisions(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Append point-in-time V6 decisions; a decision is never rewritten."""

        self.initialize()
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO alpha_v6_decisions
                        (decision_id, scan_id, source_signal_id, shadow_signal_id,
                         market_date, decision_at, ticker, strategy_version,
                         model_version, action, setup_key, regime_key,
                         safety_vetoes_json, input_hash_sha256,
                         source_lineage_hash_sha256, stored_at, payload_json)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("decision_id") or ""),
                            str(row.get("scan_id") or ""),
                            str(row.get("source_signal_id") or ""),
                            str(row.get("shadow_signal_id") or ""),
                            str(row.get("market_date") or "")[:10],
                            str(row.get("decision_at") or ""),
                            str(row.get("ticker") or "").upper(),
                            str(row.get("strategy_version") or ""),
                            str(row.get("model_version") or ""),
                            str(row.get("action") or ""),
                            str(row.get("setup_key") or ""),
                            str(row.get("regime_key") or ""),
                            json.dumps(row.get("safety_vetoes") or [], sort_keys=True),
                            str(row.get("input_hash_sha256") or ""),
                            str(row.get("source_lineage_hash_sha256") or ""),
                            datetime.now(UTC).replace(microsecond=0).isoformat(),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
            return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist V6 decisions: {exc}") from exc

    def load_alpha_v6_decisions(
        self,
        *,
        market_date: str | None = None,
        action: str | None = None,
        limit: int = 50_000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date[:10])
        if action:
            clauses.append("action = ?")
            params.append(action)
        query = "SELECT payload_json FROM alpha_v6_decisions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY decision_at ASC, decision_id ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load V6 decisions: {exc}") from exc

    def persist_alpha_v6_outcomes(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Append one immutable outcome receipt per V6 decision."""

        self.initialize()
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    payload_json = json.dumps(row, sort_keys=True)
                    identity = str(row.get("outcome_id") or "")
                    existing = connection.execute(
                        "SELECT payload_json FROM alpha_v6_outcomes WHERE outcome_id = ?",
                        (identity,),
                    ).fetchone()
                    if existing is not None:
                        if str(existing[0]) != payload_json:
                            raise StorageError(f"immutable V6 outcome conflict: {identity}")
                        skipped += 1
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO alpha_v6_outcomes
                        (outcome_id, decision_id, shadow_signal_id, market_date,
                         observed_at, activation_status, outcome_status,
                         net_return_pct, benchmark_return_pct,
                         net_excess_return_pct, source_bar_hash_sha256,
                         learning_eligible, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("outcome_id") or ""),
                            str(row.get("decision_id") or ""),
                            str(row.get("shadow_signal_id") or ""),
                            str(row.get("market_date") or "")[:10],
                            str(row.get("observed_at") or ""),
                            str(row.get("activation_status") or ""),
                            str(row.get("outcome_status") or ""),
                            _float_or_none(row.get("net_return_pct")),
                            _float_or_none(row.get("benchmark_return_pct")),
                            _float_or_none(row.get("net_excess_return_pct")),
                            str(row.get("source_bar_hash_sha256") or ""),
                            1 if row.get("learning_eligible") else 0,
                            payload_json,
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
            return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist V6 outcomes: {exc}") from exc

    def load_alpha_v6_outcomes(self, limit: int = 50_000) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json FROM alpha_v6_outcomes
                    ORDER BY market_date ASC, observed_at ASC, outcome_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load V6 outcomes: {exc}") from exc

    def persist_alpha_v6_model_run(self, row: dict[str, Any]) -> bool:
        self.initialize()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO alpha_v6_model_runs
                    (model_run_id, model_version, trained_at, training_cutoff,
                     status, training_input_hash_sha256, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row.get("model_run_id") or ""),
                        str(row.get("model_version") or ""),
                        str(row.get("trained_at") or ""),
                        str(row.get("training_cutoff") or ""),
                        str(row.get("status") or ""),
                        str(row.get("training_input_hash_sha256") or ""),
                        json.dumps(row, sort_keys=True),
                    ),
                )
                return bool(cursor.rowcount)
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist V6 model run: {exc}") from exc

    def load_alpha_v6_model_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json FROM alpha_v6_model_runs
                    ORDER BY trained_at DESC, model_run_id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load V6 model runs: {exc}") from exc

    def persist_alpha_v6_evaluation(self, row: dict[str, Any]) -> bool:
        self.initialize()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO alpha_v6_evaluations
                    (evaluation_id, model_run_id, evaluated_at, status,
                     evaluation_input_hash_sha256, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row.get("evaluation_id") or ""),
                        str(row.get("model_run_id") or ""),
                        str(row.get("evaluated_at") or ""),
                        str(row.get("status") or ""),
                        str(row.get("evaluation_input_hash_sha256") or ""),
                        json.dumps(row, sort_keys=True),
                    ),
                )
                return bool(cursor.rowcount)
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist V6 evaluation: {exc}") from exc

    def load_alpha_v6_evaluations(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json FROM alpha_v6_evaluations
                    ORDER BY evaluated_at DESC, evaluation_id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load V6 evaluations: {exc}") from exc

    def load_latest_account_performance_comparison(self) -> dict[str, Any] | None:
        """Load the latest persisted, fail-closed account comparison receipt."""

        self.initialize()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT payload_json FROM account_performance_comparisons
                    ORDER BY calculated_at DESC, comparison_id DESC LIMIT 1
                    """
                ).fetchone()
                return json.loads(str(row[0])) if row is not None else None
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load account comparison: {exc}") from exc

    def persist_alpha_v6_experiments(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        self.initialize()
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO alpha_v6_experiments
                        (experiment_id, created_at, status, hypothesis, payload_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("experiment_id") or ""),
                            str(row.get("created_at") or ""),
                            str(row.get("status") or ""),
                            str(row.get("hypothesis") or ""),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
            return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist V6 experiments: {exc}") from exc

    def load_alpha_v6_experiments(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        return self._load_v6_payload_rows("alpha_v6_experiments", "created_at", limit=limit)

    def persist_alpha_v6_holdout_evaluation(self, row: dict[str, Any]) -> bool:
        """Persist once per experiment; a second evaluation is rejected by UNIQUE."""

        self.initialize()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO alpha_v6_holdout_evaluations
                    (holdout_evaluation_id, experiment_id, evaluated_at, status,
                     evidence_hash_sha256, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row.get("holdout_evaluation_id") or ""),
                        str(row.get("experiment_id") or ""),
                        str(row.get("evaluated_at") or ""),
                        str(row.get("status") or ""),
                        str(row.get("evidence_hash_sha256") or ""),
                        json.dumps(row, sort_keys=True),
                    ),
                )
                return bool(cursor.rowcount)
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist V6 holdout evaluation: {exc}") from exc

    def load_alpha_v6_holdout_evaluations(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        return self._load_v6_payload_rows(
            "alpha_v6_holdout_evaluations", "evaluated_at", limit=limit
        )

    def persist_alpha_v6_labels(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Append immutable V6 label-family receipts."""

        self.initialize()
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    payload_json = json.dumps(row, sort_keys=True)
                    identity = str(row.get("label_id") or "")
                    existing = connection.execute(
                        "SELECT payload_json FROM alpha_v6_labels WHERE label_id = ?",
                        (identity,),
                    ).fetchone()
                    if existing is not None:
                        if str(existing[0]) != payload_json:
                            raise StorageError(f"immutable V6 label conflict: {identity}")
                        skipped += 1
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO alpha_v6_labels
                        (label_id, decision_id, market_date, observed_at, label_family,
                         label_value, learning_eligible, exclusion_reason,
                         source_bar_hash_sha256, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("label_id") or ""),
                            str(row.get("decision_id") or ""),
                            str(row.get("market_date") or "")[:10],
                            str(row.get("observed_at") or ""),
                            str(row.get("label_family") or ""),
                            _float_or_none(row.get("label_value")),
                            1 if row.get("learning_eligible") is True else 0,
                            str(row.get("exclusion_reason") or "") or None,
                            str(row.get("source_bar_hash_sha256") or "") or None,
                            payload_json,
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
            return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist V6 labels: {exc}") from exc

    def load_alpha_v6_labels(
        self,
        *,
        label_family: str | None = None,
        limit: int = 100_000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                if label_family:
                    rows = connection.execute(
                        """
                        SELECT payload_json FROM alpha_v6_labels
                        WHERE label_family = ?
                        ORDER BY market_date ASC, observed_at ASC, label_id ASC LIMIT ?
                        """,
                        (label_family, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT payload_json FROM alpha_v6_labels
                        ORDER BY market_date ASC, observed_at ASC, label_id ASC LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load V6 labels: {exc}") from exc

    def persist_alpha_v6_dataset(self, row: dict[str, Any]) -> bool:
        self.initialize()
        try:
            with self._connect() as connection:
                payload_json = json.dumps(row, sort_keys=True)
                identity = str(row.get("dataset_id") or "")
                existing = connection.execute(
                    "SELECT payload_json FROM alpha_v6_datasets WHERE dataset_id = ?",
                    (identity,),
                ).fetchone()
                if existing is not None:
                    existing_payload = _json_value(existing[0])
                    if not isinstance(existing_payload, dict) or _immutable_semantics(
                        existing_payload
                    ) != _immutable_semantics(row):
                        raise StorageError(f"immutable V6 dataset conflict: {identity}")
                    return False
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO alpha_v6_datasets
                    (dataset_id, created_at, training_cutoff, row_count,
                     dataset_hash_sha256, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row.get("dataset_id") or ""),
                        str(row.get("created_at") or ""),
                        str(row.get("training_cutoff") or "") or None,
                        int(row.get("row_count") or 0),
                        str(row.get("dataset_hash_sha256") or ""),
                        payload_json,
                    ),
                )
                return bool(cursor.rowcount)
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist V6 dataset: {exc}") from exc

    def load_alpha_v6_datasets(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        return self._load_v6_payload_rows("alpha_v6_datasets", "created_at", limit=limit)

    def persist_alpha_v6_model_artifact(self, row: dict[str, Any]) -> bool:
        self.initialize()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO alpha_v6_model_artifacts
                    (artifact_id, model_run_id, created_at, artifact_hash_sha256, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(row.get("artifact_id") or ""),
                        str(row.get("model_run_id") or ""),
                        str(row.get("created_at") or ""),
                        str(row.get("artifact_hash_sha256") or ""),
                        json.dumps(row, sort_keys=True),
                    ),
                )
                return bool(cursor.rowcount)
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist V6 model artifact: {exc}") from exc

    def persist_alpha_v6_shadow_predictions(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        self.initialize()
        inserted = 0
        skipped = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO alpha_v6_shadow_predictions
                        (prediction_id, decision_id, model_run_id, market_date,
                         generated_at, status, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("prediction_id") or ""),
                            str(row.get("decision_id") or ""),
                            str(row.get("model_run_id") or "") or None,
                            str(row.get("market_date") or "")[:10],
                            str(row.get("generated_at") or ""),
                            str(row.get("status") or ""),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
            return {"inserted": inserted, "skipped": skipped}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist V6 shadow predictions: {exc}") from exc

    def load_alpha_v6_shadow_predictions(self, *, limit: int = 1_000) -> list[dict[str, Any]]:
        self.initialize()
        return self._load_v6_payload_rows(
            "alpha_v6_shadow_predictions", "generated_at", limit=limit
        )

    def persist_alpha_v6_drift_report(self, row: dict[str, Any]) -> bool:
        return self._persist_v6_single_payload(
            table="alpha_v6_drift_reports",
            identity_field="drift_report_id",
            row=row,
            columns=("created_at", "status"),
        )

    def load_alpha_v6_drift_reports(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        return self._load_v6_payload_rows("alpha_v6_drift_reports", "created_at", limit=limit)

    def persist_alpha_v6_promotion_review(self, row: dict[str, Any]) -> bool:
        return self._persist_v6_single_payload(
            table="alpha_v6_promotion_reviews",
            identity_field="review_id",
            row=row,
            columns=("created_at", "status", "approved"),
        )

    def load_alpha_v6_promotion_reviews(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        return self._load_v6_payload_rows("alpha_v6_promotion_reviews", "created_at", limit=limit)

    def persist_alpha_v6_operational_receipt(self, row: dict[str, Any]) -> bool:
        return self._persist_v6_single_payload(
            table="alpha_v6_operational_receipts",
            identity_field="receipt_id",
            row=row,
            columns=(
                "receipt_kind",
                "as_of_date",
                "created_at",
                "status",
                "input_hash_sha256",
            ),
        )

    def load_alpha_v6_operational_receipts(
        self, *, receipt_kind: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.initialize()
        if receipt_kind is None:
            return self._load_v6_payload_rows(
                "alpha_v6_operational_receipts", "created_at", limit=limit
            )
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json FROM alpha_v6_operational_receipts
                    WHERE receipt_kind = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (receipt_kind, limit),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load V6 operational receipts: {exc}") from exc

    def persist_alpha_v6_universe(
        self, *, version: dict[str, Any], members: list[dict[str, Any]]
    ) -> bool:
        """Persist an immutable source-backed universe version and membership rows."""

        self.initialize()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO alpha_v6_universe_versions
                    (universe_id, as_of_date, created_at, membership_count,
                     source_lineage_hash_sha256, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(version.get("universe_id") or ""),
                        str(version.get("as_of_date") or "")[:10],
                        str(version.get("created_at") or ""),
                        int(version.get("membership_count") or 0),
                        str(version.get("source_lineage_hash_sha256") or ""),
                        json.dumps(version, sort_keys=True),
                    ),
                )
                inserted = bool(cursor.rowcount)
                for member in members:
                    payload = {
                        **member,
                        "universe_id": version.get("universe_id"),
                        "source_lineage_hash_sha256": version.get("source_lineage_hash_sha256"),
                        "research_only": True,
                        "broker_execution_enabled": False,
                    }
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO alpha_v6_universe_memberships
                        (universe_id, ticker, listing_status, valid_from, valid_to,
                         previous_ticker, corporate_action_type,
                         source_lineage_hash_sha256, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(version.get("universe_id") or ""),
                            str(member.get("ticker") or "").upper(),
                            str(member.get("listing_status") or ""),
                            member.get("valid_from"),
                            member.get("valid_to"),
                            member.get("previous_ticker"),
                            member.get("corporate_action_type"),
                            str(version.get("source_lineage_hash_sha256") or ""),
                            json.dumps(payload, sort_keys=True),
                        ),
                    )
            return inserted
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist V6 universe: {exc}") from exc

    def load_alpha_v6_universe_memberships(
        self, *, market_date: str, tickers: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Return latest snapshot memberships valid at market_date by ticker."""

        self.initialize()
        normalized = sorted({str(ticker or "").upper() for ticker in tickers if ticker})
        if not normalized:
            return {}
        placeholders = ", ".join("?" for _ in normalized)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                version = connection.execute(
                    """
                    SELECT universe_id, source_lineage_hash_sha256
                    FROM alpha_v6_universe_versions
                    WHERE as_of_date <= ?
                    ORDER BY as_of_date DESC, created_at DESC
                    LIMIT 1
                    """,
                    (market_date[:10],),
                ).fetchone()
                if version is None:
                    return {}
                rows = connection.execute(
                    f"""
                    SELECT payload_json FROM alpha_v6_universe_memberships
                    WHERE universe_id = ? AND ticker IN ({placeholders})
                    AND (valid_from IS NULL OR valid_from <= ?)
                    AND (valid_to IS NULL OR valid_to >= ?)
                    """,  # nosec B608
                    (str(version["universe_id"]), *normalized, market_date[:10], market_date[:10]),
                ).fetchall()
                return {
                    str(payload.get("ticker") or "").upper(): payload
                    for row in rows
                    if isinstance((payload := json.loads(str(row["payload_json"]))), dict)
                }
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load V6 universe memberships: {exc}") from exc

    def load_alpha_v6_universe_versions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return immutable universe versions newest first for preview and recovery."""

        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json FROM alpha_v6_universe_versions
                    ORDER BY as_of_date DESC, created_at DESC LIMIT ?
                    """,
                    (max(1, limit),),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load V6 universe versions: {exc}") from exc

    def load_alpha_v6_universe_members(self, *, universe_id: str) -> list[dict[str, Any]]:
        """Return one immutable universe's members for audited comparison or restore."""

        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT payload_json FROM alpha_v6_universe_memberships
                    WHERE universe_id = ? ORDER BY ticker ASC
                    """,
                    (universe_id,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load V6 universe members: {exc}") from exc

    def _persist_v6_single_payload(
        self,
        *,
        table: str,
        identity_field: str,
        row: dict[str, Any],
        columns: tuple[str, ...],
    ) -> bool:
        self.initialize()
        allowed_columns = _V6_SINGLE_PAYLOAD_COLUMNS.get(table)
        if allowed_columns is None:
            raise StorageError(f"Unsupported V6 payload table: {table}")
        names = (identity_field, *columns, "payload_json")
        table_sql = quote_sql_identifier(table, allowed=_V6_SINGLE_PAYLOAD_COLUMNS)
        names_sql = quote_sql_identifiers(names, allowed=allowed_columns)
        placeholders = ", ".join("?" for _ in names)
        values: list[Any] = [str(row.get(identity_field) or "")]
        for column in columns:
            value = row.get(column)
            values.append(1 if column == "approved" and value is True else value)
        values.append(json.dumps(row, sort_keys=True))
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    # Table and columns are allowlisted; placeholders contain only question marks.
                    f"INSERT OR IGNORE INTO {table_sql} ({names_sql}) VALUES ({placeholders})",
                    values,
                )
                return bool(cursor.rowcount)
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist V6 payload in {table}: {exc}") from exc

    def _load_v6_payload_rows(
        self, table: str, order_column: str, *, limit: int
    ) -> list[dict[str, Any]]:
        expected_order_column = _V6_PAYLOAD_TABLE_ORDERS.get(table)
        if expected_order_column != order_column:
            raise StorageError(f"Unsupported V6 payload table/order: {table}/{order_column}")
        table_sql = quote_sql_identifier(table, allowed=_V6_PAYLOAD_TABLE_ORDERS)
        order_sql = quote_sql_identifier(order_column, allowed={expected_order_column})
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    f"SELECT payload_json FROM {table_sql} ORDER BY {order_sql} DESC LIMIT ?",  # nosec B608
                    (limit,),
                ).fetchall()
                return [json.loads(str(row["payload_json"])) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load V6 payloads from {table}: {exc}") from exc

    def load_daily_runs(
        self,
        *,
        market_date: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if market_date:
            clauses.append("market_date = ?")
            params.append(market_date[:10])
        if status:
            clauses.append("status = ?")
            params.append(status)
        query = "SELECT * FROM daily_runs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC, last_attempted_at DESC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load daily runs: {exc}") from exc

    def load_daily_run_stages(
        self,
        *,
        run_id: str | None = None,
        market_date: str | None = None,
        stage_name: str | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if run_id:
            clauses.append("s.run_id = ?")
            params.append(run_id)
        if market_date:
            clauses.append("r.market_date = ?")
            params.append(market_date[:10])
        if stage_name:
            clauses.append("s.stage_name = ?")
            params.append(stage_name)
        query = "SELECT s.* FROM daily_run_stages s JOIN daily_runs r ON r.run_id = s.run_id"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY s.started_at ASC, s.stage_name ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
                return [_json_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load daily run stages: {exc}") from exc

    def _load_payloads(
        self, connection: sqlite3.Connection, table: str, run_id: str
    ) -> list[dict[str, Any]]:
        allowed = {"ranked_candidates", "top_explosive", "avoid_list"}
        table_sql = quote_sql_identifier(table, allowed=allowed)
        rows = connection.execute(
            f"SELECT payload_json FROM {table_sql} WHERE run_id = ? ORDER BY rank ASC",  # nosec B608
            (run_id,),
        ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def persist_scenario_news_items(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Upsert source records while retaining the complete private payload."""
        self.initialize()
        inserted = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    article_id = str(row.get("article_id") or "")
                    if not article_id:
                        continue
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO scenario_news_items
                        (article_id, provider, symbols_json, headline, summary, source, author,
                         source_url, created_at, updated_at, fetched_at, first_seen_at,
                         timing_kind, source_tier, content_hash_sha256,
                         source_lineage_hash_sha256, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            article_id,
                            str(row.get("provider") or ""),
                            json.dumps(row.get("symbols") or [], sort_keys=True),
                            str(row.get("headline") or ""),
                            str(row.get("summary") or ""),
                            str(row.get("source") or ""),
                            str(row.get("author") or ""),
                            str(row.get("source_url") or ""),
                            str(row.get("created_at") or ""),
                            str(row.get("updated_at") or ""),
                            str(row.get("fetched_at") or ""),
                            str(row.get("first_seen_at") or ""),
                            str(row.get("timing_kind") or ""),
                            str(row.get("source_tier") or ""),
                            str(row.get("content_hash_sha256") or ""),
                            str(row.get("source_lineage_hash_sha256") or ""),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    inserted += 1
            return {"inserted": inserted, "row_count": len(rows)}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist scenario news items: {exc}") from exc

    def load_scenario_news_items(
        self, *, start: str | None = None, end: str | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        return self._load_scenario_payloads(
            table="scenario_news_items",
            order_by="created_at DESC, article_id ASC",
            start=start,
            end=end,
            limit=limit,
        )

    def persist_scenario_extractions(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        self.initialize()
        inserted = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    extraction_id = str(row.get("extraction_id") or "")
                    if not extraction_id:
                        continue
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO scenario_claim_extractions
                        (extraction_id, article_id, model, response_id, prompt_version,
                         schema_version, input_hash_sha256, output_hash_sha256, status,
                         error_code, usage_json, created_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            extraction_id,
                            str(row.get("article_id") or ""),
                            str(row.get("model") or ""),
                            str(row.get("response_id") or ""),
                            str(row.get("prompt_version") or ""),
                            str(row.get("schema_version") or ""),
                            str(row.get("input_hash_sha256") or ""),
                            str(row.get("output_hash_sha256") or ""),
                            str(row.get("status") or ""),
                            str(row.get("error_code") or ""),
                            json.dumps(row.get("usage") or {}, sort_keys=True),
                            str(row.get("created_at") or ""),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    inserted += 1
            return {"inserted": inserted, "row_count": len(rows)}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist scenario extractions: {exc}") from exc

    def load_scenario_extraction(
        self, *, article_id: str, model: str, input_hash_sha256: str
    ) -> dict[str, Any] | None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT * FROM scenario_claim_extractions
                    WHERE article_id = ? AND input_hash_sha256 = ?
                    ORDER BY created_at DESC
                    """,
                    (article_id, input_hash_sha256),
                ).fetchall()
                for row in rows:
                    payload = _json_row(row)
                    requested_model = str(
                        payload.get("requested_model") or payload.get("model") or ""
                    )
                    if requested_model == model:
                        return payload
                return None
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load scenario extraction: {exc}") from exc

    def persist_scenario_decisions(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        self.initialize()
        inserted = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    decision_id = str(row.get("decision_id") or "")
                    if not decision_id:
                        continue
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO scenario_decisions
                        (decision_id, article_id, ticker, market_date, decision_at, event_type,
                         direction, directional_evidence_score, action, calibration_status,
                         entry_trigger, invalidation_level, target_1, time_stop, source_tier,
                         source_lineage_hash_sha256, feature_hash_sha256, cohort,
                         policy_version, feature_schema_version, research_only,
                         broker_execution_enabled, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            decision_id,
                            str(row.get("article_id") or ""),
                            str(row.get("ticker") or "").upper(),
                            str(row.get("market_date") or "")[:10],
                            str(row.get("decision_at") or ""),
                            str(row.get("event_type") or ""),
                            str(row.get("direction") or ""),
                            float(row.get("directional_evidence_score") or 0.0),
                            str(row.get("action") or ""),
                            str(row.get("calibration_status") or "UNCALIBRATED"),
                            _float_or_none(row.get("entry_trigger")),
                            _float_or_none(row.get("invalidation_level")),
                            _float_or_none(row.get("target_1")),
                            str(row.get("time_stop") or "market_close"),
                            str(row.get("source_tier") or ""),
                            str(row.get("source_lineage_hash_sha256") or ""),
                            str(row.get("feature_hash_sha256") or ""),
                            str(row.get("cohort") or ""),
                            str(row.get("policy_version") or ""),
                            str(row.get("feature_schema_version") or ""),
                            1 if row.get("research_only", True) else 0,
                            1 if row.get("broker_execution_enabled") else 0,
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    inserted += 1
            return {"inserted": inserted, "row_count": len(rows)}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist scenario decisions: {exc}") from exc

    def persist_scenario_forward_materialization(
        self,
        *,
        decision: Mapping[str, Any],
        signal: Mapping[str, Any],
        selection: Mapping[str, Any],
        link: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Atomically materialize one authoritative forward Scenario decision.

        The decision must already exist in ``scenario_decisions``.  The signal,
        selection, and lifecycle link are one immutable materialization envelope:
        retries may restate the exact envelope, but any drift or duplicate signal
        parent aborts the transaction before partial rows can survive.
        """

        self.initialize()
        decision_id = str(decision.get("decision_id") or "")
        signal_id = str(signal.get("signal_id") or "")
        if not decision_id or signal_id != f"scenario:{decision_id}":
            raise StorageError("Scenario materialization identity is incomplete or mismatched")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.row_factory = sqlite3.Row
                authoritative_row = connection.execute(
                    "SELECT * FROM scenario_decisions WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
                if authoritative_row is None:
                    raise StorageError(
                        "Scenario materialization requires an existing authoritative decision: "
                        + decision_id
                    )
                authoritative = {
                    key: authoritative_row[key] for key in authoritative_row.keys()
                }
                authoritative_projection = _scenario_decision_projection(authoritative)
                decision_projection = _scenario_decision_projection(decision)
                if authoritative_projection != decision_projection:
                    raise StorageError(
                        "Scenario materialization decision conflicts with authoritative truth: "
                        + decision_id
                    )
                authoritative_payload = _json_value(authoritative_row["payload_json"])
                if (
                    not isinstance(authoritative_payload, dict)
                    or _scenario_decision_projection(authoritative_payload)
                    != authoritative_projection
                ):
                    raise StorageError(
                        "Scenario materialization payload conflicts with authoritative truth: "
                        + decision_id
                    )
                _validate_scenario_decision_contract(authoritative_projection)
                _validate_scenario_materialization_rows(
                    decision_projection,
                    signal=signal,
                    selection=selection,
                    link=link,
                    canonical_payload=authoritative_payload,
                )

                existing_historical = connection.execute(
                    "SELECT * FROM historical_signals WHERE signal_id = ?",
                    (signal_id,),
                ).fetchone()
                if existing_historical is not None:
                    if not _scenario_historical_mirror_matches(
                        existing_historical, decision_projection
                    ):
                        raise StorageError(
                            "Scenario historical mirror conflicts with authoritative truth: "
                            + signal_id
                        )
                else:
                    connection.execute(
                        """
                        INSERT INTO historical_signals
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
                        """,
                        (
                            signal_id,
                            str(signal.get("scan_id") or ""),
                            str(signal.get("alpha_signal_id") or ""),
                            str(signal.get("generated_at") or ""),
                            str(signal.get("market_date") or "")[:10],
                            str(signal.get("ticker") or "").upper(),
                            str(signal.get("company") or ""),
                            _int_or_none(signal.get("rank")),
                            str(signal.get("source") or ""),
                            str(signal.get("source_url") or ""),
                            _float_or_none(signal.get("source_confidence")),
                            str(signal.get("data_source_kind") or ""),
                            str(signal.get("model_version") or ""),
                            str(signal.get("config_hash") or ""),
                            str(signal.get("primary_setup") or ""),
                            str(signal.get("setup_grade") or ""),
                            str(signal.get("signal_label") or ""),
                            _float_or_none(signal.get("entry_watch_level")),
                            str(signal.get("entry_trigger_type") or ""),
                            str(signal.get("entry_condition") or ""),
                            str(signal.get("confirmation_condition") or ""),
                            _float_or_none(signal.get("exit_line")),
                            _float_or_none(signal.get("invalidation_level")),
                            _float_or_none(signal.get("target_1")),
                            _float_or_none(signal.get("target_2")),
                            json.dumps(signal.get("risk_flags_json") or [], sort_keys=True),
                            json.dumps(signal.get("avoid_reasons_json") or [], sort_keys=True),
                            str(signal.get("catalyst_summary") or ""),
                            str(signal.get("telegram_event_key") or ""),
                            1 if signal.get("was_alerted") else 0,
                            str(signal.get("no_trade_reason") or ""),
                            json.dumps(signal.get("raw_payload_json") or {}, sort_keys=True),
                        ),
                    )

                selection_id = str(selection.get("selection_id") or "")
                existing_selection = connection.execute(
                    "SELECT * FROM signal_selections WHERE selection_id = ?",
                    (selection_id,),
                ).fetchone()
                if existing_selection is not None:
                    if _selection_semantics(_selection_identity_row(existing_selection)) != (
                        _selection_semantics(selection)
                    ):
                        raise StorageError(
                            "Scenario selection conflicts with authoritative truth: "
                            + selection_id
                        )
                else:
                    duplicate_selection = connection.execute(
                        """
                        SELECT selection_id FROM signal_selections
                        WHERE strategy_id = ? AND strategy_version = ?
                          AND cohort = ? AND signal_id = ?
                        """,
                        (
                            str(selection.get("strategy_id") or ""),
                            str(selection.get("strategy_version") or ""),
                            str(selection.get("cohort") or ""),
                            signal_id,
                        ),
                    ).fetchall()
                    if duplicate_selection:
                        raise StorageError(
                            "Scenario selection signal identity is already materialized: "
                            + signal_id
                        )
                    connection.execute(
                        """
                        INSERT INTO signal_selections
                        (selection_id, scan_id, signal_id, ticker, rank, strategy_id,
                         strategy_version, cohort, decision, selected_at, event_key,
                         body_sha256, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            selection_id,
                            str(selection.get("scan_id") or ""),
                            signal_id,
                            str(selection.get("ticker") or "").upper(),
                            _int_or_none(selection.get("rank")),
                            str(selection.get("strategy_id") or ""),
                            str(selection.get("strategy_version") or ""),
                            str(selection.get("cohort") or ""),
                            str(selection.get("decision") or ""),
                            str(selection.get("selected_at") or ""),
                            str(selection.get("event_key") or ""),
                            str(selection.get("body_sha256") or ""),
                            json.dumps(selection.get("payload_json") or {}, sort_keys=True),
                        ),
                    )

                existing_link = connection.execute(
                    "SELECT * FROM scenario_signal_links WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
                signal_links = connection.execute(
                    "SELECT decision_id FROM scenario_signal_links WHERE signal_id = ?",
                    (signal_id,),
                ).fetchall()
                if len(signal_links) > 1 or any(
                    str(row[0]) != decision_id for row in signal_links
                ):
                    raise StorageError(
                        "Scenario signal link identity is ambiguous: " + signal_id
                    )
                if existing_link is not None:
                    if _scenario_link_projection(existing_link) != _scenario_link_projection(
                        link
                    ):
                        raise StorageError(
                            "Scenario signal link conflicts with authoritative truth: "
                            + decision_id
                        )
                else:
                    connection.execute(
                        """
                        INSERT INTO scenario_signal_links
                        (decision_id, signal_id, scan_id, paper_intent_id, entry_intent_id,
                         exit_intent_id, position_id, entry_fill_id, exit_fill_id,
                         paper_trade_id, outcome_id, cohort, strategy_id, strategy_version,
                         created_at, updated_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            decision_id,
                            signal_id,
                            str(link.get("scan_id") or ""),
                            str(link.get("paper_intent_id") or ""),
                            str(link.get("entry_intent_id") or ""),
                            str(link.get("exit_intent_id") or ""),
                            str(link.get("position_id") or ""),
                            str(link.get("entry_fill_id") or ""),
                            str(link.get("exit_fill_id") or ""),
                            str(link.get("paper_trade_id") or ""),
                            str(link.get("outcome_id") or ""),
                            str(link.get("cohort") or ""),
                            str(link.get("strategy_id") or ""),
                            str(link.get("strategy_version") or ""),
                            str(link.get("created_at") or ""),
                            str(link.get("updated_at") or ""),
                            json.dumps(link.get("payload_json") or link, sort_keys=True),
                        ),
                    )
                return {
                    "decision_id": decision_id,
                    "signal_id": signal_id,
                    "selection_id": selection_id,
                }
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(
                f"Could not atomically materialize Scenario decision {decision_id}: {exc}"
            ) from exc

    def load_scenario_decisions(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        cohort: str | None = None,
        ticker: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("market_date >= ?")
            params.append(start[:10])
        if end:
            clauses.append("market_date <= ?")
            params.append(end[:10])
        if cohort:
            clauses.append("cohort = ?")
            params.append(cohort)
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker.upper())
        query = "SELECT * FROM scenario_decisions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY decision_at DESC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                return [_json_row(row) for row in connection.execute(query, params).fetchall()]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load scenario decisions: {exc}") from exc

    def persist_scenario_events(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Append immutable lifecycle events for every scenario decision."""

        self.initialize()
        inserted = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    event_id = str(row.get("event_id") or "")
                    decision_id = str(row.get("decision_id") or "")
                    if not event_id or not decision_id:
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO scenario_events
                        (event_id, decision_id, event_type, event_timestamp, payload_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            decision_id,
                            str(row.get("event_type") or ""),
                            str(row.get("event_timestamp") or ""),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    inserted += int(cursor.rowcount > 0)
            return {"inserted": inserted, "row_count": len(rows)}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist scenario events: {exc}") from exc

    def upsert_scenario_model_registry(self, row: dict[str, Any]) -> None:
        """Record governed policy metadata without treating it as calibration evidence."""

        self.initialize()
        model_id = str(row.get("model_id") or "")
        if not model_id:
            raise StorageError("Scenario model registry requires model_id.")
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO scenario_model_registry
                    (model_id, created_at, policy_version, feature_schema_version,
                     calibration_status, sample_count, promotion_state, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        model_id,
                        str(row.get("created_at") or ""),
                        str(row.get("policy_version") or ""),
                        str(row.get("feature_schema_version") or ""),
                        str(row.get("calibration_status") or "UNCALIBRATED"),
                        int(row.get("sample_count") or 0),
                        str(row.get("promotion_state") or "research_only"),
                        json.dumps(row, sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist scenario model registry: {exc}") from exc

    def upsert_scenario_signal_links(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        self.initialize()
        inserted = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    decision_id = str(row.get("decision_id") or "")
                    if not decision_id:
                        continue
                    connection.row_factory = sqlite3.Row
                    existing_row = connection.execute(
                        "SELECT * FROM scenario_signal_links WHERE decision_id = ?",
                        (decision_id,),
                    ).fetchone()
                    existing = _json_row(existing_row) if existing_row is not None else {}
                    stable_identity_fields = (
                        "signal_id",
                        "scan_id",
                        "paper_intent_id",
                        "entry_intent_id",
                        "exit_intent_id",
                        "position_id",
                        "entry_fill_id",
                        "exit_fill_id",
                        "paper_trade_id",
                        "outcome_id",
                    )
                    for field in stable_identity_fields:
                        current = str(existing.get(field) or "")
                        proposed = str(row.get(field) or "")
                        if current and proposed and current != proposed:
                            raise StorageError(
                                "Scenario lifecycle identity is immutable for "
                                f"{decision_id}: {field} changed from {current} to {proposed}."
                            )
                    merged = dict(existing)
                    for key, value in row.items():
                        if key in stable_identity_fields and value in {None, ""}:
                            continue
                        merged[key] = value
                    merged["decision_id"] = decision_id
                    merged["created_at"] = str(
                        existing.get("created_at") or row.get("created_at") or ""
                    )
                    merged["updated_at"] = str(
                        row.get("updated_at") or existing.get("updated_at") or ""
                    )
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO scenario_signal_links
                        (decision_id, signal_id, scan_id, paper_intent_id, entry_intent_id,
                         exit_intent_id, position_id, entry_fill_id, exit_fill_id,
                         paper_trade_id, outcome_id, cohort, strategy_id, strategy_version,
                         created_at, updated_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            decision_id,
                            str(merged.get("signal_id") or ""),
                            str(merged.get("scan_id") or ""),
                            str(merged.get("paper_intent_id") or ""),
                            str(merged.get("entry_intent_id") or ""),
                            str(merged.get("exit_intent_id") or ""),
                            str(merged.get("position_id") or ""),
                            str(merged.get("entry_fill_id") or ""),
                            str(merged.get("exit_fill_id") or ""),
                            str(merged.get("paper_trade_id") or ""),
                            str(merged.get("outcome_id") or ""),
                            str(merged.get("cohort") or ""),
                            str(merged.get("strategy_id") or ""),
                            str(merged.get("strategy_version") or ""),
                            str(merged.get("created_at") or ""),
                            str(merged.get("updated_at") or ""),
                            json.dumps(merged, sort_keys=True),
                        ),
                    )
                    inserted += 1
            return {"inserted": inserted, "row_count": len(rows)}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist scenario signal links: {exc}") from exc

    def load_scenario_signal_links(
        self, *, decision_id: str | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        self.initialize()
        query = "SELECT * FROM scenario_signal_links"
        params: list[Any] = []
        if decision_id:
            query += " WHERE decision_id = ?"
            params.append(decision_id)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                return [_json_row(row) for row in connection.execute(query, params).fetchall()]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load scenario signal links: {exc}") from exc

    def persist_scenario_run_receipt(self, row: dict[str, Any]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO scenario_run_receipts
                    (run_id, run_type, status, started_at, completed_at, error_code, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row.get("run_id") or ""),
                        str(row.get("run_type") or ""),
                        str(row.get("status") or ""),
                        str(row.get("started_at") or ""),
                        str(row.get("completed_at") or ""),
                        str(row.get("error_code") or ""),
                        json.dumps(row, sort_keys=True),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist scenario run receipt: {exc}") from exc

    def load_scenario_run_receipts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._load_scenario_payloads(
            table="scenario_run_receipts", order_by="started_at DESC, run_id ASC", limit=limit
        )

    def persist_scenario_daily_performance(self, rows: list[dict[str, Any]]) -> None:
        self.initialize()
        try:
            with self._connect() as connection:
                for row in rows:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO scenario_daily_performance
                        (market_date, cohort, strategy_id, policy_version, signal_count,
                         triggered_count, closed_eligible_count, open_count, missing_count,
                         quarantined_count, gross_return_pct, modeled_after_cost_return_pct,
                         benchmark_return_pct, excess_return_pct, hit_rate_pct, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row.get("market_date") or "")[:10],
                            str(row.get("cohort") or ""),
                            str(row.get("strategy_id") or ""),
                            str(row.get("policy_version") or ""),
                            int(row.get("signal_count") or 0),
                            int(row.get("triggered_count") or 0),
                            int(row.get("closed_eligible_count") or 0),
                            int(row.get("open_count") or 0),
                            int(row.get("missing_count") or 0),
                            int(row.get("quarantined_count") or 0),
                            _float_or_none(row.get("gross_return_pct")),
                            _float_or_none(row.get("modeled_after_cost_return_pct")),
                            _float_or_none(row.get("benchmark_return_pct")),
                            _float_or_none(row.get("excess_return_pct")),
                            _float_or_none(row.get("hit_rate_pct")),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist scenario daily performance: {exc}") from exc

    def load_scenario_daily_performance(
        self, *, start: str | None = None, end: str | None = None, limit: int = 10000
    ) -> list[dict[str, Any]]:
        return self._load_scenario_payloads(
            table="scenario_daily_performance",
            order_by="market_date DESC",
            start=start,
            end=end,
            limit=limit,
        )

    def _load_scenario_payloads(
        self,
        *,
        table: str,
        order_by: str,
        start: str | None = None,
        end: str | None = None,
        limit: int,
    ) -> list[dict[str, Any]]:
        query_specs = {
            "scenario_news_items": (
                "SELECT * FROM scenario_news_items",
                "created_at >= ?",
                "created_at <= ?",
                " ORDER BY created_at DESC, article_id ASC LIMIT ?",
                "created_at DESC, article_id ASC",
            ),
            "scenario_run_receipts": (
                "SELECT * FROM scenario_run_receipts",
                "started_at >= ?",
                "started_at <= ?",
                " ORDER BY started_at DESC, run_id ASC LIMIT ?",
                "started_at DESC, run_id ASC",
            ),
            "scenario_daily_performance": (
                "SELECT * FROM scenario_daily_performance",
                "market_date >= ?",
                "market_date <= ?",
                " ORDER BY market_date DESC LIMIT ?",
                "market_date DESC",
            ),
        }
        spec = query_specs.get(table)
        if spec is None or order_by != spec[4]:
            raise StorageError("Unsupported scenario payload table")
        self.initialize()
        query_prefix, start_clause, end_clause, order_clause, _ = spec
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append(start_clause)
            params.append(start)
        if end:
            clauses.append(end_clause)
            params.append(end)
        query = query_prefix
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += order_clause
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                return [_json_row(row) for row in connection.execute(query, params).fetchall()]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load scenario payloads: {exc}") from exc

    def persist_scenario_replay_trades(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        self.initialize()
        inserted = 0
        try:
            with self._connect() as connection:
                for row in rows:
                    identity = str(row.get("replay_trade_id") or "")
                    if not identity:
                        continue
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO scenario_replay_trades
                        (replay_trade_id, decision_id, article_id, ticker, market_date, entry_at,
                         entry_price, exit_at, exit_price, outcome_status, gross_return_pct,
                         modeled_after_cost_return_pct, quarantine_reason,
                         source_bar_hash_sha256, source_quote_hash_sha256, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            identity,
                            str(row.get("decision_id") or ""),
                            str(row.get("article_id") or ""),
                            str(row.get("ticker") or "").upper(),
                            str(row.get("market_date") or "")[:10],
                            str(row.get("entry_at") or ""),
                            _float_or_none(row.get("entry_price")),
                            str(row.get("exit_at") or ""),
                            _float_or_none(row.get("exit_price")),
                            str(row.get("outcome_status") or ""),
                            _float_or_none(row.get("gross_return_pct")),
                            _float_or_none(row.get("modeled_after_cost_return_pct")),
                            str(row.get("quarantine_reason") or ""),
                            str(row.get("source_bar_hash_sha256") or ""),
                            str(row.get("source_quote_hash_sha256") or ""),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    inserted += 1
            return {"inserted": inserted, "row_count": len(rows)}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist scenario replay trades: {exc}") from exc

    def load_scenario_replay_trades(
        self, *, start: str | None = None, end: str | None = None, limit: int = 50000
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("market_date >= ?")
            params.append(start[:10])
        if end:
            clauses.append("market_date <= ?")
            params.append(end[:10])
        query = "SELECT * FROM scenario_replay_trades"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY market_date DESC, ticker ASC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                connection.row_factory = sqlite3.Row
                return [_json_row(row) for row in connection.execute(query, params).fetchall()]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not load scenario replay trades: {exc}") from exc

    def persist_strategy_decision_receipt(
        self,
        receipt: Any,
        *,
        evidence_claims: Iterable[Any] = (),
        resolution_run: Any | None = None,
    ) -> bool:
        """Insert one immutable receipt, rejecting identity/payload drift."""

        self.initialize()
        canonical = receipt.canonical_json()
        claims = [
            claim.to_dict() if hasattr(claim, "to_dict") else dict(claim)
            for claim in evidence_claims
        ]
        run = (
            resolution_run.to_dict()
            if resolution_run is not None and hasattr(resolution_run, "to_dict")
            else resolution_run
        )
        receipt_id = str(receipt.receipt_id)
        receipt_hash = str(receipt.receipt_hash_sha256)

        def _assert_root_identity(existing: tuple[Any, ...]) -> None:
            if str(existing[0]) != receipt_hash or str(existing[1]) != canonical:
                raise StorageError("strategy decision receipt identity/payload mismatch")

        try:
            with self._connect() as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                # Serialize writers around the immutable identity check. This
                # makes an exact retry deterministic even when two scan
                # workers race to persist the same receipt.
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    (
                        "SELECT receipt_hash_sha256, canonical_json "
                        "FROM strategy_decision_receipts WHERE receipt_id = ?"
                    ),
                    (receipt_id,),
                ).fetchone()
                inserted = existing is None
                if existing is None:
                    try:
                        connection.execute(
                            """INSERT INTO strategy_decision_receipts
                            (receipt_id, receipt_hash_sha256, strategy_id, strategy_version, symbol,
                             market_date, pick_tier, research_pick_eligible, paper_entry_eligible,
                             source_identity, input_hash_sha256, canonical_json, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                receipt_id,
                                receipt_hash,
                                receipt.strategy_id,
                                receipt.strategy_version,
                                receipt.symbol,
                                receipt.market_date,
                                receipt.pick_tier.value
                                if hasattr(receipt.pick_tier, "value")
                                else str(receipt.pick_tier),
                                int(receipt.research_pick_eligible),
                                int(receipt.paper_entry_eligible),
                                receipt.source_identity,
                                receipt.input_hash_sha256,
                                canonical,
                                datetime.now(UTC).replace(microsecond=0).isoformat(),
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        # A receipt hash collision with another ID is not an
                        # exact retry and must fail closed. A concurrent ID
                        # insert is re-read and compared below.
                        existing = connection.execute(
                            """SELECT receipt_hash_sha256, canonical_json
                            FROM strategy_decision_receipts WHERE receipt_id = ?""",
                            (receipt_id,),
                        ).fetchone()
                        if existing is None:
                            hash_owner = connection.execute(
                                """SELECT receipt_id FROM strategy_decision_receipts
                                WHERE receipt_hash_sha256 = ?""",
                                (receipt_hash,),
                            ).fetchone()
                            if hash_owner is not None:
                                raise StorageError(
                                    "strategy decision receipt hash is bound to another receipt ID"
                                ) from exc
                            raise
                        inserted = False
                if existing is not None:
                    _assert_root_identity(existing)

                for result in receipt.condition_results:
                    row = result.to_dict() if hasattr(result, "to_dict") else dict(result)
                    condition_payload = json.dumps(
                        row, sort_keys=True, separators=(",", ":")
                    )
                    existing_condition = connection.execute(
                        """SELECT payload_json FROM strategy_condition_results
                        WHERE receipt_id = ? AND condition_id = ?""",
                        (receipt_id, str(row["condition_id"])),
                    ).fetchone()
                    if existing_condition is not None:
                        if str(existing_condition[0]) != condition_payload:
                            raise StorageError(
                                "strategy decision condition payload mismatch"
                            )
                        continue
                    connection.execute(
                        """INSERT INTO strategy_condition_results
                        (receipt_id, condition_id, status, source_urls_json,
                         source_hashes_json, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            receipt_id,
                            str(row["condition_id"]),
                            getattr(row.get("status"), "value", str(row.get("status") or "")),
                            json.dumps(
                                list(row.get("source_urls") or ()),
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            json.dumps(
                                list(row.get("source_hashes") or ()),
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            condition_payload,
                        ),
                    )
                for claim in claims:
                    if not isinstance(claim, Mapping):
                        raise StorageError("strategy evidence claim must be an object")
                    claim_id = str(claim.get("claim_id") or "")
                    if not claim_id:
                        raise StorageError("strategy evidence claim ID is required")
                    claim_payload = json.dumps(
                        dict(claim), sort_keys=True, separators=(",", ":")
                    )
                    existing_claim = connection.execute(
                        """SELECT receipt_id, payload_json FROM strategy_evidence_claims
                        WHERE claim_id = ?""",
                        (claim_id,),
                    ).fetchone()
                    if existing_claim is not None:
                        if str(existing_claim[1]) != claim_payload:
                            raise StorageError("strategy evidence claim payload mismatch")
                        if str(existing_claim[0]) != receipt_id:
                            raise StorageError(
                                "strategy evidence claim is already bound to another receipt"
                            )
                        continue
                    connection.execute(
                        """INSERT INTO strategy_evidence_claims
                        (claim_id, receipt_id, condition_id, symbol, source_urls_json,
                         source_hashes_json, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            claim_id,
                            receipt_id,
                            str(claim["condition_id"]),
                            str(claim["symbol"]),
                            json.dumps(
                                claim.get("source_urls") or [],
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            json.dumps(
                                claim.get("source_hashes") or [],
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            claim_payload,
                        ),
                    )
                if isinstance(run, dict):
                    run_id = str(run.get("run_id") or "")
                    if not run_id:
                        raise StorageError("strategy evidence resolution run ID is required")
                    run_payload = json.dumps(run, sort_keys=True, separators=(",", ":"))
                    existing_run = connection.execute(
                        """SELECT receipt_id, payload_json FROM strategy_evidence_resolution_runs
                        WHERE run_id = ?""",
                        (run_id,),
                    ).fetchone()
                    if existing_run is not None:
                        if str(existing_run[1]) != run_payload:
                            raise StorageError("strategy evidence resolution run payload mismatch")
                        if str(existing_run[0] or "") != receipt_id:
                            raise StorageError(
                                "strategy evidence resolution run is already bound "
                                "to another receipt"
                            )
                        return inserted
                    connection.execute(
                        """INSERT INTO strategy_evidence_resolution_runs
                        (run_id, receipt_id, symbol, market_date, requested_model, actual_model,
                         response_id, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            run_id,
                            receipt_id,
                            str(run["symbol"]),
                            str(run["market_date"]),
                            str(run["requested_model"]),
                            str(run["actual_model"]),
                            str(run["response_id"]),
                            run_payload,
                        ),
                    )
                return inserted
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist strategy decision receipt: {exc}") from exc

    def persist_strategy_decision_receipts(self, receipts: Iterable[Any]) -> dict[str, int]:
        inserted = 0
        reused = 0
        for receipt in receipts:
            if self.persist_strategy_decision_receipt(receipt):
                inserted += 1
            else:
                reused += 1
        return {"inserted": inserted, "reused": reused}

    def load_strategy_decision_receipts(
        self, *, market_date: str | None = None, strategy_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.initialize()
        params: list[Any] = []
        if market_date and strategy_id:
            query = (
                "SELECT canonical_json FROM strategy_decision_receipts "
                "WHERE market_date = ? AND strategy_id = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params.extend((market_date, strategy_id))
        elif market_date:
            query = (
                "SELECT canonical_json FROM strategy_decision_receipts "
                "WHERE market_date = ? ORDER BY created_at DESC LIMIT ?"
            )
            params.append(market_date)
        elif strategy_id:
            query = (
                "SELECT canonical_json FROM strategy_decision_receipts "
                "WHERE strategy_id = ? ORDER BY created_at DESC LIMIT ?"
            )
            params.append(strategy_id)
        else:
            query = (
                "SELECT canonical_json FROM strategy_decision_receipts "
                "ORDER BY created_at DESC LIMIT ?"
            )
        with self._connect() as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return [json.loads(str(row[0])) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            return connect_read_only(self.db_path)
        return sqlite3.connect(self.db_path, timeout=30.0)

    def connect_read_only(self) -> sqlite3.Connection:
        """Open this database without allowing the caller to mutate it."""

        return connect_read_only(self.db_path)


def _assert_applied_backfeed_allowed(
    events: list[dict[str, Any]],
    *,
    review_id: str,
    quarantine_manifest_path: str | Path | None,
    db_path: Path,
) -> None:
    if not any(event.get("applied") is True for event in events):
        return
    if quarantine_manifest_path is None:
        raise StorageError("Applied learning backfeed requires a current quarantine audit receipt.")
    from intraday_scanner.mover_pattern_audit import (  # local to avoid cycles
        assert_backfeed_not_quarantined,
    )

    assert_backfeed_not_quarantined(
        review_id,
        quarantine_manifest_path,
        db_path=db_path,
    )


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


def _selection_semantics(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Normalize immutable selection columns for exact transactional checks."""

    return (
        str(row.get("selection_id") or ""),
        str(row.get("scan_id") or ""),
        str(row.get("signal_id") or ""),
        str(row.get("ticker") or "").upper(),
        _int_or_none(row.get("rank")),
        str(row.get("strategy_id") or ""),
        str(row.get("strategy_version") or ""),
        str(row.get("cohort") or ""),
        str(row.get("decision") or ""),
        str(row.get("selected_at") or ""),
        str(row.get("event_key") or ""),
        str(row.get("body_sha256") or ""),
        _json_value(row.get("payload_json")),
    )


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


def _official_strategy_cohort_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_value(row["payload_json"])
    return {
        "official_cohort_id": str(row["official_cohort_id"]),
        "market_date": str(row["market_date"]),
        "strategy_id": str(row["strategy_id"]),
        "strategy_version": str(row["strategy_version"]),
        "cohort": str(row["cohort"]),
        "scan_id": str(row["scan_id"]),
        "event_key": str(row["event_key"]),
        "body_sha256": str(row["body_sha256"]),
        "membership_sha256": str(row["membership_sha256"]),
        "claimed_at": str(row["claimed_at"]),
        "payload_json": payload if isinstance(payload, dict) else {},
    }


_SCENARIO_DECISION_PROJECTION_FIELDS = (
    "decision_id",
    "article_id",
    "ticker",
    "market_date",
    "decision_at",
    "event_type",
    "direction",
    "directional_evidence_score",
    "action",
    "calibration_status",
    "entry_trigger",
    "invalidation_level",
    "target_1",
    "time_stop",
    "source_tier",
    "source_lineage_hash_sha256",
    "feature_hash_sha256",
    "cohort",
    "policy_version",
    "feature_schema_version",
    "research_only",
    "broker_execution_enabled",
)


def _mapping_value(row: Mapping[str, Any], key: str, default: Any = "") -> Any:
    if isinstance(row, sqlite3.Row):
        return row[key] if key in row.keys() else default
    return row.get(key, default)


def _scenario_bool(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def _scenario_optional_bool(row: Mapping[str, Any], key: str) -> bool | None:
    value = _mapping_value(row, key, None)
    if value is None or value == "":
        return None
    return _scenario_bool(value)


def _scenario_decision_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        row = {key: row[key] for key in row.keys()}
    return {
        "decision_id": str(_mapping_value(row, "decision_id") or ""),
        "article_id": str(_mapping_value(row, "article_id") or ""),
        "ticker": str(_mapping_value(row, "ticker") or "").upper(),
        "market_date": str(_mapping_value(row, "market_date") or "")[:10],
        "decision_at": str(_mapping_value(row, "decision_at") or ""),
        "event_type": str(_mapping_value(row, "event_type") or ""),
        "direction": str(_mapping_value(row, "direction") or "").lower(),
        "directional_evidence_score": _float_or_none(
            _mapping_value(row, "directional_evidence_score")
        ),
        "action": str(_mapping_value(row, "action") or "").upper(),
        "calibration_status": str(
            _mapping_value(row, "calibration_status", "UNCALIBRATED") or "UNCALIBRATED"
        ),
        "entry_trigger": _float_or_none(_mapping_value(row, "entry_trigger")),
        "invalidation_level": _float_or_none(_mapping_value(row, "invalidation_level")),
        "target_1": _float_or_none(_mapping_value(row, "target_1")),
        "time_stop": str(_mapping_value(row, "time_stop") or ""),
        "source_tier": str(_mapping_value(row, "source_tier") or ""),
        "source_lineage_hash_sha256": str(
            _mapping_value(row, "source_lineage_hash_sha256") or ""
        ),
        "feature_hash_sha256": str(_mapping_value(row, "feature_hash_sha256") or ""),
        "cohort": str(_mapping_value(row, "cohort") or ""),
        "policy_version": str(_mapping_value(row, "policy_version") or ""),
        "feature_schema_version": str(_mapping_value(row, "feature_schema_version") or ""),
        "research_only": _scenario_optional_bool(row, "research_only"),
        "broker_execution_enabled": _scenario_optional_bool(row, "broker_execution_enabled"),
    }


def _validate_scenario_decision_contract(decision: Mapping[str, Any]) -> None:
    entry = decision["entry_trigger"]
    stop = decision["invalidation_level"]
    target = decision["target_1"]
    if (
        not decision["decision_id"]
        or not decision["market_date"]
        or not decision["ticker"]
        or decision["direction"] != "bullish"
        or decision["action"] != "ENTER_LONG"
        or entry is None
        or stop is None
        or target is None
        or not (entry > stop > 0 and target > entry)
        or decision["cohort"] != SCENARIO_FORWARD_COHORT
        or decision["policy_version"] != SCENARIO_POLICY_VERSION
        or decision["feature_schema_version"] != SCENARIO_FEATURE_SCHEMA_VERSION
        or decision["research_only"] is not True
        or decision["broker_execution_enabled"] is not False
    ):
        raise StorageError("Scenario decision violates the forward paper contract")


def _scenario_embedded_decision(payload: Any) -> Mapping[str, Any] | None:
    parsed = _json_value(payload)
    if not isinstance(parsed, dict):
        return None
    embedded = parsed.get("scenario_decision")
    return embedded if isinstance(embedded, dict) else None


def _scenario_historical_mirror_matches(
    row: sqlite3.Row | Mapping[str, Any], decision: Mapping[str, Any]
) -> bool:
    actual = {key: row[key] for key in row.keys()} if isinstance(row, sqlite3.Row) else row
    embedded = _scenario_embedded_decision(_mapping_value(actual, "raw_payload_json"))
    if embedded is None or _scenario_decision_projection(embedded) != dict(decision):
        return False
    return (
        str(_mapping_value(actual, "signal_id") or "") == f"scenario:{decision['decision_id']}"
        and str(_mapping_value(actual, "market_date") or "")[:10] == decision["market_date"]
        and str(_mapping_value(actual, "ticker") or "").upper() == decision["ticker"]
        and _float_or_none(_mapping_value(actual, "entry_watch_level"))
        == decision["entry_trigger"]
        and _float_or_none(_mapping_value(actual, "invalidation_level"))
        == decision["invalidation_level"]
        and _float_or_none(_mapping_value(actual, "target_1")) == decision["target_1"]
        and str(_mapping_value(actual, "model_version") or "") == decision["policy_version"]
    )


def _scenario_plan_projection(payload: Any) -> dict[str, Any] | None:
    parsed = _json_value(payload)
    if not isinstance(parsed, dict):
        return None
    return {
        "decision_id": str(
            parsed.get("decision_id") or parsed.get("scenario_decision_id") or ""
        ),
        "market_date": str(parsed.get("market_date") or "")[:10],
        "ticker": str(parsed.get("ticker") or "").upper(),
        "direction": str(parsed.get("direction") or "").lower(),
        "action": str(parsed.get("action") or "").upper(),
        "entry_trigger": _float_or_none(parsed.get("entry_trigger")),
        "invalidation_level": _float_or_none(parsed.get("invalidation_level")),
        "target_1": _float_or_none(parsed.get("target_1")),
        "cohort": str(parsed.get("cohort") or ""),
        "policy_version": str(parsed.get("policy_version") or ""),
        "feature_schema_version": str(parsed.get("feature_schema_version") or ""),
        "research_only": _scenario_optional_bool(parsed, "research_only"),
        "broker_execution_enabled": _scenario_optional_bool(
            parsed, "broker_execution_enabled"
        ),
    }


def _scenario_selection_plan(decision: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "decision_id": decision["decision_id"],
        "market_date": decision["market_date"],
        "ticker": decision["ticker"],
        "direction": decision["direction"],
        "action": decision["action"],
        "entry_trigger": decision["entry_trigger"],
        "invalidation_level": decision["invalidation_level"],
        "target_1": decision["target_1"],
        "cohort": decision["cohort"],
        "policy_version": decision["policy_version"],
        "feature_schema_version": decision["feature_schema_version"],
        "research_only": decision["research_only"],
        "broker_execution_enabled": decision["broker_execution_enabled"],
    }


def _scenario_link_projection(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(_mapping_value(row, key) or "")
        for key in (
            "decision_id",
            "signal_id",
            "scan_id",
            "cohort",
            "strategy_id",
            "strategy_version",
        )
    )


def _validate_scenario_materialization_rows(
    decision: Mapping[str, Any],
    *,
    signal: Mapping[str, Any],
    selection: Mapping[str, Any],
    link: Mapping[str, Any],
    canonical_payload: Mapping[str, Any] | None = None,
) -> None:
    signal_id = f"scenario:{decision['decision_id']}"
    if not _scenario_historical_mirror_matches(signal, decision):
        raise StorageError("Scenario historical mirror does not match authoritative decision")
    expected_plan = _scenario_selection_plan(decision)
    selection_payload = _scenario_plan_projection(selection.get("payload_json"))
    if (
        str(selection.get("selection_id") or "") != f"scenario-selection:{decision['decision_id']}"
        or str(selection.get("scan_id") or "") != f"scenario:{decision['market_date']}"
        or str(selection.get("signal_id") or "") != signal_id
        or str(selection.get("ticker") or "").upper() != decision["ticker"]
        or str(selection.get("strategy_id") or "") != SCENARIO_STRATEGY_ID
        or str(selection.get("strategy_version") or "") != SCENARIO_POLICY_VERSION
        or str(selection.get("cohort") or "") != SCENARIO_FORWARD_COHORT
        or str(selection.get("decision") or "").lower() != "paper_entry"
        or str(selection.get("selected_at") or "") != decision["decision_at"]
        or str(selection.get("event_key") or "") != f"scenario-paper:{decision['decision_id']}"
        or str(selection.get("body_sha256") or "")
        != canonical_hash(canonical_payload if canonical_payload is not None else decision)
        or selection_payload != expected_plan
    ):
        raise StorageError("Scenario selection does not match authoritative decision plan")
    if _scenario_link_projection(link) != (
        decision["decision_id"],
        signal_id,
        f"scenario:{decision['market_date']}",
        SCENARIO_FORWARD_COHORT,
        SCENARIO_STRATEGY_ID,
        SCENARIO_POLICY_VERSION,
    ):
        raise StorageError("Scenario signal link does not match authoritative decision")


def _embedded_scenario_decision_id(payload: Any) -> str:
    parsed = _json_value(payload)
    if not isinstance(parsed, dict):
        return ""
    direct = parsed.get("decision_id") or parsed.get("scenario_decision_id")
    if direct:
        return str(direct)
    scenario_decision = parsed.get("scenario_decision")
    if isinstance(scenario_decision, dict):
        return str(scenario_decision.get("decision_id") or "")
    return ""


def _validate_signal_parent_rows(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    require_market_identity: bool,
) -> None:
    """Reject child rows without a governed historical, V6, or Scenario parent.

    The legacy signal child tables predate the V6 shadow ledger and retain a
    historical_signals foreign-key declaration.  V6 shadow outcomes/events are
    intentionally keyed by ``alpha_v6_decisions.shadow_signal_id`` instead,
    and Scenario lifecycle outcomes use ``scenario_signal_links.signal_id``.
    SQLite's single-parent constraint cannot express the valid domain. Keep the
    write boundary fail-closed with an application-level, exact identity check
    and bind outcome rows to the governed parent's day/ticker.
    """

    candidates = [
        row
        for row in rows
        if str(row.get("signal_id") or "")
    ]
    if not candidates:
        return

    signal_ids = sorted({str(row["signal_id"]) for row in candidates})
    placeholders = ",".join("?" for _ in signal_ids)
    historical = {
        str(row[0]): (
            str(row[1] or ""),
            str(row[2] or ""),
            _embedded_scenario_decision_id(row[3]),
            _json_value(row[3]),
            _float_or_none(row[4]),
            _float_or_none(row[5]),
            _float_or_none(row[6]),
            str(row[7] or ""),
        )
        for row in connection.execute(
            "SELECT signal_id, market_date, ticker, raw_payload_json, "
            "entry_watch_level, invalidation_level, target_1, model_version "
            f"FROM historical_signals WHERE signal_id IN ({placeholders})",  # nosec B608
            signal_ids,
        ).fetchall()
    }
    shadow_decisions = {
        str(row[0]): (str(row[1] or ""), str(row[2] or ""), "")
        for row in connection.execute(
            "SELECT shadow_signal_id, market_date, ticker "
            f"FROM alpha_v6_decisions WHERE shadow_signal_id IN ({placeholders})",  # nosec B608
            signal_ids,
        ).fetchall()
    }
    scenario_parents: dict[str, set[tuple[str, ...]]] = {}
    for row in connection.execute(
        "SELECT l.signal_id, d.decision_id, d.market_date, d.ticker, "
        "d.cohort, d.policy_version, d.feature_schema_version, d.research_only, "
        "d.broker_execution_enabled, d.direction, d.action, d.entry_trigger, "
        "d.invalidation_level, d.target_1, "
        "l.cohort, l.strategy_id, l.strategy_version "
        "FROM scenario_signal_links AS l "
        "JOIN scenario_decisions AS d ON d.decision_id = l.decision_id "
        f"WHERE l.signal_id IN ({placeholders})",  # nosec B608
        signal_ids,
    ).fetchall():
        signal_id = str(row[0] or "")
        if signal_id:
            scenario_parents.setdefault(signal_id, set()).add(
                tuple(str(value if value is not None else "") for value in row[1:])
            )

    missing = 0
    mismatched = 0
    ambiguous = 0
    for row in candidates:
        signal_id = str(row["signal_id"])
        historical_parent = historical.get(signal_id)
        shadow_parent = shadow_decisions.get(signal_id)
        scenario_entries = scenario_parents.get(signal_id, set())
        if signal_id.startswith("scenario:"):
            if len(scenario_entries) == 0:
                mismatched += 1
                continue
            if len(scenario_entries) != 1 or shadow_parent is not None:
                ambiguous += 1
                continue
            (
                expected_decision_id,
                expected_date,
                expected_ticker,
                decision_cohort,
                decision_policy_version,
                decision_feature_schema_version,
                decision_research_only,
                decision_broker_execution_enabled,
                decision_direction,
                decision_action,
                decision_entry_trigger,
                decision_invalidation_level,
                decision_target_1,
                link_cohort,
                link_strategy_id,
                link_strategy_version,
            ) = next(iter(scenario_entries))
            contract_matches = (
                bool(expected_decision_id)
                and bool(expected_date[:10])
                and bool(expected_ticker)
                and signal_id == f"scenario:{expected_decision_id}"
                and decision_cohort == SCENARIO_FORWARD_COHORT
                and decision_policy_version == SCENARIO_POLICY_VERSION
                and decision_feature_schema_version == SCENARIO_FEATURE_SCHEMA_VERSION
                and decision_research_only == "1"
                and decision_broker_execution_enabled == "0"
                and decision_direction == "bullish"
                and decision_action == "ENTER_LONG"
                and _float_or_none(decision_entry_trigger) is not None
                and _float_or_none(decision_invalidation_level) is not None
                and _float_or_none(decision_target_1) is not None
                and (
                    _float_or_none(decision_entry_trigger)
                    > _float_or_none(decision_invalidation_level)
                    > 0
                    and _float_or_none(decision_target_1)
                    > _float_or_none(decision_entry_trigger)
                )
                and link_cohort == decision_cohort
                and link_strategy_id == SCENARIO_STRATEGY_ID
                and link_strategy_version == decision_policy_version
            )
            historical_matches = True
            if historical_parent is not None:
                historical_date, historical_ticker, historical_decision_id = historical_parent[:3]
                historical_raw_payload = historical_parent[3]
                historical_entry = historical_parent[4]
                historical_stop = historical_parent[5]
                historical_target = historical_parent[6]
                historical_model_version = historical_parent[7]
                embedded = _scenario_embedded_decision(historical_raw_payload)
                embedded_plan = _scenario_plan_projection(embedded)
                expected_plan = {
                    "decision_id": expected_decision_id,
                    "market_date": expected_date[:10],
                    "ticker": expected_ticker.upper(),
                    "direction": decision_direction,
                    "action": decision_action,
                    "entry_trigger": _float_or_none(decision_entry_trigger),
                    "invalidation_level": _float_or_none(decision_invalidation_level),
                    "target_1": _float_or_none(decision_target_1),
                    "cohort": decision_cohort,
                    "policy_version": decision_policy_version,
                    "feature_schema_version": decision_feature_schema_version,
                    "research_only": decision_research_only == "1",
                    "broker_execution_enabled": decision_broker_execution_enabled == "1",
                }
                historical_matches = (
                    bool(historical_date)
                    and historical_date[:10] == expected_date[:10]
                    and bool(historical_ticker)
                    and historical_ticker.upper() == expected_ticker.upper()
                    and historical_decision_id == expected_decision_id
                    and embedded_plan == expected_plan
                    and historical_entry == expected_plan["entry_trigger"]
                    and historical_stop == expected_plan["invalidation_level"]
                    and historical_target == expected_plan["target_1"]
                    and historical_model_version == expected_plan["policy_version"]
                )
            if not contract_matches or not historical_matches:
                mismatched += 1
                continue
            if not require_market_identity:
                claimed_decision_id = str(
                    (row.get("payload_json") or {}).get("decision_id") or ""
                    if isinstance(row.get("payload_json"), dict)
                    else ""
                )
                if claimed_decision_id != expected_decision_id:
                    mismatched += 1
                continue
            market_date = str(row.get("market_date") or row.get("date") or "")[:10]
            ticker = str(row.get("ticker") or "").upper()
            date_matches = bool(market_date) and market_date == expected_date[:10]
            ticker_matches = bool(ticker) and ticker == expected_ticker.upper()
            claimed_decision_id = str(
                (row.get("payload_json") or {}).get("decision_id") or ""
                if isinstance(row.get("payload_json"), dict)
                else ""
            )
            if (
                not date_matches
                or not ticker_matches
                or claimed_decision_id != expected_decision_id
            ):
                mismatched += 1
            continue

        if scenario_entries:
            mismatched += 1
            continue
        parents = [
            parent for parent in (historical_parent, shadow_parent) if parent is not None
        ]
        if len(parents) == 0:
            missing += 1
            continue
        if len(parents) != 1:
            ambiguous += 1
            continue
        if not require_market_identity:
            continue
        expected_date, expected_ticker, _ = parents[0][:3]
        market_date = str(row.get("market_date") or row.get("date") or "")[:10]
        ticker = str(row.get("ticker") or "").upper()
        date_matches = bool(market_date) and market_date == expected_date[:10]
        ticker_matches = bool(ticker) and ticker == expected_ticker.upper()
        if not date_matches or not ticker_matches:
            mismatched += 1

    rejected = missing + mismatched + ambiguous
    if rejected:
        raise StorageError(
            "Signal child parent validation failed: "
            f"rejected={rejected}, missing_parent={missing}, "
            f"identity_mismatch={mismatched}, ambiguous_parent={ambiguous}"
        )


def _immutable_new_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    identity_column: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    allowed = {
        ("strategy_evaluations", "evaluation_id"),
        ("strategy_paper_trades", "trade_id"),
        ("strategy_learning_labels", "label_id"),
        ("daily_strategy_scorecards", "scorecard_id"),
        ("signal_outcomes", "signal_id"),
        ("signal_events", "event_id"),
    }
    if (table, identity_column) not in allowed:
        raise StorageError(f"Unsupported immutable strategy table: {table}")
    table_sql = quote_sql_identifier(table, allowed={pair[0] for pair in allowed})
    identity_sql = quote_sql_identifier(
        identity_column,
        allowed={pair[1] for pair in allowed if pair[0] == table},
    )
    pending: list[dict[str, Any]] = []
    for row in rows:
        identity = str(row.get(identity_column) or "").strip()
        if not identity:
            continue
        existing = connection.execute(
            f"SELECT payload_json FROM {table_sql} WHERE {identity_sql} = ? LIMIT 1",  # nosec B608
            (identity,),
        ).fetchone()
        if existing is None:
            pending.append(row)
            continue
        payload = _json_value(existing[0])
        if not isinstance(payload, dict) or _immutable_semantics(payload) != _immutable_semantics(
            row
        ):
            raise StorageError(f"Immutable {table} identity conflicts with prior truth: {identity}")
    return pending


def _immutable_semantics(value: Any) -> Any:
    volatile = {
        "reconciled_at",
        "created_at",
        "imported_at",
        "artifact_path",
        "retained_source_path",
        "source_bar_artifact_path",
        "event_timestamp",
    }
    if isinstance(value, dict):
        return {
            key: _immutable_semantics(item)
            for key, item in sorted(value.items())
            if key not in volatile
        }
    if isinstance(value, list):
        return [_immutable_semantics(item) for item in value]
    return value


def _json_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_value(row["payload_json"])
    merged = {key: row[key] for key in row.keys() if key != "payload_json"}
    if isinstance(payload, dict):
        merged.update(payload)
    return merged


def _trade_intent_action(row: Mapping[str, Any]) -> str:
    return str(row.get("action") or "").strip().upper()


def _persist_bound_monitor_receipts(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    admitted_intents_by_id: Mapping[str, Mapping[str, Any]],
    candidate_fills: list[dict[str, Any]],
) -> dict[str, int]:
    inserted = 0
    reused = 0
    for row in rows:
        intent_id = str(row.get("intent_id") or "").strip()
        receipt_id = str(row.get("receipt_id") or "").strip()
        content_hash = str(row.get("content_hash_sha256") or "").strip()
        intent = admitted_intents_by_id.get(intent_id)
        if intent is None:
            continue
        trace = intent.get("decision_trace")
        trace = trace if isinstance(trace, dict) else {}
        proof = intent.get("watcher_current_proof")
        proof = proof if isinstance(proof, dict) else {}
        quote = proof.get("quote_receipt")
        portfolio = proof.get("portfolio_receipt")
        quote = quote if isinstance(quote, dict) else {}
        portfolio = portfolio if isinstance(portfolio, dict) else {}
        frozen_lineage = intent.get("monitor_proof_lineage")
        frozen_lineage = frozen_lineage if isinstance(frozen_lineage, dict) else {}
        intent_account = _trade_intent_account_id(intent)
        canonical_without_hash = {
            key: value for key, value in row.items() if key != "content_hash_sha256"
        }
        recomputed_hash = hashlib.sha256(
            json.dumps(
                canonical_without_hash,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
        ).hexdigest()
        quote_hash = _canonical_sha256(quote)
        portfolio_hash = _canonical_sha256(portfolio)
        proof_hash = _canonical_sha256(
            {key: value for key, value in proof.items() if key != "proof_hash_sha256"}
        )
        expected_receipt_id = "monitor-publication-" + hashlib.sha256(
            (
                f"{intent.get('signal_id')}:{intent_id}:"
                f"{trace.get('plan_hash_sha256')}:{proof_hash}"
            ).encode()
        ).hexdigest()[:24]
        validation_row = intent.get("watcher_validation_row")
        if not isinstance(validation_row, dict):
            raise StorageError(
                "monitor publication receipt requires strict watcher validation envelope"
            )
        from intraday_scanner.services.luna_research_slate_service import (
            validate_watcher_current_proof,
        )

        if not validate_watcher_current_proof(validation_row):
            raise StorageError(
                "monitor publication receipt requires strict watcher validation envelope"
            )
        if (
            not receipt_id
            or not content_hash
            or row.get("schema_version")
            != "dawnstrike.alphaops.monitor_publication_receipt.v1"
            or row.get("publication_tier") != "ALERTABLE_PAPER_ENTRY"
            or int(row.get("publication_count") or 0) != 1
            or row.get("research_only") is not True
            or row.get("broker_execution") != "disabled"
            or _trade_intent_action(intent) not in {"ENTER_LONG", "ENTER_SHORT"}
            or str(row.get("market_date") or "")[:10]
            != str(intent.get("market_date") or "")[:10]
            or str(row.get("signal_id") or "") != str(intent.get("signal_id") or "")
            or str(row.get("ticker") or "").upper()
            != str(intent.get("ticker") or "").upper()
            or str(row.get("simulated_account_id") or "") != intent_account
            or str(row.get("plan_hash_sha256") or "")
            != str(trace.get("plan_hash_sha256") or "")
            or str(row.get("decision_trace_fingerprint") or "")
            != str(
                intent.get("decision_fingerprint")
                or trace.get("decision_fingerprint")
                or ""
            )
            or not proof
            or str(proof.get("proof_hash_sha256") or "") != proof_hash
            or str(proof.get("quote_hash_sha256") or "") != quote_hash
            or str(proof.get("portfolio_hash_sha256") or "") != portfolio_hash
            or str(row.get("watcher_proof_hash_sha256") or "") != proof_hash
            or str(row.get("quote_receipt_hash_sha256") or "") != quote_hash
            or str(row.get("portfolio_receipt_hash_sha256") or "") != portfolio_hash
            or receipt_id != expected_receipt_id
            or str(row.get("checked_at") or "") != str(proof.get("checked_at") or "")
            or any(
                not str(row.get(key) or "")
                or str(row.get(key) or "") != str(proof.get(key) or "")
                or str(row.get(key) or "") != str(frozen_lineage.get(key) or "")
                for key in (
                    "selection_id",
                    "cohort",
                    "source_scan_id",
                    "frozen_slate_id",
                    "frozen_slate_content_hash_sha256",
                    "frozen_research_selection_id",
                )
            )
            or str(quote.get("signal_id") or "") != str(intent.get("signal_id") or "")
            or str(portfolio.get("signal_id") or "")
            != str(intent.get("signal_id") or "")
            or str(portfolio.get("simulated_account_id") or "") != intent_account
            or content_hash != recomputed_hash
        ):
            raise StorageError("monitor publication receipt is not bound to admitted intent")
        intent_candidate_fills = [
            fill
            for fill in candidate_fills
            if str(fill.get("intent_id") or "") == intent_id
        ]
        matching_fills = [
            fill
            for fill in intent_candidate_fills
            if _valid_intent_fill(
                intent,
                fill,
                position_id=str(fill.get("position_id") or ""),
            )
        ]
        durable_fill_rows = connection.execute(
            "SELECT * FROM paper_trade_fills WHERE intent_id = ?",
            (intent_id,),
        ).fetchall()
        durable_fills = [_json_row(fill) for fill in durable_fill_rows]
        durable_fill_valid = len(durable_fills) == 1 and _valid_intent_fill(
            intent,
            durable_fills[0],
            position_id=str(durable_fills[0].get("position_id") or ""),
        )
        candidate_fill_valid = (
            len(intent_candidate_fills) == 1 and len(matching_fills) == 1
        )
        if not candidate_fill_valid and not durable_fill_valid:
            raise StorageError("monitor publication receipt lacks exact admitted fill")
        canonical_payload = json.dumps(row, sort_keys=True)
        existing = connection.execute(
            "SELECT content_hash_sha256, payload_json "
            "FROM monitor_publication_receipts WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != content_hash or str(existing[1]) != canonical_payload:
                raise StorageError("monitor publication receipt identity collision")
            reused += 1
            continue
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO monitor_publication_receipts
            (receipt_id, market_date, ticker, signal_id, plan_hash_sha256,
             content_hash_sha256, publication_count, checked_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                str(row.get("market_date") or ""),
                str(row.get("ticker") or ""),
                str(row.get("signal_id") or ""),
                str(row.get("plan_hash_sha256") or ""),
                content_hash,
                int(row.get("publication_count") or 0),
                str(row.get("checked_at") or ""),
                canonical_payload,
            ),
        )
        if cursor.rowcount:
            inserted += 1
        else:
            persisted = connection.execute(
                "SELECT content_hash_sha256, payload_json "
                "FROM monitor_publication_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            if (
                persisted is None
                or str(persisted[0]) != content_hash
                or str(persisted[1]) != canonical_payload
            ):
                raise StorageError("monitor publication receipt identity collision")
            reused += 1
    return {"inserted": inserted, "reused": reused, "count": inserted + reused}


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def _entry_admission_rejection(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    market_date: str,
    ticker: str,
    max_open_positions: int | None,
    max_daily_entries: int | None,
    reserved_entry_count: int,
    reserved_entry_tickers: set[tuple[str, str]],
) -> str:
    if (account_id, ticker) in reserved_entry_tickers:
        return "duplicate_symbol_atomic_admission"
    durable_symbol = connection.execute(
        """
        SELECT 1 FROM paper_positions p
        JOIN trade_intents i ON i.intent_id = p.entry_intent_id
        WHERE i.account_id = ? AND UPPER(p.ticker) = ?
          AND UPPER(p.status) IN ('OPEN', 'PENDING')
        LIMIT 1
        """,
        (account_id, ticker),
    ).fetchone()
    if durable_symbol is not None:
        return "duplicate_symbol_atomic_admission"
    if max_open_positions is not None:
        open_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM paper_positions p
                JOIN trade_intents i ON i.intent_id = p.entry_intent_id
                WHERE i.account_id = ? AND UPPER(p.status) IN ('OPEN', 'PENDING')
                """,
                (account_id,),
            ).fetchone()[0]
        )
        if open_count + reserved_entry_count >= max_open_positions:
            return "max_open_positions_atomic_admission"
    if max_daily_entries is not None:
        daily_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM paper_trade_fills f
                JOIN trade_intents i ON i.intent_id = f.intent_id
                WHERE i.account_id = ? AND f.market_date = ?
                  AND UPPER(f.side) IN ('BUY', 'SELL_SHORT')
                """,
                (account_id, market_date),
            ).fetchone()[0]
        )
        if daily_count + reserved_entry_count >= max_daily_entries:
            return "max_daily_entries_atomic_admission"
    return ""


_TRADE_INTENT_SEMANTIC_KEYS = (
    "signal_id", "market_date", "ticker", "episode_id", "strategy_id",
    "account_id", "mode", "lifecycle_state", "action", "decision_time",
    "decision_price", "trigger_price", "stop_price", "target_price", "quantity",
    "notional", "risk_amount", "reason", "blocked_reason", "source_observation_id",
    "notification_event_key",
)
_TRADE_INTENT_NUMERIC_KEYS = frozenset(
    {
        "decision_price",
        "trigger_price",
        "stop_price",
        "target_price",
        "quantity",
        "notional",
        "risk_amount",
    }
)
_PAPER_POSITION_SEMANTIC_KEYS = (
    "position_id", "signal_id", "market_date", "ticker", "status", "quantity",
    "entry_intent_id", "exit_intent_id", "opened_at", "closed_at", "entry_price",
    "exit_price", "stop_price", "target_price", "notional", "realized_pnl",
    "realized_return_pct", "max_favorable_excursion", "max_adverse_excursion", "updated_at",
)
_PAPER_POSITION_NUMERIC_KEYS = frozenset(
    {
        "quantity",
        "entry_price",
        "exit_price",
        "stop_price",
        "target_price",
        "notional",
        "realized_pnl",
        "realized_return_pct",
        "max_favorable_excursion",
        "max_adverse_excursion",
    }
)
_PAPER_POSITION_CLOSE_CONTINUITY_KEYS = (
    "position_id",
    "signal_id",
    "market_date",
    "ticker",
    "quantity",
    "entry_intent_id",
    "opened_at",
    "entry_price",
    "stop_price",
    "target_price",
    "notional",
)
_PAPER_POSITION_CLOSE_MUTABLE_PAYLOAD_KEYS = frozenset(
    {
        "status",
        "exit_intent_id",
        "closed_at",
        "exit_price",
        "realized_pnl",
        "realized_return_pct",
        "max_favorable_excursion",
        "max_adverse_excursion",
        "updated_at",
        "source_reconciliation_trade_id",
        "canonical_net_pnl",
        "canonical_net_return_pct",
        "canonical_fees",
        "canonical_slippage_cost",
    }
)
_PAPER_FILL_SEMANTIC_KEYS = (
    "fill_id", "position_id", "intent_id", "signal_id", "market_date", "ticker", "side",
    "fill_time", "fill_price", "quantity", "gross_notional", "slippage_bps",
)
_PAPER_FILL_NUMERIC_KEYS = frozenset({"fill_price", "quantity", "gross_notional", "slippage_bps"})


def _trade_intent_semantics_match(
    stored: sqlite3.Row | Mapping[str, Any], incoming: Mapping[str, Any]
) -> bool:
    """Require an exact immutable retry, allowing only creation-time drift."""

    def value(row: sqlite3.Row | Mapping[str, Any], key: str) -> Any:
        if isinstance(row, sqlite3.Row):
            return row[key]
        return row.get(key)

    def canonical(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in _TRADE_INTENT_SEMANTIC_KEYS:
            raw = value(row, key)
            if key in _TRADE_INTENT_NUMERIC_KEYS:
                result[key] = _float_or_none(raw)
            elif key == "action":
                result[key] = str(raw or "").strip().upper()
            else:
                result[key] = str(raw or "")
        raw_payload = value(row, "payload_json")
        payload = _json_value(raw_payload)
        if isinstance(payload, dict) and payload:
            payload = dict(payload)
        elif isinstance(row, sqlite3.Row):
            payload = {
                key: value(row, key)
                for key in _TRADE_INTENT_SEMANTIC_KEYS
                if key != "notification_event_key"
            }
        else:
            payload = {key: raw for key, raw in row.items() if key != "payload_json"}
        payload.pop("created_at", None)
        if "action" in payload:
            payload["action"] = str(payload.get("action") or "").strip().upper()
        result["payload_json"] = payload
        return result

    try:
        return canonical(stored) == canonical(incoming)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _lifecycle_semantics_match(
    stored: sqlite3.Row | Mapping[str, Any], incoming: Mapping[str, Any], *,
    keys: tuple[str, ...], numeric_keys: frozenset[str],
) -> bool:
    """Reject a retry that would rewrite an existing position or fill."""

    def value(row: sqlite3.Row | Mapping[str, Any], key: str) -> Any:
        if isinstance(row, sqlite3.Row):
            return row[key]
        return row.get(key)

    for key in keys:
        left, right = value(stored, key), value(incoming, key)
        if key in numeric_keys:
            if _float_or_none(left) != _float_or_none(right):
                return False
        elif str(left or "") != str(right or ""):
            return False
    raw_incoming_payload = value(incoming, "payload_json")
    incoming_payload = _json_value(raw_incoming_payload)
    has_incoming_payload = isinstance(raw_incoming_payload, dict) or bool(
        isinstance(raw_incoming_payload, str) and raw_incoming_payload.strip()
    )
    if has_incoming_payload and isinstance(incoming_payload, dict):
        stored_payload = _json_value(value(stored, "payload_json"))
        return isinstance(stored_payload, dict) and stored_payload == incoming_payload
    return True


def _valid_position_close_transition(
    stored: sqlite3.Row,
    incoming: Mapping[str, Any],
    *,
    exit_intent: Mapping[str, Any] | None,
    bound_exit_fill: Mapping[str, Any] | None,
) -> bool:
    """Allow only one governed OPEN/PENDING to CLOSED lifecycle transition."""

    stored_status = str(stored["status"] or "").strip().upper()
    incoming_status = str(incoming.get("status") or "").strip().upper()
    exit_intent_id = str(incoming.get("exit_intent_id") or "").strip()
    if (
        stored_status not in {"OPEN", "PENDING"}
        or incoming_status != "CLOSED"
        or str(stored["exit_intent_id"] or "").strip()
        or not exit_intent_id
        or exit_intent is None
        or exit_intent_id != str(exit_intent.get("intent_id") or "").strip()
        or bound_exit_fill is None
        or not str(bound_exit_fill.get("fill_id") or "").strip()
    ):
        return False

    for key in _PAPER_POSITION_CLOSE_CONTINUITY_KEYS:
        left, right = stored[key], incoming.get(key)
        if key in _PAPER_POSITION_NUMERIC_KEYS:
            if _float_or_none(left) != _float_or_none(right):
                return False
        elif str(left or "") != str(right or ""):
            return False

    if not _valid_intent_fill(
        exit_intent,
        bound_exit_fill,
        position_id=str(incoming.get("position_id") or ""),
    ):
        return False
    if not _exact_position_identity(exit_intent, incoming):
        return False
    for position_key, fill_key in (
        ("position_id", "position_id"),
        ("signal_id", "signal_id"),
        ("market_date", "market_date"),
        ("ticker", "ticker"),
        ("exit_intent_id", "intent_id"),
    ):
        if str(incoming.get(position_key) or "") != str(
            bound_exit_fill.get(fill_key) or ""
        ):
            return False
    if _float_or_none(incoming.get("quantity")) != _float_or_none(
        bound_exit_fill.get("quantity")
    ):
        return False
    if str(incoming.get("closed_at") or "") != str(
        bound_exit_fill.get("fill_time") or ""
    ):
        return False

    entry_price = _float_or_none(stored["entry_price"])
    exit_price = _float_or_none(bound_exit_fill.get("fill_price"))
    quantity = _float_or_none(bound_exit_fill.get("quantity"))
    side = str(bound_exit_fill.get("side") or "").strip().upper()
    stored_payload = _json_value(stored["payload_json"])
    direction = (
        str(stored_payload.get("direction") or "").strip().lower()
        if isinstance(stored_payload, dict)
        else ""
    )
    if (
        entry_price is None
        or entry_price <= 0
        or exit_price is None
        or exit_price <= 0
        or quantity is None
        or quantity <= 0
        or direction not in {"long", "short"}
        or side != ("BUY_TO_COVER" if direction == "short" else "SELL")
        or _float_or_none(incoming.get("exit_price")) != exit_price
    ):
        return False
    expected_pnl = round(
        ((entry_price - exit_price) if side == "BUY_TO_COVER" else (exit_price - entry_price))
        * quantity,
        4,
    )
    expected_return = round(
        (
            (entry_price - exit_price) / entry_price
            if side == "BUY_TO_COVER"
            else (exit_price - entry_price) / entry_price
        )
        * 100,
        4,
    )
    if (
        _float_or_none(incoming.get("realized_pnl")) != expected_pnl
        or _float_or_none(incoming.get("realized_return_pct")) != expected_return
    ):
        return False

    incoming_payload = _json_value(incoming.get("payload_json") or incoming)
    if not isinstance(stored_payload, dict) or not isinstance(incoming_payload, dict):
        return False
    stored_identity = {
        key: value
        for key, value in stored_payload.items()
        if key not in _PAPER_POSITION_CLOSE_MUTABLE_PAYLOAD_KEYS
    }
    incoming_identity = {
        key: value
        for key, value in incoming_payload.items()
        if key not in _PAPER_POSITION_CLOSE_MUTABLE_PAYLOAD_KEYS and key != "payload_json"
    }
    return stored_identity == incoming_identity


def _valid_position_entry_fill(
    position: Mapping[str, Any],
    fill: Mapping[str, Any] | None,
    *,
    entry_intent: Mapping[str, Any] | None,
) -> bool:
    """Bind a new OPEN/PENDING position to one persistable entry fill."""

    if fill is None or not str(fill.get("fill_id") or "").strip():
        return False
    entry_intent_id = str(position.get("entry_intent_id") or "").strip()
    if (
        not entry_intent_id
        or entry_intent is None
        or entry_intent_id != str(entry_intent.get("intent_id") or "").strip()
        or not _valid_intent_fill(
            entry_intent,
            fill,
            position_id=str(position.get("position_id") or ""),
        )
    ):
        return False
    if not _exact_position_identity(entry_intent, position):
        return False
    for position_key, fill_key in (
        ("position_id", "position_id"),
        ("signal_id", "signal_id"),
        ("market_date", "market_date"),
        ("ticker", "ticker"),
        ("entry_intent_id", "intent_id"),
    ):
        if str(position.get(position_key) or "") != str(fill.get(fill_key) or ""):
            return False
    if _float_or_none(position.get("quantity")) != _float_or_none(fill.get("quantity")):
        return False
    side = str(fill.get("side") or "").strip().upper()
    entry_price = _float_or_none(position.get("entry_price"))
    fill_price = _float_or_none(fill.get("fill_price"))
    direction = str(position.get("direction") or "").strip().lower()
    return (
        side in {"BUY", "SELL_SHORT"}
        and direction == ("short" if side == "SELL_SHORT" else "long")
        and entry_price is not None
        and entry_price > 0
        and entry_price == fill_price
    )


def _valid_intent_fill(
    intent: Mapping[str, Any],
    fill: Mapping[str, Any],
    *,
    position_id: str,
) -> bool:
    """Bind a fill to the exact admitted intent and declared slippage model."""

    action = _trade_intent_action(intent)
    expected_side = {
        "ENTER_LONG": "BUY",
        "ENTER_SHORT": "SELL_SHORT",
        "EXIT_LONG": "SELL",
        "EXIT_SHORT": "BUY_TO_COVER",
    }.get(action)
    if expected_side is None or str(fill.get("side") or "").strip().upper() != expected_side:
        return False
    if not _exact_research_identity(intent, fill):
        return False
    for intent_key, fill_key in (
        ("intent_id", "intent_id"),
        ("signal_id", "signal_id"),
        ("market_date", "market_date"),
        ("ticker", "ticker"),
        ("decision_time", "fill_time"),
    ):
        if str(intent.get(intent_key) or "") != str(fill.get(fill_key) or ""):
            return False
    if (
        not str(fill.get("fill_id") or "").strip()
        or str(fill.get("position_id") or "") != position_id
    ):
        return False
    decision_price = _float_or_none(intent.get("decision_price"))
    fill_price = _float_or_none(fill.get("fill_price"))
    slippage_bps = _float_or_none(fill.get("slippage_bps"))
    if decision_price is None or decision_price <= 0 or fill_price is None or slippage_bps is None:
        return False
    if fill.get("canonical_eod_repair") is True:
        reconciliation_id = str(intent.get("source_reconciliation_trade_id") or "").strip()
        return (
            bool(reconciliation_id)
            and reconciliation_id
            == str(fill.get("source_reconciliation_trade_id") or "").strip()
            and fill_price == decision_price
        )
    expected_price: float | None = None
    if action in {"ENTER_LONG", "ENTER_SHORT"}:
        trace = intent.get("decision_trace")
        computed = trace.get("computed") if isinstance(trace, dict) else None
        if isinstance(computed, dict):
            expected_price = _float_or_none(computed.get("expected_entry_price"))
    if expected_price is None:
        unfavorable = action in {"ENTER_LONG", "EXIT_SHORT"}
        expected_price = decision_price * (
            1 + slippage_bps / 10000.0 if unfavorable else 1 - slippage_bps / 10000.0
        )
    return fill_price == round(expected_price, 6)


def _exact_research_identity(
    intent: Mapping[str, Any], lifecycle_row: Mapping[str, Any]
) -> bool:
    for key in (
        "account_id", "strategy_id", "strategy_version", "episode_id",
        "selection_id", "cohort", "decision_fingerprint",
    ):
        expected = str(intent.get(key) or "").strip()
        if not expected or str(lifecycle_row.get(key) or "").strip() != expected:
            return False
    return _exact_safety_identity(intent, lifecycle_row)


def _exact_position_identity(
    intent: Mapping[str, Any], position: Mapping[str, Any]
) -> bool:
    for key in (
        "account_id", "strategy_id", "strategy_version", "episode_id",
        "selection_id", "cohort",
    ):
        expected = str(intent.get(key) or "").strip()
        if not expected or str(position.get(key) or "").strip() != expected:
            return False
    return _exact_safety_identity(intent, position)


def _exact_safety_identity(
    intent: Mapping[str, Any], lifecycle_row: Mapping[str, Any]
) -> bool:
    return (
        intent.get("research_only") is True
        and lifecycle_row.get("research_only") is True
        and str(intent.get("broker_execution") or "") == "disabled"
        and str(lifecycle_row.get("broker_execution") or "") == "disabled"
        and intent.get("broker_execution_enabled") is False
        and lifecycle_row.get("broker_execution_enabled") is False
        and "official_paper_eligible" in intent
        and lifecycle_row.get("official_paper_eligible")
        == intent.get("official_paper_eligible")
    )


def _trade_intent_row(row: sqlite3.Row) -> dict[str, Any]:
    """Merge intent payloads without allowing them to rewrite claim columns."""

    merged = _json_row(row)
    for key in (
        "intent_id",
        "signal_id",
        "market_date",
        "ticker",
        "episode_id",
        "strategy_id",
        "account_id",
        "action",
    ):
        merged[key] = row[key]
    return merged


def _trade_intent_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = row.get("payload_json")
    return payload if isinstance(payload, dict) else {}


def _trade_intent_episode_id(row: Mapping[str, Any]) -> str:
    payload = _trade_intent_payload(row)
    return str(row.get("episode_id") or payload.get("episode_id") or "").strip()


def _trade_intent_strategy_id(row: Mapping[str, Any]) -> str:
    payload = _trade_intent_payload(row)
    return str(row.get("strategy_id") or payload.get("strategy_id") or "").strip()


def _trade_intent_account_id(row: Mapping[str, Any]) -> str:
    payload = _trade_intent_payload(row)
    return str(
        row.get("account_id")
        or payload.get("account_id")
        or payload.get("simulated_account_id")
        or ""
    ).strip()


def _backfill_trade_intent_identity(connection: sqlite3.Connection) -> None:
    """Backfill identity columns when upgrading pre-episode databases."""

    rows = connection.execute(
        "SELECT rowid, intent_id, market_date, action, decision_time, created_at, "
        "episode_id, strategy_id, account_id, payload_json FROM trade_intents "
        "ORDER BY decision_time ASC, created_at ASC, rowid ASC"
    ).fetchall()
    claimed_episodes: set[tuple[str, str, str, str]] = set()
    for row in rows:
        payload = _json_value(row[9])
        if not isinstance(payload, dict):
            payload = {}
        market_date = str(row[2] or "")[:10]
        action = str(row[3] or "").strip().upper()
        episode_id = str(row[6] or payload.get("episode_id") or "").strip()
        strategy_id = str(row[7] or payload.get("strategy_id") or "").strip()
        account_id = str(
            row[8]
            or payload.get("account_id")
            or payload.get("simulated_account_id")
            or ""
        ).strip()
        claim_key = (market_date, account_id, strategy_id, episode_id)
        if (
            episode_id
            and action in {"ENTER_LONG", "ENTER_SHORT"}
            and claim_key in claimed_episodes
        ):
            episode_id = ""
        elif episode_id and action in {"ENTER_LONG", "ENTER_SHORT"}:
            claimed_episodes.add(claim_key)
        if (action, episode_id, strategy_id, account_id) != (
            str(row[3] or ""),
            str(row[6] or ""),
            str(row[7] or ""),
            str(row[8] or ""),
        ):
            connection.execute(
                "UPDATE trade_intents SET action = ?, episode_id = ?, strategy_id = ?, "
                "account_id = ? WHERE intent_id = ?",
                (action, episode_id, strategy_id, account_id, row[1]),
            )


def _raw_json_row(row: sqlite3.Row) -> dict[str, Any]:
    """Keep database columns distinct from an untrusted JSON projection."""

    payload = _json_value(row["payload_json"])
    return {
        "columns": {key: row[key] for key in row.keys() if key != "payload_json"},
        "payload_json": payload,
    }


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
    merged = {key: row[key] for key in row.keys() if key not in {"payload_json", "bucket_json"}}
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
    if value is None or value == "":
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
