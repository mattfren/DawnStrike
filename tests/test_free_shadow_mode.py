import csv
import json
from pathlib import Path

from intraday_scanner.cli import main
from intraday_scanner.dashboard.data_loader import load_sqlite
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.services.free_shadow_mode import (
    build_free_universe,
    import_manual_outcomes,
    import_manual_snapshot,
    print_upload_prompt,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_print_upload_prompt_instructs_not_to_invent_fields():
    prompt = print_upload_prompt()

    assert "Do not invent missing values" in prompt
    assert "ticker,company,previous_close" in prompt
    assert "catalyst_url" in prompt


def test_manual_snapshot_import_calculates_volume_gap_and_marks_unknowns(tmp_path):
    source = tmp_path / "manual.csv"
    source.write_text(
        "ticker,company,previous_close,premarket_price,premarket_high,premarket_low,"
        "premarket_volume,source,as_of_timestamp\n"
        "TEST,Test Corp,2.00,3.00,3.10,2.70,100000,manual_upload,"
        "2026-06-18T09:25:00-04:00\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "shadow.sqlite"

    result = import_manual_snapshot(
        input_path=source,
        out_dir=tmp_path / "normalized",
        store=SQLiteScanStore(db_path),
        persist=True,
    )

    rows = _read_csv(Path(result["path"]))
    row = rows[0]
    assert row["dollar_volume"] == "300000.0"
    assert row["gap_pct"] == "50.0"
    assert row["data_source_kind"] == "manual"
    assert row["shadow_mode"] == "true"
    assert row["paid_data"] == "false"
    assert "float_shares_unknown" in row["coverage_warning"]
    assert SQLiteScanStore(db_path).load_manual_snapshot_uploads()[0]["row_count"] == 1


def test_free_shadow_mode_template_workflow_persists_and_reports(tmp_path):
    db_path = tmp_path / "shadow.sqlite"
    snapshot_out = tmp_path / "snapshot"
    scan_out = tmp_path / "scan"
    audit_out = tmp_path / "audit"
    report_out = tmp_path / "report"

    assert main(["init-db", "--db-path", str(db_path)]) == 0
    assert (
        main(
            [
                "import-manual-snapshot",
                "--input",
                "templates/manual_premarket_snapshot_template.csv",
                "--out",
                str(snapshot_out),
                "--db-path",
                str(db_path),
                "--persist",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "free-shadow-scan",
                "--snapshot",
                str(snapshot_out / "premarket_snapshot.csv"),
                "--db-path",
                str(db_path),
                "--out-dir",
                str(scan_out),
                "--persist",
                "--print",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "import-manual-outcomes",
                "--input",
                "templates/manual_outcomes_template.csv",
                "--db-path",
                str(db_path),
                "--persist",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "audit-manual-outcomes",
                "--db-path",
                str(db_path),
                "--out-dir",
                str(audit_out),
                "--persist",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "free-shadow-report",
                "--db-path",
                str(db_path),
                "--out-dir",
                str(report_out),
                "--persist",
            ]
        )
        == 0
    )

    ranked = _read_csv(scan_out / "ranked_candidates.csv")
    audit_rows = _read_csv(audit_out / "manual_audit_trades.csv")
    report = json.loads((report_out / "free_shadow_report.json").read_text(encoding="utf-8"))
    dashboard = load_sqlite(db_path)

    assert ranked[0]["data_source_kind"] == "manual"
    assert ranked[0]["paid_data"] == "False"
    wide = next(row for row in audit_rows if row["ticker"] == "WIDE")
    assert wide["close_return_pct"] == ""
    assert wide["close_return_status"] == "unavailable"
    assert report["manual_uploaded_data"] is True
    assert report["paid_data"] is False
    assert report["top_1_close_return_pct"] == 27.31
    assert report["top_3_close_return_pct"] == 24.49
    assert report["compounded_top_3_equity_curve"]
    assert dashboard["shadow_mode"] is True
    assert dashboard["manual_outcomes"]
    assert dashboard["shadow_report"]["trade_count"] == 3


def test_manual_outcome_before_recommendation_is_rejected(tmp_path):
    db_path = tmp_path / "shadow.sqlite"
    snapshot_out = tmp_path / "snapshot"
    early = tmp_path / "early_outcome.csv"

    main(["init-db", "--db-path", str(db_path)])
    main(
        [
            "import-manual-snapshot",
            "--input",
            "templates/manual_premarket_snapshot_template.csv",
            "--out",
            str(snapshot_out),
            "--db-path",
            str(db_path),
            "--persist",
        ]
    )
    main(
        [
            "free-shadow-scan",
            "--snapshot",
            str(snapshot_out / "premarket_snapshot.csv"),
            "--db-path",
            str(db_path),
            "--out-dir",
            str(tmp_path / "scan"),
            "--persist",
        ]
    )
    early.write_text(
        "date,ticker,entry_time,entry_price,price_1m,price_5m,price_15m,lunch_price,"
        "close_price,high_after_entry,low_after_entry,halted,source,notes\n"
        "2026-06-18,NOVA,2026-06-18T09:20:00-04:00,5.2,,,,,,,,false,"
        "manual_outcome_upload,too early\n",
        encoding="utf-8",
    )

    try:
        import_manual_outcomes(
            input_path=early,
            store=SQLiteScanStore(db_path),
            persist=True,
        )
    except SnapshotValidationError as exc:
        assert "before recommendation" in str(exc)
    else:
        raise AssertionError("Expected no-lookahead validation to reject early outcome")


def test_template_csvs_are_valid_inputs(tmp_path):
    db_path = tmp_path / "shadow.sqlite"
    snapshot = import_manual_snapshot(
        input_path="templates/manual_premarket_snapshot_template.csv",
        out_dir=tmp_path / "snapshot",
        store=SQLiteScanStore(db_path),
        persist=True,
    )

    assert Path(snapshot["path"]).exists()
    outcomes = _read_csv("templates/manual_outcomes_template.csv")
    assert outcomes[0]["source"] == "manual_outcome_upload"


def test_build_free_universe_uses_fixture_without_network(tmp_path):
    result = build_free_universe(
        out_path=tmp_path / "universe_us_common.csv",
        rejected_path=tmp_path / "universe_rejected.csv",
        summary_path=tmp_path / "universe_build_summary.json",
    )

    rows = _read_csv(result["paths"]["universe"])
    rejected = _read_csv(result["paths"]["rejected"])
    assert rows
    assert rows[0]["source"] == "bundled_fixture_free"
    assert result["summary"]["paid_data"] is False
    assert result["summary"]["fixture_mode"] is True
    assert len(rejected) == result["summary"]["rejected_count"]


def _read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))
