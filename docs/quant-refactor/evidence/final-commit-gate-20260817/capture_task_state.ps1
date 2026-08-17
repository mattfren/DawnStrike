param(
    [Parameter(Mandatory = $true)]
    [string]$Output
)

$ErrorActionPreference = "Stop"
$name = "Dawnstrike AlphaOps Monitor 5m"
$task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
$info = Get-ScheduledTaskInfo -TaskName $name -ErrorAction Stop
$actions = @(
    $task.Actions | ForEach-Object {
        [ordered]@{
            execute = $_.Execute
            arguments = $_.Arguments
            working_directory = $_.WorkingDirectory
        }
    }
)
$stateRoot = "C:\r\dawnstrike-state"
$logs = @(
    Get-ChildItem -LiteralPath (Join-Path $stateRoot "logs") -File |
        Where-Object {
            $_.Name -match "2026-08-17" -and
            $_.Name -match "^(alpha_monitor|trade_watch|scenario_monitor|record_stage-intraday_monitor|record_stage-scenario_intelligence).*\.(receipt\.json|stdout\.log|stderr\.log)$"
        } |
        Sort-Object FullName |
        ForEach-Object {
            [ordered]@{
                path = $_.FullName
                length = $_.Length
                mtime_utc = $_.LastWriteTimeUtc.ToString("o")
            }
        }
)
$payload = [ordered]@{
    schema_version = "dawnstrike.final_commit_gate_task_state.v1"
    captured_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    task_name = $task.TaskName
    task_path = $task.TaskPath
    enabled = [bool]$task.Settings.Enabled
    state = [string]$task.State
    last_run_time_utc = $info.LastRunTime.ToUniversalTime().ToString("o")
    last_task_result = [int64]$info.LastTaskResult
    next_run_time_utc = $info.NextRunTime.ToUniversalTime().ToString("o")
    actions = $actions
    matching_runtime_log_files = $logs
}
$rendered = $payload | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText((Join-Path (Get-Location) $Output), $rendered + [Environment]::NewLine)
$rendered
