$ErrorActionPreference = "Stop"

$worktree = "C:\r\dawnstrike-quant-refactor-20260811"
$processes = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.ProcessId -ne $PID -and
            $_.Name -match "^(python|python3|pytest)(\.exe)?$" -and
            $_.CommandLine -match [regex]::Escape($worktree) -and
            $_.CommandLine -match "run_pytest_shard\.py|(^|\s)-m\s+pytest(\s|$)|pytest(\.exe)?(\s|$)"
        } |
        Sort-Object ProcessId |
        Select-Object ProcessId, ParentProcessId, Name, CreationDate, CommandLine
)

$payload = [ordered]@{
    schema_version = "dawnstrike.recovery_process_audit.v1"
    captured_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    worktree = $worktree
    unauthorized_process_count = $processes.Count
    processes = $processes
}
$payload | ConvertTo-Json -Depth 6
if ($processes.Count -ne 0) { exit 2 }
