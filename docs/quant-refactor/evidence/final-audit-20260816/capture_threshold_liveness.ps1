$ErrorActionPreference = "Stop"
$processId = 25464
$first = Get-Process -Id $processId -ErrorAction Stop
$firstCpu = [double]$first.CPU
$firstAt = [DateTimeOffset]::UtcNow
Start-Sleep -Seconds 15
$second = Get-Process -Id $processId -ErrorAction Stop
$secondCpu = [double]$second.CPU
$secondAt = [DateTimeOffset]::UtcNow
$stdoutPath = Join-Path $PSScriptRoot "full-pytest.stdout.txt"
$payload = [ordered]@{
    gate = "full-pytest"
    captured_at_utc = $secondAt.ToString("o")
    process_id = $processId
    start_time = $second.StartTime.ToUniversalTime().ToString("o")
    elapsed_seconds = [Math]::Round(((Get-Date) - $second.StartTime).TotalSeconds, 6)
    responding = [bool]$second.Responding
    cpu_seconds_before = [Math]::Round($firstCpu, 6)
    cpu_seconds_after = [Math]::Round($secondCpu, 6)
    sample_seconds = [Math]::Round(($secondAt - $firstAt).TotalSeconds, 6)
    cpu_delta_seconds = [Math]::Round($secondCpu - $firstCpu, 6)
    making_cpu_progress = (($secondCpu - $firstCpu) -gt 1.0)
    working_set_bytes = [long]$second.WorkingSet64
    stdout_artifact_exists_during_run = Test-Path -LiteralPath $stdoutPath
    output_note = "run_gate.ps1 buffers redirected stdout in memory and writes the raw artifact only after child termination"
    bounded_remaining_window_seconds = 1800
}
[System.IO.File]::WriteAllText(
    (Join-Path $PSScriptRoot "full-pytest.threshold-liveness.json"),
    (($payload | ConvertTo-Json -Depth 6) + "`n"),
    [System.Text.UTF8Encoding]::new($false)
)
$payload | ConvertTo-Json -Depth 6
if (-not $payload.responding -or -not $payload.making_cpu_progress) { exit 2 }
