"""SQLite storage adapter."""

from __future__ import annotations

import json
import sqlite3
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
                    """
                )
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
