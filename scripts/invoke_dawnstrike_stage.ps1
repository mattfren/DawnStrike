[CmdletBinding()]
param()

function Assert-DawnstrikeNoReparsePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$AllowMissingLeaf
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $driveRoot = [System.IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($driveRoot)) {
        throw "$Label does not have a valid filesystem root."
    }
    $relative = $fullPath.Substring($driveRoot.Length).Trim('\')
    $current = $driveRoot.TrimEnd('\')
    $parts = if ($relative) { $relative -split '\\' } else { @() }
    for ($index = 0; $index -lt $parts.Count; $index++) {
        $current = Join-Path $current $parts[$index]
        if (-not (Test-Path -LiteralPath $current)) {
            if ($AllowMissingLeaf -and $index -eq ($parts.Count - 1)) {
                return
            }
            throw "$Label is missing or has a missing parent component."
        }
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse-point component."
        }
    }
}

function Ensure-DawnstrikeStageDirectory {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $missing = New-Object System.Collections.Generic.List[string]
    $cursor = $fullPath
    while (-not (Test-Path -LiteralPath $cursor -PathType Container)) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label contains a reparse-point component."
            }
            throw "$Label must be a directory."
        }
        $missing.Add($cursor)
        $parent = Split-Path -Parent $cursor
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) {
            throw "$Label parent directory does not exist."
        }
        $cursor = $parent.TrimEnd('\')
    }
    Assert-DawnstrikeNoReparsePath $cursor "$Label existing parent"
    for ($index = $missing.Count - 1; $index -ge 0; $index--) {
        $target = $missing[$index]
        $parent = Split-Path -Parent $target
        Assert-DawnstrikeNoReparsePath $parent "$Label parent"
        Assert-DawnstrikeNoReparsePath $target $Label -AllowMissingLeaf
        New-Item -ItemType Directory -Path $target -ErrorAction Stop | Out-Null
        Assert-DawnstrikeNoReparsePath $target $Label
    }
    return $fullPath
}

function Skip-DawnstrikeJsonWhitespace {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Raw,
        [Parameter(Mandatory = $true)][ref]$Index
    )

    while ($Index.Value -lt $Raw.Length -and $Raw[$Index.Value] -in @(' ', "`t", "`r", "`n")) {
        $Index.Value++
    }
}

function Read-DawnstrikeJsonStringToken {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Raw,
        [Parameter(Mandatory = $true)][ref]$Index
    )

    if ($Index.Value -ge $Raw.Length -or $Raw[$Index.Value] -ne '"') {
        throw "JSON string token is missing."
    }
    $Index.Value++
    $builder = [System.Text.StringBuilder]::new()
    while ($Index.Value -lt $Raw.Length) {
        $character = $Raw[$Index.Value]
        $Index.Value++
        if ($character -eq '"') {
            return $builder.ToString()
        }
        if ([int][char]$character -lt 0x20) {
            throw "JSON string contains an unescaped control character."
        }
        if ($character -ne '\') {
            [void]$builder.Append($character)
            continue
        }
        if ($Index.Value -ge $Raw.Length) {
            throw "JSON string escape is incomplete."
        }
        $escape = $Raw[$Index.Value]
        $Index.Value++
        if ($escape -eq 'u') {
            if ($Index.Value + 4 -gt $Raw.Length) {
                throw "JSON unicode escape is incomplete."
            }
            $hex = $Raw.Substring($Index.Value, 4)
            if ($hex -notmatch '^[0-9A-Fa-f]{4}$') {
                throw "JSON unicode escape is invalid."
            }
            [void]$builder.Append([char][Convert]::ToInt32($hex, 16))
            $Index.Value += 4
            continue
        }
        switch ($escape) {
            '"' { [void]$builder.Append('"'); continue }
            '\' { [void]$builder.Append('\'); continue }
            '/' { [void]$builder.Append('/'); continue }
            'b' { [void]$builder.Append("`b"); continue }
            'f' { [void]$builder.Append("`f"); continue }
            'n' { [void]$builder.Append("`n"); continue }
            'r' { [void]$builder.Append("`r"); continue }
            't' { [void]$builder.Append("`t"); continue }
            default { throw "JSON string escape is invalid." }
        }
    }
    throw "JSON string is unterminated."
}

function Read-DawnstrikeJsonValue {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Raw,
        [Parameter(Mandatory = $true)][ref]$Index
    )

    Skip-DawnstrikeJsonWhitespace $Raw $Index
    if ($Index.Value -ge $Raw.Length) {
        throw "JSON value is missing."
    }
    $character = $Raw[$Index.Value]
    if ($character -eq '{') {
        $Index.Value++
        $names = [System.Collections.Generic.HashSet[string]]::new(
            [System.StringComparer]::OrdinalIgnoreCase
        )
        Skip-DawnstrikeJsonWhitespace $Raw $Index
        if ($Index.Value -lt $Raw.Length -and $Raw[$Index.Value] -eq '}') {
            $Index.Value++
            return
        }
        while ($true) {
            Skip-DawnstrikeJsonWhitespace $Raw $Index
            $name = Read-DawnstrikeJsonStringToken $Raw $Index
            try {
                $canonicalName = $name.Normalize([System.Text.NormalizationForm]::FormC).ToUpperInvariant()
            }
            catch {
                throw "JSON property name cannot be normalized."
            }
            if (-not $names.Add($canonicalName)) {
                throw "JSON contains duplicate properties."
            }
            Skip-DawnstrikeJsonWhitespace $Raw $Index
            if ($Index.Value -ge $Raw.Length -or $Raw[$Index.Value] -ne ':') {
                throw "JSON object property separator is missing."
            }
            $Index.Value++
            $null = Read-DawnstrikeJsonValue $Raw $Index
            Skip-DawnstrikeJsonWhitespace $Raw $Index
            if ($Index.Value -ge $Raw.Length) {
                throw "JSON object is unterminated."
            }
            if ($Raw[$Index.Value] -eq '}') {
                $Index.Value++
                return
            }
            if ($Raw[$Index.Value] -ne ',') {
                throw "JSON object separator is invalid."
            }
            $Index.Value++
        }
    }
    if ($character -eq '[') {
        $Index.Value++
        Skip-DawnstrikeJsonWhitespace $Raw $Index
        if ($Index.Value -lt $Raw.Length -and $Raw[$Index.Value] -eq ']') {
            $Index.Value++
            return
        }
        while ($true) {
            $null = Read-DawnstrikeJsonValue $Raw $Index
            Skip-DawnstrikeJsonWhitespace $Raw $Index
            if ($Index.Value -ge $Raw.Length) {
                throw "JSON array is unterminated."
            }
            if ($Raw[$Index.Value] -eq ']') {
                $Index.Value++
                return
            }
            if ($Raw[$Index.Value] -ne ',') {
                throw "JSON array separator is invalid."
            }
            $Index.Value++
        }
    }
    if ($character -eq '"') {
        $null = Read-DawnstrikeJsonStringToken $Raw $Index
        return
    }
    foreach ($literal in @('true', 'false', 'null')) {
        if ($Index.Value + $literal.Length -le $Raw.Length -and
            $Raw.Substring($Index.Value, $literal.Length) -ceq $literal) {
            $Index.Value += $literal.Length
            return
        }
    }
    $number = [regex]::Match(
        $Raw.Substring($Index.Value),
        '^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?'
    )
    if ($number.Success) {
        $Index.Value += $number.Length
        return
    }
    throw "JSON value is invalid."
}

function Assert-DawnstrikeJsonUniqueProperties {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Raw)

    $index = 0
    $null = Read-DawnstrikeJsonValue $Raw ([ref]$index)
    Skip-DawnstrikeJsonWhitespace $Raw ([ref]$index)
    if ($index -ne $Raw.Length) {
        throw "JSON contains trailing data."
    }
}

function Get-DawnstrikeLockOwnerStateFromPayload {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Payload)

    try {
        $processId = 0
        if (-not [int]::TryParse([string]$Payload.process_id, [ref]$processId) -or $processId -le 0) {
            return "UNKNOWN"
        }
        $startedValue = [string]$Payload.process_started_at_utc
        if ([string]::IsNullOrWhiteSpace($startedValue)) { return "UNKNOWN" }
        $recordedStart = [DateTimeOffset]::Parse($startedValue).ToUniversalTime()
        $ownerProcess = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -eq $ownerProcess) { return "DEAD" }
        $processStarted = [DateTimeOffset]$ownerProcess.StartTime.ToUniversalTime()
        if ($processStarted.UtcDateTime.Ticks -eq $recordedStart.UtcDateTime.Ticks) {
            return "ACTIVE"
        }
        return "DEAD"
    }
    catch {
        return "UNKNOWN"
    }
}

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
    if (
        -not [string]::IsNullOrWhiteSpace($ActivationId) -and
        $Owner -notin @("runtime_activation", "runtime_rollback")
    ) {
        throw "Receipt-bound daily locks require an allowlisted operation owner."
    }
    if (-not [string]::IsNullOrWhiteSpace($PreparedReceiptSha256) -and $PreparedReceiptSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "PreparedReceiptSha256 is invalid."
    }
    if (-not [string]::IsNullOrWhiteSpace($PreparedReceiptFileSha256) -and $PreparedReceiptFileSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "PreparedReceiptFileSha256 is invalid."
    }
    $lockRoot = Join-Path $StateRoot "locks"
    Assert-DawnstrikeNoReparsePath $StateRoot "StateRoot"
    $lockRoot = Ensure-DawnstrikeStageDirectory $lockRoot "Daily lock directory"
    $lockPath = Join-Path $lockRoot ("dawnstrike-daily-" + $MarketDate + ".lock")
    Assert-DawnstrikeNoReparsePath $lockPath "Daily lock" -AllowMissingLeaf
    if (Test-Path -LiteralPath $lockPath -PathType Leaf) {
        $age = ((Get-Date).ToUniversalTime() - (Get-Item -LiteralPath $lockPath).LastWriteTimeUtc).TotalMinutes
        $existingPayload = $null
        try {
            $existingRaw = Get-Content -LiteralPath $lockPath -Raw -Encoding UTF8
            Assert-DawnstrikeJsonUniqueProperties $existingRaw
            $existingPayload = $existingRaw | ConvertFrom-Json
        }
        catch {
            # Owner-state validation below remains the fail-closed result for
            # malformed or otherwise ambiguous lock payloads.
        }
        $bindingStatus = [string]$existingPayload.receipt_binding_status
        $isReceiptBoundActivation =
            [string]$existingPayload.schema_version -eq "dawnstrike.daily_run_lock.v4" -and
            (
                [string]$existingPayload.owner -eq "runtime_activation" -or
                $bindingStatus -in @("BOUND", "UNBOUND")
            )
        if ($isReceiptBoundActivation) {
            # A v4 activation lock is a crash-recovery boundary, including the
            # short sequential-binding transition where one lock is UNBOUND.
            # Normal stages may not archive or supersede it; only the exact
            # receipt-bound rollback path may prove and archive the pair.
            return [pscustomobject]@{
                acquired = $false
                lock_path = $lockPath
                reason = "receipt_bound_activation_requires_rollback"
                age_minutes = [math]::Round($age, 2)
            }
        }
        $ownerState = Get-DawnstrikeLockOwnerStateFromPayload $existingPayload
        # Wall-clock age is diagnostic only.  A long-running owner must never
        # be evicted merely because it crossed StaleAfterMinutes; doing so
        # permits two daily runs to mutate the same research ledger.  Recovery
        # is allowed only when PID/start-time proof says the owner is dead (or
        # the payload cannot prove any owner exists).
        if ($ownerState -eq "DEAD") {
            $stalePath = "$lockPath.archived.$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')).$([guid]::NewGuid().ToString('N'))"
            Assert-DawnstrikeNoReparsePath $lockPath "Daily lock"
            Assert-DawnstrikeNoReparsePath $stalePath "Daily lock archive" -AllowMissingLeaf
            Move-Item -LiteralPath $lockPath -Destination $stalePath -ErrorAction Stop
            Assert-DawnstrikeNoReparsePath $stalePath "Daily lock archive"
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
        research_only = $true
        broker_execution_enabled = $false
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
        Assert-DawnstrikeJsonUniqueProperties $raw
        $payload = $raw | ConvertFrom-Json
        $processId = 0
        if (-not [int]::TryParse([string]$payload.process_id, [ref]$processId) -or $processId -le 0) {
            return $false
        }
        $ownerProcess = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($null -eq $ownerProcess) { return $false }
        $processStarted = [DateTimeOffset]$ownerProcess.StartTime.ToUniversalTime()
        $startedValue = [string]$payload.process_started_at_utc
        if (-not [string]::IsNullOrWhiteSpace($startedValue)) {
            $recordedStart = [DateTimeOffset]::Parse($startedValue).ToUniversalTime()
            return $processStarted.UtcDateTime.Ticks -eq $recordedStart.UtcDateTime.Ticks
        }
        $acquiredAt = [DateTimeOffset]::Parse([string]$payload.acquired_at).ToUniversalTime()
        return $processStarted.UtcDateTime.Ticks -le $acquiredAt.UtcDateTime.AddTicks([TimeSpan]::TicksPerSecond * 5).Ticks
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
        Assert-DawnstrikeNoReparsePath $LockPath "Daily lock"
        if (($lockItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            return "UNKNOWN"
        }
        $raw = Get-Content -LiteralPath $LockPath -Raw -ErrorAction Stop
        Assert-DawnstrikeJsonUniqueProperties $raw
        $payload = $raw | ConvertFrom-Json
        return Get-DawnstrikeLockOwnerStateFromPayload $payload
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
            Assert-DawnstrikeNoReparsePath $Lock.lock_path "Daily lock"
            $raw = Get-Content -LiteralPath $Lock.lock_path -Raw -ErrorAction Stop
            Assert-DawnstrikeJsonUniqueProperties $raw
            $payload = $raw | ConvertFrom-Json
            if ([string]$payload.lock_token -eq [string]$Lock.lock_token) {
                Assert-DawnstrikeNoReparsePath $Lock.lock_path "Daily lock"
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
        Assert-DawnstrikeNoReparsePath $StateRoot "StateRoot"
        $receiptRoot = Ensure-DawnstrikeStageDirectory $receiptRoot "Lock denial receipt directory"
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
        Assert-DawnstrikeNoReparsePath $path "Lock denial receipt" -AllowMissingLeaf
        Assert-DawnstrikeNoReparsePath $temporary "Lock denial receipt temporary file" -AllowMissingLeaf
        [System.IO.File]::WriteAllText($temporary, $payload, [System.Text.UTF8Encoding]::new($false))
        Assert-DawnstrikeNoReparsePath $temporary "Lock denial receipt temporary file"
        Assert-DawnstrikeNoReparsePath $path "Lock denial receipt" -AllowMissingLeaf
        Move-Item -LiteralPath $temporary -Destination $path
        Assert-DawnstrikeNoReparsePath $path "Lock denial receipt"
        return $true
    }
    catch {
        return $false
    }
}
