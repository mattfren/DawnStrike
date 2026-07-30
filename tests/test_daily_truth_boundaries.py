import json
import sqlite3
from pathlib import Path

from intraday_scanner.performance.service import CanonicalPerformanceService
from intraday_scanner.performance.snapshot import write_public_snapshot
from intraday_scanner.services.daily_finalize_service import DailyFinalizeService
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_public_snapshot_is_as_of_and_has_no_local_path(tmp_path: Path) -> None:
    db_path = tmp_path / "truth.sqlite"
    SQLiteScanStore(db_path).initialize()
    with sqlite3.connect(db_path) as connection:
        for market_date, signal_id in (("2026-07-29", "signal-old"), ("2026-07-30", "signal-new")):
            connection.execute(
                """
                INSERT INTO historical_signals
                (signal_id, scan_id, generated_at, market_date, ticker, signal_label,
                 risk_flags_json, avoid_reasons_json, raw_payload_json)
                VALUES (?, ?, ?, ?, ?, ?, '[]', '[]', '{}')
                """,
                (
                    signal_id,
                    "scan-" + signal_id,
                    f"{market_date}T13:00:00+00:00",
                    market_date,
                    "NOVA",
                    "WATCH",
                ),
            )

    CanonicalPerformanceService(db_path).reconcile(now="2026-07-30T21:00:00+00:00")
    result = write_public_snapshot(
        db_path,
        tmp_path / "public" / "performance.json",
        market_date="2026-07-29",
    )
    payload = json.loads((tmp_path / "public" / "performance.json").read_text(encoding="utf-8"))

    assert payload["as_of_market_date"] == "2026-07-29"
    assert {row["market_date"] for row in payload["daily"]} == {"2026-07-29"}
    assert result["manifest"]["artifact_path"] == "performance.json"
    assert ":\\" not in json.dumps(result["manifest"])


def test_upstream_receipt_populates_stage_manifest(tmp_path: Path) -> None:
    db_path = tmp_path / "receipt.sqlite"
    SQLiteScanStore(db_path).initialize()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO automation_runs
            (id, run_type, status, started_at, completed_at, out_dir, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "eod-1",
                "alphaops_eod",
                "complete",
                "2026-07-29T20:00:00+00:00",
                "2026-07-29T20:05:00+00:00",
                "outputs/alpha_report",
                json.dumps(
                    {
                        "market_date": "2026-07-29",
                        "status": "complete",
                        "stages": [
                            {"stage": name, "status": "complete"}
                            for name in (
                                "source_collection",
                                "candidate_normalization",
                                "selection",
                                "delivery",
                                "paper_fills",
                                "outcome_capture",
                            )
                        ],
                    }
                ),
            ),
        )

    result = DailyFinalizeService(db_path, tmp_path / "public").run(
        market_date="2026-07-29", now="2026-07-29T21:00:00+00:00"
    )
    source_stage = next(
        item for item in result["stage_manifest"]["stages"] if item["stage"] == "source_collection"
    )
    assert result["upstream_status"] == "complete"
    assert source_stage["status"] == "LOCAL_VERIFIED"
    assert (tmp_path / "public" / "daily-finalize.jsonl").exists()
