[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [Parameter(Mandatory = $true)][string]$StateRoot,
    [Parameter(Mandatory = $true)][string]$MarketDate,
    [Parameter(Mandatory = $true)][string]$Owner,
    [Parameter(Mandatory = $true)][string]$ResultPath,
    [int]$HoldSeconds = 0
)

# Test-owned OS process for scenario J (competing writers). Dot-sources the
# REAL production lock code in scripts/invoke_dawnstrike_stage.ps1 unmodified
# - lock acquisition itself is never patched - and reports what it observed
# as JSON so the test process can assert on it. Optionally holds the lock for
# HoldSeconds to give a second contender a real window to be denied, or to be
# genuinely killed out from under the lock (for the dead-owner recovery leg).

$ErrorActionPreference = "Stop"
. (Join-Path $RepoRoot "scripts\invoke_dawnstrike_stage.ps1")

$lock = Enter-DawnstrikeDailyRunLock -StateRoot $StateRoot -MarketDate $MarketDate -Owner $Owner
$result = [ordered]@{
    owner        = $Owner
    pid          = $PID
    acquired     = $lock.acquired
    reason       = $lock.reason
    lock_path    = [string]$lock.lock_path
    lock_token   = [string]$lock.lock_token
    observed_at  = [DateTime]::UtcNow.ToString("o")
}
($result | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $ResultPath -Encoding utf8

if ($lock.acquired -and $HoldSeconds -gt 0) {
    Start-Sleep -Seconds $HoldSeconds
    Exit-DawnstrikeDailyRunLock -Lock $lock
    $result.released = $true
    ($result | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $ResultPath -Encoding utf8
}
