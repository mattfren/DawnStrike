from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from intraday_scanner.cli import main
from intraday_scanner.dashboard.data_loader import (
    load_calendar_daily_returns,
    load_calendar_day_detail,
    load_calendar_days,
    load_calendar_equity_curve,
    load_calendar_missing_outcomes,
    load_sqlite,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

DAY = "2026-06-20"


def _signal(
    ticker: str,
    rank: int,
    *,
    scan_id: str = "scan-audited",
    day: str = DAY,
    alpha_score: float = 80.0,
    can_alert: bool = True,
    no_trade_reason: str = "",
) -> dict[str, object]:
    return {
        "scan_id": scan_id,
        "signal_key": f"{scan_id}:{rank}:{ticker}",
        "ticker": ticker,
        "rank": rank,
        "timestamp": f"{day}T13:30:00+00:00",
        "alpha_score": alpha_score,
        "total_score": alpha_score,
        "edge_bucket": "HIGH",
        "confidence_bucket": "EARLY",
        "can_alert": can_alert,
        "no_trade_reason": no_trade_reason,
        "company": f"{ticker} Inc",
        "setup_key": "grade:A|gap:clean",
        "risk_score": 20,
        "source_confidence": 88,
        "gap_pct": 52.5,
        "premarket_price": 5.0,
        "entry_trigger": 5.1,
        "breakout_trigger": 5.1,
        "invalidation": 4.7,
        "invalidation_level": 4.7,
        "target_1": 5.9,
        "first_target": 5.9,
        "catalyst_summary": "Fresh catalyst",
        "catalyst_category": "news",
        "risk_flags": "",
        "source": "manual",
        "data_source_kind": "manual",
        "manual_uploaded_data": True,
    }


def _audit(
    ticker: str,
    rank: int,
    close_return: float,
    *,
    scan_id: str = "scan-audited",
    entry_price: float = 5.0,
) -> dict[str, object]:
    return {
        "scan_id": scan_id,
        "ticker": ticker,
        "rank": rank,
        "audit_status": "audited",
        "recommendation_timestamp": f"{DAY}T13:30:00+00:00",
        "entry_time": f"{DAY}T13:31:00+00:00",
        "entry_price": entry_price,
        "return_1m_pct": close_return / 4,
        "return_5m_pct": close_return / 2,
        "return_15m_pct": close_return * 0.75,
        "lunch_return_pct": close_return - 1,
        "close_return_pct": close_return,
        "high_return_pct": close_return + 4,
        "low_drawdown_pct": -2.0,
        "source": "manual_fixture",
    }


def _seed_audited_day(db_path: Path) -> SQLiteScanStore:
    store = SQLiteScanStore(db_path)
    store.persist_alpha_signals(
        [
            _signal("NOVA", 1, alpha_score=90),
            _signal("RIFT", 2, alpha_score=70),
            _signal("MOON", 3, alpha_score=50),
        ]
    )
    store.record_notification(
        event_key="alphaops:scan-audited:telegram",
        channel="telegram",
        run_id="scan-audited",
        payload={
            "title": "Dawnstrike Alpha Check",
            "telegram_compact_message": "Watchlist saved for manual review.",
        },
    )
    store.persist_manual_audit(
        {"created_at": f"{DAY}T20:30:00+00:00", "trade_count": 3},
        [
            _audit("NOVA", 1, 6.0),
            _audit("RIFT", 2, 3.0),
            _audit("MOON", 3, -3.0),
        ],
    )
    return store


def test_calendar_empty_database_and_missing_tables_are_safe(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.sqlite"
    db_path.write_bytes(b"")

    days = load_calendar_days(db_path, DAY, DAY)
    detail = load_calendar_day_detail(db_path, DAY)

    assert days[0]["status"] == "NO DATA"
    assert days[0]["top1_return"] is None
    assert detail["return_rows"] == []
    assert any("Missing optional calendar tables" in item for item in detail["warnings"])


def test_calendar_pending_no_trade_and_audited_statuses(tmp_path: Path) -> None:
    db_path = tmp_path / "calendar.sqlite"
    store = _seed_audited_day(db_path)
    pending_day = "2026-06-21"
    no_trade_day = "2026-06-22"
    store.persist_alpha_signals(
        [_signal("WAIT", 1, scan_id="scan-pending", day=pending_day, alpha_score=61)],
        replace=False,
    )
    store.persist_alpha_signals(
        [
            _signal(
                "SKIP",
                1,
                scan_id="scan-no-trade",
                day=no_trade_day,
                can_alert=False,
                no_trade_reason="No clean edge",
            )
        ],
        replace=False,
    )

    days = {row["date"]: row for row in load_calendar_days(db_path, DAY, no_trade_day)}
    pending_detail = load_calendar_day_detail(db_path, pending_day)
    no_trade_detail = load_calendar_day_detail(db_path, no_trade_day)

    assert days[DAY]["status"] == "AUDITED"
    assert days[pending_day]["status"] == "PICKS PENDING OUTCOMES"
    assert days[no_trade_day]["status"] == "NO TRADE"
    assert pending_detail["return_rows"][0]["audit_status"] == "Outcome needed"
    assert pending_detail["return_rows"][0]["close_return"] is None
    assert pending_detail["missing_outcomes"][0]["audit_status"] == "Outcome needed"
    assert no_trade_detail["missing_outcome_count"] == 0


def test_calendar_equal_weight_math_and_compounded_curve(tmp_path: Path) -> None:
    db_path = tmp_path / "calendar.sqlite"
    _seed_audited_day(db_path)

    daily = load_calendar_daily_returns(db_path, DAY, DAY)[0]
    curve = load_calendar_equity_curve(db_path, DAY, DAY)[0]
    detail = load_calendar_day_detail(db_path, DAY)

    assert daily["top1_close_return"] == 6.0
    assert daily["top3_close_return"] == 2.0
    assert daily["top5_close_return"] == 2.0
    assert curve["top1_compounded_return"] == 6.0
    assert curve["top3_compounded_return"] == 2.0
    assert detail["basket_returns"]["return_note"].startswith("Scenario returns")
    assert detail["return_rows"][0]["recommended_exit_policy"] == "not_recorded"
    assert detail["return_rows"][0]["recommended_exit_return"] is None
    assert detail["return_rows"][0]["high_after_entry_label"] == "opportunity, not realized"


def test_calendar_monitor_exit_uses_saved_monitor_event_only(tmp_path: Path) -> None:
    db_path = tmp_path / "calendar.sqlite"
    store = SQLiteScanStore(db_path)
    store.persist_alpha_signals([_signal("NOVA", 1)])
    store.persist_manual_audit(
        {"created_at": f"{DAY}T20:30:00+00:00", "trade_count": 1},
        [_audit("NOVA", 1, 4.0, entry_price=5.0)],
    )
    store.persist_monitor_events(
        [
            {
                "ticker": "NOVA",
                "event_type": "invalidated",
                "severity": "critical",
                "created_at": f"{DAY}T14:00:00+00:00",
                "current_price": 4.75,
            }
        ],
        run_id="scan-audited",
    )

    row = load_calendar_day_detail(db_path, DAY)["return_rows"][0]

    assert row["recommended_exit_policy"] == "monitor_exit_signal"
    assert row["recommended_exit_return"] == -5.0
    assert row["monitor_exit_return"] == -5.0


def test_calendar_load_sqlite_includes_calendar_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "calendar.sqlite"
    _seed_audited_day(db_path)

    data = load_sqlite(db_path)

    assert data["calendar_start_date"] <= DAY <= data["calendar_end_date"]
    assert data["calendar_days"]
    assert data["calendar_daily_returns"]
    assert data["calendar_equity_curve"]
    assert "calendar_missing_outcomes" in data


def test_calendar_reads_optional_historical_tables_when_present(tmp_path: Path) -> None:
    db_path = tmp_path / "optional.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE recommendation_theses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                rank INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE source_reliability (
                source TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                reliability_score REAL NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE alpha_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE performance_cumulative (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE manual_audit_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO recommendation_theses
            (run_id, ticker, rank, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "thesis-run",
                "THES",
                1,
                f"{DAY}T13:30:00+00:00",
                json.dumps(
                    {
                        "run_id": "thesis-run",
                        "ticker": "THES",
                        "rank": 1,
                        "created_at": f"{DAY}T13:30:00+00:00",
                        "company": "Thesis Co",
                        "score": 77,
                        "breakout_trigger": 5.1,
                        "invalidation_level": 4.7,
                        "first_target": 5.9,
                        "source": "manual",
                        "data_source_kind": "manual",
                    }
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO source_reliability
            (source, updated_at, reliability_score, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                "manual",
                f"{DAY}T20:00:00+00:00",
                88.0,
                json.dumps({"source": "manual", "reliability_score": 88.0}),
            ),
        )
        connection.execute(
            "INSERT INTO alpha_reports (report_date, payload_json) VALUES (?, ?)",
            (DAY, json.dumps({"status": "complete", "real_days": 3})),
        )
        connection.execute(
            "INSERT INTO performance_cumulative (created_at, payload_json) VALUES (?, ?)",
            (f"{DAY}T20:00:00+00:00", json.dumps({"top3_compounded_return": 4.2})),
        )
        connection.execute(
            "INSERT INTO manual_audit_summary (created_at, payload_json) VALUES (?, ?)",
            (f"{DAY}T20:30:00+00:00", json.dumps({"trade_count": 0})),
        )

    detail = load_calendar_day_detail(db_path, DAY)

    assert detail["status"] == "PICKS PENDING OUTCOMES"
    assert detail["picks"][0]["ticker"] == "THES"
    assert detail["recommendation_theses"][0]["ticker"] == "THES"
    assert detail["source_reliability"]["manual"]["reliability_score"] == 88.0
    assert detail["alpha_reports"][0]["status"] == "complete"
    assert detail["performance_cumulative"][0]["top3_compounded_return"] == 4.2
    assert detail["manual_audit_summary"][0]["trade_count"] == 0


def test_calendar_cli_writes_report_outputs_and_redacts_secret_text(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = tmp_path / "calendar.sqlite"
    out_dir = tmp_path / "report"
    store = _seed_audited_day(db_path)
    store.record_notification(
        event_key="alphaops:scan-audited:telegram-secret-marker",
        channel="telegram",
        run_id="scan-audited",
        payload={
            "message": "telegram_bot_token=SECRET_TOKEN telegram_chat_id=SECRET_CHAT",
        },
    )

    status = main(
        [
            "calendar-report",
            "--db-path",
            str(db_path),
            "--out-dir",
            str(out_dir),
            "--month",
            "2026-06",
        ]
    )
    captured = capsys.readouterr()
    report_json = json.loads(captured.out)
    details_text = (out_dir / "calendar_day_details.json").read_text()
    markdown_text = (out_dir / "calendar_report.md").read_text()

    assert status == 0
    assert (out_dir / "calendar_days.csv").exists()
    assert (out_dir / "calendar_equity_curve.csv").exists()
    assert (out_dir / "missing_outcomes.csv").exists()
    assert report_json["status"] == "complete"
    assert "SECRET_TOKEN" not in captured.out
    assert "SECRET_CHAT" not in details_text
    assert "SECRET_TOKEN" not in markdown_text


def test_calendar_missing_outcomes_list_never_zeroes_missing_returns(tmp_path: Path) -> None:
    db_path = tmp_path / "calendar.sqlite"
    store = SQLiteScanStore(db_path)
    store.persist_alpha_signals([_signal("WAIT", 1, scan_id="scan-pending")])

    detail = load_calendar_day_detail(db_path, DAY)
    missing = load_calendar_missing_outcomes(db_path, DAY, DAY)

    assert detail["status"] == "PICKS PENDING OUTCOMES"
    assert detail["top1_return"] is None
    assert detail["return_rows"][0]["close_return"] is None
    assert missing[0]["ticker"] == "WAIT"
    assert missing[0]["audit_status"] == "Outcome needed"


def test_calendar_code_does_not_add_order_execution() -> None:
    checked_paths = [
        Path("intraday_scanner/dashboard/data_loader.py"),
        Path("intraday_scanner/services/calendar_report_service.py"),
        Path("app.py"),
    ]
    forbidden = [
        "TradingClient",
        "submit_order",
        "place_order",
        "create_order",
        "market_order",
        "limit_order",
        "execute_trade",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked_paths)

    assert not any(term in combined for term in forbidden)
