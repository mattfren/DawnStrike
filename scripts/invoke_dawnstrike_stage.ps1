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
        $ownerActive = $false
        try {
            $existingPayload = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
            $ownerProcess = Get-Process -Id ([int]$existingPayload.process_id) -ErrorAction SilentlyContinue
            if ($ownerProcess) {
                $acquiredAt = [DateTimeOffset]::Parse([string]$existingPayload.acquired_at).UtcDateTime
                # A reused PID belongs to a process that started after this lock.
                $ownerActive = $ownerProcess.StartTime.ToUniversalTime() -le $acquiredAt.AddSeconds(5)
            }
        }
        catch {
            $ownerActive = $false
        }
        if (-not $ownerActive -or $age -ge $StaleAfterMinutes) {
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
    $lockToken = [guid]::NewGuid().ToString("N")
    $payload = [ordered]@{
        schema_version = "dawnstrike.daily_run_lock.v2"
        market_date = $MarketDate
        owner = $Owner
        acquired_at = [DateTime]::UtcNow.ToString("o")
        process_id = $PID
        lock_token = $lockToken
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
        lock_token = $lockToken
    }
}

function Exit-DawnstrikeDailyRunLock {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Lock)

    if ($Lock.acquired -and (Test-Path -LiteralPath $Lock.lock_path -PathType Leaf)) {
        try {
            $payload = Get-Content -LiteralPath $Lock.lock_path -Raw | ConvertFrom-Json
            if ([string]$payload.lock_token -eq [string]$Lock.lock_token) {
                Remove-Item -LiteralPath $Lock.lock_path -Force
            }
        }
        catch {
            # Never delete a lock whose ownership cannot be proven.
        }
    }
}

function Write-DawnstrikeLockDenialReceipt {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][string]$Owner,
        [Parameter(Mandatory = $true)][object]$Lock
    )

    try {
        $receiptRoot = Join-Path $StateRoot "receipts\lock-denials"
        New-Item -ItemType Directory -Path $receiptRoot -Force | Out-Null
        $recordedAt = [DateTimeOffset]::UtcNow
        $payload = [ordered]@{
            schema_version = "dawnstrike.lock_denial.v1"
            market_date = $MarketDate
            owner = $Owner
            reason = [string]$Lock.reason
            lock_path = [string]$Lock.lock_path
            age_minutes = $Lock.age_minutes
            recorded_at = $recordedAt.ToString("o")
            research_only = $true
            broker_execution_enabled = $false
        } | ConvertTo-Json -Depth 4
        $name = "$MarketDate-$Owner-$($recordedAt.ToString('yyyyMMddTHHmmssfffZ')).json"
        $path = Join-Path $receiptRoot $name
        $temporary = "$path.$([guid]::NewGuid().ToString('N')).tmp"
        [System.IO.File]::WriteAllText($temporary, $payload, [System.Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $path
        return $true
    }
    catch {
        return $false
    }
}
