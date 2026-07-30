import json
import sqlite3
from pathlib import Path

from intraday_scanner.performance.service import CanonicalPerformanceService
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_read_only_reconcile_does_not_create_canonical_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "readonly.sqlite"
    SQLiteScanStore(db_path).initialize()
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TABLE IF EXISTS portfolio_daily_performance")
        connection.execute("DROP TABLE IF EXISTS portfolio_performance_rows")
        before = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'portfolio_%'"
        ).fetchall()
    result = CanonicalPerformanceService(db_path).reconcile(persist=False)
    with sqlite3.connect(db_path) as connection:
        after = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'portfolio_%'"
        ).fetchall()
    assert result["row_count"] == 0
    assert after == before


def test_as_of_reconcile_includes_history_through_requested_date(tmp_path: Path) -> None:
    db_path = tmp_path / "as-of.sqlite"
    SQLiteScanStore(db_path).initialize()
    with sqlite3.connect(db_path) as connection:
        for signal_id, market_date in (
            ("old", "2026-07-28"),
            ("current", "2026-07-29"),
            ("future", "2026-07-30"),
        ):
            connection.execute(
                """
                INSERT INTO historical_signals
                (signal_id, scan_id, generated_at, market_date, ticker, signal_label,
                 risk_flags_json, avoid_reasons_json, raw_payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    f"scan-{signal_id}",
                    f"{market_date}T13:00:00+00:00",
                    market_date,
                    "NOVA",
                    "WATCH",
                    "[]",
                    "[]",
                    json.dumps({"source_url": f"https://example.test/{signal_id}"}),
                ),
            )

    result = CanonicalPerformanceService(db_path).reconcile(
        market_date="2026-07-29", persist=False
    )

    assert {row["market_date"] for row in result["rows"]} == {"2026-07-28", "2026-07-29"}
