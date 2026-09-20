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

function ConvertTo-DawnstrikeUtcDateTimeOffset {
    # Windows PowerShell 5.1's ConvertFrom-Json leaves an ISO-8601 JSON
    # string as [string]; PowerShell 7's ConvertFrom-Json silently
    # coerces the same value straight to [DateTime]. Casting that
    # [DateTime] back to [string] with the default, culture-formatted
    # ToString() drops both the "Z" designator and sub-second precision,
    # so re-parsing the resulting string lands in the machine's local
    # offset instead of UTC -- misclassifying a live owner as dead.
    # Normalise by branching on the actual runtime type instead of
    # assuming a string, so both engines resolve to the identical UTC
    # instant.
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowNull()]$Value)

    if ($Value -is [DateTime]) {
        $dt = $Value
        if ($dt.Kind -eq [System.DateTimeKind]::Unspecified) {
            # Source values are always UTC ("...Z") ISO-8601 timestamps.
            # An Unspecified Kind must be explicitly tagged UTC -- never
            # assumed to already be local wall-clock time.
            $dt = [DateTime]::SpecifyKind($dt, [System.DateTimeKind]::Utc)
        }
        return [DateTimeOffset]$dt.ToUniversalTime()
    }
    return [DateTimeOffset]::Parse(
        [string]$Value,
        [cultureinfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::RoundtripKind
    ).ToUniversalTime()
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
        $startedHasValue = $false
        if ($null -ne $startedProperty -and $null -ne $startedProperty.Value) {
            if ($startedProperty.Value -is [DateTime]) {
                $startedHasValue = $true
            } elseif (-not [string]::IsNullOrWhiteSpace([string]$startedProperty.Value)) {
                $startedHasValue = $true
            }
        }
        if ($startedHasValue) {
            # A reused PID has a different creation time.  Compare exact
            # process start identity rather than mutable lock-file age.
            $recordedStart = ConvertTo-DawnstrikeUtcDateTimeOffset -Value $startedProperty.Value
            return $processStarted.UtcDateTime.Ticks -eq $recordedStart.UtcDateTime.Ticks
        }
        # v2 locks (dawnstrike.daily_run_lock.v2) predate the exact
        # process-start field.  Retain the old
        # acquired_at relationship for compatibility, but never age-evict a
        # live PID.  A process that started after acquired_at is a reused PID;
        # an unparseable acquired_at is ambiguous and therefore fail-closed.
        try {
            $acquiredAt = ConvertTo-DawnstrikeUtcDateTimeOffset -Value $payload.acquired_at
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
