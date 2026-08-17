$ErrorActionPreference = "Stop"
$processId = 25464
$cim = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
if ($null -eq $cim) { throw "Expected pytest process $processId is absent." }
if ([string]$cim.CommandLine -notmatch 'python\.exe -m pytest -q -p no:cacheprovider') {
    throw "Refusing to terminate unexpected process: $($cim.CommandLine)"
}
$runtime = Get-Process -Id $processId -ErrorAction Stop
$stdoutPath = Join-Path $PSScriptRoot "full-pytest.stdout.txt"
$payload = [ordered]@{
    gate = "full-pytest"
    reason = "bounded_remaining_window_exhausted_after_healthy_threshold_check"
    classification = "INFRA_FAILURE"
    process_id = $processId
    parent_process_id = [int]$cim.ParentProcessId
    command_line = [string]$cim.CommandLine
    started_at_utc = $runtime.StartTime.ToUniversalTime().ToString("o")
    captured_before_termination_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    elapsed_seconds_before_termination = [Math]::Round(((Get-Date) - $runtime.StartTime).TotalSeconds, 6)
    cpu_seconds_before_termination = [Math]::Round([double]$runtime.CPU, 6)
    responding_before_termination = [bool]$runtime.Responding
    working_set_bytes_before_termination = [long]$runtime.WorkingSet64
    stdout_artifact_exists_before_termination = Test-Path -LiteralPath $stdoutPath
    output_tail = $null
    output_note = "run_gate.ps1 buffers redirected stdout in memory and writes the raw artifact only after child termination; no live output tail was available"
    termination_method = "Stop-Process -Id 25464 -Force"
    termination_target_verified = $true
    terminated = $false
    terminated_at_utc = $null
}
[System.IO.File]::WriteAllText(
    (Join-Path $PSScriptRoot "full-pytest.bounded-termination.json"),
    (($payload | ConvertTo-Json -Depth 6) + "`n"),
    [System.Text.UTF8Encoding]::new($false)
)
Stop-Process -Id $processId -Force
Start-Sleep -Seconds 3
$payload.terminated = ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue))
$payload.terminated_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
[System.IO.File]::WriteAllText(
    (Join-Path $PSScriptRoot "full-pytest.bounded-termination.json"),
    (($payload | ConvertTo-Json -Depth 6) + "`n"),
    [System.Text.UTF8Encoding]::new($false)
)
$payload | ConvertTo-Json -Depth 6
if (-not $payload.terminated) { exit 2 }
