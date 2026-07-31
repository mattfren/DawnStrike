import json
import sqlite3
from pathlib import Path

from intraday_scanner.services.outcome_gap_service import outcome_gap_report
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_terminal_missing_outcome_stays_gap_and_never_learning_eligible(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "truth.sqlite"
    store = SQLiteScanStore(db_path)
    store.initialize()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO historical_signals
            (signal_id, scan_id, generated_at, market_date, ticker,
             signal_label, risk_flags_json, avoid_reasons_json,
             raw_payload_json)
            VALUES (?, ?, ?, ?, ?, ?, '[]', '[]', ?)
            """,
            (
                "signal-gap",
                "scan-gap",
                "2026-07-31T13:00:00+00:00",
                "2026-07-31",
                "BOOM",
                "WATCH",
                json.dumps({"can_alert": True}),
            ),
        )
    store.persist_outcome_capture_attempts(
        [
            {
                "attempt_id": "attempt-gap",
                "run_id": "capture-gap",
                "signal_id": "signal-gap",
                "market_date": "2026-07-31",
                "ticker": "BOOM",
                "status": "terminal_missing",
                "terminal": True,
                "learning_eligible": False,
                "provider_chain": ["yahoo", "alpaca"],
                "attempted_at": "2026-07-31T21:00:00+00:00",
                "error_code": "providers_exhausted",
                "error_detail": "No sourced close.",
            }
        ]
    )

    result = outcome_gap_report(
        db_path=db_path,
        market_date="2026-07-31",
    )

    assert result["status"] == "DEGRADED"
    assert result["missing_outcome_count"] == 1
    assert result["terminal_missing_count"] == 1
    assert result["learning_eligible_missing_count"] == 0
    assert result["missing_truth_is_zero"] is False
    assert result["gaps"][0]["error_code"] == "providers_exhausted"


def test_no_eligible_candidates_is_not_reported_as_zero_return(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "empty.sqlite"

    result = outcome_gap_report(
        db_path=db_path,
        market_date="2026-07-31",
    )

    assert result["status"] == "NO_ELIGIBLE"
    assert result["eligible_candidate_count"] == 0
    assert result["missing_truth_is_zero"] is False
