from __future__ import annotations

import json
from pathlib import Path

SCRIPT_ROOT = Path("scripts")
SCHEDULER_ROOT = Path("data/v2_scheduler")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_omega_scheduler_scripts_exist_and_run_expected_commands() -> None:
    after_close = _read(SCRIPT_ROOT / "run_omega_scheduler_after_close.ps1")
    morning = _read(SCRIPT_ROOT / "run_omega_scheduler_morning_check.ps1")
    verify = _read(SCRIPT_ROOT / "run_omega_scheduler_verify.ps1")

    assert "omega_scheduler_common.ps1" in after_close
    assert "omega_scheduler_common.ps1" in morning
    assert "omega_scheduler_common.ps1" in verify
    assert "'after-close'" in after_close
    assert "'morning-check'" in morning
    assert "'verify'" in verify
    assert "'doctor'" in verify
    for text in (after_close, morning):
        assert "'--autodata'" in text
        assert "'--learn'" in text
        assert "'--market-masters'" in text
        assert "'--date'" in text
    assert "--market-masters" not in verify


def test_omega_scheduler_scripts_are_noninteractive_and_do_not_install_tasks() -> None:
    combined = "\n".join(
        _read(path)
        for path in (
            SCRIPT_ROOT / "omega_scheduler_common.ps1",
            SCRIPT_ROOT / "run_omega_scheduler_after_close.ps1",
            SCRIPT_ROOT / "run_omega_scheduler_morning_check.ps1",
            SCRIPT_ROOT / "run_omega_scheduler_verify.ps1",
        )
    ).lower()

    forbidden = (
        "schtasks",
        "register-scheduledtask",
        "start-process",
        "invoke-webrequest",
        "invoke-restmethod",
        "read-host",
        "out-gridview",
        "start http",
        "start-process",
        "submit" + "_order",
        "place" + "_order",
        "create" + "_order",
    )
    assert not any(term in combined for term in forbidden)
    assert "set-location -literalpath $reporoot" in combined
    assert "$previouserroractionpreference = $erroractionpreference" in combined
    assert "$erroractionpreference = 'continue'" in combined
    assert "data/v2_scheduler/logs" in combined
    assert "data/v2_scheduler/status" in combined
    assert "exit $exitcode" in combined


def test_omega_scheduler_status_artifacts_are_safe_and_portable() -> None:
    raw_json = (SCHEDULER_ROOT / "status/latest_status.json").read_text()
    assert raw_json.startswith("{")
    payload = json.loads(raw_json)
    markdown = _read(SCHEDULER_ROOT / "status/latest_status.md")

    assert payload["schema_version"] == "v2.scheduler_status.v1"
    assert payload["repo_root"] == "."
    assert payload["scheduled_task_installed"] is False
    assert payload["browser_opened"] is False
    assert payload["live_trading_enabled"] is False
    assert "C:\\Users\\" not in json.dumps(payload)
    assert "Live trading enabled: `false`" in markdown
    common = _read(SCRIPT_ROOT / "omega_scheduler_common.ps1")
    assert "market_masters_enabled" in common
    assert "latest_market_masters_status" in common
    assert "Market Masters enabled:" in common
    assert (SCHEDULER_ROOT / "logs").exists()


def test_omega_scheduler_docs_cover_manual_task_scheduler_setup_only() -> None:
    runbook = _read(Path("docs/operations/omega_scheduler_runbook.md")).lower()
    examples = _read(Path("docs/operations/windows_task_scheduler_examples.md")).lower()

    assert "does not install scheduled tasks" in runbook
    assert "do not run" in runbook
    assert "concurrently" in runbook
    assert "do not install tasks automatically" in examples
    assert "do not start a new instance" in examples
    assert "run_omega_scheduler_after_close.ps1" in examples
    assert "run_omega_scheduler_morning_check.ps1" in examples
    assert "run_omega_scheduler_verify.ps1" in examples
    assert "register-scheduledtask" not in runbook + examples
    assert "schtasks" not in runbook + examples
