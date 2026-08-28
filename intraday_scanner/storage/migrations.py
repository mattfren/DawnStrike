"""Additive SQLite migrations for Dawnstrike storage."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

# The governed opportunity stores in this base intentionally accept schema 30
# only. Strategy receipts are an additive sidecar migration and therefore do
# not advance that legacy marker.
CURRENT_SCHEMA_VERSION = 30

_STRATEGY_RECEIPT_TABLES = (
    "strategy_decision_receipts",
    "strategy_condition_results",
    "strategy_evidence_claims",
    "strategy_evidence_resolution_runs",
    "research_episode_outcome_bridges",
)
_STRATEGY_RECEIPT_TRIGGERS = (
    "strategy_decision_receipts_no_update",
    "strategy_decision_receipts_no_delete",
    "strategy_condition_results_no_update",
    "strategy_condition_results_no_delete",
    "strategy_evidence_claims_no_update",
    "strategy_evidence_claims_no_delete",
    "strategy_evidence_resolution_runs_no_update",
    "strategy_evidence_resolution_runs_no_delete",
    "research_episode_outcome_bridges_no_update",
    "research_episode_outcome_bridges_no_delete",
)

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
        if target_version in {31, 32, 33}:
            missing_tables = {
                name
                for name in _STRATEGY_RECEIPT_TABLES
                if connection.execute(
                    """SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = ? LIMIT 1""",
                    (name,),
                ).fetchone()
                is None
            }
            missing_triggers = {
                name
                for name in _STRATEGY_RECEIPT_TRIGGERS
                if connection.execute(
                    """SELECT 1 FROM sqlite_master
                    WHERE type = 'trigger' AND name = ? LIMIT 1""",
                    (name,),
                ).fetchone()
                is None
            }
            v6_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(alpha_v6_decisions)").fetchall()
            }
            missing_v6_availability = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alpha_v6_decisions'"
                ).fetchone()
                is not None
                and "stored_at" not in v6_columns
            )
            if not missing_tables and not missing_triggers and not missing_v6_availability:
                continue
            migration(connection)
            # Do not advance schema_version: older governed stores validate
            # the 29/30 marker and the receipt tables are independently additive.
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


def _migration_017_alphaops_v6_one_time_holdout(
    connection: sqlite3.Connection,
) -> None:
    """Enforce one immutable untouched-holdout evaluation per experiment."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_v6_holdout_evaluations (
            holdout_evaluation_id TEXT PRIMARY KEY,
            experiment_id TEXT NOT NULL UNIQUE,
            evaluated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence_hash_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(experiment_id) REFERENCES alpha_v6_experiments(experiment_id)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_holdout_evaluations_time
        ON alpha_v6_holdout_evaluations(evaluated_at, status);
        """
    )


def _migration_018_alphaops_v6_operational_receipts(
    connection: sqlite3.Connection,
) -> None:
    """Persist daily and weekly V6 operating receipts without mutable summaries."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_v6_operational_receipts (
            receipt_id TEXT PRIMARY KEY,
            receipt_kind TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            input_hash_sha256 TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_operational_receipts_kind_time
        ON alpha_v6_operational_receipts(receipt_kind, as_of_date, created_at);
        """
    )


def _migration_019_account_comparison_contract(
    connection: sqlite3.Connection,
) -> None:
    """Persist only fail-closed account-comparison receipts and their input hash."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS account_performance_comparisons (
            comparison_id TEXT PRIMARY KEY,
            calculated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            input_hash_sha256 TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_account_comparisons_time
        ON account_performance_comparisons(calculated_at, comparison_id);
        """
    )


def _migration_020_scenario_lifecycle_identity(
    connection: sqlite3.Connection,
) -> None:
    """Link every Scenario decision to the complete durable paper lifecycle."""

    # Some historical databases are created exclusively through this formal
    # ledger and have never passed through SQLiteScanStore.initialize().  Create
    # the base table here before adding its later identity columns so the
    # migration is both additive and independently runnable.
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS scenario_signal_links (
            decision_id TEXT PRIMARY KEY,
            signal_id TEXT,
            scan_id TEXT,
            paper_intent_id TEXT,
            position_id TEXT,
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
        """
    )
    for column in (
        "entry_intent_id TEXT",
        "exit_intent_id TEXT",
        "entry_fill_id TEXT",
        "exit_fill_id TEXT",
        "paper_trade_id TEXT",
    ):
        _add_column_if_missing(connection, "scenario_signal_links", column)


def _migration_021_official_strategy_cohort_lock(
    connection: sqlite3.Connection,
) -> None:
    """Freeze one exact official Telegram cohort per strategy session."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS official_strategy_cohorts (
            official_cohort_id TEXT PRIMARY KEY,
            market_date TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            cohort TEXT NOT NULL,
            scan_id TEXT NOT NULL,
            event_key TEXT NOT NULL,
            body_sha256 TEXT NOT NULL,
            membership_sha256 TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (market_date, strategy_id, strategy_version, cohort)
        );
        CREATE INDEX IF NOT EXISTS idx_official_strategy_cohort_scan
        ON official_strategy_cohorts(scan_id, event_key);
        CREATE INDEX IF NOT EXISTS idx_official_strategy_cohort_date
        ON official_strategy_cohorts(market_date, cohort);
        """
    )


def _migration_022_intraday_evidence_spine(connection: sqlite3.Connection) -> None:
    """Add append-only indexes for retained intraday evidence and lineage."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS intraday_provider_capability_receipts (
            capability_receipt_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            feed TEXT NOT NULL,
            entitlement TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            request_start TEXT NOT NULL,
            request_end TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            code_sha TEXT NOT NULL,
            raw_artifact_hash_sha256 TEXT NOT NULL,
            normalized_artifact_hash_sha256 TEXT NOT NULL,
            retention_status TEXT NOT NULL,
            capabilities_json TEXT NOT NULL,
            receipt_hash_sha256 TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_intraday_capability_provider_feed
        ON intraday_provider_capability_receipts(provider, feed, entitlement, fetched_at);

        CREATE TABLE IF NOT EXISTS intraday_artifact_manifests (
            artifact_manifest_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            feed TEXT NOT NULL,
            artifact_kind TEXT NOT NULL,
            symbol TEXT NOT NULL,
            market_date TEXT NOT NULL,
            exchange_session_id TEXT NOT NULL,
            request_start TEXT NOT NULL,
            request_end TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            code_sha TEXT NOT NULL,
            raw_artifact_hash_sha256 TEXT NOT NULL,
            normalized_artifact_hash_sha256 TEXT NOT NULL,
            raw_artifact_path TEXT NOT NULL,
            normalized_artifact_path TEXT NOT NULL,
            retention_status TEXT NOT NULL,
            artifact_identity TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_intraday_artifact_symbol_date
        ON intraday_artifact_manifests(symbol, market_date, provider, feed);
        CREATE INDEX IF NOT EXISTS idx_intraday_artifact_session
        ON intraday_artifact_manifests(exchange_session_id, fetched_at);
        CREATE INDEX IF NOT EXISTS idx_intraday_artifact_hashes
        ON intraday_artifact_manifests(raw_artifact_hash_sha256, normalized_artifact_hash_sha256);

        CREATE TABLE IF NOT EXISTS intraday_coverage_receipts (
            coverage_receipt_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            feed TEXT NOT NULL,
            entitlement TEXT NOT NULL,
            symbol TEXT NOT NULL,
            market_date TEXT NOT NULL,
            exchange_session_id TEXT NOT NULL,
            request_start TEXT NOT NULL,
            request_end TEXT NOT NULL,
            observed_start TEXT,
            observed_end TEXT,
            status TEXT NOT NULL,
            artifact_manifest_id TEXT,
            code_sha TEXT NOT NULL,
            raw_artifact_hash_sha256 TEXT NOT NULL,
            normalized_artifact_hash_sha256 TEXT NOT NULL,
            retention_status TEXT NOT NULL,
            coverage_identity TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (artifact_manifest_id)
                REFERENCES intraday_artifact_manifests(artifact_manifest_id)
        );
        CREATE INDEX IF NOT EXISTS idx_intraday_coverage_symbol_date
        ON intraday_coverage_receipts(symbol, market_date, status);
        CREATE INDEX IF NOT EXISTS idx_intraday_coverage_provider_feed
        ON intraday_coverage_receipts(provider, feed, entitlement, exchange_session_id);

        CREATE TABLE IF NOT EXISTS legacy_policy_classifications (
            classification_id TEXT PRIMARY KEY,
            source_db_hash_sha256 TEXT NOT NULL,
            source_code_sha TEXT NOT NULL,
            classifier_version TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            inferred_policy TEXT NOT NULL,
            membership_hash_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (source_db_hash_sha256, classifier_version, membership_hash_sha256)
        );
        CREATE INDEX IF NOT EXISTS idx_legacy_policy_source
        ON legacy_policy_classifications(source_db_hash_sha256, generated_at);
        """
    )


def _migration_023_alpha_path_replay_reconciliations(
    connection: sqlite3.Connection,
) -> None:
    """Add append-only path replay and legacy excursion reconciliation facts."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_path_replays (
            path_replay_id TEXT PRIMARY KEY,
            cohort TEXT NOT NULL,
            selection_id TEXT NOT NULL,
            signal_id TEXT,
            market_date TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            artifact_identity TEXT NOT NULL,
            artifact_hash_sha256 TEXT NOT NULL,
            path_truth_status TEXT NOT NULL,
            conservative_policy_result TEXT,
            entry_at TEXT,
            entry_price REAL,
            target_touched_at TEXT,
            stop_touched_at TEXT,
            exit_at TEXT,
            exit_price REAL,
            mfe_price REAL,
            mfe_at TEXT,
            mae_price REAL,
            mae_at TEXT,
            retrospective_research_eligible INTEGER NOT NULL,
            prospective_promotion_eligible INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (cohort, selection_id, policy_version, artifact_identity)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_path_replays_market_status
        ON alpha_path_replays(market_date, path_truth_status, cohort);
        CREATE INDEX IF NOT EXISTS idx_alpha_path_replays_signal
        ON alpha_path_replays(signal_id, selection_id);
        CREATE INDEX IF NOT EXISTS idx_alpha_path_replays_artifact
        ON alpha_path_replays(artifact_hash_sha256, artifact_identity);

        CREATE TABLE IF NOT EXISTS paper_position_excursion_reconciliations (
            reconciliation_id TEXT PRIMARY KEY,
            position_id TEXT NOT NULL,
            path_replay_id TEXT NOT NULL,
            source_bar_hash_sha256 TEXT NOT NULL,
            source_quote_hash_sha256 TEXT NOT NULL,
            path_truth_status TEXT NOT NULL,
            mfe_price REAL,
            mfe_at TEXT,
            mae_price REAL,
            mae_at TEXT,
            mfe_lower_bound REAL,
            mfe_upper_bound REAL,
            mae_lower_bound REAL,
            mae_upper_bound REAL,
            reconciliation_receipt_hash_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (position_id, path_replay_id),
            FOREIGN KEY (path_replay_id) REFERENCES alpha_path_replays(path_replay_id)
        );
        CREATE INDEX IF NOT EXISTS idx_position_excursion_position
        ON paper_position_excursion_reconciliations(position_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_position_excursion_status
        ON paper_position_excursion_reconciliations(path_truth_status, created_at);
        """
    )


def _migration_024_catalyst_evidence(
    connection: sqlite3.Connection,
) -> None:
    """Add append-only point-in-time catalyst evidence and extraction lineage."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS catalyst_evidence_events (
            event_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            source_content_hash_sha256 TEXT NOT NULL,
            published_at TEXT,
            first_seen_at TEXT NOT NULL,
            available_at_decision INTEGER NOT NULL,
            decision_at TEXT,
            event_type TEXT NOT NULL,
            polarity TEXT NOT NULL,
            financing_mechanism TEXT NOT NULL,
            novelty TEXT NOT NULL,
            timing TEXT NOT NULL,
            source_coverage_status TEXT NOT NULL,
            promotional_status TEXT NOT NULL,
            rumor_status TEXT NOT NULL,
            squeeze_mechanics TEXT NOT NULL,
            confidence_status TEXT NOT NULL,
            raw_artifact_path TEXT,
            raw_artifact_hash_sha256 TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (source_kind, source_content_hash_sha256)
        );
        CREATE INDEX IF NOT EXISTS idx_catalyst_events_symbol_time
        ON catalyst_evidence_events(symbol, published_at, first_seen_at);
        CREATE INDEX IF NOT EXISTS idx_catalyst_events_decision_availability
        ON catalyst_evidence_events(symbol, decision_at, available_at_decision);
        CREATE INDEX IF NOT EXISTS idx_catalyst_events_feature
        ON catalyst_evidence_events(event_type, financing_mechanism, timing);

        CREATE TABLE IF NOT EXISTS catalyst_claim_extractions (
            extraction_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            source_content_hash_sha256 TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            model TEXT NOT NULL,
            input_hash_sha256 TEXT NOT NULL,
            output_hash_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence_spans_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (event_id, input_hash_sha256, prompt_version, schema_version),
            FOREIGN KEY (event_id) REFERENCES catalyst_evidence_events(event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_catalyst_extractions_event
        ON catalyst_claim_extractions(event_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_catalyst_extractions_source_hash
        ON catalyst_claim_extractions(source_content_hash_sha256, input_hash_sha256);
        """
    )


def _migration_025_v6_evidence_lineage(
    connection: sqlite3.Connection,
) -> None:
    """Carry immutable evidence identity through every V6 research receipt."""

    tables = (
        "alpha_v6_decisions",
        "alpha_v6_outcomes",
        "alpha_v6_labels",
        "alpha_v6_datasets",
        "alpha_v6_model_runs",
        "alpha_v6_model_artifacts",
        "alpha_v6_evaluations",
        "alpha_v6_operational_receipts",
    )

    columns = (
        "source_artifact_hash_sha256 TEXT",
        "path_replay_id TEXT",
        "benchmark_hash_sha256 TEXT",
        "observed_cost_model_identity TEXT",
        "modeled_cost_model_identity TEXT",
        "evidence_cohort TEXT",
        "retrospective_research_eligible INTEGER",
        "prospective_promotion_eligible INTEGER",
        "evidence_lineage_hash_sha256 TEXT",
    )
    existing_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for table in tables:
        if table not in existing_tables:
            continue
        for column in columns:
            _add_column_if_missing(connection, table, column)
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_evidence_lineage "
            f"ON {table}(evidence_cohort, retrospective_research_eligible, "
            "prospective_promotion_eligible, evidence_lineage_hash_sha256)"
        )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_v6_evidence_lineage (
            lineage_id TEXT PRIMARY KEY,
            entity_kind TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            source_artifact_hash_sha256 TEXT,
            path_replay_id TEXT,
            benchmark_hash_sha256 TEXT,
            observed_cost_model_identity TEXT,
            modeled_cost_model_identity TEXT,
            evidence_cohort TEXT,
            retrospective_research_eligible INTEGER,
            prospective_promotion_eligible INTEGER,
            evidence_lineage_hash_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (entity_kind, entity_id, evidence_lineage_hash_sha256)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_lineage_entity
        ON alpha_v6_evidence_lineage(entity_kind, entity_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_alpha_v6_lineage_cohort
        ON alpha_v6_evidence_lineage(evidence_cohort, retrospective_research_eligible,
                                     prospective_promotion_eligible);
        """
    )


def _migration_026_trade_attribution_evidence(
    connection: sqlite3.Connection,
) -> None:
    """Add immutable case/factor receipts for diagnostic attribution."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS trade_attribution_cases (
            case_id TEXT PRIMARY KEY,
            trade_id TEXT NOT NULL UNIQUE,
            market_date TEXT,
            ticker TEXT,
            strategy_id TEXT,
            attribution_status TEXT NOT NULL,
            coverage_status TEXT NOT NULL,
            evidence_hash_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trade_attribution_cases_date
        ON trade_attribution_cases(market_date, strategy_id, coverage_status);
        CREATE INDEX IF NOT EXISTS idx_trade_attribution_cases_status
        ON trade_attribution_cases(attribution_status, created_at);

        CREATE TABLE IF NOT EXISTS trade_attribution_factors (
            factor_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            factor_key TEXT NOT NULL,
            factor_status TEXT NOT NULL,
            evidence_hash_sha256 TEXT,
            evaluator_version TEXT NOT NULL,
            confidence_basis TEXT NOT NULL,
            counterfactual_policy TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (factor_status IN (
                'observed_defect', 'supported_contributor', 'suspected',
                'unknown', 'not_applicable'
            )),
            UNIQUE (case_id, factor_key, factor_id),
            FOREIGN KEY(case_id) REFERENCES trade_attribution_cases(case_id)
        );
        CREATE INDEX IF NOT EXISTS idx_trade_attribution_factors_case
        ON trade_attribution_factors(case_id, factor_key, factor_status);
        CREATE INDEX IF NOT EXISTS idx_trade_attribution_factors_status
        ON trade_attribution_factors(factor_status, created_at);
        """
    )


def _migration_027_opportunity_pipeline_runs(
    connection: sqlite3.Connection,
) -> None:
    """Add append-only canonical opportunity-run persistence."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS opportunity_pipeline_runs (
            run_id TEXT PRIMARY KEY,
            result_content_hash_sha256 TEXT NOT NULL,
            preparation_id TEXT NOT NULL,
            preparation_content_hash_sha256 TEXT NOT NULL,
            decision_context_id TEXT,
            decision_context_content_hash_sha256 TEXT,
            dataset_id TEXT NOT NULL,
            dataset_content_id TEXT NOT NULL,
            universe_snapshot_id TEXT NOT NULL,
            universe_snapshot_content_hash_sha256 TEXT NOT NULL,
            decision_at TEXT NOT NULL,
            result_schema_version TEXT NOT NULL,
            result_json TEXT NOT NULL,
            artifact_count INTEGER NOT NULL CHECK (artifact_count >= 0),
            artifact_inventory_hash_sha256 TEXT NOT NULL,
            receipt_id TEXT NOT NULL UNIQUE,
            receipt_json TEXT NOT NULL,
            research_only INTEGER NOT NULL CHECK (research_only = 1),
            first_recorded_at TEXT NOT NULL,
            CHECK (
                (decision_context_id IS NULL AND decision_context_content_hash_sha256 IS NULL)
                OR
                (decision_context_id IS NOT NULL
                 AND decision_context_content_hash_sha256 IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_pipeline_runs_decision
        ON opportunity_pipeline_runs(decision_at, run_id);
        CREATE INDEX IF NOT EXISTS idx_opportunity_pipeline_runs_dataset
        ON opportunity_pipeline_runs(dataset_id, dataset_content_id);
        CREATE INDEX IF NOT EXISTS idx_opportunity_pipeline_runs_universe
        ON opportunity_pipeline_runs(universe_snapshot_id,
                                     universe_snapshot_content_hash_sha256);

        CREATE TABLE IF NOT EXISTS opportunity_run_artifacts (
            run_id TEXT NOT NULL,
            inventory_ordinal INTEGER NOT NULL CHECK (inventory_ordinal >= 0),
            artifact_family TEXT NOT NULL CHECK (artifact_family IN (
                'universe_snapshot',
                'prepared_pipeline',
                'strategy_expectancy_binding',
                'cheap_feature_snapshot',
                'rich_feature_snapshot',
                'benchmark_feature_snapshot',
                'opportunity_candidate',
                'market_regime',
                'security_regime',
                'strategy_evaluation',
                'ranked_opportunity',
                'pipeline_risk_policy',
                'execution_risk_evidence',
                'decision_run_context',
                'trade_decision',
                'decision_trace'
            )),
            family_ordinal INTEGER NOT NULL CHECK (family_ordinal >= 0),
            artifact_id TEXT NOT NULL,
            evaluation_id TEXT,
            decision_id TEXT,
            artifact_schema_version TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash_sha256 TEXT NOT NULL,
            first_recorded_at TEXT NOT NULL,
            PRIMARY KEY (run_id, inventory_ordinal),
            UNIQUE (run_id, artifact_family, family_ordinal),
            UNIQUE (run_id, artifact_family, artifact_id),
            FOREIGN KEY (run_id) REFERENCES opportunity_pipeline_runs(run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_artifacts_identity
        ON opportunity_run_artifacts(artifact_family, artifact_id,
                                     content_hash_sha256);
        CREATE INDEX IF NOT EXISTS idx_opportunity_artifacts_evaluation
        ON opportunity_run_artifacts(run_id, evaluation_id, artifact_family,
                                     family_ordinal);
        CREATE INDEX IF NOT EXISTS idx_opportunity_artifacts_decision
        ON opportunity_run_artifacts(run_id, decision_id, artifact_family,
                                     family_ordinal);

        CREATE TRIGGER IF NOT EXISTS opportunity_pipeline_runs_no_update
        BEFORE UPDATE ON opportunity_pipeline_runs
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_pipeline_runs is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_pipeline_runs_no_delete
        BEFORE DELETE ON opportunity_pipeline_runs
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_pipeline_runs is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_run_artifacts_no_update
        BEFORE UPDATE ON opportunity_run_artifacts
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_run_artifacts is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_run_artifacts_no_delete
        BEFORE DELETE ON opportunity_run_artifacts
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_run_artifacts is append-only');
        END;
        """
    )


def _migration_028_opportunity_outcomes(
    connection: sqlite3.Connection,
) -> None:
    """Add append-only outcome batches, records, and supersession lineage."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS opportunity_outcome_receipts (
            outcome_receipt_id TEXT PRIMARY KEY,
            receipt_content_hash_sha256 TEXT NOT NULL,
            receipt_kind TEXT NOT NULL CHECK (receipt_kind IN ('initial', 'correction')),
            batch_id TEXT NOT NULL UNIQUE,
            batch_content_hash_sha256 TEXT NOT NULL,
            batch_schema_version TEXT NOT NULL CHECK (
                batch_schema_version = 'v2.opportunity.outcome_label_batch.v2'
            ),
            batch_json TEXT NOT NULL,
            run_id TEXT NOT NULL,
            run_content_hash_sha256 TEXT NOT NULL,
            run_persistence_receipt_id TEXT NOT NULL,
            run_persistence_receipt_content_hash_sha256 TEXT NOT NULL,
            source_dataset_id TEXT NOT NULL,
            source_dataset_content_hash_sha256 TEXT NOT NULL,
            policy_id TEXT NOT NULL,
            policy_content_hash_sha256 TEXT NOT NULL,
            decision_at TEXT NOT NULL,
            batch_recorded_at TEXT NOT NULL,
            persisted_at TEXT NOT NULL,
            supersedes_outcome_receipt_id TEXT,
            supersedes_outcome_receipt_content_hash_sha256 TEXT,
            record_count INTEGER NOT NULL CHECK (record_count >= 0),
            artifact_count INTEGER NOT NULL CHECK (artifact_count >= 1),
            artifact_inventory_hash_sha256 TEXT NOT NULL,
            receipt_schema_version TEXT NOT NULL CHECK (
                receipt_schema_version = 'v2.opportunity.outcome_persistence_receipt.v1'
            ),
            receipt_json TEXT NOT NULL,
            research_only INTEGER NOT NULL CHECK (research_only = 1),
            database_schema_version INTEGER NOT NULL CHECK (database_schema_version = 28),
            UNIQUE (run_id, outcome_receipt_id),
            UNIQUE (
                run_id, outcome_receipt_id, receipt_content_hash_sha256
            ),
            CHECK (artifact_count = record_count + 1),
            CHECK (
                (receipt_kind = 'initial'
                 AND supersedes_outcome_receipt_id IS NULL
                 AND supersedes_outcome_receipt_content_hash_sha256 IS NULL)
                OR
                (receipt_kind = 'correction'
                 AND supersedes_outcome_receipt_id IS NOT NULL
                 AND supersedes_outcome_receipt_content_hash_sha256 IS NOT NULL)
            ),
            FOREIGN KEY (run_id) REFERENCES opportunity_pipeline_runs(run_id),
            FOREIGN KEY (
                run_id, supersedes_outcome_receipt_id,
                supersedes_outcome_receipt_content_hash_sha256
            ) REFERENCES opportunity_outcome_receipts(
                run_id, outcome_receipt_id, receipt_content_hash_sha256
            )
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_outcome_receipt_root
        ON opportunity_outcome_receipts(run_id)
        WHERE supersedes_outcome_receipt_id IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_outcome_receipt_successor
        ON opportunity_outcome_receipts(supersedes_outcome_receipt_id)
        WHERE supersedes_outcome_receipt_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_opportunity_outcome_receipts_run
        ON opportunity_outcome_receipts(run_id, persisted_at, outcome_receipt_id);
        CREATE INDEX IF NOT EXISTS idx_opportunity_outcome_receipts_batch
        ON opportunity_outcome_receipts(batch_id, batch_content_hash_sha256);

        CREATE TABLE IF NOT EXISTS opportunity_outcome_records (
            outcome_receipt_id TEXT NOT NULL,
            record_ordinal INTEGER NOT NULL CHECK (record_ordinal >= 0),
            run_id TEXT NOT NULL,
            evaluation_id TEXT NOT NULL,
            horizon_id TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            outcome_id TEXT NOT NULL,
            outcome_content_hash_sha256 TEXT NOT NULL,
            outcome_schema_version TEXT NOT NULL CHECK (
                outcome_schema_version = 'v2.opportunity.outcome_record.v3'
            ),
            outcome_json TEXT NOT NULL,
            completeness TEXT NOT NULL CHECK (completeness IN (
                'complete', 'partial', 'pending', 'censored', 'unavailable'
            )),
            entry_status TEXT NOT NULL CHECK (entry_status IN (
                'filled', 'no_entry', 'not_applicable', 'pending',
                'entry_bar_ambiguous', 'gap_through_ambiguous',
                'unattainable', 'unsupported'
            )),
            path_status TEXT NOT NULL CHECK (path_status IN (
                'target_first', 'stop_first', 'horizon_exit', 'no_entry',
                'entry_bar_ambiguous', 'same_bar_ambiguous',
                'gap_through_ambiguous', 'pending_horizon',
                'missing_bars', 'halt_censored', 'corporate_action_censored',
                'unsupported_evidence', 'unattainable_fill', 'not_applicable'
            )),
            supersedes_outcome_receipt_id TEXT,
            supersedes_outcome_id TEXT,
            supersedes_outcome_content_hash_sha256 TEXT,
            first_persisted_at TEXT NOT NULL,
            PRIMARY KEY (outcome_receipt_id, record_ordinal),
            UNIQUE (outcome_receipt_id, evaluation_id, horizon_id),
            UNIQUE (outcome_receipt_id, outcome_id),
            UNIQUE (
                run_id, outcome_receipt_id, outcome_id,
                outcome_content_hash_sha256
            ),
            CHECK (
                (supersedes_outcome_receipt_id IS NULL
                 AND supersedes_outcome_id IS NULL
                 AND supersedes_outcome_content_hash_sha256 IS NULL)
                OR
                (supersedes_outcome_receipt_id IS NOT NULL
                 AND supersedes_outcome_id IS NOT NULL
                 AND supersedes_outcome_content_hash_sha256 IS NOT NULL)
            ),
            FOREIGN KEY (run_id, outcome_receipt_id)
                REFERENCES opportunity_outcome_receipts(run_id, outcome_receipt_id),
            FOREIGN KEY (
                run_id, supersedes_outcome_receipt_id, supersedes_outcome_id,
                supersedes_outcome_content_hash_sha256
            ) REFERENCES opportunity_outcome_records(
                run_id, outcome_receipt_id, outcome_id,
                outcome_content_hash_sha256
            )
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_outcome_record_root
        ON opportunity_outcome_records(run_id, evaluation_id, horizon_id)
        WHERE supersedes_outcome_receipt_id IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_outcome_record_successor
        ON opportunity_outcome_records(
            run_id, supersedes_outcome_receipt_id, supersedes_outcome_id,
            supersedes_outcome_content_hash_sha256
        )
        WHERE supersedes_outcome_receipt_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_opportunity_outcome_records_pair
        ON opportunity_outcome_records(
            run_id, evaluation_id, horizon_id, outcome_receipt_id, record_ordinal
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_outcome_records_identity
        ON opportunity_outcome_records(
            outcome_id, outcome_content_hash_sha256, outcome_schema_version
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_outcome_records_decision
        ON opportunity_outcome_records(run_id, decision_id, outcome_receipt_id);
        CREATE INDEX IF NOT EXISTS idx_opportunity_outcome_records_status
        ON opportunity_outcome_records(run_id, completeness, path_status);

        CREATE TRIGGER IF NOT EXISTS opportunity_outcome_receipts_no_update
        BEFORE UPDATE ON opportunity_outcome_receipts
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_outcome_receipts is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_outcome_receipts_no_delete
        BEFORE DELETE ON opportunity_outcome_receipts
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_outcome_receipts is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_outcome_records_no_update
        BEFORE UPDATE ON opportunity_outcome_records
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_outcome_records is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_outcome_records_no_delete
        BEFORE DELETE ON opportunity_outcome_records
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_outcome_records is append-only');
        END;
        """
    )


def _migration_029_opportunity_research(
    connection: sqlite3.Connection,
) -> None:
    """Add append-only missed-opportunity and discovery-metric storage."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS opportunity_miss_receipts (
            miss_receipt_id TEXT PRIMARY KEY,
            receipt_content_hash_sha256 TEXT NOT NULL,
            receipt_kind TEXT NOT NULL CHECK (receipt_kind IN ('initial', 'correction')),
            analysis_key TEXT NOT NULL,
            batch_id TEXT NOT NULL UNIQUE,
            batch_content_hash_sha256 TEXT NOT NULL,
            batch_schema_version TEXT NOT NULL CHECK (
                batch_schema_version = 'v2.opportunity.miss_reconciliation_batch.v1'
            ),
            batch_json TEXT NOT NULL,
            exchange_session_id TEXT NOT NULL,
            session_open_at TEXT NOT NULL,
            session_close_at TEXT NOT NULL,
            membership_as_of_at TEXT NOT NULL,
            requested_query_start_at TEXT NOT NULL,
            requested_through_at TEXT NOT NULL,
            requested_symbols_json TEXT NOT NULL,
            requested_symbol_count INTEGER NOT NULL CHECK (requested_symbol_count >= 0),
            empty_eligible_universe INTEGER NOT NULL CHECK (
                empty_eligible_universe IN (0, 1)
            ),
            authority_claim TEXT NOT NULL CHECK (authority_claim IN (
                'market_complete', 'bounded_cohort', 'no_authority'
            )),
            source_scope_status TEXT NOT NULL CHECK (source_scope_status IN (
                'complete_market', 'complete_bounded', 'partial',
                'pending', 'unavailable'
            )),
            inventory_status TEXT NOT NULL CHECK (inventory_status IN (
                'complete_authoritative', 'complete_bounded', 'partial',
                'pending', 'unavailable'
            )),
            qualification_policy_id TEXT NOT NULL,
            qualification_policy_content_hash_sha256 TEXT NOT NULL,
            qualification_batch_id TEXT NOT NULL,
            qualification_batch_content_hash_sha256 TEXT NOT NULL,
            session_replay_id TEXT NOT NULL,
            session_replay_content_hash_sha256 TEXT NOT NULL,
            session_disposition TEXT NOT NULL CHECK (session_disposition IN (
                'correct_no_trade', 'false_positive', 'caught', 'missed',
                'too_late', 'mixed', 'pending', 'censored', 'unavailable', 'unknown'
            )),
            batch_recorded_at TEXT NOT NULL,
            persisted_at TEXT NOT NULL,
            supersedes_miss_receipt_id TEXT,
            supersedes_miss_receipt_content_hash_sha256 TEXT,
            record_count INTEGER NOT NULL CHECK (record_count >= 0),
            run_binding_count INTEGER NOT NULL CHECK (run_binding_count >= 0),
            artifact_count INTEGER NOT NULL CHECK (artifact_count >= 1),
            artifact_inventory_hash_sha256 TEXT NOT NULL,
            receipt_schema_version TEXT NOT NULL CHECK (
                receipt_schema_version = 'v2.opportunity.miss_persistence_receipt.v1'
            ),
            receipt_json TEXT NOT NULL,
            research_only INTEGER NOT NULL CHECK (research_only = 1),
            promotion_eligible INTEGER NOT NULL CHECK (promotion_eligible = 0),
            database_schema_version INTEGER NOT NULL CHECK (database_schema_version = 29),
            UNIQUE (analysis_key, miss_receipt_id, receipt_content_hash_sha256),
            UNIQUE (analysis_key, miss_receipt_id),
            UNIQUE (miss_receipt_id, receipt_content_hash_sha256),
            CHECK (session_open_at < session_close_at),
            CHECK (membership_as_of_at <= session_open_at),
            CHECK (requested_query_start_at <= session_open_at),
            CHECK (requested_query_start_at <= requested_through_at),
            CHECK (requested_through_at = session_close_at),
            CHECK (
                (requested_symbol_count > 0 AND empty_eligible_universe = 0)
                OR
                (requested_symbol_count = 0
                 AND empty_eligible_universe = 1
                 AND authority_claim = 'market_complete'
                 AND source_scope_status = 'complete_market'
                 AND inventory_status = 'complete_authoritative')
            ),
            CHECK (artifact_count = 1 + record_count + run_binding_count),
            CHECK (
                (receipt_kind = 'initial'
                 AND supersedes_miss_receipt_id IS NULL
                 AND supersedes_miss_receipt_content_hash_sha256 IS NULL)
                OR
                (receipt_kind = 'correction'
                 AND supersedes_miss_receipt_id IS NOT NULL
                 AND supersedes_miss_receipt_content_hash_sha256 IS NOT NULL)
            ),
            FOREIGN KEY (
                analysis_key, supersedes_miss_receipt_id,
                supersedes_miss_receipt_content_hash_sha256
            ) REFERENCES opportunity_miss_receipts(
                analysis_key, miss_receipt_id, receipt_content_hash_sha256
            )
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_miss_receipt_root
        ON opportunity_miss_receipts(analysis_key)
        WHERE supersedes_miss_receipt_id IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_miss_receipt_successor
        ON opportunity_miss_receipts(
            analysis_key, supersedes_miss_receipt_id,
            supersedes_miss_receipt_content_hash_sha256
        )
        WHERE supersedes_miss_receipt_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_opportunity_miss_receipts_scope
        ON opportunity_miss_receipts(analysis_key, persisted_at, miss_receipt_id);
        CREATE INDEX IF NOT EXISTS idx_opportunity_miss_receipts_batch
        ON opportunity_miss_receipts(batch_id, batch_content_hash_sha256);
        CREATE INDEX IF NOT EXISTS idx_opportunity_miss_receipts_session_policy
        ON opportunity_miss_receipts(
            exchange_session_id, qualification_policy_id, persisted_at
        );

        CREATE TABLE IF NOT EXISTS opportunity_miss_records (
            miss_receipt_id TEXT NOT NULL,
            record_ordinal INTEGER NOT NULL CHECK (record_ordinal >= 0),
            analysis_key TEXT NOT NULL,
            session_opportunity_key TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL CHECK (direction IN ('long', 'short')),
            horizon_id TEXT NOT NULL,
            opportunity_id TEXT NOT NULL,
            opportunity_content_hash_sha256 TEXT NOT NULL,
            miss_record_id TEXT NOT NULL,
            miss_record_content_hash_sha256 TEXT NOT NULL,
            miss_record_schema_version TEXT NOT NULL CHECK (
                miss_record_schema_version = 'v2.opportunity.missed_opportunity_record.v1'
            ),
            miss_record_json TEXT NOT NULL,
            disposition TEXT NOT NULL CHECK (disposition IN (
                'caught', 'missed', 'too_late', 'unknown'
            )),
            category TEXT CHECK (category IS NULL OR category IN (
                'universe_miss', 'data_miss', 'feature_miss', 'anomaly_miss',
                'regime_misclassification', 'strategy_miss', 'scoring_miss',
                'quality_gate_miss', 'execution_filter', 'unknown'
            )),
            first_persisted_at TEXT NOT NULL,
            PRIMARY KEY (miss_receipt_id, record_ordinal),
            UNIQUE (miss_receipt_id, session_opportunity_key),
            UNIQUE (miss_receipt_id, miss_record_id),
            CHECK (
                (disposition = 'caught' AND category IS NULL)
                OR
                (disposition <> 'caught' AND category IS NOT NULL)
            ),
            FOREIGN KEY (analysis_key, miss_receipt_id)
                REFERENCES opportunity_miss_receipts(analysis_key, miss_receipt_id)
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_miss_records_stable_key
        ON opportunity_miss_records(
            analysis_key, session_opportunity_key, miss_receipt_id
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_miss_records_identity
        ON opportunity_miss_records(
            miss_record_id, miss_record_content_hash_sha256,
            miss_record_schema_version
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_miss_records_status
        ON opportunity_miss_records(analysis_key, disposition, category);

        CREATE TABLE IF NOT EXISTS opportunity_miss_run_bindings (
            miss_receipt_id TEXT NOT NULL,
            binding_ordinal INTEGER NOT NULL CHECK (binding_ordinal >= 0),
            binding_id TEXT NOT NULL,
            binding_content_hash_sha256 TEXT NOT NULL,
            binding_schema_version TEXT NOT NULL CHECK (
                binding_schema_version = 'v2.opportunity.session_run_binding.v1'
            ),
            binding_json TEXT NOT NULL,
            run_id TEXT NOT NULL,
            run_content_hash_sha256 TEXT NOT NULL,
            run_persistence_receipt_id TEXT NOT NULL,
            run_persistence_receipt_content_hash_sha256 TEXT NOT NULL,
            outcome_replay_id TEXT NOT NULL,
            outcome_replay_content_hash_sha256 TEXT NOT NULL,
            outcome_head_receipt_id TEXT NOT NULL,
            outcome_head_receipt_content_hash_sha256 TEXT NOT NULL,
            decision_at TEXT NOT NULL,
            PRIMARY KEY (miss_receipt_id, binding_ordinal),
            UNIQUE (miss_receipt_id, run_id),
            UNIQUE (miss_receipt_id, binding_id),
            FOREIGN KEY (miss_receipt_id)
                REFERENCES opportunity_miss_receipts(miss_receipt_id),
            FOREIGN KEY (run_id) REFERENCES opportunity_pipeline_runs(run_id),
            FOREIGN KEY (
                run_id, outcome_head_receipt_id,
                outcome_head_receipt_content_hash_sha256
            ) REFERENCES opportunity_outcome_receipts(
                run_id, outcome_receipt_id, receipt_content_hash_sha256
            )
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_miss_run_bindings_parent
        ON opportunity_miss_run_bindings(
            run_id, outcome_head_receipt_id,
            outcome_head_receipt_content_hash_sha256
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_miss_run_bindings_order
        ON opportunity_miss_run_bindings(miss_receipt_id, decision_at, run_id);
        CREATE INDEX IF NOT EXISTS idx_opportunity_miss_run_bindings_identity
        ON opportunity_miss_run_bindings(
            binding_id, binding_content_hash_sha256, binding_schema_version
        );

        CREATE TABLE IF NOT EXISTS opportunity_metric_receipts (
            metric_receipt_id TEXT PRIMARY KEY,
            receipt_content_hash_sha256 TEXT NOT NULL,
            receipt_kind TEXT NOT NULL CHECK (receipt_kind IN ('initial', 'correction')),
            report_kind TEXT NOT NULL CHECK (report_kind IN ('session', 'multi_session')),
            scope_key TEXT NOT NULL,
            report_id TEXT NOT NULL UNIQUE,
            report_content_hash_sha256 TEXT NOT NULL,
            report_schema_version TEXT NOT NULL CHECK (report_schema_version IN (
                'v2.opportunity.session_discovery_metric_report.v1',
                'v2.opportunity.discovery_metric_report.v1'
            )),
            report_json TEXT NOT NULL,
            metric_policy_id TEXT NOT NULL,
            metric_policy_content_hash_sha256 TEXT NOT NULL,
            exchange_session_id TEXT,
            session_open_at TEXT,
            session_close_at TEXT,
            parent_miss_receipt_id TEXT,
            parent_miss_receipt_content_hash_sha256 TEXT,
            cohort_id TEXT,
            report_recorded_at TEXT,
            persisted_at TEXT NOT NULL,
            supersedes_metric_receipt_id TEXT,
            supersedes_metric_receipt_content_hash_sha256 TEXT,
            session_binding_count INTEGER NOT NULL CHECK (session_binding_count >= 0),
            metric_value_count INTEGER NOT NULL CHECK (metric_value_count = 9),
            artifact_count INTEGER NOT NULL CHECK (artifact_count >= 1),
            artifact_inventory_hash_sha256 TEXT NOT NULL,
            receipt_schema_version TEXT NOT NULL CHECK (
                receipt_schema_version = 'v2.opportunity.metric_persistence_receipt.v1'
            ),
            receipt_json TEXT NOT NULL,
            research_only INTEGER NOT NULL CHECK (research_only = 1),
            promotion_eligible INTEGER NOT NULL CHECK (promotion_eligible = 0),
            database_schema_version INTEGER NOT NULL CHECK (database_schema_version = 29),
            UNIQUE (scope_key, metric_receipt_id, receipt_content_hash_sha256),
            UNIQUE (metric_receipt_id, receipt_content_hash_sha256),
            UNIQUE (
                metric_receipt_id, receipt_content_hash_sha256, scope_key,
                report_id, report_content_hash_sha256
            ),
            CHECK (
                (receipt_kind = 'initial'
                 AND supersedes_metric_receipt_id IS NULL
                 AND supersedes_metric_receipt_content_hash_sha256 IS NULL)
                OR
                (receipt_kind = 'correction'
                 AND supersedes_metric_receipt_id IS NOT NULL
                 AND supersedes_metric_receipt_content_hash_sha256 IS NOT NULL)
            ),
            CHECK (
                (report_kind = 'session'
                 AND report_schema_version =
                    'v2.opportunity.session_discovery_metric_report.v1'
                 AND exchange_session_id IS NOT NULL
                 AND session_open_at IS NOT NULL
                 AND session_close_at IS NOT NULL
                 AND parent_miss_receipt_id IS NOT NULL
                 AND parent_miss_receipt_content_hash_sha256 IS NOT NULL
                 AND cohort_id IS NULL
                 AND session_binding_count = 0
                 AND report_recorded_at IS NOT NULL
                 AND session_open_at < session_close_at
                 AND artifact_count = 1)
                OR
                (report_kind = 'multi_session'
                 AND report_schema_version =
                    'v2.opportunity.discovery_metric_report.v1'
                 AND exchange_session_id IS NULL
                 AND session_open_at IS NULL
                 AND session_close_at IS NULL
                 AND parent_miss_receipt_id IS NULL
                 AND parent_miss_receipt_content_hash_sha256 IS NULL
                 AND cohort_id IS NOT NULL
                 AND ((session_binding_count = 0 AND report_recorded_at IS NULL)
                      OR (session_binding_count > 0 AND report_recorded_at IS NOT NULL))
                 AND artifact_count = 1 + session_binding_count)
            ),
            FOREIGN KEY (
                scope_key, supersedes_metric_receipt_id,
                supersedes_metric_receipt_content_hash_sha256
            ) REFERENCES opportunity_metric_receipts(
                scope_key, metric_receipt_id, receipt_content_hash_sha256
            ),
            FOREIGN KEY (
                parent_miss_receipt_id,
                parent_miss_receipt_content_hash_sha256
            ) REFERENCES opportunity_miss_receipts(
                miss_receipt_id, receipt_content_hash_sha256
            )
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_metric_receipt_root
        ON opportunity_metric_receipts(scope_key)
        WHERE supersedes_metric_receipt_id IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_metric_receipt_successor
        ON opportunity_metric_receipts(
            scope_key, supersedes_metric_receipt_id,
            supersedes_metric_receipt_content_hash_sha256
        )
        WHERE supersedes_metric_receipt_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_opportunity_metric_receipts_scope
        ON opportunity_metric_receipts(scope_key, persisted_at, metric_receipt_id);
        CREATE INDEX IF NOT EXISTS idx_opportunity_metric_receipts_policy_kind
        ON opportunity_metric_receipts(metric_policy_id, report_kind, persisted_at);
        CREATE INDEX IF NOT EXISTS idx_opportunity_metric_receipts_parent_miss
        ON opportunity_metric_receipts(
            parent_miss_receipt_id, parent_miss_receipt_content_hash_sha256
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_metric_receipts_cohort
        ON opportunity_metric_receipts(cohort_id, report_id);

        CREATE TABLE IF NOT EXISTS opportunity_metric_session_bindings (
            metric_receipt_id TEXT NOT NULL,
            session_ordinal INTEGER NOT NULL CHECK (session_ordinal >= 0),
            binding_id TEXT NOT NULL,
            binding_content_hash_sha256 TEXT NOT NULL,
            binding_schema_version TEXT NOT NULL CHECK (
                binding_schema_version =
                    'v2.opportunity.metric_session_report_binding.v1'
            ),
            binding_json TEXT NOT NULL,
            exchange_session_id TEXT NOT NULL,
            child_metric_receipt_id TEXT NOT NULL,
            child_metric_receipt_content_hash_sha256 TEXT NOT NULL,
            child_metric_scope_key TEXT NOT NULL,
            child_session_report_id TEXT NOT NULL,
            child_session_report_content_hash_sha256 TEXT NOT NULL,
            child_miss_receipt_id TEXT NOT NULL,
            child_miss_receipt_content_hash_sha256 TEXT NOT NULL,
            PRIMARY KEY (metric_receipt_id, session_ordinal),
            UNIQUE (metric_receipt_id, exchange_session_id),
            UNIQUE (metric_receipt_id, child_metric_receipt_id),
            UNIQUE (metric_receipt_id, binding_id),
            CHECK (child_metric_receipt_id <> metric_receipt_id),
            FOREIGN KEY (metric_receipt_id)
                REFERENCES opportunity_metric_receipts(metric_receipt_id),
            FOREIGN KEY (
                child_metric_receipt_id,
                child_metric_receipt_content_hash_sha256,
                child_metric_scope_key,
                child_session_report_id,
                child_session_report_content_hash_sha256
            ) REFERENCES opportunity_metric_receipts(
                metric_receipt_id, receipt_content_hash_sha256,
                scope_key,
                report_id, report_content_hash_sha256
            ),
            FOREIGN KEY (
                child_miss_receipt_id,
                child_miss_receipt_content_hash_sha256
            ) REFERENCES opportunity_miss_receipts(
                miss_receipt_id, receipt_content_hash_sha256
            )
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_metric_bindings_child_metric
        ON opportunity_metric_session_bindings(
            child_metric_receipt_id, child_metric_receipt_content_hash_sha256
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_metric_bindings_child_miss
        ON opportunity_metric_session_bindings(
            child_miss_receipt_id, child_miss_receipt_content_hash_sha256
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_metric_bindings_order
        ON opportunity_metric_session_bindings(
            metric_receipt_id, session_ordinal, exchange_session_id
        );

        CREATE TRIGGER IF NOT EXISTS opportunity_miss_receipts_no_update
        BEFORE UPDATE ON opportunity_miss_receipts BEGIN
            SELECT RAISE(ABORT, 'opportunity_miss_receipts is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_miss_receipts_no_delete
        BEFORE DELETE ON opportunity_miss_receipts BEGIN
            SELECT RAISE(ABORT, 'opportunity_miss_receipts is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_miss_records_no_update
        BEFORE UPDATE ON opportunity_miss_records BEGIN
            SELECT RAISE(ABORT, 'opportunity_miss_records is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_miss_records_no_delete
        BEFORE DELETE ON opportunity_miss_records BEGIN
            SELECT RAISE(ABORT, 'opportunity_miss_records is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_miss_run_bindings_no_update
        BEFORE UPDATE ON opportunity_miss_run_bindings BEGIN
            SELECT RAISE(ABORT, 'opportunity_miss_run_bindings is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_miss_run_bindings_no_delete
        BEFORE DELETE ON opportunity_miss_run_bindings BEGIN
            SELECT RAISE(ABORT, 'opportunity_miss_run_bindings is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_metric_receipts_no_update
        BEFORE UPDATE ON opportunity_metric_receipts BEGIN
            SELECT RAISE(ABORT, 'opportunity_metric_receipts is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_metric_receipts_no_delete
        BEFORE DELETE ON opportunity_metric_receipts BEGIN
            SELECT RAISE(ABORT, 'opportunity_metric_receipts is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_metric_session_bindings_no_update
        BEFORE UPDATE ON opportunity_metric_session_bindings BEGIN
            SELECT RAISE(ABORT, 'opportunity_metric_session_bindings is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_metric_session_bindings_no_delete
        BEFORE DELETE ON opportunity_metric_session_bindings BEGIN
            SELECT RAISE(ABORT, 'opportunity_metric_session_bindings is append-only');
        END;
        """
    )


def _migration_030_opportunity_validation(
    connection: sqlite3.Connection,
) -> None:
    """Add immutable validation bundles and database-owned locked-OOS use."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS opportunity_validation_receipts (
            validation_receipt_id TEXT PRIMARY KEY,
            receipt_content_hash_sha256 TEXT NOT NULL,
            semantic_lock_key TEXT NOT NULL,
            lock_authority_key TEXT NOT NULL,
            holdout_inventory_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'research_evidence', 'locked_oos_consumed', 'invalid_lock',
                'retrospective', 'reused', 'missing_evidence',
                'non_predeclared'
            )),
            fresh_lock_eligible INTEGER NOT NULL CHECK (
                fresh_lock_eligible IN (0, 1)
            ),
            preparation_id TEXT NOT NULL,
            preparation_content_hash_sha256 TEXT NOT NULL,
            preparation_schema_version TEXT NOT NULL CHECK (
                preparation_schema_version =
                    'v2.opportunity.chronological_validation_preparation.v1'
            ),
            preparation_json TEXT NOT NULL,
            metric_report_id TEXT NOT NULL,
            metric_report_content_hash_sha256 TEXT NOT NULL,
            metric_report_schema_version TEXT NOT NULL CHECK (
                metric_report_schema_version =
                    'v2.opportunity.validation_trading_metric_report.v1'
            ),
            metric_report_json TEXT NOT NULL,
            robustness_report_id TEXT NOT NULL,
            robustness_report_content_hash_sha256 TEXT NOT NULL,
            robustness_report_schema_version TEXT NOT NULL CHECK (
                robustness_report_schema_version =
                    'v2.opportunity.validation_robustness_report.v1'
            ),
            robustness_report_json TEXT NOT NULL,
            holdout_access_evidence_id TEXT NOT NULL,
            holdout_access_content_hash_sha256 TEXT NOT NULL,
            holdout_access_schema_version TEXT NOT NULL CHECK (
                holdout_access_schema_version =
                    'v2.opportunity.validation_holdout_access_evidence.v1'
            ),
            holdout_access_json TEXT NOT NULL,
            corpus_id TEXT NOT NULL,
            split_plan_id TEXT NOT NULL,
            split_policy_id TEXT NOT NULL,
            split_policy_content_hash_sha256 TEXT NOT NULL,
            split_policy_declared_at TEXT NOT NULL,
            code_identity TEXT NOT NULL,
            code_content_hash_sha256 TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            confirmatory_unit_id TEXT NOT NULL,
            confirmatory_unit_content_hash_sha256 TEXT NOT NULL,
            corpus_policy_id TEXT NOT NULL,
            corpus_policy_content_hash_sha256 TEXT NOT NULL,
            metric_policy_id TEXT NOT NULL,
            metric_policy_content_hash_sha256 TEXT NOT NULL,
            robustness_policy_id TEXT NOT NULL,
            robustness_policy_content_hash_sha256 TEXT NOT NULL,
            oos_session_count INTEGER NOT NULL CHECK (oos_session_count >= 0),
            oos_session_inventory_hash_sha256 TEXT NOT NULL,
            result_set_hash_sha256 TEXT NOT NULL,
            persisted_at TEXT NOT NULL,
            lifecycle_mutation_count INTEGER NOT NULL CHECK (
                lifecycle_mutation_count = 0
            ),
            take_authorization INTEGER NOT NULL CHECK (take_authorization = 0),
            research_only INTEGER NOT NULL CHECK (research_only = 1),
            promotion_eligible INTEGER NOT NULL CHECK (promotion_eligible = 0),
            database_schema_version INTEGER NOT NULL CHECK (
                database_schema_version = 30
            ),
            receipt_schema_version TEXT NOT NULL CHECK (
                receipt_schema_version =
                    'v2.opportunity.validation_persistence_receipt.v1'
            ),
            receipt_json TEXT NOT NULL,
            UNIQUE (validation_receipt_id, receipt_content_hash_sha256),
            CHECK (
                (status = 'locked_oos_consumed'
                 AND fresh_lock_eligible = 1
                 AND oos_session_count > 0)
                OR status <> 'locked_oos_consumed'
            ),
            CHECK (
                status NOT IN (
                    'invalid_lock', 'retrospective', 'reused',
                    'missing_evidence', 'non_predeclared'
                ) OR fresh_lock_eligible = 0
            )
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_validation_consumed_lock
        ON opportunity_validation_receipts(semantic_lock_key)
        WHERE status = 'locked_oos_consumed';
        CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_validation_consumed_authority
        ON opportunity_validation_receipts(lock_authority_key)
        WHERE status = 'locked_oos_consumed';
        CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_validation_consumed_inventory
        ON opportunity_validation_receipts(holdout_inventory_key)
        WHERE status = 'locked_oos_consumed';
        CREATE INDEX IF NOT EXISTS idx_opportunity_validation_receipts_preparation
        ON opportunity_validation_receipts(
            preparation_id, preparation_content_hash_sha256
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_validation_receipts_result
        ON opportunity_validation_receipts(
            metric_report_id, robustness_report_id, persisted_at
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_validation_receipts_policy
        ON opportunity_validation_receipts(
            robustness_policy_id, strategy_id, persisted_at
        );

        CREATE TABLE IF NOT EXISTS opportunity_validation_oos_sessions (
            validation_receipt_id TEXT NOT NULL,
            session_ordinal INTEGER NOT NULL CHECK (session_ordinal >= 0),
            session_source_id TEXT NOT NULL,
            session_content_hash_sha256 TEXT NOT NULL,
            exchange_session_id TEXT NOT NULL,
            session_open_at TEXT NOT NULL,
            session_close_at TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role = 'locked_oos'),
            PRIMARY KEY (validation_receipt_id, session_ordinal),
            UNIQUE (validation_receipt_id, session_source_id),
            CHECK (session_open_at < session_close_at),
            FOREIGN KEY (validation_receipt_id)
                REFERENCES opportunity_validation_receipts(validation_receipt_id)
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_validation_oos_inventory
        ON opportunity_validation_oos_sessions(
            session_source_id, session_content_hash_sha256,
            validation_receipt_id
        );
        CREATE INDEX IF NOT EXISTS idx_opportunity_validation_oos_order
        ON opportunity_validation_oos_sessions(
            validation_receipt_id, session_ordinal, session_open_at
        );

        CREATE TRIGGER IF NOT EXISTS opportunity_validation_receipts_no_update
        BEFORE UPDATE ON opportunity_validation_receipts BEGIN
            SELECT RAISE(ABORT, 'opportunity_validation_receipts is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_validation_receipts_no_delete
        BEFORE DELETE ON opportunity_validation_receipts BEGIN
            SELECT RAISE(ABORT, 'opportunity_validation_receipts is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_validation_oos_sessions_no_update
        BEFORE UPDATE ON opportunity_validation_oos_sessions BEGIN
            SELECT RAISE(ABORT, 'opportunity_validation_oos_sessions is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS opportunity_validation_oos_sessions_no_delete
        BEFORE DELETE ON opportunity_validation_oos_sessions BEGIN
            SELECT RAISE(ABORT, 'opportunity_validation_oos_sessions is append-only');
        END;
        """
    )


def _migration_031_strategy_decision_receipts(connection: sqlite3.Connection) -> None:
    """Add immutable, additive strategy decision evidence tables."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS strategy_decision_receipts (
            receipt_id TEXT PRIMARY KEY,
            receipt_hash_sha256 TEXT NOT NULL UNIQUE,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            symbol TEXT NOT NULL,
            market_date TEXT NOT NULL,
            pick_tier TEXT NOT NULL,
            research_pick_eligible INTEGER NOT NULL,
            paper_entry_eligible INTEGER NOT NULL,
            source_identity TEXT NOT NULL,
            input_hash_sha256 TEXT NOT NULL,
            canonical_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_decision_receipts_lookup
        ON strategy_decision_receipts(market_date, strategy_id, symbol, pick_tier);
        CREATE TABLE IF NOT EXISTS strategy_condition_results (
            receipt_id TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            status TEXT NOT NULL,
            source_urls_json TEXT NOT NULL,
            source_hashes_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (receipt_id, condition_id),
            FOREIGN KEY (receipt_id) REFERENCES strategy_decision_receipts(receipt_id)
        );
        CREATE TABLE IF NOT EXISTS strategy_evidence_claims (
            claim_id TEXT PRIMARY KEY,
            receipt_id TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            source_urls_json TEXT NOT NULL,
            source_hashes_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY (receipt_id) REFERENCES strategy_decision_receipts(receipt_id)
        );
        CREATE TABLE IF NOT EXISTS strategy_evidence_resolution_runs (
            run_id TEXT PRIMARY KEY,
            receipt_id TEXT,
            symbol TEXT NOT NULL,
            market_date TEXT NOT NULL,
            requested_model TEXT NOT NULL,
            actual_model TEXT NOT NULL,
            response_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY (receipt_id) REFERENCES strategy_decision_receipts(receipt_id)
        );
        CREATE TRIGGER IF NOT EXISTS strategy_decision_receipts_no_update
        BEFORE UPDATE ON strategy_decision_receipts BEGIN
            SELECT RAISE(ABORT, 'strategy_decision_receipts is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS strategy_decision_receipts_no_delete
        BEFORE DELETE ON strategy_decision_receipts BEGIN
            SELECT RAISE(ABORT, 'strategy_decision_receipts is append-only');
        END;
        CREATE INDEX IF NOT EXISTS idx_strategy_condition_results_receipt
        ON strategy_condition_results(receipt_id, condition_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_evidence_claims_receipt
        ON strategy_evidence_claims(receipt_id, condition_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_evidence_runs_receipt
        ON strategy_evidence_resolution_runs(receipt_id, market_date);
        CREATE TRIGGER IF NOT EXISTS strategy_condition_results_no_update
        BEFORE UPDATE ON strategy_condition_results BEGIN
            SELECT RAISE(ABORT, 'strategy_condition_results is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS strategy_condition_results_no_delete
        BEFORE DELETE ON strategy_condition_results BEGIN
            SELECT RAISE(ABORT, 'strategy_condition_results is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS strategy_evidence_claims_no_update
        BEFORE UPDATE ON strategy_evidence_claims BEGIN
            SELECT RAISE(ABORT, 'strategy_evidence_claims is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS strategy_evidence_claims_no_delete
        BEFORE DELETE ON strategy_evidence_claims BEGIN
            SELECT RAISE(ABORT, 'strategy_evidence_claims is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS strategy_evidence_resolution_runs_no_update
        BEFORE UPDATE ON strategy_evidence_resolution_runs BEGIN
            SELECT RAISE(ABORT, 'strategy_evidence_resolution_runs is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS strategy_evidence_resolution_runs_no_delete
        BEFORE DELETE ON strategy_evidence_resolution_runs BEGIN
            SELECT RAISE(ABORT, 'strategy_evidence_resolution_runs is append-only');
        END;
        """
    )


def _migration_032_v6_decision_availability(connection: sqlite3.Connection) -> None:
    """Add the immutable V6 persisted-availability boundary sidecar."""

    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alpha_v6_decisions'"
    ).fetchone():
        _add_column_if_missing(
            connection, "alpha_v6_decisions", "stored_at TEXT NOT NULL DEFAULT ''"
        )


def _migration_033_research_episode_outcome_bridges(connection: sqlite3.Connection) -> None:
    """Add immutable selection-only research episode outcome joins."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS research_episode_outcome_bridges (
            bridge_id TEXT PRIMARY KEY,
            bridge_hash_sha256 TEXT NOT NULL UNIQUE,
            selection_id TEXT NOT NULL,
            slate_id TEXT NOT NULL,
            slate_content_hash_sha256 TEXT NOT NULL,
            episode_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            market_date TEXT NOT NULL,
            selected_at TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            receipt_id TEXT NOT NULL,
            receipt_hash_sha256 TEXT NOT NULL,
            outcome_status TEXT NOT NULL,
            learning_eligible INTEGER NOT NULL,
            source_observation_id TEXT,
            source_observation_hash_sha256 TEXT,
            source_path_id TEXT,
            source_path_hash_sha256 TEXT,
            source_cutoff TEXT,
            outcome_artifact_id TEXT,
            outcome_artifact_hash_sha256 TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_research_episode_outcome_bridges_selection
        ON research_episode_outcome_bridges(market_date, selection_id, ticker);
        CREATE INDEX IF NOT EXISTS idx_research_episode_outcome_bridges_receipt
        ON research_episode_outcome_bridges(market_date, receipt_id);
        CREATE TRIGGER IF NOT EXISTS research_episode_outcome_bridges_no_update
        BEFORE UPDATE ON research_episode_outcome_bridges BEGIN
            SELECT RAISE(ABORT, 'research_episode_outcome_bridges is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS research_episode_outcome_bridges_no_delete
        BEFORE DELETE ON research_episode_outcome_bridges BEGIN
            SELECT RAISE(ABORT, 'research_episode_outcome_bridges is append-only');
        END;
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
    (17, _migration_017_alphaops_v6_one_time_holdout),
    (18, _migration_018_alphaops_v6_operational_receipts),
    (19, _migration_019_account_comparison_contract),
    (20, _migration_020_scenario_lifecycle_identity),
    (21, _migration_021_official_strategy_cohort_lock),
    (22, _migration_022_intraday_evidence_spine),
    (23, _migration_023_alpha_path_replay_reconciliations),
    (24, _migration_024_catalyst_evidence),
    (25, _migration_025_v6_evidence_lineage),
    (26, _migration_026_trade_attribution_evidence),
    (27, _migration_027_opportunity_pipeline_runs),
    (28, _migration_028_opportunity_outcomes),
    (29, _migration_029_opportunity_research),
    (30, _migration_030_opportunity_validation),
    (31, _migration_031_strategy_decision_receipts),
    (32, _migration_032_v6_decision_availability),
    (33, _migration_033_research_episode_outcome_bridges),
)
