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
        $ownerActive = Test-DawnstrikeLockOwnerActive -LockPath $lockPath
        # Wall-clock age is diagnostic only.  A long-running owner must never
        # be evicted merely because it crossed StaleAfterMinutes; doing so
        # permits two daily runs to mutate the same research ledger.  Recovery
        # is allowed only when PID/start-time proof says the owner is dead (or
        # the payload cannot prove any owner exists).
        if (-not $ownerActive) {
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
        schema_version = "dawnstrike.daily_run_lock.v3"
        market_date = $MarketDate
        owner = $Owner
        acquired_at = [DateTime]::UtcNow.ToString("o")
        process_id = $PID
        process_started_at_utc = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString("o")
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

function Test-DawnstrikeLockOwnerActive {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$LockPath)

    try {
        $lockItem = Get-Item -LiteralPath $LockPath -ErrorAction Stop
        if (($lockItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            return $false
        }
        $payload = Get-Content -LiteralPath $LockPath -Raw -ErrorAction Stop | ConvertFrom-Json
        $processId = 0
        if (-not [int]::TryParse([string]$payload.process_id, [ref]$processId) -or $processId -le 0) {
            return $false
        }
        $ownerProcess = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -eq $ownerProcess) { return $false }
        $processStarted = [DateTimeOffset]$ownerProcess.StartTime.ToUniversalTime()
        $startedProperty = $payload.PSObject.Properties["process_started_at_utc"]
        if ($null -ne $startedProperty -and -not [string]::IsNullOrWhiteSpace([string]$startedProperty.Value)) {
            # A reused PID has a different creation time.  Compare exact
            # process start identity rather than mutable lock-file age.
            $recordedStart = [DateTimeOffset]::Parse([string]$startedProperty.Value).ToUniversalTime()
            return $processStarted.UtcDateTime.Ticks -eq $recordedStart.UtcDateTime.Ticks
        }
        # v2 locks (dawnstrike.daily_run_lock.v2) predate the exact
        # process-start field.  Retain the old
        # acquired_at relationship for compatibility, but never age-evict a
        # live PID.  A process that started after acquired_at is a reused PID;
        # an unparseable acquired_at is ambiguous and therefore fail-closed.
        try {
            $acquiredAt = [DateTimeOffset]::Parse([string]$payload.acquired_at).ToUniversalTime()
            return $processStarted.UtcDateTime.Ticks -le $acquiredAt.UtcDateTime.AddTicks([TimeSpan]::TicksPerSecond * 5).Ticks
        }
        catch {
            return $true
        }
    }
    catch {
        return $false
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
