from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest

from intraday_scanner.v2.command_center import build_command_center
from intraday_scanner.v2.command_center.builder import REQUIRED_PAGES
from intraday_scanner.v2.omega_sentinel import core as sentinel_core
from intraday_scanner.v2.omega_sentinel.core import (
    _acquire_lock,
    _classify_alert,
    _release_lock,
    artifact_index,
    clear_stale_locks,
    doctor,
    generate_scheduler_scripts,
    init,
    lock_status,
    run,
    trial_status,
)

RUN_DATE = date(2026, 6, 29)
PICK_HASH = "hash-20260629"


class _FakeOmegaResult:
    def to_dict(self) -> dict[str, object]:
        return {
            "build_id": "omega-fixture",
            "dashboard_index": "data/v2_command_center/production.html",
            "frozen_pick_hash": PICK_HASH,
            "quality_score": 98,
            "status": "complete",
            "warnings": ["candidate_blocked_by_decision_engine"],
        }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    _write_json(
        Path("data/v2_forward_evidence/frozen_picks/2026-06-29_picks.json"),
        {
            "accepted_candidates": [],
            "accepted_end_date": "2026-06-26",
            "blocked_candidates": [{"pick_id": "blocked", "strategy_id": "s1"}],
            "completed_bar_proof": True,
            "date": "2026-06-29",
            "evidence_mode": "forward",
            "no_setup_explanations": [],
            "pick_set_hash": PICK_HASH,
            "watchlist_candidates": [{"pick_id": "watch", "strategy_id": "s2"}],
        },
    )
    _write_json(
        Path("data/v2_forward_evidence/reports/daily/2026-06-29.json"),
        {
            "accepted_data_end_date": "2026-06-26",
            "closes": 0,
            "date": "2026-06-29",
            "datatruth_status": "reconciled_with_minor_diffs",
            "fills": 0,
            "frozen_pick_hash": PICK_HASH,
            "open_positions": 0,
            "orders_pending": 1,
            "paper_ops": {"status": "passed"},
            "riskhub_status": "blocked",
            "warnings": ["candidate_blocked_by_decision_engine"],
        },
    )
    _write_json(
        Path("data/v2_forward_evidence/reconciliation/evidence_integrity.json"),
        {
            "frozen_pick_hashes": {"checked": [{"path": "p", "status": "passed"}]},
            "status": "passed",
            "warnings": [],
        },
    )
    _write_json(
        Path("data/v2_forward_evidence/reports/riskhub_daily.json"),
        {"kill_switch_active": True, "riskhub_status": "blocked", "status": "passed"},
    )
    _write_json(
        Path("data/v2_forward_evidence/strategy_evidence/strategy_evidence_omega.json"),
        {
            "rows": [
                {"evidence_status": "watch", "strategy_id": "s1"},
                {"evidence_status": "quarantined", "strategy_id": "s2"},
            ],
            "status": "passed",
        },
    )
    _write_json(
        Path("data/v2_forward_evidence/calendar/strategy_calendar_summary.json"),
        {"status": "passed", "true_forward_days_available": 1},
    )
    _write_text(
        Path("data/v2_forward_evidence/calendar/strategy_daily_returns.csv"),
        "\n".join(
            [
                "date,evidence_mode,strategy_id,daily_return_pct,drawdown_pct,trades_opened,trades_closed",
                "2026-06-29,forward,s1,0.0,0.0,1,0",
                "2026-06-29,shadow_forward_replay,s1,9.9,0.0,99,99",
            ]
        )
        + "\n",
    )
    _write_json(
        Path("data/v2_command_center/command_center_qa.json"),
        {
            "broken_links": [],
            "page_count": len(REQUIRED_PAGES),
            "required_page_count": len(REQUIRED_PAGES),
            "status": "passed",
        },
    )
    _write_text(Path("data/v2_command_center/production.html"), "research-only; no live execution.")
    _write_json(Path("data/v2_omega/reports/omega_summary.json"), {"status": "complete"})
    _write_json(Path("docs/audit/omega_build_state.json"), {"status": "complete"})
    _write_json(Path("data/v2_data_truth/manifests/latest.json"), {"status": "passed"})
    _write_text(Path("data/v2_paper_ops/reports/paper_ops_summary.md"), "# PaperOps\n")
    _write_text(Path("tests/test_v2_omega_sentinel.py"), "# fixture marker\n")
    monkeypatch.setattr(sentinel_core, "build_omega", lambda **_: _FakeOmegaResult())
    return Path("data/v2_omega_sentinel")


def test_run_lock_blocks_overlap_and_clears_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = _seed_fixture(tmp_path, monkeypatch)
    init(output_root=output_root)
    lock = _acquire_lock(
        output_root=output_root,
        run_date=RUN_DATE,
        command="test",
        stale_after_minutes=240,
    )

    assert lock_status(output_root=output_root)["state"] == "locked"
    with pytest.raises(RuntimeError):
        _acquire_lock(
            output_root=output_root,
            run_date=RUN_DATE,
            command="test",
            stale_after_minutes=240,
        )
    _release_lock(output_root=output_root, lock_id=str(lock["lock_id"]))
    assert lock_status(output_root=output_root)["state"] == "unlocked"

    old_lock = {
        "created_at": (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat(),
        "lock_id": "old",
        "stale_after_minutes": 1,
    }
    _write_json(output_root / "run_locks" / "latest.lock.json", old_lock)
    assert lock_status(output_root=output_root)["state"] == "stale"
    assert clear_stale_locks(output_root=output_root)["cleared"] is True


def test_sentinel_run_writes_status_alert_report_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = _seed_fixture(tmp_path, monkeypatch)

    result = run(run_date=RUN_DATE, output_root=output_root)
    status = json.loads((output_root / "status/latest_status.json").read_text())
    alert = json.loads((output_root / "alerts/latest_alert.json").read_text())

    assert result.status == "complete"
    assert result.quality_score >= 99
    assert result.alert_level == "yellow"
    assert status["completed_bar_status"] == "passed"
    assert status["frozen_pick_hash"] == PICK_HASH
    assert alert["alert_level"] == "yellow"
    assert (output_root / "reports/latest_omega_sentinel_report.md").exists()
    assert lock_status(output_root=output_root)["state"] == "unlocked"


def test_alert_classification_escalates_red_and_all_blocked_yellow() -> None:
    yellow, warnings, critical = _classify_alert(
        {
            "accepted_candidate_count": 0,
            "blocked_candidate_count": 2,
            "command_center_status": "passed",
            "completed_bar_status": "passed",
            "evidence_integrity_status": "passed",
            "frozen_pick_hash": "hash",
            "pending_orders": 0,
            "status": "completed",
            "warnings": [],
        }
    )
    red, _, red_critical = _classify_alert(
        {
            "accepted_candidate_count": 1,
            "blocked_candidate_count": 0,
            "command_center_status": "passed",
            "completed_bar_status": "failed",
            "evidence_integrity_status": "passed",
            "frozen_pick_hash": "",
            "status": "completed",
            "warnings": [],
        }
    )

    assert yellow == "yellow"
    assert critical == []
    assert "candidates blocked by RiskHub or Decision Engine" in warnings
    assert red == "red"
    assert "missing frozen pick hash" in red_critical


def test_trial_status_counts_forward_and_excludes_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = _seed_fixture(tmp_path, monkeypatch)
    init(output_root=output_root)
    _write_json(
        output_root / "status/2026-06-29_status.json",
        {
            "accepted_candidate_count": 0,
            "blocked_candidate_count": 2,
            "completed_bar_status": "passed",
            "data_truth_status": "reconciled_with_minor_diffs",
            "evidence_integrity_status": "passed",
            "evidence_mode": "forward",
            "frozen_pick_count": 2,
            "frozen_pick_hash": PICK_HASH,
            "run_date": "2026-06-29",
        },
    )
    _write_json(
        output_root / "status/2026-06-30_status.json",
        {
            "completed_bar_status": "passed",
            "data_truth_status": "passed",
            "evidence_integrity_status": "passed",
            "evidence_mode": "shadow_forward_replay",
            "frozen_pick_hash": "shadow",
            "run_date": "2026-06-30",
        },
    )

    trial = trial_status(output_root=output_root)
    opened = cast(dict[str, int], trial["strategy_forward_trade_count"])
    closed = cast(dict[str, int], trial["strategy_forward_closed_trade_count"])

    assert trial["completed_forward_days"] == 1
    assert trial["days_with_all_candidates_blocked"] == 1
    assert opened["s1"] == 1
    assert closed["s1"] == 0
    assert trial["days_remaining_to_minimum_evidence"] == 29


def test_artifact_index_hashes_without_deleting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = _seed_fixture(tmp_path, monkeypatch)
    init(output_root=output_root)
    source = Path("data/v2_forward_evidence/frozen_picks/2026-06-29_picks.json")

    index = artifact_index(output_root=output_root, run_date=RUN_DATE)
    rows = cast(list[dict[str, object]], index["rows"])

    assert source.exists()
    assert any(row["retention_class"] == "critical_evidence" for row in rows)
    assert all("C:" not in str(row["path"]) for row in rows)
    assert all(row["sha256"] for row in rows)


def test_scheduler_scripts_are_local_and_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = _seed_fixture(tmp_path, monkeypatch)

    result = generate_scheduler_scripts(output_root=output_root)
    scripts = cast(list[str], result["scripts"])
    for script in scripts:
        text = Path(script).read_text(encoding="utf-8").lower()
        assert "omega_sentinel" in text
        assert "schtasks" not in text
        assert "secret" not in text
        assert "broker" not in text


def test_command_center_sentinel_pages_pass_qa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = _seed_fixture(tmp_path, monkeypatch)
    run(run_date=RUN_DATE, output_root=output_root)

    result = build_command_center()
    qa = json.loads(Path("data/v2_command_center/command_center_qa.json").read_text())

    assert result.status == "passed"
    assert qa["status"] == "passed"
    assert "production.html" in REQUIRED_PAGES
    production = Path("data/v2_command_center/production.html").read_text(encoding="utf-8")
    assert "Command Center X2 is the default local production operator view" in production
    assert "research-only; no live execution." in production.lower()
    assert "<script" not in production.lower()
    for page in (
        "omega_sentinel.html",
        "forward_trial.html",
        "daily_status.html",
        "alerts.html",
        "artifact_index.html",
    ):
        text = Path("data/v2_command_center", page).read_text(encoding="utf-8")
        assert "research-only; no live execution." in text.lower()
        assert "<script" not in text.lower()


def test_doctor_detects_missing_artifact_and_passes_valid_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = _seed_fixture(tmp_path, monkeypatch)
    run(run_date=RUN_DATE, output_root=output_root)
    assert doctor(output_root=output_root)["status"] == "passed"

    Path("data/v2_omega/reports/omega_summary.json").unlink()
    failed = doctor(output_root=output_root)
    assert failed["status"] == "failed"
    failures = cast(list[str], failed["failures"])
    assert any("omega_summary" in item for item in failures)


def test_sentinel_safety_scan_has_no_forbidden_runtime_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = _seed_fixture(tmp_path, monkeypatch)
    init(output_root=output_root)

    safety = sentinel_core._safety_scan(output_root)

    assert safety["failures"] == []
