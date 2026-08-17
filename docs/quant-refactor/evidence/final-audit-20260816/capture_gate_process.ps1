param(
    [ValidateSet("midrun", "after")]
    [string]$Phase = "midrun"
)
$ErrorActionPreference = "Stop"
$processes = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'run_gate\.ps1.*full-pytest|pytest -q -p no:cacheprovider'
} | ForEach-Object {
    $runtime = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
    [ordered]@{
        process_id = [int]$_.ProcessId
        parent_process_id = [int]$_.ParentProcessId
        creation_date = [string]$_.CreationDate
        command_line = [string]$_.CommandLine
        cpu_seconds = if ($runtime) { [Math]::Round($runtime.CPU, 6) } else { $null }
        responding = if ($runtime) { [bool]$runtime.Responding } else { $null }
    }
})
$payload = [ordered]@{
    captured_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    processes = $processes
}
[System.IO.File]::WriteAllText(
    (Join-Path $PSScriptRoot "full-pytest.process-$Phase.json"),
    (($payload | ConvertTo-Json -Depth 6) + "`n"),
    [System.Text.UTF8Encoding]::new($false)
)
$payload | ConvertTo-Json -Depth 6
