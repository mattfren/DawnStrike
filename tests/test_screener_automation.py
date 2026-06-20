import csv
import shutil
import subprocess
from pathlib import Path

import pytest

from intraday_scanner.cli import main
from intraday_scanner.dashboard.data_loader import load_sqlite
from intraday_scanner.errors import DataProviderError, SnapshotValidationError
from intraday_scanner.services import screener_automation
from intraday_scanner.services.screener_automation import (
    auto_shadow_daily,
    auto_shadow_from_screener,
    deterministic_normalize_screener,
    normalize_screener_file,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

FIXTURE_ALIASES = Path("tests/fixtures/raw_screener_aliases.csv")
FIXTURE_MESSY = Path("tests/fixtures/raw_screener_messy.txt")


@pytest.fixture
def isolated_screener_dirs(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    failed = tmp_path / "failed"
    manual = tmp_path / "manual"
    logs = tmp_path / "logs"
    monkeypatch.setattr(screener_automation, "SCREENER_INBOX", inbox)
    monkeypatch.setattr(screener_automation, "SCREENER_PROCESSED", processed)
    monkeypatch.setattr(screener_automation, "SCREENER_FAILED", failed)
    monkeypatch.setattr(screener_automation, "MANUAL_DATA_DIR", manual)
    monkeypatch.setattr(screener_automation, "LOG_DIR", logs)
    monkeypatch.setattr(screener_automation, "LOG_PATH", logs / "screener_automation.log")
    return {"inbox": inbox, "processed": processed, "failed": failed, "manual": manual}


def test_deterministic_screener_aliases_compute_core_fields():
    normalized = deterministic_normalize_screener(
        FIXTURE_ALIASES, imported_at="2026-06-20T13:30:00+00:00"
    )

    row = normalized.rows[0]
    assert row["ticker"] == "NOVA"
    assert row["premarket_price"] == 5.2
    assert row["premarket_volume"] == 1_500_000
    assert row["dollar_volume"] == 7_800_000
    assert row["gap_pct"] == 89.090909
    assert row["data_source_kind"] == "manual"
    assert row["shadow_mode"] == "true"
    assert row["paid_data"] == "false"
    assert row["source"] == "screener_import"
    assert row["raw_file_path"].endswith("raw_screener_aliases.csv")
    assert row["imported_at"] == "2026-06-20T13:30:00+00:00"


def test_unknown_enrichment_fields_stay_blank():
    normalized = deterministic_normalize_screener(
        FIXTURE_MESSY, imported_at="2026-06-20T13:31:00+00:00"
    )

    row = normalized.rows[0]
    assert row["float_shares"] == ""
    assert row["market_cap"] == ""
    assert row["short_float_pct"] == ""
    assert "float_shares_unknown" in row["coverage_warning"]
    assert row["missing_enrichment_count"] > 0


def test_normalize_screener_file_command_writes_canonical_snapshot(tmp_path):
    out_dir = tmp_path / "normalized"
    db_path = tmp_path / "shadow.sqlite"

    status = main(
        [
            "normalize-screener-file",
            "--input",
            str(FIXTURE_ALIASES),
            "--out",
            str(out_dir),
            "--db-path",
            str(db_path),
            "--ai-normalizer",
            "none",
        ]
    )

    rows = _read_csv(out_dir / "premarket_snapshot.csv")
    assert status == 0
    assert rows[0]["ticker"] == "NOVA"
    assert rows[0]["dollar_volume"] == "7800000.0"
    assert rows[0]["gap_pct"] == "89.090909"
    assert SQLiteScanStore(db_path).load_manual_snapshot_uploads()[0]["row_count"] == 4


def test_auto_shadow_from_screener_persists_scan_and_archives_raw(
    tmp_path, isolated_screener_dirs
):
    source = tmp_path / "export.csv"
    shutil.copy2(FIXTURE_ALIASES, source)
    db_path = tmp_path / "shadow.sqlite"
    out_dir = tmp_path / "auto_shadow"

    result = auto_shadow_from_screener(
        input_path=source,
        db_path=db_path,
        out_dir=out_dir,
        ai_normalizer="none",
        persist=True,
        print_rows=True,
    )
    dashboard = load_sqlite(db_path)

    assert result["status"] == "success"
    assert result["scan_summary"]["top_ticker"] == "NOVA"
    assert not source.exists()
    assert list(isolated_screener_dirs["processed"].glob("*processed*.csv"))
    assert dashboard["shadow_mode"] is True
    assert dashboard["screener_automation_runs"][0]["status"] == "success"


def test_failed_auto_shadow_moves_raw_to_failed(tmp_path, isolated_screener_dirs):
    source = tmp_path / "bad_export.csv"
    source.write_text("Symbol,Last\nBROKE,3.25\n", encoding="utf-8")

    with pytest.raises(SnapshotValidationError):
        auto_shadow_from_screener(
            input_path=source,
            db_path=tmp_path / "shadow.sqlite",
            out_dir=tmp_path / "auto_shadow",
            ai_normalizer="none",
            persist=True,
        )

    assert not source.exists()
    assert list(isolated_screener_dirs["failed"].glob("*failed*.csv"))


def test_duplicate_hash_is_skipped_and_archived(tmp_path, isolated_screener_dirs):
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    shutil.copy2(FIXTURE_ALIASES, first)
    shutil.copy2(FIXTURE_ALIASES, second)
    db_path = tmp_path / "shadow.sqlite"

    first_result = auto_shadow_from_screener(
        input_path=first,
        db_path=db_path,
        out_dir=tmp_path / "first_out",
        ai_normalizer="none",
        persist=True,
    )
    second_result = auto_shadow_from_screener(
        input_path=second,
        db_path=db_path,
        out_dir=tmp_path / "second_out",
        ai_normalizer="none",
        persist=True,
    )

    assert first_result["status"] == "success"
    assert second_result["status"] == "skipped_duplicate"
    assert not second.exists()
    assert list(isolated_screener_dirs["processed"].glob("*duplicate*.csv"))


def test_watch_screener_inbox_processes_one_file_and_stops(tmp_path, isolated_screener_dirs):
    inbox = tmp_path / "watch_inbox"
    inbox.mkdir()
    shutil.copy2(FIXTURE_ALIASES, inbox / "morning.csv")

    status = main(
        [
            "watch-screener-inbox",
            "--inbox",
            str(inbox),
            "--db-path",
            str(tmp_path / "shadow.sqlite"),
            "--out-root",
            str(tmp_path / "auto_shadow"),
            "--ai-normalizer",
            "none",
            "--max-files",
            "1",
            "--poll-seconds",
            "1",
        ]
    )

    assert status == 0
    assert not list(inbox.glob("*.csv"))
    assert list(isolated_screener_dirs["processed"].glob("*processed*.csv"))


def test_auto_shadow_daily_processes_latest_dated_file(tmp_path, isolated_screener_dirs):
    inbox = tmp_path / "daily_inbox"
    inbox.mkdir()
    shutil.copy2(FIXTURE_ALIASES, inbox / "screener_2026-06-20.csv")

    result = auto_shadow_daily(
        date="2026-06-20",
        db_path=tmp_path / "shadow.sqlite",
        inbox=inbox,
        out_root=tmp_path / "auto_shadow",
        ai_normalizer="none",
    )

    assert result["status"] == "success"
    assert result["scan_summary"]["top_ticker"] == "NOVA"
    assert result["shadow_report_status"] == "skipped_no_manual_outcomes"
    assert list(isolated_screener_dirs["processed"].glob("*processed*.csv"))


def test_codex_missing_executable_has_clear_error(tmp_path, monkeypatch):
    bad = tmp_path / "unparseable.txt"
    bad.write_text("not enough columns\n", encoding="utf-8")
    monkeypatch.setattr(screener_automation.shutil, "which", lambda _name: None)

    with pytest.raises(DataProviderError) as excinfo:
        normalize_screener_file(
            input_path=bad,
            out_dir=tmp_path / "normalized",
            ai_normalizer="codex-cli",
        )

    assert "Codex CLI is not installed" in str(excinfo.value)


def test_codex_malformed_output_is_rejected(tmp_path, monkeypatch):
    bad = tmp_path / "unparseable.txt"
    bad.write_text("not enough columns\n", encoding="utf-8")
    monkeypatch.setattr(screener_automation.shutil, "which", lambda _name: "codex")

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="codex 1.0", stderr="")
        output_arg = cmd.index("--output-last-message") + 1
        Path(cmd[output_arg]).write_text("wrong,columns\nstill,bad\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(screener_automation.subprocess, "run", fake_run)

    with pytest.raises(SnapshotValidationError):
        normalize_screener_file(
            input_path=bad,
            out_dir=tmp_path / "normalized",
            ai_normalizer="codex-cli",
        )


def test_dashboard_loader_includes_screener_automation_status(tmp_path, isolated_screener_dirs):
    source = tmp_path / "export.csv"
    shutil.copy2(FIXTURE_ALIASES, source)
    db_path = tmp_path / "shadow.sqlite"
    auto_shadow_from_screener(
        input_path=source,
        db_path=db_path,
        out_dir=tmp_path / "auto_shadow",
        ai_normalizer="none",
        persist=True,
    )

    dashboard = load_sqlite(db_path)

    assert dashboard["screener_automation_status"]["latest_auto_shadow_run"]["status"] == "success"
    assert dashboard["screener_automation_status"]["latest_normalized_snapshot"]


def _read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))
