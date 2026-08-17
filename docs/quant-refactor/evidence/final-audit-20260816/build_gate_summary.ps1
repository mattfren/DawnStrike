$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$gates = @()
foreach ($exitFile in @(Get-ChildItem -LiteralPath $root -Filter "*.exit.json" | Sort-Object Name)) {
    $record = Get-Content -Raw -LiteralPath $exitFile.FullName | ConvertFrom-Json
    $name = [string]$record.gate
    $stdout = Join-Path $root "$name.stdout.txt"
    $stderr = Join-Path $root "$name.stderr.txt"
    $command = Join-Path $root "$name.command.txt"
    $gates += [ordered]@{
        gate = $name
        command = [string]$record.command
        cwd = [string]$record.cwd
        started_at_utc = [string]$record.started_at_utc
        ended_at_utc = [string]$record.ended_at_utc
        elapsed_seconds = [double]$record.elapsed_seconds
        exit_code = [int]$record.exit_code
        command_sha256 = if (Test-Path $command) { (Get-FileHash -Algorithm SHA256 $command).Hash.ToLowerInvariant() } else { $null }
        stdout_sha256 = if (Test-Path $stdout) { (Get-FileHash -Algorithm SHA256 $stdout).Hash.ToLowerInvariant() } else { $null }
        stdout_bytes = if (Test-Path $stdout) { (Get-Item $stdout).Length } else { $null }
        stderr_sha256 = if (Test-Path $stderr) { (Get-FileHash -Algorithm SHA256 $stderr).Hash.ToLowerInvariant() } else { $null }
        stderr_bytes = if (Test-Path $stderr) { (Get-Item $stderr).Length } else { $null }
    }
}
$payload = [ordered]@{
    created_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    environment_source = "repository-state-before.json"
    gate_count = $gates.Count
    gates = $gates
    omitted_exact_ci_gate = [ordered]@{
        command = "py -m pip_audit -r requirements.lock"
        reason = "network/provider actions prohibited by final-audit instruction"
    }
}
[System.IO.File]::WriteAllText(
    (Join-Path $root "gate-summary.json"),
    (($payload | ConvertTo-Json -Depth 8) + "`n"),
    [System.Text.UTF8Encoding]::new($false)
)
$payload | Select-Object gate_count | ConvertTo-Json
