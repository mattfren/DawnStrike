from __future__ import annotations

import csv
from pathlib import Path

from intraday_scanner.cli import main
from intraday_scanner.dashboard.data_loader import load_calendar_day_detail
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.services.free_shadow_mode import import_manual_outcomes
from intraday_scanner.services.return_attribution_service import (
    record_alpha_historical_signals,
    record_no_trade_historical_signal,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

DAY = "2026-06-20"
SIGNAL_TIME = f"{DAY}T13:30:00+00:00"
FIXTURE_CONFIG = Path("tests/fixtures/web_sources_fixture.yaml")


def test_alpha_cycle_creates_historical_signal_and_links_notification(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.sqlite"
    out_dir = tmp_path / "alpha"

    assert (
        main(
            [
                "alpha-cycle",
                "--config",
                str(FIXTURE_CONFIG),
                "--db-path",
                str(db_path),
                "--out-dir",
                str(out_dir),
                "--notify",
                "console",
            ]
        )
        == 0
    )

    store = SQLiteScanStore(db_path)
    signals = store.load_historical_signals(limit=20)
    events = store.load_signal_events(limit=100)

    assert signals
    assert signals[0]["signal_label"] in {"BREAKOUT WATCH", "ENTRY WATCH", "WATCH ONLY"}
    assert signals[0]["entry_watch_level"] is not None
    assert signals[0]["telegram_event_key"]
    assert any(row["event_type"] == "ENTRY_WATCH_CREATED" for row in events)
    assert any(row["event_type"] == "TELEGRAM_SENT" for row in events)


def test_no_trade_historical_signal_creates_no_trade_calendar_day(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "ledger.sqlite")
    record_no_trade_historical_signal(
        store,
        scan_id="scan-no-edge",
        generated_at=SIGNAL_TIME,
        reason="No clean edge",
        source_summary={"candidate_count": 0, "source": "fixture"},
        candidate_count=0,
    )

    detail = load_calendar_day_detail(tmp_path / "ledger.sqlite", DAY)

    assert detail["status"] == "NO TRADE"
    assert detail["picks"][0]["label/action"] == "NO CLEAN EDGE"
    assert detail["missing_outcome_count"] == 0


def test_manual_outcome_import_matches_historical_signal_and_rejects_early(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_signals(store, [("NOVA", 1, 10.0)])
    good = tmp_path / "outcomes.csv"
    _write_outcomes(good, [{"ticker": "NOVA", "entry_price": 10.0, "close_price": 11.0}])

    result = import_manual_outcomes(input_path=good, store=store, persist=True)
    outcomes = store.load_signal_outcomes()
    events = store.load_signal_events()

    assert result["accepted_count"] == 1
    assert outcomes[0]["signal_id"] == "scan-ledger:1:NOVA"
    assert outcomes[0]["validated_against_signal_timestamp"] is True
    assert any(row["event_type"] == "OUTCOME_IMPORTED" for row in events)

    early = tmp_path / "early.csv"
    _write_outcomes(
        early,
        [
            {
                "ticker": "NOVA",
                "entry_time": f"{DAY}T13:20:00+00:00",
                "entry_price": 10.0,
            }
        ],
    )

    try:
        import_manual_outcomes(input_path=early, store=store, persist=True)
    except SnapshotValidationError as exc:
        assert "No valid outcome rows imported" in str(exc)
    else:
        raise AssertionError("Expected before-signal outcome to be rejected")
    assert (tmp_path / "rejected_outcomes.csv").exists()
    assert "before recommendation" in (tmp_path / "rejected_outcomes.csv").read_text()


def test_return_attribution_math_missing_values_and_historical_report(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.sqlite"
    out_dir = tmp_path / "attrib"
    report_dir = tmp_path / "report"
    store = SQLiteScanStore(db_path)
    _seed_signals(
        store,
        [
            ("NOVA", 1, 10.0),
            ("RIFT", 2, 20.0),
            ("MOON", 3, 30.0),
        ],
    )
    outcomes = tmp_path / "outcomes.csv"
    _write_outcomes(
        outcomes,
        [
            {"ticker": "NOVA", "entry_price": 10.0, "price_5m": 10.5, "close_price": 11.0},
            {"ticker": "RIFT", "entry_price": 20.0, "price_5m": 19.0, "close_price": 18.0},
            {"ticker": "MOON", "entry_price": 30.0, "price_5m": 33.0, "close_price": 36.0},
        ],
    )
    imported = import_manual_outcomes(input_path=outcomes, store=store, persist=True)
    assert imported["accepted_count"] == 3

    assert (
        main(
            [
                "attribute-returns",
                "--db-path",
                str(db_path),
                "--out-dir",
                str(out_dir),
                "--persist",
            ]
        )
        == 0
    )
    daily = store.load_daily_signal_performance()
    attribution = store.load_signal_return_attribution()

    assert daily[0]["top1_close_return"] == 10.0
    assert daily[0]["top3_close_return"] == 6.6667
    assert daily[0]["evidence_status"] == "Not enough history yet."
    assert any(
        row["exit_policy"] == "high_opportunity"
        and row["scenario_or_recommended"] == "scenario"
        for row in attribution
    )
    assert any(row["entry_policy"] == "trigger_touch" for row in attribution)
    assert (out_dir / "cumulative_equity_curve.csv").exists()

    assert (
        main(
            [
                "historical-report",
                "--db-path",
                str(db_path),
                "--out-dir",
                str(report_dir),
            ]
        )
        == 0
    )
    assert (report_dir / "historical_signals.csv").exists()
    assert (report_dir / "accuracy_by_setup.csv").exists()
    assert (report_dir / "accuracy_by_source.csv").exists()
    assert (report_dir / "accuracy_by_score_bucket.csv").exists()
    assert "Not enough history yet" in (report_dir / "historical_report.md").read_text()


def test_partial_outcome_does_not_become_zero_and_calendar_statuses(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_signals(store, [("NOVA", 1, 10.0)])
    partial = tmp_path / "partial.csv"
    _write_outcomes(
        partial,
        [{"ticker": "NOVA", "entry_price": 10.0, "price_5m": 10.5, "close_price": ""}],
    )
    import_manual_outcomes(input_path=partial, store=store, persist=True)

    assert (
        main(
            [
                "attribute-returns",
                "--db-path",
                str(db_path),
                "--out-dir",
                str(tmp_path / "attrib"),
                "--persist",
            ]
        )
        == 0
    )
    rows = store.load_signal_return_attribution()
    close = next(row for row in rows if row["exit_policy"] == "close")
    detail = load_calendar_day_detail(db_path, DAY)

    assert close["return_pct"] is None
    assert close["audit_status"] == "unavailable"
    assert detail["status"] == "OUTCOMES PARTIAL"
    assert detail["return_rows"][0]["close_return"] is None

    pending_db = tmp_path / "pending.sqlite"
    _seed_signals(SQLiteScanStore(pending_db), [("WAIT", 1, 10.0)])
    pending = load_calendar_day_detail(pending_db, DAY)
    assert pending["status"] == "PICKS PENDING OUTCOMES"
    assert pending["missing_outcomes"][0]["audit_status"] == "Outcome needed"


def test_historical_outputs_redact_secret_text_and_implementation_has_no_execution_path(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_signals(store, [("NOVA", 1, 10.0)])
    outcomes = tmp_path / "outcomes.csv"
    _write_outcomes(
        outcomes,
        [
            {
                "ticker": "NOVA",
                "entry_price": 10.0,
                "close_price": 11.0,
                "notes": "telegram_bot_token=SECRET_TOKEN",
            }
        ],
    )
    import_manual_outcomes(input_path=outcomes, store=store, persist=True)
    main([
        "attribute-returns",
        "--db-path",
        str(db_path),
        "--out-dir",
        str(tmp_path / "attrib"),
        "--persist",
    ])
    main([
        "historical-report",
        "--db-path",
        str(db_path),
        "--out-dir",
        str(tmp_path / "report"),
    ])

    report_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "report").glob("*")
    )
    assert "SECRET_TOKEN" not in report_text

    forbidden = [
        "submit_order",
        "place_order",
        "create_order",
        "TradingClient",
        "alpaca.trading",
        "market_order",
        "limit_order",
        "execute_trade",
    ]
    implementation_files = [
        Path("intraday_scanner/services/return_attribution_service.py"),
        Path("intraday_scanner/services/alpha_cycle_service.py"),
        Path("intraday_scanner/services/free_shadow_mode.py"),
        Path("intraday_scanner/storage/sqlite_store.py"),
        Path("intraday_scanner/cli.py"),
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in implementation_files)
    assert not any(term in text for term in forbidden)


def _seed_signals(store: SQLiteScanStore, specs: list[tuple[str, int, float]]) -> None:
    record_alpha_historical_signals(
        store,
        [
            {
                "scan_id": "scan-ledger",
                "signal_key": f"scan-ledger:{rank}:{ticker}",
                "ticker": ticker,
                "rank": rank,
                "timestamp": SIGNAL_TIME,
                "company": f"{ticker} Corp",
                "alpha_score": 90 - rank,
                "score": 90 - rank,
                "edge_bucket": "HIGH",
                "confidence_bucket": "INSUFFICIENT_SAMPLE",
                "can_alert": True,
                "setup_key": "grade:A|gap:clean",
                "setup_grade": "A",
                "source": "fixture_public_table",
                "source_confidence": 88,
                "data_source_kind": "fixture",
                "entry_trigger": entry,
                "breakout_trigger": entry,
                "invalidation": round(entry * 0.9, 4),
                "invalidation_level": round(entry * 0.9, 4),
                "target_1": round(entry * 1.1, 4),
                "target_2": round(entry * 1.2, 4),
                "catalyst_summary": "Fixture catalyst",
                "risk_flags": "",
                "avoid_reasons": "",
            }
            for ticker, rank, entry in specs
        ],
        source_summary={"source": "fixture_public_table", "data_source_kind": "fixture"},
    )


def _write_outcomes(path: Path, rows: list[dict[str, object]]) -> None:
    defaults = {
        "date": DAY,
        "entry_time": f"{DAY}T13:31:00+00:00",
        "entry_price": "",
        "price_1m": "",
        "price_5m": "",
        "price_15m": "",
        "lunch_price": "",
        "close_price": "",
        "high_after_entry": "",
        "low_after_entry": "",
        "halted": "false",
        "source": "manual_outcome_upload",
        "notes": "",
    }
    output = []
    for row in rows:
        merged = {**defaults, **row}
        entry = float(merged["entry_price"]) if merged["entry_price"] not in {"", None} else 10.0
        if merged["price_1m"] == "":
            merged["price_1m"] = round(entry * 1.01, 4)
        if merged["price_15m"] == "":
            merged["price_15m"] = round(entry * 1.03, 4)
        if merged["lunch_price"] == "":
            merged["lunch_price"] = round(entry * 1.04, 4)
        if merged["high_after_entry"] == "":
            merged["high_after_entry"] = round(entry * 1.25, 4)
        if merged["low_after_entry"] == "":
            merged["low_after_entry"] = round(entry * 0.95, 4)
        output.append(merged)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "date",
                "ticker",
                "entry_time",
                "entry_price",
                "price_1m",
                "price_5m",
                "price_15m",
                "lunch_price",
                "close_price",
                "high_after_entry",
                "low_after_entry",
                "halted",
                "source",
                "notes",
            ],
        )
        writer.writeheader()
        writer.writerows(output)
