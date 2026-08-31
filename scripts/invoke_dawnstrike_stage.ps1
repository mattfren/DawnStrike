[CmdletBinding()]
param()

function Enter-DawnstrikeDailyRunLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][string]$Owner,
        [string]$ActivationId = "",
        [string]$PreparedReceiptName = "",
        [string]$PreparedReceiptSha256 = "",
        [string]$PreparedReceiptFileSha256 = "",
        [int]$StaleAfterMinutes = 240
    )

    if ($StaleAfterMinutes -le 0) {
        throw "StaleAfterMinutes must be positive."
    }
    if (-not [string]::IsNullOrWhiteSpace($ActivationId) -and $ActivationId -notmatch '^[0-9a-f]{24}$') {
        throw "ActivationId must be a lowercase 24-hex value."
    }
    if (-not [string]::IsNullOrWhiteSpace($PreparedReceiptName) -and $PreparedReceiptName -notmatch '^runtime-activation-[0-9a-f]{24}\.prepared\.json$') {
        throw "PreparedReceiptName is invalid."
    }
    if (-not [string]::IsNullOrWhiteSpace($ActivationId) -and [string]::IsNullOrWhiteSpace($PreparedReceiptName)) {
        throw "PreparedReceiptName is required when ActivationId is provided."
    }
    if (-not [string]::IsNullOrWhiteSpace($PreparedReceiptSha256) -and $PreparedReceiptSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "PreparedReceiptSha256 is invalid."
    }
    if (-not [string]::IsNullOrWhiteSpace($PreparedReceiptFileSha256) -and $PreparedReceiptFileSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "PreparedReceiptFileSha256 is invalid."
    }
    $lockRoot = Join-Path $StateRoot "locks"
    New-Item -ItemType Directory -Path $lockRoot -Force | Out-Null
    $lockPath = Join-Path $lockRoot ("dawnstrike-daily-" + $MarketDate + ".lock")
    if (Test-Path -LiteralPath $lockPath -PathType Leaf) {
        $age = ((Get-Date).ToUniversalTime() - (Get-Item -LiteralPath $lockPath).LastWriteTimeUtc).TotalMinutes
        $ownerState = Get-DawnstrikeLockOwnerState -LockPath $lockPath
        # Wall-clock age is diagnostic only.  A long-running owner must never
        # be evicted merely because it crossed StaleAfterMinutes; doing so
        # permits two daily runs to mutate the same research ledger.  Recovery
        # is allowed only when PID/start-time proof says the owner is dead (or
        # the payload cannot prove any owner exists).
        if ($ownerState -eq "DEAD") {
            $stalePath = "$lockPath.archived.$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')).$([guid]::NewGuid().ToString('N'))"
            Move-Item -LiteralPath $lockPath -Destination $stalePath -ErrorAction Stop
        } else {
            return [pscustomobject]@{
                acquired = $false
                lock_path = $lockPath
                reason = if ($ownerState -eq "UNKNOWN") { "ambiguous_lock" } else { "active_lock" }
                age_minutes = [math]::Round($age, 2)
            }
        }
    }
    $lockToken = [guid]::NewGuid().ToString("N")
    $payloadObject = [ordered]@{
        schema_version = if ([string]::IsNullOrWhiteSpace($ActivationId)) { "dawnstrike.daily_run_lock.v3" } else { "dawnstrike.daily_run_lock.v4" }
        market_date = $MarketDate
        owner = $Owner
        acquired_at = [DateTime]::UtcNow.ToString("o")
        process_id = $PID
        process_started_at_utc = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString("o")
        lock_token = $lockToken
    }
    if (-not [string]::IsNullOrWhiteSpace($ActivationId)) {
        $payloadObject.activation_id = $ActivationId
        $payloadObject.prepared_receipt_name = $PreparedReceiptName
        $payloadObject.prepared_receipt_sha256 = if ([string]::IsNullOrWhiteSpace($PreparedReceiptSha256)) { $null } else { $PreparedReceiptSha256 }
        $payloadObject.prepared_receipt_file_sha256 = if ([string]::IsNullOrWhiteSpace($PreparedReceiptFileSha256)) { $null } else { $PreparedReceiptFileSha256 }
        $payloadObject.receipt_binding_status = if ([string]::IsNullOrWhiteSpace($PreparedReceiptSha256)) { "UNBOUND" } else { "BOUND" }
    }
    $payload = $payloadObject | ConvertTo-Json -Depth 4
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

    # Legacy dawnstrike.daily_run_lock.v2 payloads remain diagnostic-only;
    # receipt-bound stale recovery requires the v4 process-start contract.
    try {
        $raw = Get-Content -LiteralPath $LockPath -Raw -ErrorAction Stop
        $payload = $raw | ConvertFrom-Json
        $processId = 0
        if (-not [int]::TryParse([string]$payload.process_id, [ref]$processId) -or $processId -le 0) {
            return $false
        }
        $ownerProcess = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -eq $ownerProcess) { return $false }
        $processStarted = [DateTimeOffset]$ownerProcess.StartTime.ToUniversalTime()
        $startedMatch = [regex]::Match($raw, '"process_started_at_utc"\s*:\s*"([^"]+)"')
        if ($startedMatch.Success -and -not [string]::IsNullOrWhiteSpace($startedMatch.Groups[1].Value)) {
            $recordedStart = [DateTimeOffset]::Parse($startedMatch.Groups[1].Value).ToUniversalTime()
            return $processStarted.UtcDateTime.Ticks -eq $recordedStart.UtcDateTime.Ticks
        }
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

function Get-DawnstrikeLockOwnerState {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$LockPath)

    try {
        $lockItem = Get-Item -LiteralPath $LockPath -Force -ErrorAction Stop
        if (($lockItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            return "UNKNOWN"
        }
        $raw = Get-Content -LiteralPath $LockPath -Raw -ErrorAction Stop
        $payload = $raw | ConvertFrom-Json
        $processId = 0
        if (-not [int]::TryParse([string]$payload.process_id, [ref]$processId) -or $processId -le 0) {
            return "UNKNOWN"
        }
        # ConvertFrom-Json materializes RFC3339 values as local DateTime on
        # some PowerShell versions. Read the original JSON string so the
        # process-start identity keeps its UTC offset and full precision.
        $startedMatch = [regex]::Match($raw, '"process_started_at_utc"\s*:\s*"([^"]+)"')
        if (-not $startedMatch.Success -or [string]::IsNullOrWhiteSpace($startedMatch.Groups[1].Value)) {
            return "UNKNOWN"
        }
        $recordedStart = [DateTimeOffset]::Parse($startedMatch.Groups[1].Value).ToUniversalTime()
        $ownerProcess = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -eq $ownerProcess) { return "DEAD" }
        $processStarted = [DateTimeOffset]$ownerProcess.StartTime.ToUniversalTime()
        if ($processStarted.UtcDateTime.Ticks -eq $recordedStart.UtcDateTime.Ticks) {
            return "ACTIVE"
        }
        # A live PID with a different start identity is a reused PID and the
        # recorded owner is therefore dead.  Wall-clock age is never proof.
        return "DEAD"
    }
    catch {
        return "UNKNOWN"
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
