from pathlib import Path

import intraday_scanner.services.scheduler_doctor_service as scheduler_service


def test_scheduler_doctor_is_external_blocked_when_canonical_task_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (
        "run_daily_finalize.ps1",
        "register_daily_finalize_task.ps1",
        "restore_previous_publish_task.ps1",
    ):
        (scripts / name).write_text("placeholder", encoding="utf-8")

    monkeypatch.setattr(
        scheduler_service,
        "_query_scheduled_task",
        lambda: {
            "name": scheduler_service.CANONICAL_TASK_NAME,
            "state": "missing",
            "enabled": None,
            "last_task_result": None,
        },
    )

    result = scheduler_service.scheduler_doctor(tmp_path)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["expected_task_name"] == "Dawnstrike 10of10 Daily Finalize"


def test_scheduler_doctor_accepts_one_healthy_task(tmp_path: Path, monkeypatch) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (
        "run_daily_finalize.ps1",
        "register_daily_finalize_task.ps1",
        "restore_previous_publish_task.ps1",
    ):
        (scripts / name).write_text("placeholder", encoding="utf-8")

    monkeypatch.setattr(
        scheduler_service,
        "_query_scheduled_task",
        lambda: {
            "name": scheduler_service.CANONICAL_TASK_NAME,
            "state": "Ready",
            "enabled": True,
            "last_task_result": 0,
        },
    )

    assert scheduler_service.scheduler_doctor(tmp_path)["status"] == "LOCAL_VERIFIED"
