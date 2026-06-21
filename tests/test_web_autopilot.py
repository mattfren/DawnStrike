import shutil
from pathlib import Path

from intraday_scanner.cli import main
from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers import browser_table_provider
from intraday_scanner.providers.browser_table_provider import ingest_browser_table
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.providers.public_table_provider import (
    extract_html_tables,
    normalize_public_table_rows,
    select_best_table,
)
from intraday_scanner.providers.web_source_base import (
    WebCollectionConfig,
    WebSourceConfig,
    ensure_allowed_url,
    fetch_text,
    load_web_sources_config,
)
from intraday_scanner.services import web_collection_service
from intraday_scanner.services.ai_research_service import run_ai_research, validate_ai_csv
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

FIXTURE_CONFIG = Path("tests/fixtures/web_sources_fixture.yaml")
RAW_SCREENER = Path("tests/fixtures/raw_screener_aliases.csv")


class _FakeTelegramNotifier:
    sent_events: list[tuple[str, str]] = []

    def __init__(self, config):
        self.config = config

    def send(self, event):
        self.sent_events.append((event.event_key, event.title))


def _use_fake_telegram(monkeypatch):
    _FakeTelegramNotifier.sent_events = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-do-not-print")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat-do-not-print")
    monkeypatch.setattr(web_collection_service, "TelegramNotifier", _FakeTelegramNotifier)
    return _FakeTelegramNotifier.sent_events


def test_web_config_loading():
    config = load_web_sources_config(FIXTURE_CONFIG)

    assert config.enabled is True
    assert config.rate_limit_seconds == 0
    assert "allowed.test" in config.allowed_domains
    assert any(source.type == "public_table_url" for source in config.sources)


def test_domain_allowlist_enforcement():
    ensure_allowed_url("https://allowed.test/fixture", allowed_domains=("allowed.test",))

    try:
        ensure_allowed_url("https://blocked.test/fixture", allowed_domains=("allowed.test",))
    except Exception as exc:
        assert "not in configured allowed_domains" in str(exc)
    else:
        raise AssertionError("blocked domain should fail")


def test_robots_block_policy_mocked():
    source = WebSourceConfig(
        name="blocked",
        type="public_table_url",
        url="https://allowed.test/fixture",
        params={"robots_allowed": False},
    )
    config = WebCollectionConfig(
        enabled=True,
        respect_robots=True,
        user_agent="test",
        timeout_seconds=1,
        rate_limit_seconds=0,
        save_raw=False,
        allowed_domains=("allowed.test",),
        sources=(source,),
    )

    result = fetch_text(source, config)

    assert result.status == "failed"
    assert "robots policy blocks" in result.failure_reason


def test_public_table_fixture_extraction_and_multiple_table_selection():
    html = Path("tests/fixtures/public_table_fixture.html").read_text(encoding="utf-8")
    tables = extract_html_tables(html)
    best = select_best_table(tables)

    assert len(tables) == 2
    assert best is not None
    assert best.index == 1
    assert best.score > tables[0].score


def test_table_normalization_and_lineage():
    html = Path("tests/fixtures/public_table_fixture.html").read_text(encoding="utf-8")
    best = select_best_table(extract_html_tables(html))
    assert best is not None

    rows, warnings = normalize_public_table_rows(
        best,
        source_name="fixture_public_table",
        source_url="https://allowed.test/fixture",
        raw_file_path="outputs/raw_source.html",
    )

    assert warnings == []
    assert rows[0]["ticker"] == "NOVA"
    assert rows[0]["data_source_kind"] == "web_url"
    assert rows[0]["source_url"] == "https://allowed.test/fixture"
    assert "url_table_unverified" in rows[0]["coverage_warning"]


def test_browser_table_fixture_extracts_rows(tmp_path):
    source = _browser_source("browser_table", "tests/fixtures/browser_table_fixture.html")

    result = ingest_browser_table(
        source=source,
        config=_browser_config(),
        out_dir=tmp_path / "browser",
    )

    assert result["status"] == "success"
    assert result["rows_normalized"] == 1
    rows = list(read_snapshot_csv(result["paths"]["premarket_snapshot"]))
    assert rows[0].ticker == "NOVA"
    assert rows[0].data_source_kind == "browser_url"
    assert "browser_rendered_public_table_unverified" in rows[0].coverage_warning


def test_browser_grid_fixture_extracts_rows(tmp_path):
    source = _browser_source("browser_grid", "tests/fixtures/browser_grid_fixture.html")

    result = ingest_browser_table(
        source=source,
        config=_browser_config(),
        out_dir=tmp_path / "browser",
    )

    assert result["status"] == "success"
    assert result["rows_normalized"] == 1
    rows = list(read_snapshot_csv(result["paths"]["premarket_snapshot"]))
    assert rows[0].ticker == "RIFT"


def test_browser_blocked_fixture_reports_login_required(tmp_path):
    source = _browser_source("browser_blocked", "tests/fixtures/browser_blocked_fixture.html")

    result = ingest_browser_table(
        source=source,
        config=_browser_config(),
        out_dir=tmp_path / "browser",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "login_required"


def test_browser_no_table_fixture_reports_no_candidate_table(tmp_path):
    source = _browser_source("browser_no_table", "tests/fixtures/browser_no_table_fixture.html")

    result = ingest_browser_table(
        source=source,
        config=_browser_config(),
        out_dir=tmp_path / "browser",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "no_candidate_table"


def test_browser_dependency_missing_gives_install_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_table_provider, "_playwright_available", lambda: False)
    source = WebSourceConfig(
        name="browser_live",
        type="browser_table_url",
        enabled=True,
        url="https://allowed.test/premarket",
    )

    result = ingest_browser_table(
        source=source,
        config=_browser_config(),
        out_dir=tmp_path / "browser",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "browser_extractor_not_available"
    assert "py -m pip install -e \".[browser]\"" in result["failure_reason"]


def test_web_ingest_public_table_outputs_and_persistence(tmp_path):
    db_path = tmp_path / "web.sqlite"
    out_dir = tmp_path / "ingest"

    assert (
        main(
            [
                "web-ingest-public-table",
                "--url",
                "https://allowed.test/fixture",
                "--config",
                str(FIXTURE_CONFIG),
                "--db-path",
                str(db_path),
                "--out-dir",
                str(out_dir),
                "--persist",
                "--print",
            ]
        )
        == 0
    )

    store = SQLiteScanStore(db_path)
    assert (out_dir / "extracted_tables.csv").exists()
    assert (out_dir / "premarket_snapshot.csv").exists()
    assert store.load_web_fetch_results()[0]["row_count"] == 2
    assert store.load_source_health()[0]["source"] == "fixture_public_table"
    assert store.load_normalized_source_rows()[0]["source"] == "fixture_public_table"


def test_web_ingest_missing_required_fields_reports_failure(tmp_path):
    config_path = _write_web_config(
        tmp_path,
        public_fixture=Path("tests/fixtures/public_table_missing_fields.html"),
        local_inbox=tmp_path / "empty",
    )
    (tmp_path / "empty").mkdir()

    result = web_collection_service.web_ingest_public_table(
        url="https://allowed.test/fixture",
        config_path=config_path,
        db_path=tmp_path / "web.sqlite",
        out_dir=tmp_path / "ingest",
        persist=True,
    )

    assert result["status"] == "no_valid_rows"
    assert "missing required market columns" in ";".join(result["warnings"])


def test_halt_and_sec_fixture_collection(tmp_path):
    db_path = tmp_path / "web.sqlite"

    halt = web_collection_service.web_collect_halts(
        config_path=FIXTURE_CONFIG,
        db_path=db_path,
        out_dir=tmp_path / "halts",
        persist=True,
    )
    sec = web_collection_service.web_collect_sec_risk(
        config_path=FIXTURE_CONFIG,
        db_path=db_path,
        out_dir=tmp_path / "sec",
        tickers=["OFFER"],
        persist=True,
    )

    store = SQLiteScanStore(db_path)
    assert halt["event_count"] == 1
    assert store.load_halt_events()[0]["ticker"] == "HALT"
    assert sec["event_count"] >= 1
    assert store.load_sec_risk_events()[0]["ticker"] == "OFFER"


def test_web_build_universe_filters_non_common(tmp_path):
    result = web_collection_service.web_build_universe(
        config_path=FIXTURE_CONFIG,
        db_path=tmp_path / "web.sqlite",
        out_path=tmp_path / "universe.csv",
        persist=True,
    )

    text = (tmp_path / "universe.csv").read_text(encoding="utf-8")
    assert result["accepted_count"] == 2
    assert "NOVA" in text
    assert "FUND" not in text


def test_ai_none_mode_and_malformed_csv(tmp_path):
    store = SQLiteScanStore(tmp_path / "web.sqlite")
    result = run_ai_research(
        rows=[{"ticker": "NOVA", "catalyst_headline": "Company announces contract"}],
        mode="none",
        store=store,
        persist=True,
        out_dir=tmp_path / "ai",
    )

    assert result["run"]["status"] == "rule_only"
    assert result["outputs"][0]["classification"] == "bullish"
    assert store.load_ai_research_runs()[0]["mode"] == "none"
    try:
        validate_ai_csv("ticker,classification\nNOVA,bullish\n")
    except Exception as exc:
        assert "missing required column" in str(exc)
    else:
        raise AssertionError("malformed AI CSV should fail")


def test_codex_cli_missing_executable_clear_error(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)

    try:
        run_ai_research(rows=[{"ticker": "NOVA"}], mode="codex-cli")
    except Exception as exc:
        assert "codex executable" in str(exc)
    else:
        raise AssertionError("missing codex should fail")


def test_telegram_dry_run_does_not_block_real_send(tmp_path, monkeypatch, capsys):
    sent_events = _use_fake_telegram(monkeypatch)
    db_path = tmp_path / "web.sqlite"
    today = web_collection_service._today()

    assert main(["telegram-test", "--dry-run", "--db-path", str(db_path)]) == 0
    assert main(["telegram-test", "--db-path", str(db_path)]) == 0

    out = capsys.readouterr().out
    assert "telegram_test:" in out
    assert ":telegram:dry_run" in out
    assert ":telegram:real" in out
    assert sent_events == [(f"telegram_test:{today}:telegram:real", "DAWNSTRIKE TELEGRAM TEST")]
    notifications = SQLiteScanStore(db_path).load_recent_notifications(limit=10)
    by_key = {row["event_key"]: row for row in notifications}
    dry_row = by_key[f"telegram_test:{today}:telegram:dry_run"]
    real_row = by_key[f"telegram_test:{today}:telegram:real"]
    assert dry_row["dry_run"] is True
    assert dry_row["send_attempted"] is False
    assert dry_row["status"] == "dry_run"
    assert real_row["dry_run"] is False
    assert real_row["send_attempted"] is True
    assert real_row["status"] == "sent"


def test_telegram_real_mode_dedupes_second_send(tmp_path, monkeypatch, capsys):
    sent_events = _use_fake_telegram(monkeypatch)
    db_path = tmp_path / "web.sqlite"

    assert main(["telegram-test", "--db-path", str(db_path)]) == 0
    assert main(["telegram-test", "--db-path", str(db_path)]) == 0

    out = capsys.readouterr().out
    notifications = SQLiteScanStore(db_path).load_recent_notifications(limit=10)
    assert len(sent_events) == 1
    assert "skipped_duplicate" in out
    assert len([row for row in notifications if row["channel"] == "telegram"]) == 1


def test_telegram_force_bypasses_test_dedupe(tmp_path, monkeypatch, capsys):
    sent_events = _use_fake_telegram(monkeypatch)
    db_path = tmp_path / "web.sqlite"
    today = web_collection_service._today()

    assert main(["telegram-test", "--db-path", str(db_path)]) == 0
    assert main(["telegram-test", "--db-path", str(db_path), "--force"]) == 0

    out = capsys.readouterr().out
    notifications = SQLiteScanStore(db_path).load_recent_notifications(limit=10)
    force_rows = [
        row
        for row in notifications
        if row["event_key"].startswith(f"telegram_test:{today}:telegram:real:force:")
    ]
    assert len(sent_events) == 2
    assert '"forced": true' in out
    assert len(force_rows) == 1
    assert force_rows[0]["dedupe_bypassed"] is True
    assert force_rows[0]["send_attempted"] is True


def test_telegram_missing_secrets_fails_clearly_for_real_mode(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        web_collection_service,
        "load_config",
        lambda **kwargs: ScannerConfig(
            database_path=kwargs["database_path"],
            notifier_channels=kwargs["notifier_channels"],
        ),
    )
    db_path = tmp_path / "web.sqlite"

    assert main(["telegram-test", "--db-path", str(db_path)]) == 1

    captured = capsys.readouterr()
    assert "Telegram requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID" in captured.err
    assert "DAWNSTRIKE TELEGRAM TEST" not in captured.out
    notifications = SQLiteScanStore(db_path).load_recent_notifications()
    assert notifications == []


def test_telegram_dry_run_works_without_secrets(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        web_collection_service,
        "load_config",
        lambda **kwargs: ScannerConfig(
            database_path=kwargs["database_path"],
            notifier_channels=kwargs["notifier_channels"],
        ),
    )
    db_path = tmp_path / "web.sqlite"

    assert main(["telegram-test", "--dry-run", "--db-path", str(db_path)]) == 0

    out = capsys.readouterr().out
    notifications = SQLiteScanStore(db_path).load_recent_notifications()
    assert "DAWNSTRIKE TELEGRAM TEST" in out
    assert len(notifications) == 1
    assert notifications[0]["dry_run"] is True
    assert notifications[0]["send_attempted"] is False


def test_telegram_test_output_and_rows_do_not_expose_secrets(tmp_path, monkeypatch, capsys):
    token = "test-token-do-not-print"
    chat_id = "test-chat-do-not-print"
    sent_events = _use_fake_telegram(monkeypatch)
    db_path = tmp_path / "web.sqlite"

    assert main(["telegram-test", "--db-path", str(db_path)]) == 0

    captured = capsys.readouterr()
    notifications = SQLiteScanStore(db_path).load_recent_notifications()
    persisted_text = repr(notifications)
    assert len(sent_events) == 1
    assert token not in captured.out
    assert token not in captured.err
    assert token not in persisted_text
    assert chat_id not in captured.out
    assert chat_id not in captured.err
    assert chat_id not in persisted_text


def test_web_auto_collect_uses_local_inbox_before_public_table(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy2(RAW_SCREENER, inbox / "raw.csv")
    config_path = _write_web_config(tmp_path, local_inbox=inbox)
    monkeypatch.setattr(
        web_collection_service,
        "ingest_public_table",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("public table should not run")),
    )

    result = web_collection_service.web_auto_collect(
        config_path=config_path,
        db_path=tmp_path / "web.sqlite",
        out_dir=tmp_path / "auto",
        persist=True,
    )

    assert result["status"] == "success"
    assert result["source_summary"]["candidate_count"] == 4
    assert result["rows"][0]["ticker"]
    assert not (inbox / "raw.csv").exists()
    assert Path(result["source_summary"]["attempts"][0]["raw_archive_path"]).exists()


def test_web_auto_collect_public_table_source_health_and_dashboard_loader(tmp_path):
    from intraday_scanner.dashboard.data_loader import load_sqlite

    db_path = tmp_path / "web.sqlite"
    result = web_collection_service.web_auto_collect(
        config_path=FIXTURE_CONFIG,
        db_path=db_path,
        out_dir=tmp_path / "auto",
        persist=True,
    )
    dashboard = load_sqlite(db_path)

    assert result["status"] == "success"
    assert dashboard["web_automation_status"]["latest_source_summary"]["candidate_count"] == 2
    assert dashboard["web_automation_status"]["source_health"]


def test_web_telegram_daemon_dry_run_and_no_source_failure(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    config_path = _write_web_config(
        tmp_path,
        local_inbox=empty,
        public_enabled=False,
    )
    db_path = tmp_path / "web.sqlite"

    assert (
        main(
            [
                "web-telegram-daemon",
                "--config",
                str(config_path),
                "--automation-config",
                "config/automation.example.yaml",
                "--db-path",
                str(db_path),
                "--out-root",
                str(tmp_path / "web_telegram"),
                "--ai-mode",
                "none",
                "--notify",
                "console",
                "--dry-run",
                "--max-cycles",
                "1",
            ]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert out.count("[dry-run:console]") == 1
    assert "📡 Dawnstrike Source Check" in out
    assert "No usable rows found." in out
    assert "Drop CSV into data\\inbox\\screener" in out
    assert "📊 Dawnstrike Summary" not in out


def test_web_telegram_daemon_dry_run_with_fixture_formats_watchlist(tmp_path, capsys):
    db_path = tmp_path / "web.sqlite"

    assert (
        main(
            [
                "web-telegram-daemon",
                "--config",
                str(FIXTURE_CONFIG),
                "--automation-config",
                "config/automation.example.yaml",
                "--db-path",
                str(db_path),
                "--out-root",
                str(tmp_path / "web_telegram"),
                "--ai-mode",
                "none",
                "--notify",
                "console",
                "--dry-run",
                "--max-cycles",
                "1",
            ]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "🚀 Dawnstrike Watchlist" in out
    assert "🎯 Trigger" in out
    assert "👀 Manual Monitor Needed" in out
    assert "📥 Outcome Data Needed" in out
    assert "Research only. No orders placed." in out
    assert SQLiteScanStore(db_path).load_latest_scan()["summary"]["top_ticker"] == "NOVA"


def test_web_source_doctor_reports_candidate_sources(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    config_path = _write_web_config(tmp_path, local_inbox=empty, public_enabled=True)

    assert (
        main(
            [
                "web-source-doctor",
                "--config",
                str(config_path),
                "--out-dir",
                str(tmp_path / "doctor"),
                "--print",
            ]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "fixture_public_table" in out
    assert '"classification": "candidate"' in out
    result = (tmp_path / "doctor" / "source_doctor.json").read_text(encoding="utf-8")
    assert "candidate" in result


def test_no_order_execution_path_in_web_autopilot_code():
    forbidden = [
        "submit_order",
        "place_order",
        "create_order",
        "TradingClient",
        "alpaca.trading",
        "broker execution",
        "auto trade",
        "order submission",
        "buy recommendation",
        "sell recommendation",
    ]
    files = [
        Path("intraday_scanner/services/web_collection_service.py"),
        Path("intraday_scanner/services/ai_research_service.py"),
        Path("intraday_scanner/providers/web_source_base.py"),
        Path("scripts/run_web_telegram_daemon.bat"),
        Path("scripts/run_web_telegram_once.bat"),
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in files)

    assert not any(term in text for term in forbidden)


def _write_web_config(
    tmp_path: Path,
    *,
    local_inbox: Path,
    public_fixture: Path = Path("tests/fixtures/public_table_fixture.html"),
    public_enabled: bool = True,
) -> Path:
    config_path = tmp_path / "web_sources.yaml"
    config_path.write_text(
        "\n".join(
            [
                "enabled: true",
                "respect_robots: true",
                "user_agent: test",
                "timeout_seconds: 1",
                "rate_limit_seconds: 0",
                "save_raw: true",
                "allowed_domains:",
                "  - allowed.test",
                "sources:",
                "  - name: local_inbox",
                "    type: local_inbox",
                "    enabled: true",
                f"    path: {local_inbox}",
                "  - name: fixture_public_table",
                "    type: public_table_url",
                f"    enabled: {str(public_enabled).lower()}",
                "    url: https://allowed.test/fixture",
                f"    fixture_path: {public_fixture}",
                "  - name: nasdaq_halts",
                "    type: nasdaq_trade_halts_rss",
                "    enabled: false",
                "  - name: sec_edgar",
                "    type: sec_edgar",
                "    enabled: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _browser_config() -> WebCollectionConfig:
    return WebCollectionConfig(
        enabled=True,
        respect_robots=True,
        user_agent="test",
        timeout_seconds=1,
        rate_limit_seconds=0,
        save_raw=True,
        allowed_domains=("allowed.test",),
        sources=(),
    )


def _browser_source(name: str, fixture_path: str) -> WebSourceConfig:
    return WebSourceConfig(
        name=name,
        type="browser_table_url",
        enabled=True,
        url="https://allowed.test/premarket",
        fixture_path=fixture_path,
    )
