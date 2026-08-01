[CmdletBinding()]
param()

function Enter-DawnstrikeDailyRunLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][string]$Owner,
        [int]$StaleAfterMinutes = 240
    )

    if ($StaleAfterMinutes -le 0) {
        throw "StaleAfterMinutes must be positive."
    }
    $lockRoot = Join-Path $StateRoot "locks"
    New-Item -ItemType Directory -Path $lockRoot -Force | Out-Null
    $lockPath = Join-Path $lockRoot ("dawnstrike-daily-" + $MarketDate + ".lock")
    if (Test-Path -LiteralPath $lockPath -PathType Leaf) {
        $age = ((Get-Date).ToUniversalTime() - (Get-Item -LiteralPath $lockPath).LastWriteTimeUtc).TotalMinutes
        if ($age -ge $StaleAfterMinutes) {
            $stalePath = "$lockPath.stale.$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))"
            Move-Item -LiteralPath $lockPath -Destination $stalePath -ErrorAction Stop
        } else {
            return [pscustomobject]@{
                acquired = $false
                lock_path = $lockPath
                reason = "active_lock"
                age_minutes = [math]::Round($age, 2)
            }
        }
    }
    $payload = [ordered]@{
        schema_version = "dawnstrike.daily_run_lock.v1"
        market_date = $MarketDate
        owner = $Owner
        acquired_at = [DateTime]::UtcNow.ToString("o")
        process_id = $PID
    } | ConvertTo-Json -Depth 3
    try {
        $handle = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        try {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
            $handle.Write($bytes, 0, $bytes.Length)
        } finally {
            $handle.Dispose()
        }
    } catch [System.IO.IOException] {
        return [pscustomobject]@{
            acquired = $false
            lock_path = $lockPath
            reason = "concurrent_lock_acquisition"
            age_minutes = $null
        }
    }
    return [pscustomobject]@{
        acquired = $true
        lock_path = $lockPath
        reason = "acquired"
        age_minutes = 0
    }
}

function Exit-DawnstrikeDailyRunLock {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Lock)

    if ($Lock.acquired -and (Test-Path -LiteralPath $Lock.lock_path -PathType Leaf)) {
        Remove-Item -LiteralPath $Lock.lock_path -Force
    }
}
