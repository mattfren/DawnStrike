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
    assert "Required daily finalize run blocked by daily lock" in runner
    assert "exit $(if ($receiptWritten) { 3 } else { 2 })" in runner

    eod = Path("scripts/run_alphaops_eod.ps1").read_text(encoding="utf-8")
    assert "Required AlphaOps EOD run blocked by daily lock" in eod
    assert "exit $(if ($receiptWritten) { 3 } else { 2 })" in eod
    assert "Write-DawnstrikeLockDenialReceipt" in eod
    assert "Write-DawnstrikeLockDenialReceipt" in runner

    weekly = Path("scripts/run_alphaops_weekly_training.ps1").read_text(
        encoding="utf-8"
    )
    assert "Write-DawnstrikeLockDenialReceipt" in weekly

    lock_helper = Path("scripts/invoke_dawnstrike_stage.ps1").read_text(
        encoding="utf-8"
    )
    assert "dawnstrike.daily_run_lock.v2" in lock_helper
    assert "lock_token" in lock_helper
    assert "Get-Process -Id" in lock_helper
    assert "dawnstrike.lock_denial.v1" in lock_helper

    morning = Path("scripts/run_alphaops_morning.ps1").read_text(encoding="utf-8")
    monitor = Path("scripts/run_alphaops_monitor.ps1").read_text(encoding="utf-8")
    assert "Required AlphaOps morning run blocked by daily lock" in morning
    assert "Required AlphaOps monitor run blocked by daily lock" in monitor
    assert "daily_lock_unavailable" in morning
    assert "daily_lock_unavailable" in monitor


def test_task_scripts_use_the_installed_windows_battery_safe_switches() -> None:
    scripts = (
        Path("scripts/register_alphaops_tasks.ps1").read_text(encoding="utf-8"),
        Path("scripts/register_daily_finalize_task.ps1").read_text(encoding="utf-8"),
    )

    for script in scripts:
        assert "-AllowStartIfOnBatteries" in script
        assert "-DontStopIfGoingOnBatteries" in script
        assert "-DisallowStartIfOnBatteries" not in script
        assert "-StopIfGoingOnBatteries" not in script


def test_alphaops_monitor_builds_a_weekly_repetition_cim_pattern() -> None:
    register = Path("scripts/register_alphaops_tasks.ps1").read_text(
        encoding="utf-8"
    )

    assert '-ClassName "MSFT_TaskRepetitionPattern"' in register
    assert '-ClientOnly' in register
    assert 'Interval = "PT5M"' in register
    assert 'RepetitionDuration = "PT6H35M"' in register
    assert 'Duration = [string]$definition.RepetitionDuration' in register
    assert 'Start = "21:00"' in register
    assert "ExecutionLimitMinutes = 60" in register
    assert "ExecutionLimitMinutes = 4" in register
    assert "New-TimeSpan -Minutes ([int]$definition.ExecutionLimitMinutes)" in register
    assert "validate_web_source_config.py" in register
    assert "failed semantic validation" in register
    assert "source-config-validation.json" in register
    assert "--runtime-root" in register
    assert "--receipt" in register
    assert "RestartIntervalMinutes = 15" in register
    assert "ReuseExistingPrincipal" in register
    assert "Set-ScheduledTask" in register
    assert "Existing Password tasks require RunAsCredential" in register
    assert "-ErrorAction Stop" in register
    assert "preserving its approved stored principal" in register
    assert register.index("$taskPreflight = @{}") < register.index("Set-ScheduledTask")
    assert register.index("Existing AlphaOps tasks do not share") < register.index(
        "Set-ScheduledTask"
    )
    assert "$trigger.Repetition = $repetition" in register
    assert "$trigger.Repetition.Interval =" not in register


def test_weekly_training_waits_for_exact_release_finalize_receipt() -> None:
    runner = Path("scripts/run_alphaops_weekly_training.ps1").read_text(
        encoding="utf-8"
    )
    verifier = Path("scripts/verify_daily_finalize_receipt.py").read_text(
        encoding="utf-8"
    )

    assert "verify_daily_finalize_receipt.py" in runner
    assert "Task Scheduler will apply bounded retry" in runner
    assert "FINALIZE_STAGES" in verifier
    assert '"publication"' in verifier
    assert '"readiness"' in verifier


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
    assert "DAWNSTRIKE_FORWARD_GAP_HMAC_KEY" in helper
    assert "Write-Output" not in helper
    for filename in runners:
        runner = Path("scripts", filename).read_text(encoding="utf-8")
        assert 'import_dawnstrike_environment.ps1"' in runner
        assert "Import-DawnstrikeEnvironment -StateRoot $state" in runner


def test_native_process_runner_preserves_real_exit_code_when_stderr_is_used() -> None:
    runner = Path("scripts/dawnstrike_process_runner.ps1").read_text(
        encoding="utf-8"
    )

    assert '$previousErrorActionPreference = $ErrorActionPreference' in runner
    assert '$ErrorActionPreference = "Continue"' in runner
    assert '$ErrorActionPreference = $previousErrorActionPreference' in runner
    assert "$exitCode = if ($null -eq $LASTEXITCODE)" in runner


def test_eod_retry_reuses_only_a_reconciled_existing_paperops_day() -> None:
    runner = Path("scripts/run_alphaops_eod.ps1").read_text(encoding="utf-8")

    assert '"reports\\daily\\forward_$MarketDate.json"' in runner
    assert '"paperops-resume-reconcile-$MarketDate"' in runner
    assert "$reuseExistingDailyReport = ($paperExit -eq 0)" in runner
    assert "-not $reuseExistingDailyReport" in runner
    assert "all truth checks below still run" in runner
