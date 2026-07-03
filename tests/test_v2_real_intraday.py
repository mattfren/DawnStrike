# ruff: noqa: E501
from __future__ import annotations

import ast
import csv
import json
from datetime import date
from pathlib import Path

from intraday_scanner.v2.command_center import build_command_center
from intraday_scanner.v2.evidence_commit import propose
from intraday_scanner.v2.fill_truth import resolve_pending
from intraday_scanner.v2.real_intraday import (
    aggregate_daily,
    build,
    import_intraday,
    init,
    readiness,
    reconcile_daily,
    report,
    template,
    trial_day,
    validate,
)

RUN_DATE = date(2026, 6, 29)
ORDER_ID = "order:forward:2026-06-29:cross_sectional_relative_strength:v1.0:QQQ:2026-06-26T13:30:00+00:00:long"
PICK_ID = "forward:2026-06-29:cross_sectional_relative_strength:v1.0:QQQ:2026-06-26T13:30:00+00:00:long"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _append_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            json.dump(row, handle, sort_keys=True)
            handle.write("\n")


def _seed_daily_reference(close: float = 101.0) -> None:
    _write_csv(
        Path("data/v2_data_truth/normalized/latest_ohlcv.csv"),
        [
            {
                "symbol": "QQQ",
                "timestamp": "2026-06-29T13:30:00+00:00",
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": close,
                "volume": 3000,
            }
        ],
    )
    _write_json(
        Path("data/v2_data_truth/manifests/latest.json"),
        {
            "accepted_bar_count": 1,
            "provider_id": "local_daily_fixture",
            "schema_version": "fixture",
            "snapshot_id": "datatruth_fixture_20260629",
        },
    )


def _seed_paper_ops() -> None:
    pending = {
        "direction": "long",
        "entry": 100.0,
        "mode": "forward",
        "order_id": ORDER_ID,
        "order_status": "pending",
        "pick_id": PICK_ID,
        "quantity": 10,
        "risk_per_unit": 5.0,
        "run_id": "paper_ops:forward:2026-06-29:fixture",
        "signal_time": "2026-06-26T13:30:00+00:00",
        "stop": 95.0,
        "strategy_id": "cross_sectional_relative_strength",
        "strategy_version": "v1.0",
        "symbol": "QQQ",
        "target": 110.0,
    }
    _write_json(Path("data/v2_paper_ops/state/pending_orders.json"), [pending])
    _write_json(Path("data/v2_paper_ops/state/open_positions.json"), [])
    _write_json(Path("data/v2_paper_ops/state/replay_pending_orders.json"), [])
    _write_json(Path("data/v2_paper_ops/state/paper_ops_config.json"), {"starting_equity": 100000.0})
    _write_json(
        Path("data/v2_paper_ops/state/strategy_registry.json"),
        [
            {
                "strategy_id": "cross_sectional_relative_strength",
                "strategy_status": "experimental",
                "strategy_version": "v1.0",
            }
        ],
    )
    _append_jsonl(
        Path("data/v2_paper_ops/ledger/paper_ledger.jsonl"),
        [
            {
                "event_id": "pick-fixture",
                "event_type": "paper_pick_decision",
                "mode": "forward",
                "payload": {
                    "pick_id": PICK_ID,
                    "strategy_status": "experimental",
                    "strategy_version": "v1.0",
                },
                "run_id": "paper_ops:forward:2026-06-29:fixture",
                "strategy_id": "cross_sectional_relative_strength",
                "symbol": "QQQ",
                "trade_date": RUN_DATE.isoformat(),
            },
            {
                "event_id": "order-fixture",
                "event_type": "paper_order_created",
                "mode": "forward",
                "payload": pending,
                "run_id": "paper_ops:forward:2026-06-29:fixture",
                "strategy_id": "cross_sectional_relative_strength",
                "symbol": "QQQ",
                "trade_date": RUN_DATE.isoformat(),
            },
        ],
    )
    _write_json(
        Path("data/v2_forward_evidence/frozen_picks/2026-06-29_picks.json"),
        {"pick_set_hash": "frozen-real-intraday-fixture", "rows": [{"pick_id": PICK_ID}]},
    )
    _write_json(
        Path("data/v2_forward_evidence/pick_hashes/2026-06-29_hash.json"),
        {"pick_set_hash": "frozen-real-intraday-fixture"},
    )


def _write_intraday(path: Path, close: float = 101.0) -> None:
    _write_csv(
        path,
        [
            {"Date": "2026-06-29", "Time": "09:30:00", "O": 100.0, "H": 100.5, "L": 99.0, "C": 100.2, "V": 1000},
            {"Date": "2026-06-29", "Time": "10:00:00", "O": 100.2, "H": 102.0, "L": 100.0, "C": 101.0, "V": 1000},
            {"Date": "2026-06-29", "Time": "16:00:00", "O": 101.0, "H": 101.2, "L": 100.8, "C": close, "V": 1000},
        ],
    )


def test_init_and_template_create_required_import_directories(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    initialized = init()
    generated = template()

    for path in (
        Path("data/v2_real_intraday/imports/real"),
        Path("data/v2_real_intraday/imports/demo"),
        Path("data/v2_real_intraday/import_templates"),
    ):
        assert path.is_dir()
    for name in (
        "example_1min_intraday.csv",
        "example_5min_intraday.csv",
        "README.md",
    ):
        assert Path("data/v2_real_intraday/import_templates", name).exists()
    assert "data/v2_real_intraday/import_templates/README.md" in generated["templates"]
    assert "data/v2_real_intraday/import_templates/README.md" in initialized["templates"]
    readme = Path("data/v2_real_intraday/import_templates/README.md").read_text(encoding="utf-8")
    assert "templates only" in readme.lower()
    assert "not market evidence" in readme.lower()


def test_import_profiles_parse_aliases_infer_symbol_and_hash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = Path("QQQ_2026-06-29_intraday.csv")
    _write_intraday(path)

    result = import_intraday(path=path, source_label="real_local_intraday", source_timezone="America/New_York")

    assert result["accepted_row_count"] == 3
    assert result["source_label"] == "real_local_intraday"
    assert result["symbols"] == ["QQQ"]
    assert result["source_file_sha256"]
    assert result["timezone_assumption"] == "America/New_York"
    assert Path("data/v2_real_intraday/normalized/latest_intraday_ohlcv.csv").exists()


def test_validation_detects_duplicate_invalid_and_demo_label(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = Path("QQQ_bad.csv")
    _write_csv(
        path,
        [
            {"symbol": "QQQ", "timestamp": "2026-06-29T13:30:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100},
            {"symbol": "QQQ", "timestamp": "2026-06-29T13:30:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100},
            {"symbol": "QQQ", "timestamp": "2026-06-29T14:00:00+00:00", "open": 100, "high": 99, "low": 98, "close": 100, "volume": 100},
        ],
    )

    result = import_intraday(path=path, source_label="synthetic_demo_intraday")
    validation = validate(run_date=RUN_DATE)
    rejected_files = list(Path("data/v2_real_intraday/rejections").glob("rejected_rows_*.csv"))

    assert result["rejected_row_count"] == 2
    assert validation["source_label"] == "synthetic_demo_intraday"
    assert any("synthetic/demo source is blocked" in item for item in validation["warnings"])
    assert rejected_files and "duplicate timestamp" in rejected_files[0].read_text(encoding="utf-8")


def test_aggregate_and_reconcile_daily_statuses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_daily_reference(close=101.0)
    path = Path("QQQ_real.csv")
    _write_intraday(path)
    import_intraday(path=path, source_label="real_local_intraday", source_timezone="America/New_York")

    aggregate = aggregate_daily(run_date=RUN_DATE)
    reconciliation = reconcile_daily(run_date=RUN_DATE)

    row = aggregate["aggregates"][0]
    assert row["open"] == 100.0
    assert row["high"] == 102.0
    assert row["low"] == 99.0
    assert row["close"] == 101.0
    assert row["volume"] == 3000
    assert reconciliation["reconciliation_status"] == "reconciled"

    _seed_daily_reference(close=103.0)
    mismatch = reconcile_daily(run_date=RUN_DATE)
    assert mismatch["reconciliation_status"] == "mismatch"


def test_real_local_reconciled_evidence_can_commit_when_explicit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_daily_reference()
    _seed_paper_ops()
    path = Path("QQQ_real.csv")
    _write_intraday(path)
    init()
    import_intraday(path=path, source_label="real_local_intraday", source_timezone="America/New_York")
    build(run_date=RUN_DATE)
    resolve_pending(run_date=RUN_DATE)

    proposal = propose(run_date=RUN_DATE, require_real_intraday=True)
    trial = trial_day(run_date=RUN_DATE, commit=True)

    assert proposal["eligible"] == 1
    assert trial["trial_mode"] == "explicit_commit"
    assert trial["commitbridge"]["commit_events"] == 3
    assert json.loads(Path("data/v2_paper_ops/state/pending_orders.json").read_text()) == []


def test_synthetic_demo_trial_day_is_propose_only_and_blocked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_daily_reference()
    _seed_paper_ops()
    path = Path("QQQ_demo.csv")
    _write_intraday(path)
    import_intraday(path=path, source_label="synthetic_demo_intraday", source_timezone="America/New_York")

    trial = trial_day(run_date=RUN_DATE, commit=False)
    ready = readiness(run_date=RUN_DATE)

    assert trial["trial_mode"] == "propose_only"
    assert trial["commitbridge"]["commit_events"] == 0
    assert ready["status"] == "blocked_demo_or_synthetic"


def test_first_real_evidence_report_blocks_empty_real_import_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_daily_reference()
    _seed_paper_ops()
    init()
    import_payload = import_intraday(
        path=Path("data/v2_real_intraday/imports/real"),
        source_label="real_local_intraday",
        source_timezone="America/New_York",
    )
    trial = trial_day(run_date=RUN_DATE, commit=False)
    report_payload = report()

    activation = json.loads(Path("data/v2_real_intraday/reports/first_real_evidence_activation.json").read_text(encoding="utf-8"))
    scorecard = json.loads(Path("data/v2_real_intraday/reports/first_real_evidence_quality_scorecard.json").read_text(encoding="utf-8"))

    assert import_payload["file_count"] == 0
    assert trial["commitbridge"]["commit_events"] == 0
    assert report_payload["first_real_evidence_status"] == "blocked_needs_real_intraday"
    assert activation["overall_status"] == "BLOCKED_WAITING_FOR_REAL_CSV"
    assert activation["files_imported"] == []
    assert activation["commitbridge"]["commit_events"] == 0
    assert scorecard["score"] == 100
    assert Path("docs/audit/omega_first_real_evidence_release_summary.md").exists()
    assert Path("docs/audit/omega_first_real_evidence_red_team.md").exists()
    assert Path("docs/operations/first_real_evidence_runbook.md").exists()


def test_command_center_generates_real_intraday_pages(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_daily_reference()
    path = Path("QQQ_real.csv")
    _write_intraday(path)
    import_intraday(path=path, source_label="real_local_intraday", source_timezone="America/New_York")
    build(run_date=RUN_DATE)
    readiness(run_date=RUN_DATE)
    trial_day(run_date=RUN_DATE, commit=False)

    center = build_command_center()

    assert center.status == "passed"
    for page in (
        "real_intraday.html",
        "intraday_reconciliation.html",
        "real_evidence_trial.html",
        "import_readiness.html",
        "intraday_import_templates.html",
    ):
        text = Path("data/v2_command_center", page).read_text(encoding="utf-8")
        assert "research-only; no live execution." in text.lower()
        assert "<script" not in text.lower()
        assert "C:\\Users\\" not in text


def test_real_intraday_modules_avoid_live_execution_and_database_imports() -> None:
    forbidden_import_roots = {
        "app",
        "httpx",
        "requests",
        "socket",
        "sqlite3",
        "streamlit",
        "urllib",
    }
    forbidden_import_prefixes = {"intraday_scanner.integrations", "intraday_scanner.storage"}
    forbidden_calls = {"connect", "execute", "executemany", "submit" + "_order"}

    for path in Path("intraday_scanner/v2/real_intraday").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_import_roots, path
                    assert not any(alias.name.startswith(prefix) for prefix in forbidden_import_prefixes)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_import_roots, path
                assert not any(node.module.startswith(prefix) for prefix in forbidden_import_prefixes)
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    assert func.attr not in forbidden_calls, path
                elif isinstance(func, ast.Name):
                    assert func.id not in forbidden_calls, path
