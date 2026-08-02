from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from intraday_scanner.services.alpha_alert_replay_service import (
    replay_alpha_alert_history,
    write_alpha_alert_replay_report,
)


def test_replay_blocks_legacy_alertable_losses_using_decision_inputs_only(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.sqlite"
    _write_legacy_rows(db_path, close_prices=[9.0, 8.0, 7.0, 6.0, 5.0])
    before = db_path.read_bytes()

    report = replay_alpha_alert_history(db_path=db_path)

    assert db_path.read_bytes() == before
    assert report["status"] == "PASS"
    assert report["contract"]["decision_time_only"] is True
    assert report["contract"]["outcomes_used_for_decision"] is False
    assert report["summary"] == {
        "signal_count": 5,
        "stored_alertable_count": 5,
        "replay_alertable_count": 0,
        "legacy_alert_truth_mismatch_count": 5,
        "gross_close_eligible_count": 5,
        "gross_close_loss_count": 5,
        "gross_close_losses_replay_blocked_count": 5,
        "gross_close_losses_replay_unblocked_count": 0,
    }
    assert all(row["replay_blocked_decision_time"] for row in report["records"])
    assert all(
        "edge bucket below alert threshold" in row["replay_block_reasons"]
        for row in report["records"]
    )


def test_replay_decision_is_unchanged_when_only_future_outcomes_change(
    tmp_path: Path,
) -> None:
    losing_db = tmp_path / "losing.sqlite"
    winning_db = tmp_path / "winning.sqlite"
    _write_legacy_rows(losing_db, close_prices=[9.0])
    _write_legacy_rows(winning_db, close_prices=[11.0])

    losing = replay_alpha_alert_history(db_path=losing_db)["records"][0]
    winning = replay_alpha_alert_history(db_path=winning_db)["records"][0]

    assert losing["decision_input_hash"] == winning["decision_input_hash"]
    assert losing["replay_alert_gate_status"] == winning["replay_alert_gate_status"]
    assert losing["replay_can_alert"] == winning["replay_can_alert"] is False
    assert losing["is_gross_close_loss"] is True
    assert winning["is_gross_close_loss"] is False


def test_replay_writes_a_machine_readable_artifact(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    out_path = tmp_path / "reports" / "alert-replay.json"
    _write_legacy_rows(db_path, close_prices=[9.0])

    result = write_alpha_alert_replay_report(db_path=db_path, out_path=out_path)

    assert result["artifact_path"] == str(out_path)
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["status"] == "PASS"
    assert written["summary"]["gross_close_losses_replay_blocked_count"] == 1


def _write_legacy_rows(db_path: Path, *, close_prices: list[float]) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE alpha_signals (
                signal_key TEXT PRIMARY KEY,
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
            CREATE TABLE signal_outcomes (
                signal_id TEXT PRIMARY KEY,
                outcome_status TEXT NOT NULL,
                entry_price REAL,
                close_price REAL
            );
            """
        )
        for index, close_price in enumerate(close_prices, start=1):
            signal_id = f"legacy-scan:{index}:LOSS{index}"
            payload = {
                "signal_key": signal_id,
                "scan_id": "legacy-scan",
                "ticker": f"LOSS{index}",
                "rank": index,
                "timestamp": "2026-07-30T13:10:00Z",
                "alpha_score": 50.0,
                "can_alert": True,
                "no_trade_reason": "",
                "alert_gate_status": "NEEDS_CONFIRMATION",
                "source_confidence": 34.5,
                "source_count": 2,
                "data_quality_score": 75.0,
                "edge_bucket": "LOW",
                "confidence_bucket": "INSUFFICIENT_SAMPLE",
                "setup_grade": "C",
                "catalyst_confidence": 0.2,
                "price": 10.0,
                "volume": 100_000,
                "previous_close": 8.0,
                "float_shares": 1_000_000,
                "premarket_high": 10.0,
                "premarket_low": 9.0,
                "halt_status": "CLEAR",
                "sec_risk_status": "CLEAR",
                "corporate_action_status": "CLEAR",
                "source_quality_status": "CLEAR",
            }
            connection.execute(
                """
                INSERT INTO alpha_signals
                (signal_key, scan_id, ticker, rank, timestamp, alpha_score,
                 edge_bucket, confidence_bucket, can_alert, no_trade_reason, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    "legacy-scan",
                    f"LOSS{index}",
                    index,
                    "2026-07-30T13:10:00Z",
                    50.0,
                    "LOW",
                    "INSUFFICIENT_SAMPLE",
                    1,
                    "",
                    json.dumps(payload, sort_keys=True),
                ),
            )
            connection.execute(
                """
                INSERT INTO signal_outcomes
                (signal_id, outcome_status, entry_price, close_price)
                VALUES (?, 'complete_sourced', 10.0, ?)
                """,
                (signal_id, close_price),
            )
