from pathlib import Path


def test_task_scripts_exist_and_do_not_overwrite_existing_task() -> None:
    register = Path("scripts/register_daily_finalize_task.ps1").read_text(encoding="utf-8")
    runner = Path("scripts/run_daily_finalize.ps1").read_text(encoding="utf-8")
    assert "Dawnstrike 10of10 Daily Finalize" in register
    assert "already exists" in register
    assert "--retry-delay-seconds" in runner
