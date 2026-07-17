from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_scheduler_preview_covers_early_and_regular_close_without_mutation() -> None:
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / "scripts" / "mover-pattern-lab" / "register_daily_workflow.ps1"),
            "-Config",
            str(root / "config" / "mover_daily_workflow.example.json"),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    preview = json.loads(result.stdout)
    assert preview["status"] == "preview_only"
    assert preview["reconcile_retry_interval_minutes"] == 30
    assert preview["reconcile_retry_start_et"] == "13:10"
    assert preview["reconcile_retry_end_et"] == "16:40"
    assert preview["legacy_task_names_to_remove"] == [
        "Dawnstrike Mover Paper Reconcile"
    ]
    reconciliations = [
        row for row in preview["tasks"] if row["stage"] == "Reconcile"
    ]
    assert [row["source_start_clock_et"] for row in reconciliations] == [
        "13:10",
        "13:40",
        "14:10",
        "14:40",
        "15:10",
        "15:40",
        "16:10",
        "16:40",
    ]
    assert [row["reconcile_retry_index"] for row in reconciliations] == list(range(8))
    assert len({row["task_name"] for row in preview["tasks"]}) == len(preview["tasks"])
    assert preview["research_only"] is True
    assert preview["broker_execution_enabled"] is False
