from __future__ import annotations

import json
from pathlib import Path

from intraday_scanner.v2.autonomous_runner import TASKS, init, report, status, verify, watchdog

SCRIPT_ROOT = Path("scripts")
RUNNER_ROOT = Path("data/v2_autonomous_runner")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_autonomous_runner_scripts_exist_and_preserve_exit_codes() -> None:
    scripts = {
        "install": SCRIPT_ROOT / "install_omega_autonomous_tasks.ps1",
        "uninstall": SCRIPT_ROOT / "uninstall_omega_autonomous_tasks.ps1",
        "status": SCRIPT_ROOT / "status_omega_autonomous_tasks.ps1",
        "test": SCRIPT_ROOT / "test_omega_autonomous_tasks.ps1",
        "watchdog": SCRIPT_ROOT / "run_omega_scheduler_watchdog.ps1",
    }
    for path in scripts.values():
        assert path.exists()

    combined = "\n".join(_read(path) for path in scripts.values()).lower()
    assert "set-location -literalpath $reporoot" in combined
    assert "exit $exitcode" in combined
    assert "multipleinstances ignorenew" in combined
    assert "do_not_start_new_instance" in combined
    assert "register-scheduledtask" in _read(scripts["install"]).lower()
    assert "unregister-scheduledtask" in _read(scripts["uninstall"]).lower()
    assert "get-scheduledtask" in _read(scripts["uninstall"]).lower()


def test_autonomous_runner_scripts_do_not_expose_secrets_or_live_trading() -> None:
    combined = "\n".join(
        _read(path)
        for path in (
            SCRIPT_ROOT / "install_omega_autonomous_tasks.ps1",
            SCRIPT_ROOT / "uninstall_omega_autonomous_tasks.ps1",
            SCRIPT_ROOT / "status_omega_autonomous_tasks.ps1",
            SCRIPT_ROOT / "test_omega_autonomous_tasks.ps1",
            SCRIPT_ROOT / "run_omega_scheduler_watchdog.ps1",
        )
    ).lower()
    forbidden = (
        "submit" + "_order",
        "place" + "_order",
        "create" + "_order",
        "live_execute",
        "authorization: bearer ",
        "api_key=",
        "secret=",
        "password=",
        "token=",
    )
    assert not any(term in combined for term in forbidden)
    assert "live_trading_enabled = $false" in combined


def test_autonomous_runner_task_definitions_are_safe() -> None:
    init()
    definitions = sorted((RUNNER_ROOT / "task_definitions").glob("*.json"))
    assert len(definitions) >= len(TASKS)
    all_text = "\n".join(_read(path).lower() for path in definitions)
    for task in TASKS:
        assert task.task_name.lower() in all_text
        assert task.script.lower() in all_text
        assert task.schedule_time in all_text
    assert "ignorenew" in all_text
    assert "run_only_when_user_logged_on" in all_text
    assert "secrets_embedded" in all_text
    assert "submit" + "_order" not in all_text
    assert "place" + "_order" not in all_text
    assert "create" + "_order" not in all_text


def test_autonomous_runner_core_is_isolated_from_app_streamlit_and_sqlite() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in Path("intraday_scanner/v2/autonomous_runner").glob("*.py")
    )
    assert "import app" not in text
    assert "from app" not in text
    assert "import streamlit" not in text
    assert "from streamlit" not in text
    assert "import sqlite" + "3" not in text
    assert "." + "sqlite" not in text
    assert "intraday_scanner.storage" not in text


def test_autonomous_runner_reports_and_command_center_pages_generate() -> None:
    init()
    status_payload = status()
    watchdog_payload = watchdog()
    verify_payload = verify()
    report_payload = report()

    assert status_payload["schema_version"] == "v2.autonomous_runner.status.v1"
    assert watchdog_payload["schema_version"] == "v2.autonomous_runner.watchdog.v1"
    assert verify_payload["status"] == "passed"
    assert report_payload["status"] == "reported"
    assert status_payload["market_masters_enabled"] is True
    assert status_payload["latest_market_masters_status"] != "missing"
    assert watchdog_payload["market_masters_enabled"] is True
    assert watchdog_payload["market_masters_shadow_only"] is True
    assert watchdog_payload["market_masters_champion_registry_changed"] is False

    for path in (
        RUNNER_ROOT / "status/latest_status.json",
        RUNNER_ROOT / "status/latest_status.md",
        RUNNER_ROOT / "reports/task_installation_report.md",
        RUNNER_ROOT / "reports/task_installation_report.json",
        RUNNER_ROOT / "reports/autonomous_runner_status.md",
        RUNNER_ROOT / "reports/autonomous_runner_status.json",
        RUNNER_ROOT / "reports/market_masters_autonomy_status.md",
        RUNNER_ROOT / "reports/market_masters_autonomy_status.json",
        RUNNER_ROOT / "health/watchdog_latest.md",
        RUNNER_ROOT / "health/watchdog_latest.json",
    ):
        assert path.exists()

    for name in (
        "autonomous_runner.html",
        "task_scheduler.html",
        "scheduler_status.html",
        "watchdog.html",
        "missed_runs.html",
    ):
        html = _read(Path("data/v2_command_center") / name)
        assert "<script" not in html.lower()
        assert "C:\\Users\\" not in html
        assert "Research-only; no live execution." in html

    autonomous_html = _read(Path("data/v2_command_center/autonomous_runner.html"))
    scheduler_html = _read(Path("data/v2_command_center/scheduler_status.html"))
    assert "market_masters.html" in autonomous_html
    assert "market_masters_challengers.html" in autonomous_html
    assert "Market Masters" in scheduler_html

    payload = json.loads((RUNNER_ROOT / "reports/task_installation_report.json").read_text())
    assert payload["uninstall_command"].endswith(
        "scripts/uninstall_omega_autonomous_tasks.ps1 -Yes"
    )

    wiring_docs = (
        "docs/audit/omega_market_masters_autonomy_wiring_summary.md",
        "docs/audit/omega_market_masters_autonomy_wiring_quality_scorecard.md",
        "docs/audit/omega_market_masters_autonomy_wiring_red_team.md",
        "docs/audit/omega_market_masters_autonomy_wiring_build_state.json",
    )
    for path in wiring_docs:
        assert Path(path).exists()
