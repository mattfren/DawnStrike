param(
    [Parameter(Mandatory = $true)]
    [string]$Output
)

$ErrorActionPreference = "Stop"
$processes = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.ProcessId -ne $PID -and
            $_.Name -match "^(python|python3|pytest|py)(\.exe)?$" -and
            $_.CommandLine -match "run_pytest_shard\.py|(^|\s)-m\s+pytest(\s|$)|pytest(\.exe)?(\s|$)"
        } |
        Sort-Object ProcessId |
        Select-Object ProcessId, ParentProcessId, Name, CreationDate, CommandLine
)
$payload = [ordered]@{
    schema_version = "dawnstrike.final_gate_process_audit.v1"
    captured_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    unauthorized_process_count = $processes.Count
    processes = $processes
}
$json = $payload | ConvertTo-Json -Depth 6
[IO.File]::WriteAllText((Join-Path (Get-Location) $Output), $json + [Environment]::NewLine)
$json
if ($processes.Count -ne 0) { exit 2 }
