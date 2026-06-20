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
        "recorded_at": result.created_at,
        "rank": row.get("rank"),
        "ticker": row.get("ticker"),
        "score": row.get("score"),
        "component_scores": row.get("score_breakdown"),
        "thesis": _thesis(row),
        "catalyst_summary": row.get("catalyst_headline") or "No catalyst headline available.",
        "catalyst_url": row.get("catalyst_url") or "",
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
