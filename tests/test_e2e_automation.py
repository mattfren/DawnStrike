import csv
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from intraday_scanner.cli import main
from intraday_scanner.dashboard.data_loader import load_sqlite
from intraday_scanner.services import e2e_automation_service
from intraday_scanner.services.e2e_automation_service import (
    automation_outcomes,
    automation_summary,
    discover_screener_source,
    load_automation_config,
)
from intraday_scanner.services.time_utils import get_market_date, get_operator_date
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

RAW_SCREENER = Path("tests/fixtures/raw_screener_aliases.csv")


def test_automation_config_loading_and_missing_config_fallback(tmp_path):
    config = load_automation_config(tmp_path / "missing.yaml")

    assert config.db_path == Path("data/shadow_real.sqlite")
    assert config.screener_sources[0].name == "local_inbox"
    assert "console" in config.notification_channels


def test_source_priority_uses_local_inbox_before_url(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy2(RAW_SCREENER, inbox / "raw.csv")
    config_path = _write_config(tmp_path, inbox=inbox, url_enabled=True)
    config = load_automation_config(config_path)

    monkeypatch.setattr(
        e2e_automation_service,
        "safe_url_ingest_screener",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("network should not run")),
    )

    source = discover_screener_source(config, "2026-06-20")

    assert source.kind == "inbox"
    assert source.path == inbox / "raw.csv"


def test_morning_automation_run_persists_official_call_and_notifications(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy2(RAW_SCREENER, inbox / "raw.csv")
    db_path = tmp_path / "automation.sqlite"
    out_root = tmp_path / "automation"
    config_path = _write_config(tmp_path, inbox=inbox, db_path=db_path, out_root=out_root)

    assert (
        main(
            [
                "automation-morning",
                "--config",
                str(config_path),
                "--db-path",
                str(db_path),
                "--out-root",
                str(out_root),
                "--date",
                "2026-06-20",
                "--notify",
            ]
        )
        == 0
    )

    store = SQLiteScanStore(db_path)
    latest = store.load_latest_scan()
    notifications = store.load_recent_notifications()
    runs = store.load_automation_runs()

    assert latest is not None
    assert latest["summary"]["top_ticker"] == "NOVA"
    assert latest["summary"]["created_at"]
    assert runs[0]["status"] == "success"
    assert any(row["channel_hint"] == "top_picks" for row in notifications)
    assert not (inbox / "raw.csv").exists()
    assert (out_root / "2026-06-20" / "morning" / "run_summary.json").exists()
    assert (out_root / "2026-06-20" / "morning" / "ranked_candidates.csv").exists()


def test_no_screener_source_sends_no_data_notification(tmp_path):
    inbox = tmp_path / "empty_inbox"
    inbox.mkdir()
    db_path = tmp_path / "automation.sqlite"
    out_root = tmp_path / "automation"
    config_path = _write_config(tmp_path, inbox=inbox, db_path=db_path, out_root=out_root)

    assert (
        main(
            [
                "automation-morning",
                "--config",
                str(config_path),
                "--db-path",
                str(db_path),
                "--out-root",
                str(out_root),
                "--date",
                "2026-06-20",
                "--notify",
            ]
        )
        == 0
    )

    notifications = SQLiteScanStore(db_path).load_recent_notifications()
    assert any(row["channel_hint"] == "source_failed" for row in notifications)


def test_notification_dedupe_for_repeated_no_data(tmp_path):
    inbox = tmp_path / "empty_inbox"
    inbox.mkdir()
    db_path = tmp_path / "automation.sqlite"
    out_root = tmp_path / "automation"
    config_path = _write_config(tmp_path, inbox=inbox, db_path=db_path, out_root=out_root)

    for _ in range(2):
        assert (
            main(
                [
                    "automation-morning",
                    "--config",
                    str(config_path),
                    "--db-path",
                    str(db_path),
                    "--out-root",
                    str(out_root),
                    "--date",
                    "2026-06-20",
                    "--notify",
                ]
            )
            == 0
        )

    source_failed = [
        row
        for row in SQLiteScanStore(db_path).load_recent_notifications(limit=20)
        if row["channel_hint"] == "source_failed"
    ]
    assert len(source_failed) == 1


def test_monitor_open_without_live_source_sends_manual_monitor_required(tmp_path):
    db_path, out_root, config_path = _seed_morning(tmp_path)

    assert (
        main(
            [
                "automation-monitor-open",
                "--config",
                str(config_path),
                "--db-path",
                str(db_path),
                "--out-root",
                str(out_root),
                "--max-iterations",
                "1",
                "--notify",
            ]
        )
        == 0
    )

    store = SQLiteScanStore(db_path)
    assert store.load_recent_monitor_events()[0]["event_type"] == "manual_monitor_required"
    assert any(
        row["channel_hint"] == "monitor_alert"
        for row in store.load_recent_notifications(limit=20)
    )


def test_outcome_missing_reminder_includes_template_and_notification(tmp_path):
    db_path, out_root, config_path = _seed_morning(tmp_path)

    result = automation_outcomes(
        config_path=config_path,
        db_path=db_path,
        out_root=out_root,
        run_date="2026-06-20",
        notify=True,
    )
    notifications = SQLiteScanStore(db_path).load_recent_notifications()

    assert result["status"] == "missing"
    assert Path(result["reminder_path"]).exists()
    template = Path(result["reminder_path"]).read_text(encoding="utf-8")
    assert "ticker" in template
    assert "NOVA" in template
    assert "RIFT" in template
    assert "WIDE" in template
    assert result["tickers"] == ["NOVA", "RIFT", "WIDE"]
    assert any(row["channel_hint"] == "outcome_missing" for row in notifications)
    assert any(row["channel_hint"] == "lunch_reminder" for row in notifications)
    assert any(row["channel_hint"] == "close_reminder" for row in notifications)


def test_outcome_reminder_without_saved_picks_is_clear(tmp_path):
    db_path = tmp_path / "automation.sqlite"
    out_root = tmp_path / "automation"
    config_path = _write_config(tmp_path, db_path=db_path, out_root=out_root)

    result = automation_outcomes(
        config_path=config_path,
        db_path=db_path,
        out_root=out_root,
        run_date="2026-06-20",
        notify=True,
    )
    notifications = SQLiteScanStore(db_path).load_recent_notifications(limit=10)

    assert result["status"] == "missing"
    assert result["tickers"] == []
    assert result["no_saved_picks"] is True
    assert "No saved picks found." in notifications[0]["body"]


def test_market_date_uses_configured_operator_timezone():
    utc_now = datetime(2026, 6, 21, 2, 30, tzinfo=timezone.utc)

    assert get_operator_date("America/Chicago", now=utc_now) == "2026-06-20"
    assert get_market_date("America/Chicago", now=utc_now) == "2026-06-20"


def test_outcome_reminder_default_path_uses_market_date(tmp_path, monkeypatch):
    db_path, out_root, config_path = _seed_morning(tmp_path)
    monkeypatch.setattr(e2e_automation_service, "get_market_date", lambda _timezone: "2026-06-20")

    result = automation_outcomes(
        config_path=config_path,
        db_path=db_path,
        out_root=out_root,
        notify=False,
    )

    assert result["status"] == "missing"
    assert result["reminder_path"].endswith("outcomes_2026-06-20.csv")


def test_outcome_file_import_archive_audit_and_summary_notification(tmp_path):
    db_path, out_root, config_path = _seed_morning(tmp_path)
    outcome_path = _write_outcome_file(db_path, tmp_path / "outcomes" / "outcomes_2026-06-20.csv")

    result = automation_outcomes(
        config_path=config_path,
        db_path=db_path,
        out_root=out_root,
        run_date="2026-06-20",
        notify=True,
    )
    store = SQLiteScanStore(db_path)

    assert result["status"] == "success"
    assert not outcome_path.exists()
    assert store.load_manual_outcomes()
    assert store.load_latest_manual_audit_summary()["trade_count"] >= 1
    assert store.load_latest_shadow_report()["manual_outcome_count"] >= 1
    assert any(
        row["channel_hint"] == "audit_completed"
        for row in store.load_recent_notifications(limit=20)
    )


def test_daily_summary_notification_and_dashboard_loader(tmp_path):
    db_path, out_root, config_path = _seed_morning(tmp_path)
    result = automation_summary(
        config_path=config_path,
        db_path=db_path,
        out_root=out_root,
        run_date="2026-06-20",
        notify=True,
    )
    dashboard = load_sqlite(db_path)

    assert result["status"] == "success"
    assert (out_root / "2026-06-20" / "summary" / "daily_summary.json").exists()
    assert dashboard["automation_status"]["latest_run"]["run_type"] == "summary"
    assert dashboard["automation_status"]["latest_notification"]


def test_daemon_dry_run_max_cycles_and_scripts_exist(tmp_path):
    db_path = tmp_path / "automation.sqlite"
    out_root = tmp_path / "automation"
    config_path = _write_config(tmp_path, db_path=db_path, out_root=out_root)

    assert (
        main(
            [
                "automation-daemon",
                "--config",
                str(config_path),
                "--db-path",
                str(db_path),
                "--out-root",
                str(out_root),
                "--dry-run",
                "--max-cycles",
                "1",
                "--notify",
            ]
        )
        == 0
    )
    assert Path("scripts/run_automation_once.bat").read_text(encoding="utf-8").find(
        "py -m intraday_scanner.cli"
    ) >= 0
    assert Path("scripts/run_automation_daemon.bat").exists()
    assert Path("scripts/register_dawnstrike_automation.ps1").exists()


def test_automation_run_modes_once_and_dry_run(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy2(RAW_SCREENER, inbox / "raw.csv")
    db_path = tmp_path / "automation.sqlite"
    out_root = tmp_path / "automation"
    config_path = _write_config(tmp_path, inbox=inbox, db_path=db_path, out_root=out_root)

    assert (
        main(
            [
                "automation-run",
                "--mode",
                "once",
                "--config",
                str(config_path),
                "--db-path",
                str(db_path),
                "--out-root",
                str(out_root),
                "--date",
                "2026-06-20",
                "--notify",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "automation-run",
                "--mode",
                "dry-run",
                "--config",
                str(config_path),
                "--db-path",
                str(db_path),
                "--out-root",
                str(out_root),
                "--date",
                "2026-06-20",
                "--max-cycles",
                "1",
                "--notify",
            ]
        )
        == 0
    )


def test_url_ingestion_disabled_by_default_no_network(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, url_enabled=False)
    config = load_automation_config(config_path)
    monkeypatch.setattr(
        e2e_automation_service,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network called")),
    )

    source = discover_screener_source(config, "2026-06-20")

    assert source.status == "missing"


def test_no_order_execution_path_in_automation_code():
    forbidden = [
        "submit_order",
        "place_order",
        "create_order",
        "TradingClient",
        "alpaca.trading",
    ]
    files = [
        Path("intraday_scanner/services/e2e_automation_service.py"),
        Path("intraday_scanner/cli.py"),
        Path("scripts/run_automation_once.bat"),
        Path("scripts/run_automation_daemon.bat"),
    ]

    text = "\n".join(path.read_text(encoding="utf-8") for path in files)

    assert not any(term in text for term in forbidden)


def _seed_morning(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy2(RAW_SCREENER, inbox / "raw.csv")
    db_path = tmp_path / "automation.sqlite"
    out_root = tmp_path / "automation"
    outcomes = tmp_path / "outcomes"
    config_path = _write_config(
        tmp_path,
        inbox=inbox,
        db_path=db_path,
        out_root=out_root,
        outcome_inbox=outcomes,
    )
    assert (
        main(
            [
                "automation-morning",
                "--config",
                str(config_path),
                "--db-path",
                str(db_path),
                "--out-root",
                str(out_root),
                "--date",
                "2026-06-20",
                "--notify",
            ]
        )
        == 0
    )
    return db_path, out_root, config_path


def _write_outcome_file(db_path: Path, path: Path) -> Path:
    store = SQLiteScanStore(db_path)
    latest = store.load_latest_scan()
    assert latest is not None
    summary = cast(dict[str, Any], latest["summary"])
    ranked = cast(list[dict[str, Any]], latest["ranked_candidates"])
    timestamp = str(summary["created_at"])
    rows = []
    for row in ranked[:3]:
        entry = float(row["premarket_price"])
        rows.append(
            {
                "date": timestamp[:10],
                "ticker": row["ticker"],
                "entry_time": timestamp,
                "entry_price": entry,
                "price_1m": round(entry * 1.01, 4),
                "price_5m": round(entry * 1.02, 4),
                "price_15m": round(entry * 1.03, 4),
                "lunch_price": round(entry * 1.04, 4),
                "close_price": round(entry * 1.05, 4),
                "high_after_entry": round(entry * 1.08, 4),
                "low_after_entry": round(entry * 0.96, 4),
                "halted": "false",
                "source": "manual_outcome_upload",
                "notes": "test outcome",
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _write_config(
    tmp_path: Path,
    *,
    inbox: Path | None = None,
    db_path: Path | None = None,
    out_root: Path | None = None,
    outcome_inbox: Path | None = None,
    url_enabled: bool = False,
) -> Path:
    inbox = inbox or tmp_path / "inbox"
    db_path = db_path or tmp_path / "automation.sqlite"
    out_root = out_root or tmp_path / "automation"
    outcome_inbox = outcome_inbox or tmp_path / "outcomes"
    config_path = tmp_path / "automation.yaml"
    config_path.write_text(
        "\n".join(
            [
                "timezone: America/Chicago",
                "market_timezone: America/New_York",
                f"db_path: {db_path}",
                f"out_root: {out_root}",
                "notification_channels:",
                "  - console",
                "screener_sources:",
                "  - name: local_inbox",
                "    type: inbox",
                f"    path: {inbox}",
                "    enabled: true",
                "  - name: url_disabled",
                "    type: url",
                "    url: https://example.test/table",
                f"    enabled: {str(url_enabled).lower()}",
                "normalizer:",
                "  preferred: deterministic",
                "  fallback: none",
                "schedule:",
                "  morning_scan_time_ct: \"08:10\"",
                "monitor:",
                "  enabled: true",
                "  interval_seconds: 60",
                "  max_symbols: 5",
                "outcomes:",
                "  mode: manual_file_or_reminder",
                f"  inbox: {outcome_inbox}",
                "  reminder_if_missing: true",
                "notifications:",
                "  dedupe: true",
                "  send_top_picks: true",
                "  send_failures: true",
                "  send_lunch_reminder: true",
                "  send_close_reminder: true",
                "  send_daily_summary: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path
