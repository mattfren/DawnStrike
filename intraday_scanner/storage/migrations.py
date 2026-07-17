"""Additive SQLite migrations for Dawnstrike storage."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

CURRENT_SCHEMA_VERSION = 4

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


MIGRATIONS: tuple[tuple[int, Migration], ...] = (
    (1, _migration_001_benchmark_tables),
    (2, _migration_002_signal_selection_delivery_identity),
    (3, _migration_003_strategy_paper_reconciliation),
    (4, _migration_004_scorecard_session_truth),
)
