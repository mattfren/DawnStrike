"""Additive SQLite migrations for Dawnstrike storage."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

CURRENT_SCHEMA_VERSION = 16

Migration = Callable[[sqlite3.Connection], None]


def get_schema_version(connection: sqlite3.Connection) -> int:
    _ensure_schema_table(connection)
    row = connection.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return int(row[0]) if row else 0


def set_schema_version(connection: sqlite3.Connection, version: int) -> None:
    _ensure_schema_table(connection)
    connection.execute("DELETE FROM schema_version")
    connection.execute(
        """
        INSERT INTO schema_version (version, applied_at)
        VALUES (?, ?)
        """,
        (version, datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
    )


def run_migrations(connection: sqlite3.Connection) -> int:
    _ensure_schema_table(connection)
    version = get_schema_version(connection)
    for target_version, migration in MIGRATIONS:
        if version >= target_version:
            continue
        migration(connection)
        set_schema_version(connection, target_version)
        version = target_version
    return version


def _ensure_schema_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def _migration_001_benchmark_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS benchmark_observations (
            benchmark_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            market_date TEXT NOT NULL,
            open_price REAL,
            close_price REAL,
            one_min_price REAL,
            five_min_price REAL,
            fifteen_min_price REAL,
            lunch_price REAL,
            source TEXT NOT NULL,
            source_quality TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS benchmark_performance (
            market_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            return_1m REAL,
            return_5m REAL,
            return_15m REAL,
            return_lunch REAL,
            return_close REAL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (market_date, symbol)
        );
        """
    )


def _migration_002_signal_selection_delivery_identity(
    connection: sqlite3.Connection,
) -> None:
    """Add immutable strategy/selection identity and per-signal delivery truth."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS strategy_versions (
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            definition_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (strategy_id, strategy_version)
        );

        CREATE TABLE IF NOT EXISTS signal_selections (
            selection_id TEXT PRIMARY KEY,
            scan_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            rank INTEGER,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            cohort TEXT NOT NULL,
            decision TEXT NOT NULL,
            selected_at TEXT NOT NULL,
            event_key TEXT NOT NULL,
            body_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (strategy_id, strategy_version, cohort, signal_id)
        );
        CREATE INDEX IF NOT EXISTS idx_signal_selections_scan
        ON signal_selections(scan_id, cohort, rank);
        CREATE INDEX IF NOT EXISTS idx_signal_selections_event
        ON signal_selections(event_key, rank);

        CREATE TABLE IF NOT EXISTS notification_delivery_memberships (
            membership_id TEXT PRIMARY KEY,
            selection_id TEXT,
            scan_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            cohort TEXT NOT NULL,
            decision TEXT NOT NULL,
            selected_at TEXT NOT NULL,
            event_key TEXT NOT NULL,
            channel TEXT NOT NULL,
            delivery_status TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            delivered_at TEXT,
            body_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (event_key, channel, signal_id)
        );
        CREATE INDEX IF NOT EXISTS idx_notification_delivery_scan
        ON notification_delivery_memberships(scan_id, channel, delivery_status);
        CREATE INDEX IF NOT EXISTS idx_notification_delivery_signal
        ON notification_delivery_memberships(signal_id, attempted_at);
        CREATE INDEX IF NOT EXISTS idx_notification_delivery_event
        ON notification_delivery_memberships(event_key, channel);
        """
    )


def _migration_003_strategy_paper_reconciliation(
    connection: sqlite3.Connection,
) -> None:
    """Add sourced paper lifecycles, split learning labels, and daily scorecards."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS strategy_evaluations (
            evaluation_id TEXT PRIMARY KEY,
            selection_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            market_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            cohort TEXT NOT NULL,
            terminal_state TEXT NOT NULL,
            reconciliation_status TEXT NOT NULL,
            activated INTEGER,
            filled INTEGER NOT NULL,
            closed INTEGER NOT NULL,
            net_return_pct REAL,
            source_bar_hash_sha256 TEXT,
            execution_policy_version TEXT NOT NULL,
            reconciled_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_evaluations_day_strategy
        ON strategy_evaluations(market_date, strategy_id, cohort);
        CREATE INDEX IF NOT EXISTS idx_strategy_evaluations_signal
        ON strategy_evaluations(signal_id, strategy_id);

        CREATE TABLE IF NOT EXISTS strategy_paper_trades (
            trade_id TEXT PRIMARY KEY,
            selection_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            market_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            cohort TEXT NOT NULL,
            direction TEXT NOT NULL,
            decision_time TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            entry_fill_price REAL NOT NULL,
            exit_time TEXT NOT NULL,
            exit_fill_price REAL NOT NULL,
            exit_reason TEXT NOT NULL,
            quantity REAL NOT NULL,
            notional REAL NOT NULL,
            net_pnl REAL NOT NULL,
            net_return_pct REAL NOT NULL,
            r_multiple REAL,
            fees REAL NOT NULL,
            slippage_cost REAL NOT NULL,
            source_bar_hash_sha256 TEXT NOT NULL,
            execution_policy_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_paper_trades_day_strategy
        ON strategy_paper_trades(market_date, strategy_id, cohort);
        CREATE INDEX IF NOT EXISTS idx_strategy_paper_trades_signal
        ON strategy_paper_trades(signal_id, strategy_id);

        CREATE TABLE IF NOT EXISTS strategy_learning_labels (
            label_id TEXT PRIMARY KEY,
            evaluation_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            market_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            cohort TEXT NOT NULL,
            label_family TEXT NOT NULL,
            label_value REAL,
            r_multiple REAL,
            eligible INTEGER NOT NULL,
            exclusion_reason TEXT,
            source_bar_hash_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_learning_family
        ON strategy_learning_labels(strategy_id, label_family, market_date);

        CREATE TABLE IF NOT EXISTS daily_strategy_scorecards (
            scorecard_id TEXT PRIMARY KEY,
            market_date TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            cohort TEXT NOT NULL,
            execution_policy_version TEXT NOT NULL,
            selected_count INTEGER NOT NULL,
            delivered_count INTEGER NOT NULL,
            resolved_count INTEGER NOT NULL,
            triggered_count INTEGER NOT NULL,
            not_triggered_count INTEGER NOT NULL,
            filled_count INTEGER NOT NULL,
            closed_count INTEGER NOT NULL,
            unresolved_count INTEGER NOT NULL,
            wins INTEGER NOT NULL,
            losses INTEGER NOT NULL,
            flats INTEGER NOT NULL,
            activation_rate_pct REAL,
            win_rate_pct REAL,
            average_net_return_pct REAL,
            net_pnl REAL NOT NULL,
            return_on_allocated_capital_pct REAL,
            average_r REAL,
            expectancy_r REAL,
            profit_factor REAL,
            fees REAL NOT NULL,
            slippage_cost REAL NOT NULL,
            reconciliation_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (
                market_date, strategy_id, strategy_version, cohort,
                execution_policy_version
            )
        );
        CREATE INDEX IF NOT EXISTS idx_daily_strategy_scorecards_strategy
        ON daily_strategy_scorecards(strategy_id, cohort, market_date);
        """
    )


def _migration_004_scorecard_session_truth(connection: sqlite3.Connection) -> None:
    """Distinguish an explicit no-trade decision from a missing morning run."""

    connection.executescript(
        """
        ALTER TABLE daily_strategy_scorecards
        ADD COLUMN session_status TEXT NOT NULL DEFAULT 'unknown';
        ALTER TABLE daily_strategy_scorecards
        ADD COLUMN no_trade_count INTEGER NOT NULL DEFAULT 0;
        """
    )


def _migration_005_canonical_performance(connection: sqlite3.Connection) -> None:
    """Add the single auditable performance read model and publication manifest."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS portfolio_performance_rows (
            record_id TEXT PRIMARY KEY,
            market_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            cohort TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            signal_id TEXT,
            rank INTEGER,
            record_status TEXT NOT NULL,
            entry_price REAL,
            exit_price REAL,
            quantity REAL,
            notional_cents INTEGER,
            gross_pnl_cents INTEGER,
            fees_cents INTEGER,
            slippage_cents INTEGER,
            net_pnl_cents INTEGER,
            return_pct REAL,
            benchmark_return_pct REAL,
            excess_return_pct REAL,
            source_refs_json TEXT NOT NULL,
            source_hash_sha256 TEXT NOT NULL,
            input_hash_sha256 TEXT NOT NULL,
            observed_at TEXT,
            reconciled_at TEXT NOT NULL,
            quarantine_reason TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_portfolio_performance_rows_day_cohort
        ON portfolio_performance_rows(market_date, cohort, strategy_id);

        CREATE TABLE IF NOT EXISTS portfolio_daily_performance (
            performance_id TEXT PRIMARY KEY,
            market_date TEXT NOT NULL,
            cohort TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            status TEXT NOT NULL,
            gross_pnl_cents INTEGER,
            fees_cents INTEGER,
            slippage_cents INTEGER,
            net_pnl_cents INTEGER,
            allocated_capital_cents INTEGER,
            return_pct REAL,
            benchmark_return_pct REAL,
            excess_return_pct REAL,
            realized_trade_count INTEGER NOT NULL,
            unrealized_trade_count INTEGER NOT NULL,
            missing_outcome_count INTEGER NOT NULL,
            quarantined_count INTEGER NOT NULL,
            source_hash_sha256 TEXT NOT NULL,
            input_hash_sha256 TEXT NOT NULL,
            calculated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (market_date, cohort, strategy_id, strategy_version)
        );
        CREATE INDEX IF NOT EXISTS idx_portfolio_daily_performance_day
        ON portfolio_daily_performance(market_date, cohort);

        CREATE TABLE IF NOT EXISTS performance_reconciliation_issues (
            issue_id TEXT PRIMARY KEY,
            record_id TEXT,
            market_date TEXT NOT NULL,
            severity TEXT NOT NULL,
            issue_code TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_performance_issues_day
        ON performance_reconciliation_issues(market_date, severity);

        CREATE TABLE IF NOT EXISTS public_snapshot_manifests (
            manifest_id TEXT PRIMARY KEY,
            market_date TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            input_hash_sha256 TEXT NOT NULL,
            payload_sha256 TEXT,
            artifact_path TEXT,
            row_count INTEGER NOT NULL,
            byte_count INTEGER,
            failure_reason TEXT,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_finalize_runs (
            run_id TEXT PRIMARY KEY,
            market_date TEXT NOT NULL,
            status TEXT NOT NULL,
            stage TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            input_hash_sha256 TEXT,
            output_hash_sha256 TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            failure_reason TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_daily_finalize_runs_day
        ON daily_finalize_runs(market_date, started_at);
        """
    )


def _migration_006_canonical_performance_repair(connection: sqlite3.Connection) -> None:
    """Repair databases that already used version 5 for an older local migration."""

    _migration_005_canonical_performance(connection)


def _migration_007_canonical_gross_return_fields(connection: sqlite3.Connection) -> None:
    """Keep observed gross return separate from after-cost return."""

    _add_column_if_missing(connection, "portfolio_performance_rows", "gross_return_pct REAL")
    _add_column_if_missing(connection, "portfolio_daily_performance", "gross_return_pct REAL")


def _migration_008_equity_and_contract_metadata(connection: sqlite3.Connection) -> None:
    """Add optional portfolio-equity evidence and explicit daily metadata."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS portfolio_equity_observations (
            observation_id TEXT PRIMARY KEY,
            market_date TEXT NOT NULL,
            cohort TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            opening_equity_cents INTEGER,
            ending_equity_cents INTEGER,
            source_refs_json TEXT NOT NULL,
            source_hash_sha256 TEXT NOT NULL,
            observed_at TEXT,
            payload_json TEXT NOT NULL,
            UNIQUE (market_date, cohort, strategy_id, strategy_version)
        );
        CREATE INDEX IF NOT EXISTS idx_portfolio_equity_observations_day
        ON portfolio_equity_observations(market_date, cohort);
        """
    )
    for column in (
        "opening_equity_cents INTEGER",
        "ending_equity_cents INTEGER",
        "unrealized_pnl_cents INTEGER",
        "cumulative_return_pct REAL",
        "drawdown_pct REAL",
        "exposure_cents INTEGER",
        "execution_policy_version TEXT",
        "calculation_version TEXT",
        "evidence_state TEXT",
        "coverage_json TEXT",
        "source_refs_json TEXT",
    ):
        _add_column_if_missing(connection, "portfolio_daily_performance", column)


def _migration_009_snapshot_versions(connection: sqlite3.Connection) -> None:
    """Keep every distinct public snapshot version append-only by manifest id."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS public_snapshot_versions (
            manifest_id TEXT PRIMARY KEY,
            market_date TEXT NOT NULL,
            status TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            input_hash_sha256 TEXT NOT NULL,
            payload_sha256 TEXT,
            artifact_path TEXT,
            row_count INTEGER NOT NULL,
            byte_count INTEGER,
            failure_reason TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_public_snapshot_versions_day
        ON public_snapshot_versions(market_date, generated_at);
        """
    )


def _migration_010_performance_row_metadata(connection: sqlite3.Connection) -> None:
    """Keep execution policy and portfolio-observation metadata on each row."""

    for column in (
        "execution_policy_version TEXT NOT NULL DEFAULT 'unregistered-policy'",
        "trade_count INTEGER NOT NULL DEFAULT 1",
        "open_position_count INTEGER NOT NULL DEFAULT 0",
        "unrealized_pnl_cents INTEGER",
        "record_type TEXT NOT NULL DEFAULT 'trade'",
    ):
        _add_column_if_missing(connection, "portfolio_performance_rows", column)


def _migration_011_v5_paper_account_ledger(connection: sqlite3.Connection) -> None:
    """Add the prospective AlphaOps v5 simulated-account truth ledger."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_accounts (
            account_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            activation_timestamp TEXT NOT NULL,
            opening_equity_cents INTEGER NOT NULL,
            currency TEXT NOT NULL,
            account_type TEXT NOT NULL,
            execution_policy_version TEXT NOT NULL,
            cost_model_version TEXT NOT NULL,
            research_only INTEGER NOT NULL,
            broker_execution_enabled INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS paper_account_daily_ledger (
            ledger_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            market_date TEXT NOT NULL,
            cohort TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            execution_policy_version TEXT NOT NULL,
            cost_model_version TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence_state TEXT NOT NULL,
            beginning_equity_cents INTEGER,
            external_flow_cents INTEGER,
            realized_gross_pnl_cents INTEGER,
            fees_cents INTEGER,
            slippage_cents INTEGER,
            realized_net_pnl_cents INTEGER,
            unrealized_pnl_change_cents INTEGER,
            cash_cents INTEGER,
            position_market_value_cents INTEGER,
            ending_equity_cents INTEGER,
            market_benchmark_return_pct REAL,
            cash_benchmark_return_pct REAL,
            gross_return_pct REAL,
            net_return_pct REAL,
            excess_return_pct REAL,
            accounting_delta_cents INTEGER,
            trade_count INTEGER NOT NULL,
            open_position_count INTEGER NOT NULL,
            source_refs_json TEXT NOT NULL,
            source_hash_sha256 TEXT NOT NULL,
            input_hash_sha256 TEXT NOT NULL,
            calculated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (account_id, market_date),
            FOREIGN KEY(account_id) REFERENCES paper_accounts(account_id)
        );
        CREATE INDEX IF NOT EXISTS idx_paper_account_daily_ledger_day
        ON paper_account_daily_ledger(market_date, account_id);

        CREATE TABLE IF NOT EXISTS public_calendar_manifests (
            manifest_id TEXT PRIMARY KEY,
            market_date TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            canonical_input_hash_sha256 TEXT NOT NULL,
            payload_sha256 TEXT,
            artifact_path TEXT,
            day_count INTEGER NOT NULL,
            byte_count INTEGER,
            failure_reason TEXT,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS public_calendar_versions (
            manifest_id TEXT PRIMARY KEY,
            market_date TEXT NOT NULL,
            status TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            canonical_input_hash_sha256 TEXT NOT NULL,
            payload_sha256 TEXT,
            artifact_path TEXT,
            day_count INTEGER NOT NULL,
            byte_count INTEGER,
            failure_reason TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_public_calendar_versions_day
        ON public_calendar_versions(market_date, generated_at);
        """
    )
    for column in (
        "account_id TEXT",
        "external_flow_cents INTEGER",
        "cash_cents INTEGER",
        "position_market_value_cents INTEGER",
        "accounting_delta_cents INTEGER",
        "cash_benchmark_return_pct REAL",
        "ledger_status TEXT",
    ):
        _add_column_if_missing(connection, "portfolio_daily_performance", column)


def _migration_012_outcome_capture_truth(connection: sqlite3.Connection) -> None:
    """Persist every required outcome-capture attempt, including terminal misses."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS outcome_capture_attempts (
            attempt_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            market_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            status TEXT NOT NULL,
            terminal INTEGER NOT NULL,
            learning_eligible INTEGER NOT NULL,
            provider_chain_json TEXT NOT NULL,
            source_refs_json TEXT NOT NULL,
            source_bar_hash_sha256 TEXT,
            attempted_at TEXT NOT NULL,
            resolved_at TEXT,
            error_code TEXT,
            error_detail TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_outcome_capture_attempts_day
        ON outcome_capture_attempts(market_date, status, ticker);
        CREATE INDEX IF NOT EXISTS idx_outcome_capture_attempts_signal
        ON outcome_capture_attempts(signal_id, attempted_at);
        """
    )


def _migration_013_shared_daily_run_ledger(connection: sqlite3.Connection) -> None:
    """Add one release-bound DAG ledger shared by every scheduled daily stage."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_runs (
            run_id TEXT PRIMARY KEY,
            market_date TEXT NOT NULL,
            release_sha TEXT NOT NULL,
            runtime_root TEXT NOT NULL,
            state_root TEXT NOT NULL,
            scheduler_version TEXT NOT NULL,
            strategy_versions_json TEXT NOT NULL,
            status TEXT NOT NULL,
            current_stage TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            last_attempted_at TEXT NOT NULL,
            failed_stage TEXT,
            failure_reason TEXT,
            source_data_watermark TEXT,
            publication_timestamp TEXT,
            deployed_source_sha TEXT,
            deployed_build_sha TEXT,
            payload_json TEXT NOT NULL,
            UNIQUE (market_date, release_sha)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_runs_day_status
        ON daily_runs(market_date, status, last_attempted_at);

        CREATE TABLE IF NOT EXISTS daily_run_stages (
            stage_event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage_name TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            status TEXT NOT NULL,
            required INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            exit_code INTEGER,
            input_hash_sha256 TEXT,
            output_hash_sha256 TEXT,
            source_data_watermark TEXT,
            error_code TEXT,
            error_detail TEXT,
            payload_json TEXT NOT NULL,
            UNIQUE (run_id, stage_name, attempt_no),
            FOREIGN KEY(run_id) REFERENCES daily_runs(run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_run_stages_run
        ON daily_run_stages(run_id, stage_name, attempt_no);
        CREATE INDEX IF NOT EXISTS idx_daily_run_stages_status
        ON daily_run_stages(status, completed_at);
        """
    )


def _migration_014_alphaops_v6_shadow_ledger(
    connection: sqlite3.Connection,
) -> None:
    """Add immutable point-in-time decision, outcome, model, and experiment ledgers."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_v6_decisions (
            decision_id TEXT PRIMARY KEY,
            scan_id TEXT NOT NULL,
            source_signal_id TEXT NOT NULL,
            shadow_signal_id TEXT NOT NULL UNIQUE,
            market_date TEXT NOT NULL,
            decision_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            model_version TEXT NOT NULL,
            action TEXT NOT NULL,
            setup_key TEXT,
            regime_key TEXT,
            safety_vetoes_json TEXT NOT NULL,
            input_hash_sha256 TEXT NOT NULL,
            source_lineage_hash_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (scan_id, source_signal_id, strategy_version)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_decisions_day
        ON alpha_v6_decisions(market_date, decision_at, action);
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_decisions_signal
        ON alpha_v6_decisions(source_signal_id, decision_at);

        CREATE TABLE IF NOT EXISTS alpha_v6_outcomes (
            outcome_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL UNIQUE,
            shadow_signal_id TEXT NOT NULL UNIQUE,
            market_date TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            activation_status TEXT NOT NULL,
            outcome_status TEXT NOT NULL,
            net_return_pct REAL,
            benchmark_return_pct REAL,
            net_excess_return_pct REAL,
            source_bar_hash_sha256 TEXT,
            learning_eligible INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(decision_id) REFERENCES alpha_v6_decisions(decision_id)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_outcomes_day
        ON alpha_v6_outcomes(market_date, outcome_status, learning_eligible);

        CREATE TABLE IF NOT EXISTS alpha_v6_model_runs (
            model_run_id TEXT PRIMARY KEY,
            model_version TEXT NOT NULL,
            trained_at TEXT NOT NULL,
            training_cutoff TEXT,
            status TEXT NOT NULL,
            training_input_hash_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_model_runs_time
        ON alpha_v6_model_runs(trained_at, model_version);

        CREATE TABLE IF NOT EXISTS alpha_v6_evaluations (
            evaluation_id TEXT PRIMARY KEY,
            model_run_id TEXT NOT NULL,
            evaluated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            evaluation_input_hash_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(model_run_id) REFERENCES alpha_v6_model_runs(model_run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_evaluations_model
        ON alpha_v6_evaluations(model_run_id, evaluated_at);

        CREATE TABLE IF NOT EXISTS alpha_v6_experiments (
            experiment_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            hypothesis TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        """
    )


def _migration_015_alphaops_v6_research_contracts(
    connection: sqlite3.Connection,
) -> None:
    """Add versioned V6 labels, datasets, predictions, drift, and review receipts."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_v6_labels (
            label_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL,
            market_date TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            label_family TEXT NOT NULL,
            label_value REAL,
            learning_eligible INTEGER NOT NULL,
            exclusion_reason TEXT,
            source_bar_hash_sha256 TEXT,
            payload_json TEXT NOT NULL,
            UNIQUE (decision_id, label_family, label_id),
            FOREIGN KEY(decision_id) REFERENCES alpha_v6_decisions(decision_id)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_labels_family_day
        ON alpha_v6_labels(label_family, market_date, learning_eligible);

        CREATE TABLE IF NOT EXISTS alpha_v6_datasets (
            dataset_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            training_cutoff TEXT,
            row_count INTEGER NOT NULL,
            dataset_hash_sha256 TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_datasets_cutoff
        ON alpha_v6_datasets(training_cutoff, created_at);

        CREATE TABLE IF NOT EXISTS alpha_v6_model_artifacts (
            artifact_id TEXT PRIMARY KEY,
            model_run_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            artifact_hash_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (model_run_id, artifact_hash_sha256),
            FOREIGN KEY(model_run_id) REFERENCES alpha_v6_model_runs(model_run_id)
        );

        CREATE TABLE IF NOT EXISTS alpha_v6_shadow_predictions (
            prediction_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL,
            model_run_id TEXT,
            market_date TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (decision_id, model_run_id),
            FOREIGN KEY(decision_id) REFERENCES alpha_v6_decisions(decision_id),
            FOREIGN KEY(model_run_id) REFERENCES alpha_v6_model_runs(model_run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_predictions_day
        ON alpha_v6_shadow_predictions(market_date, generated_at);

        CREATE TABLE IF NOT EXISTS alpha_v6_drift_reports (
            drift_report_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS alpha_v6_promotion_reviews (
            review_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            approved INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );
        """
    )


def _migration_016_alphaops_v6_universe_registry(
    connection: sqlite3.Connection,
) -> None:
    """Add a source-lineage versioned universe with listing and action history."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_v6_universe_versions (
            universe_id TEXT PRIMARY KEY,
            as_of_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            membership_count INTEGER NOT NULL,
            source_lineage_hash_sha256 TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_universe_versions_date
        ON alpha_v6_universe_versions(as_of_date, created_at);

        CREATE TABLE IF NOT EXISTS alpha_v6_universe_memberships (
            universe_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            listing_status TEXT NOT NULL,
            valid_from TEXT,
            valid_to TEXT,
            previous_ticker TEXT,
            corporate_action_type TEXT,
            source_lineage_hash_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (universe_id, ticker),
            FOREIGN KEY(universe_id) REFERENCES alpha_v6_universe_versions(universe_id)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_universe_membership_ticker
        ON alpha_v6_universe_memberships(ticker, universe_id);
        """
    )


def _add_column_if_missing(
    connection: sqlite3.Connection, table: str, column_definition: str
) -> None:
    column_name = column_definition.split()[0]
    existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column_name not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column_definition}")


MIGRATIONS: tuple[tuple[int, Migration], ...] = (
    (1, _migration_001_benchmark_tables),
    (2, _migration_002_signal_selection_delivery_identity),
    (3, _migration_003_strategy_paper_reconciliation),
    (4, _migration_004_scorecard_session_truth),
    (5, _migration_005_canonical_performance),
    (6, _migration_006_canonical_performance_repair),
    (7, _migration_007_canonical_gross_return_fields),
    (8, _migration_008_equity_and_contract_metadata),
    (9, _migration_009_snapshot_versions),
    (10, _migration_010_performance_row_metadata),
    (11, _migration_011_v5_paper_account_ledger),
    (12, _migration_012_outcome_capture_truth),
    (13, _migration_013_shared_daily_run_ledger),
    (14, _migration_014_alphaops_v6_shadow_ledger),
    (15, _migration_015_alphaops_v6_research_contracts),
    (16, _migration_016_alphaops_v6_universe_registry),
)
