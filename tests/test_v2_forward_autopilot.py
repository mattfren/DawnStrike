from __future__ import annotations

import ast
import csv
import json
from pathlib import Path

from intraday_scanner.v2.calendar_intelligence import build_calendar_intelligence
from intraday_scanner.v2.command_center.builder import REQUIRED_PAGES, build_command_center
from intraday_scanner.v2.evidence_vault import (
    create_paths,
    verify_frozen_pick_hashes,
    write_frozen_pick_set,
)
from intraday_scanner.v2.forward_autopilot.core import _nested_count, _source_artifact_hashes


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_evidence_vault_hashes_are_stable_and_zero_checked_fails(tmp_path: Path) -> None:
    empty = create_paths(tmp_path / "empty_forward")
    empty_check = verify_frozen_pick_hashes(empty)
    assert empty_check["status"] == "failed"
    assert empty_check["failures"] == ["no frozen pick files found"]

    paths = create_paths(tmp_path / "forward")
    payload: dict[str, object] = {
        "accepted_candidates": [{"pick_id": "pick-a", "symbol": "TST"}],
        "accepted_end_date": "2026-06-26",
        "blocked_candidates": [],
        "code_version": "fixture",
        "data_snapshot_id": "snapshot-20260626",
        "date": "2026-06-29",
        "evidence_mode": "forward",
        "schema_version": "fixture",
    }

    first = write_frozen_pick_set(
        payload=payload,
        date_value="2026-06-29",
        evidence_mode="forward",
        paths=paths,
    )
    second = write_frozen_pick_set(
        payload=payload,
        date_value="2026-06-29",
        evidence_mode="forward",
        paths=paths,
    )
    changed = write_frozen_pick_set(
        payload={**payload, "accepted_end_date": "2026-06-27"},
        date_value="2026-06-29",
        evidence_mode="forward",
        paths=paths,
    )
    changed_again = write_frozen_pick_set(
        payload={**payload, "accepted_end_date": "2026-06-27"},
        date_value="2026-06-29",
        evidence_mode="forward",
        paths=paths,
    )
    check = verify_frozen_pick_hashes(paths)

    assert first.status == "written"
    assert second.status == "verified_existing"
    assert first.pick_set_hash == second.pick_set_hash
    assert changed.status == "superseding_written"
    assert changed_again.status == "verified_existing"
    assert changed_again.reason == "same_superseding_hash"
    assert changed.pick_set_hash != first.pick_set_hash
    assert check["status"] == "passed"
    assert len(check["checked"]) == 2


def test_calendar_intelligence_writes_json_and_overtrading_report(tmp_path: Path) -> None:
    paper = tmp_path / "paper"
    forward = tmp_path / "forward"
    _write_csv(
        paper / "calendar" / "strategy_daily_returns.csv",
        [
            {
                "average_r": 0.2,
                "cumulative_return_pct": 1.0,
                "daily_return_pct": 1.0,
                "data_snapshot_id": "snapshot-20260626",
                "date": "2026-06-29",
                "drawdown_pct": 0,
                "ending_equity": 10100,
                "expectancy_r": 0.2,
                "fees_paid": 0,
                "flats": 0,
                "losses": 0,
                "open_positions": 1,
                "pending_orders": 0,
                "realized_pnl": 0,
                "slippage_estimate": 0,
                "starting_equity": 10000,
                "strategy_id": "test_strategy",
                "strategy_status": "watch",
                "strategy_version": "v1",
                "total_pnl": 100,
                "trades_closed": 0,
                "trades_opened": 1,
                "unrealized_pnl": 100,
                "warnings": "",
                "wins": 0,
            }
        ],
    )

    result = build_calendar_intelligence(output_root=forward, paper_ops_root=paper)
    rows = json.loads((forward / "calendar" / "strategy_daily_returns.json").read_text())
    overtrading = (forward / "calendar" / "strategy_overtrading_report.csv").read_text()

    assert result.rows == 1
    assert rows[0]["evidence_mode"] == "forward"
    assert rows[0]["pick_set_hash"] == "n/a"
    assert "test_strategy" in overtrading


def test_source_artifact_hashes_ignore_volatile_json_timestamps(tmp_path: Path) -> None:
    manifest = tmp_path / "latest.json"
    _write_json(
        manifest,
        {
            "created_at": "2026-06-30T12:00:00+00:00",
            "snapshot_id": "snapshot-1",
        },
    )
    first = _source_artifact_hashes((manifest,))
    _write_json(
        manifest,
        {
            "created_at": "2026-06-30T12:01:00+00:00",
            "snapshot_id": "snapshot-1",
        },
    )
    second = _source_artifact_hashes((manifest,))
    _write_json(
        manifest,
        {
            "created_at": "2026-06-30T12:02:00+00:00",
            "snapshot_id": "snapshot-2",
        },
    )
    third = _source_artifact_hashes((manifest,))

    assert first == second
    assert second != third


def test_nested_count_ignores_non_numeric_repeated_keys() -> None:
    payload = {
        "check": {"open_positions": 0},
        "state": {"open_positions": []},
        "summary": {"nested": {"open_positions": 2}},
    }

    assert _nested_count(payload, "open_positions") == 2


def test_command_center_builds_omega_forward_pages_without_false_blocked_warning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    titan = tmp_path / "titan"
    alpha = tmp_path / "alpha"
    data_truth = tmp_path / "datatruth"
    paper = tmp_path / "paper"
    forward = tmp_path / "forward"
    output = tmp_path / "command_center"

    _write_json(titan / "decision_engine" / "decision_cards.json", [{"symbol": "TST"}])
    _write_json(titan / "decision_engine" / "watchlist.json", [{"symbol": "ALT"}])
    _write_text(titan / "risk" / "risk_report.md", "# Risk\n")
    _write_text(alpha / "reports" / "alpha_lab_summary.md", "# Alpha\n")
    _write_json(alpha / "reports" / "strategy_comparison.json", [{"strategy_id": "test"}])
    _write_text(data_truth / "reports" / "data_truth_summary.md", "# DataTruth\nsingle_provider\n")
    _write_text(paper / "reports" / "paper_ops_summary.md", "# PaperOps\n")
    _write_text(paper / "calendar" / "calendar_summary.md", "# Calendar\n")
    _write_text(paper / "reports" / "strategy_evidence_summary.md", "# Evidence\n")
    _write_json(
        paper / "reports" / "forward_readiness.json",
        {"status": "ready_with_warnings", "warnings": ["not blocked by substring"]},
    )
    _write_text(Path("docs/audit/titan_release_summary.md"), "# Titan Audit\n")
    _write_text(Path("docs/operations/titan_daily_runbook.md"), "# Titan Runbook\n")
    _write_text(Path("docs/audit/forward_autopilot_summary.md"), "# Forward Summary\n")

    _write_text(forward / "calendar" / "strategy_calendar_summary.md", "# Forward Calendar\n")
    _write_csv(
        forward / "calendar" / "strategy_decay_report.csv",
        [{"strategy_id": "test", "status": "watch"}],
    )
    _write_json(
        forward / "frozen_picks" / "2026-06-29_picks.json",
        {
            "accepted_candidates": [{"pick_id": "p1", "symbol": "TST"}],
            "blocked_candidates": [],
            "date": "2026-06-29",
            "pick_set_hash": "hash",
        },
    )
    _write_text(forward / "reports" / "riskhub_daily.md", "# RiskHub Daily\n")
    _write_text(forward / "reports" / "daily" / "2026-06-29.md", "# Daily Run\n")
    _write_text(forward / "reconciliation" / "evidence_integrity.md", "# Integrity\n")
    _write_text(
        forward / "shadow_replay" / "reports" / "shadow_replay_summary.md",
        "# Shadow\n",
    )

    result = build_command_center(
        output_root=output,
        titan_root=titan,
        alpha_root=alpha,
        data_truth_root=data_truth,
        paper_ops_root=paper,
        forward_root=forward,
    )
    qa = json.loads((output / "command_center_qa.json").read_text())

    assert result.status == "passed"
    assert qa["status"] == "passed"
    assert qa["page_count"] == len(REQUIRED_PAGES)
    assert all((output / page).exists() for page in REQUIRED_PAGES)
    assert "PaperOps readiness is blocked." not in result.warnings


def test_v2_forward_modules_do_not_import_legacy_ui_storage_or_brokers() -> None:
    roots = [
        Path("intraday_scanner/v2/forward_autopilot"),
        Path("intraday_scanner/v2/evidence_vault"),
        Path("intraday_scanner/v2/calendar_intelligence"),
        Path("intraday_scanner/v2/omega"),
    ]
    forbidden = {
        "app",
        "streamlit",
        "sqlite3",
        "intraday_scanner.storage",
        "intraday_scanner.integrations",
    }
    imports: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)

    assert not sorted(
        imported
        for imported in imports
        for blocked in forbidden
        if imported == blocked or imported.startswith(blocked + ".")
    )
