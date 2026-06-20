import json
import subprocess
import sys

import tomllib

from intraday_scanner.cli import main
from intraday_scanner.config import ScannerConfig
from intraday_scanner.models import SnapshotRow
from intraday_scanner.providers.csv_enrichment_provider import CsvEnrichmentProvider
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.scheduler import (
    ScheduledJob,
    record_scheduler_failure,
    schedule_as_rows_for_date,
)
from intraday_scanner.scoring import score_universe
from intraday_scanner.services.audit_service import calculate_audit
from intraday_scanner.services.enrichment_service import enrich_snapshots
from intraday_scanner.services.historical_ingestion_service import (
    backfill_snapshot_runs,
    ingest_minute_bars,
)
from intraday_scanner.services.market_calendar import is_market_day
from intraday_scanner.services.performance_service import build_performance_report
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_module_entry_cli_path_works():
    result = subprocess.run(
        [sys.executable, "-m", "intraday_scanner.cli", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "live-scan" in result.stdout


def test_dev_extras_metadata_contains_required_tools():
    with open("pyproject.toml", "rb") as handle:
        pyproject = tomllib.load(handle)

    dev = pyproject["project"]["optional-dependencies"]["dev"]

    assert any(item.startswith("pytest") for item in dev)
    assert any(item.startswith("ruff") for item in dev)
    assert any(item.startswith("mypy") for item in dev)


def test_live_scan_without_universe_fails_actionably(capsys):
    status = main(["live-scan", "--provider", "alpaca"])

    captured = capsys.readouterr()

    assert status == 1
    assert "--universe-file" in captured.err


def test_live_scan_uses_supplied_universe_and_records_counts(monkeypatch, tmp_path):
    class FakeAlpacaProvider:
        def __init__(self, config):
            self.config = config

        def validate_credentials(self):
            return None

        def get_premarket_snapshot(self, symbols, config):
            return [
                SnapshotRow(
                    ticker=symbol,
                    company=symbol,
                    premarket_price=5.20,
                    previous_close=2.75,
                    premarket_high=5.35,
                    premarket_low=3.60,
                    premarket_volume=1_500_000,
                    float_shares=18_000_000,
                    market_cap=94_000_000,
                    spread_pct=1.2,
                    short_float_pct=18.5,
                    has_news=True,
                    current_halt=False,
                    recent_offering=False,
                    reverse_split_90d=False,
                    source="fake_alpaca",
                    as_of_timestamp="2026-06-18T09:25:00-04:00",
                    dollar_volume=7_800_000,
                    gap_pct=89.09,
                    catalyst_headline="fixture",
                )
                for symbol in symbols[:3]
            ]

    monkeypatch.setattr("intraday_scanner.cli.AlpacaProvider", FakeAlpacaProvider)
    db_path = tmp_path / "scanner.sqlite"

    status = main(
        [
            "live-scan",
            "--provider",
            "alpaca",
            "--universe-file",
            "sample_data/universe_sample.csv",
            "--db-path",
            str(db_path),
            "--out-dir",
            str(tmp_path / "live"),
            "--persist",
        ]
    )

    health = SQLiteScanStore(db_path).load_provider_health()
    counts = json.loads(next(row for row in health if row["provider"] == "alpaca:counts")["detail"])

    assert status == 0
    assert counts["symbols_requested"] == 8
    assert counts["snapshot_row_count"] == 3
    assert counts["symbols_passing_filters"] == 3


def test_enrichment_file_patches_known_fields_and_reports_completeness(tmp_path):
    enrichment = tmp_path / "enrichment.csv"
    enrichment.write_text(
        "ticker,float_shares,market_cap,short_float_pct,has_news,catalyst_headline,catalyst_url\n"
        "NOVA,20000000,100000000,22,true,Confirmed catalyst,https://example.test/nova\n",
        encoding="utf-8",
    )
    snapshots = read_snapshot_csv("sample_data/premarket_snapshot_sample.csv")[:1]

    enriched, report = enrich_snapshots(
        snapshots,
        ScannerConfig(),
        [CsvEnrichmentProvider(enrichment)],
    )

    assert enriched[0].float_shares == 20_000_000
    assert enriched[0].catalyst_url == "https://example.test/nova"
    assert report["applied_by_provider"]["csv_enrichment"] == 1
    assert report["completeness_pct"] > 0


def test_catalyst_url_propagates_to_candidate_and_recommendation_history(tmp_path):
    snapshot = read_snapshot_csv("sample_data/premarket_snapshot_sample.csv")[0]
    patched = SnapshotRow(
        **{**snapshot.to_dict(), "catalyst_url": "https://example.test/catalyst"}
    )
    result = score_universe([patched], ScannerConfig())
    db_path = tmp_path / "scanner.sqlite"

    SQLiteScanStore(db_path).persist_scan_result(result)
    latest = SQLiteScanStore(db_path).load_latest_scan()
    theses = SQLiteScanStore(db_path).load_recommendation_theses()

    assert latest is not None
    assert latest["ranked_candidates"][0]["catalyst_url"] == "https://example.test/catalyst"
    assert theses[0]["catalyst_url"] == "https://example.test/catalyst"


def test_breakout_entry_mode_waits_for_trigger_and_marks_no_entry():
    rows = [{"ticker": "NOVA", "breakout_trigger": "11", "score": "99", "rank": "1"}]
    bars = [
        {
            "ticker": "NOVA",
            "timestamp": "2026-06-18T09:30:00-04:00",
            "open": "10",
            "high": "10.5",
            "low": "9.8",
            "close": "10.1",
            "volume": "1000",
        }
    ]

    result = calculate_audit(
        rows,
        bars,
        ScannerConfig(entry_mode="breakout", slippage_bps=0),
        top_n=1,
    )

    assert result.trades[0]["audit_status"] == "no_entry_trigger"
    assert result.summary["no_entry_trigger_count"] == 1
    assert result.summary["audit_unavailable_count"] == 0


def test_breakout_entry_mode_enters_on_trigger_bar():
    rows = [{"ticker": "NOVA", "breakout_trigger": "11", "score": "99", "rank": "1"}]
    bars = [
        {
            "ticker": "NOVA",
            "timestamp": "2026-06-18T09:30:00-04:00",
            "open": "10",
            "high": "10.5",
            "low": "9.8",
            "close": "10.1",
            "volume": "1000",
        },
        {
            "ticker": "NOVA",
            "timestamp": "2026-06-18T09:35:00-04:00",
            "open": "10.9",
            "high": "11.2",
            "low": "10.8",
            "close": "11.1",
            "volume": "1000",
        },
    ]

    result = calculate_audit(
        rows,
        bars,
        ScannerConfig(entry_mode="breakout", slippage_bps=0),
        top_n=1,
    )

    assert result.trades[0]["audit_status"] == "audited"
    assert result.trades[0]["entry_mode"] == "breakout"
    assert result.trades[0]["entry_time"] == "2026-06-18T09:35:00-04:00"
    assert result.trades[0]["entry_price"] == 11


def test_performance_report_includes_compounded_equity_curve():
    report = build_performance_report(
        [
            {"audit_status": "audited", "close_return_pct": "10", "lunch_return_pct": "5"},
            {"audit_status": "audited", "close_return_pct": "-10", "lunch_return_pct": "-2"},
        ]
    )

    curve = report["compounded_close_equity_curve"]

    assert curve[-1]["equity"] == 0.99
    assert report["cumulative_close_return_note"].startswith("Simple sum")


def test_scheduler_calendar_holiday_early_close_and_failure_health(tmp_path):
    assert not is_market_day(__import__("datetime").date(2026, 6, 19))
    closed_rows = schedule_as_rows_for_date(__import__("datetime").date(2026, 6, 19))
    early_rows = schedule_as_rows_for_date(__import__("datetime").date(2026, 11, 27))
    store = SQLiteScanStore(tmp_path / "scanner.sqlite")
    job = ScheduledJob("test-job", "08:00", "py -m intraday_scanner.cli --help", "test")

    record_scheduler_failure(store, job, RuntimeError("boom"))

    assert closed_rows[0]["will_run"] is False
    assert early_rows[0]["early_close_time_ct"] == "12:00"
    assert store.load_provider_health()[0]["provider"] == "scheduler"


def test_notify_test_persists_and_dedupes_console_notification(tmp_path, capsys):
    db_path = tmp_path / "scanner.sqlite"

    assert main(["notify-test", "--db-path", str(db_path)]) == 0
    assert main(["notify-test", "--db-path", str(db_path)]) == 0

    output = capsys.readouterr().out
    with __import__("sqlite3").connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM notifications_sent").fetchone()[0]

    assert "skipped=1" in output
    assert count == 1


def test_historical_ingestion_and_backfill_are_fixture_labeled(tmp_path):
    ingest = ingest_minute_bars(
        input_path="sample_data/minute_bars/2026-06-18.csv",
        out_dir=tmp_path / "bars",
    )
    backfill = backfill_snapshot_runs(
        minute_bars="sample_data/builder/premarket_bars_sample.csv",
        previous_close="sample_data/builder/previous_close_sample.csv",
        metadata="sample_data/builder/metadata_sample.csv",
        out_dir=tmp_path / "backfill",
        config=ScannerConfig(database_path=tmp_path / "scanner.sqlite"),
        persist=True,
    )

    assert ingest["fixture_only"] is True
    assert backfill["fixture_only"] is True
    assert backfill["snapshot_row_count"] > 0
