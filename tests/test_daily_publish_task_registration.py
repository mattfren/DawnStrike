from pathlib import Path


def test_task_scripts_exist_and_do_not_overwrite_existing_task() -> None:
    register = Path("scripts/register_daily_finalize_task.ps1").read_text(encoding="utf-8")
    runner = Path("scripts/run_daily_finalize.ps1").read_text(encoding="utf-8")
    assert "Dawnstrike 10of10 Daily Finalize" in register
    assert "already exists" in register
    assert "ReplaceExisting" in register
    assert "-RuntimeRoot" in register
    assert "-StateRoot" in register
    assert "-SourceRoot" not in register
    assert "-PublicationMode" in register
    assert "-AllowDegraded" not in register
    assert "--retry-delay-seconds" in runner
    assert '$dbPath = Join-Path $state "shadow_real.sqlite"' in runner
    assert "publish_vercel_public.ps1" in runner


def test_alphaops_monitor_builds_a_weekly_repetition_cim_pattern() -> None:
    register = Path("scripts/register_alphaops_tasks.ps1").read_text(
        encoding="utf-8"
    )

    assert '-ClassName "MSFT_TaskRepetitionPattern"' in register
    assert '-ClientOnly' in register
    assert 'Interval = "PT5M"' in register
    assert 'Duration = "PT$([int]$definition.DurationHours)H"' in register
    assert "$trigger.Repetition = $repetition" in register
    assert "$trigger.Repetition.Interval =" not in register


def test_all_scheduled_runners_import_allowlisted_state_secrets() -> None:
    helper = Path("scripts/import_dawnstrike_environment.ps1").read_text(
        encoding="utf-8"
    )
    runners = (
        "run_alphaops_morning.ps1",
        "run_alphaops_monitor.ps1",
        "run_alphaops_eod.ps1",
        "run_daily_finalize.ps1",
    )

    assert 'Join-Path $StateRoot "secrets\\runtime.env"' in helper
    assert "SetEnvironmentVariable" in helper
    assert "TELEGRAM_BOT_TOKEN" in helper
    assert "TELEGRAM_CHAT_ID" in helper
    assert "Write-Output" not in helper
    for filename in runners:
        runner = Path("scripts", filename).read_text(encoding="utf-8")
        assert 'import_dawnstrike_environment.ps1"' in runner
        assert "Import-DawnstrikeEnvironment -StateRoot $state" in runner
