import shutil
from pathlib import Path

from intraday_scanner.cli import main
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


def test_telegram_missing_env_and_dry_run_dedupe(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    db_path = tmp_path / "web.sqlite"

    assert main(["telegram-test", "--db-path", str(db_path)]) == 1
    assert main(["telegram-test", "--dry-run", "--db-path", str(db_path)]) == 0
    assert main(["telegram-test", "--dry-run", "--db-path", str(db_path)]) == 0

    out = capsys.readouterr().out
    assert "DAWNSTRIKE TELEGRAM TEST" in out
    notifications = SQLiteScanStore(db_path).load_recent_notifications()
    assert len([row for row in notifications if row["channel"] == "telegram"]) == 1


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
    assert "DAWNSTRIKE RISK ALERT" in out
    assert "No market data was fabricated" in out


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
    assert "DAWNSTRIKE MORNING WATCHLIST" in out
    assert "BREAKOUT TRIGGER" in out
    assert "Research/watchlist only" in out
    assert SQLiteScanStore(db_path).load_latest_scan()["summary"]["top_ticker"] == "NOVA"


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
