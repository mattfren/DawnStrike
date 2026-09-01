[CmdletBinding()]
param()

if (
    [int]$PSVersionTable.PSVersion.Major -lt 5 -or
    [string]$PSVersionTable.PSEdition -ne "Desktop"
) {
    throw "Dawnstrike stage locking requires Windows PowerShell 5.1 or later (Desktop edition)."
}

function Enter-DawnstrikeLockOperationMutex {
    [CmdletBinding()]
    param([int]$TimeoutMilliseconds = 60000)

    $mutex = New-Object System.Threading.Mutex($false, "Global\Dawnstrike.LockHandshake.v1")
    try {
        if (-not $mutex.WaitOne($TimeoutMilliseconds)) {
            $mutex.Dispose()
            throw "Dawnstrike lock handshake mutex could not be acquired."
        }
        return $mutex
    }
    catch {
        $mutex.Dispose()
        throw
    }
}

function Exit-DawnstrikeLockOperationMutex {
    [CmdletBinding()]
    param([AllowNull()][object]$Mutex)

    if ($null -eq $Mutex) { return }
    try { $Mutex.ReleaseMutex() } catch { }
    $Mutex.Dispose()
}

function Assert-DawnstrikeLockNoReparseComponents {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = [System.IO.Path]::GetFullPath($Path)
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -ne $item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label contains a reparse-point path component."
    }
    $parentPath = if ($null -ne $item -and $item.PSIsContainer) {
        $item.FullName
    }
    else {
        Split-Path -Parent $full
    }
    $current = [System.IO.DirectoryInfo]::new($parentPath)
    while ($null -ne $current) {
        $currentItem = Get-Item -LiteralPath $current.FullName -Force -ErrorAction SilentlyContinue
        if ($null -ne $currentItem -and ($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse-point path component."
        }
        if ([string]::Equals(
            $current.FullName.TrimEnd('\'),
            $current.Root.FullName.TrimEnd('\'),
            [System.StringComparison]::OrdinalIgnoreCase
        )) { break }
        $current = $current.Parent
    }
}

function Get-DawnstrikeLockBytesSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Get-DawnstrikeLockSnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$AllowMissing
    )

    $full = [System.IO.Path]::GetFullPath($Path)
    Assert-DawnstrikeLockNoReparseComponents $full $Label
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        if ($AllowMissing) {
            return [pscustomobject]@{
                path = $full
                present = $false
                bytes_sha256 = $null
                byte_count = 0
                lock_token = $null
                payload = $null
            }
        }
        throw "$Label is missing."
    }
    if (
        $item.PSIsContainer -or
        ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) { throw "$Label is not a regular non-reparse file." }
    $first = [System.IO.File]::ReadAllBytes($full)
    $firstHash = Get-DawnstrikeLockBytesSha256 $first
    Assert-DawnstrikeLockNoReparseComponents $full $Label
    $second = [System.IO.File]::ReadAllBytes($full)
    $secondHash = Get-DawnstrikeLockBytesSha256 $second
    if ($firstHash -ne $secondHash -or $first.Length -ne $second.Length) {
        throw "$Label changed while taking its same-byte snapshot."
    }
    try {
        $payload = [System.Text.Encoding]::UTF8.GetString($first) | ConvertFrom-Json
    }
    catch { throw "$Label is not valid JSON." }
    if (
        $null -eq $payload -or
        [string]::IsNullOrWhiteSpace([string]$payload.lock_token) -or
        [string]$payload.research_only -ne 'True' -or
        [string]$payload.broker_execution_enabled -ne 'False'
    ) { throw "$Label does not carry the exact safety identity." }
    return [pscustomobject]@{
        path = $full
        present = $true
        bytes_sha256 = $firstHash
        byte_count = $first.Length
        lock_token = [string]$payload.lock_token
        payload = $payload
    }
}

function Remove-DawnstrikeOwnedLock {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Lock, [Parameter(Mandatory = $true)][string]$Label)

    if (-not [bool]$Lock.acquired) { return }
    $current = Get-DawnstrikeLockSnapshot -Path ([string]$Lock.lock_path) -Label $Label
    if (
        -not $current.present -or
        [string]$current.lock_token -ne [string]$Lock.lock_token -or
        [string]$current.bytes_sha256 -ne [string]$Lock.bytes_sha256
    ) { throw "$Label ownership changed; refusing to remove it." }
    Remove-Item -LiteralPath ([string]$Lock.lock_path) -Force -ErrorAction Stop
    $after = Get-DawnstrikeLockSnapshot -Path ([string]$Lock.lock_path) -Label $Label -AllowMissing
    if ($after.present) { throw "$Label could not be removed after ownership was proven." }
}

function Move-DawnstrikeDeadDailyLockToArchive {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Snapshot,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not [bool]$Snapshot.present) {
        throw "$Label cannot be archived because its exact snapshot is absent."
    }
    $sourcePath = [System.IO.Path]::GetFullPath([string]$Snapshot.path)
    $sourceHash = [string]$Snapshot.bytes_sha256
    if ($sourceHash -notmatch '^[0-9a-f]{64}$') {
        throw "$Label does not carry an exact SHA-256 identity."
    }
    $archivePath = "$sourcePath.stale-dead-$sourceHash"
    Assert-DawnstrikeLockNoReparseComponents $sourcePath $Label
    Assert-DawnstrikeLockNoReparseComponents $archivePath "$Label archive"
    if (Test-Path -LiteralPath $archivePath) {
        throw "$Label deterministic stale archive already exists; refusing an ambiguous overwrite."
    }
    $again = Get-DawnstrikeLockSnapshot -Path $sourcePath -Label $Label
    if (
        [string]$again.bytes_sha256 -ne $sourceHash -or
        [string]$again.lock_token -ne [string]$Snapshot.lock_token
    ) {
        throw "$Label changed before its dead-owner archive could be committed."
    }
    [System.IO.File]::Move($sourcePath, $archivePath)
    $afterSource = Get-DawnstrikeLockSnapshot -Path $sourcePath -Label $Label -AllowMissing
    if ($afterSource.present) {
        throw "$Label archival did not remove the original lock path."
    }
    $archive = Get-DawnstrikeLockSnapshot -Path $archivePath -Label "$Label archive"
    if (
        [string]$archive.bytes_sha256 -ne $sourceHash -or
        [string]$archive.lock_token -ne [string]$Snapshot.lock_token
    ) {
        throw "$Label stale archive does not preserve the exact original bytes."
    }
    return [pscustomobject]@{
        path = $archivePath
        bytes_sha256 = $sourceHash
        lock_token = [string]$Snapshot.lock_token
    }
}

function Resolve-DawnstrikeForeignDailyLocksCore {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$LockRoot,
        [Parameter(Mandatory = $true)][string]$CurrentLockPath
    )

    $currentFull = [System.IO.Path]::GetFullPath($CurrentLockPath)
    $records = @()
    $foreign = @(
        Get-ChildItem -LiteralPath $LockRoot -Filter "dawnstrike-daily-*.lock" -File -Force |
            Where-Object {
                [System.IO.Path]::GetFullPath($_.FullName) -ne $currentFull
            } |
            Sort-Object -Property FullName
    )
    foreach ($item in $foreign) {
        if ([string]$item.Name -notmatch '^dawnstrike-daily-(\d{4}-\d{2}-\d{2})\.lock$') {
            throw "Counterpart daily run lock has a noncanonical filename."
        }
        $pathDate = [string]$Matches[1]
        $snapshot = Get-DawnstrikeLockSnapshot -Path $item.FullName -Label "Counterpart daily run lock"
        if (
            [string]$snapshot.payload.market_date -ne $pathDate -or
            [string]::IsNullOrWhiteSpace([string]$snapshot.payload.owner)
        ) {
            throw "Counterpart daily run lock is not bound to its canonical date and owner."
        }
        $ownerActive = Test-DawnstrikeLockOwnerActive -LockPath $item.FullName
        $verified = Get-DawnstrikeLockSnapshot -Path $item.FullName -Label "Counterpart daily run lock"
        if (
            [string]$verified.bytes_sha256 -ne [string]$snapshot.bytes_sha256 -or
            [string]$verified.lock_token -ne [string]$snapshot.lock_token
        ) {
            throw "Counterpart daily run lock changed while its owner identity was checked."
        }
        $records += [pscustomobject]@{
            snapshot = $verified
            owner_active = [bool]$ownerActive
        }
    }
    $active = @($records | Where-Object { $_.owner_active })
    if ($active.Count -gt 0) {
        return [pscustomobject]@{
            blocked = $true
            active_count = $active.Count
            archived_count = 0
        }
    }
    $archived = 0
    foreach ($record in $records) {
        $null = Move-DawnstrikeDeadDailyLockToArchive `
            -Snapshot $record.snapshot `
            -Label "Counterpart daily run lock"
        $archived += 1
    }
    return [pscustomobject]@{
        blocked = $false
        active_count = 0
        archived_count = $archived
    }
}

function Enter-DawnstrikeDailyRunLockCore {
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
    Assert-DawnstrikeLockNoReparseComponents $lockRoot "Daily lock root"
    New-Item -ItemType Directory -Path $lockRoot -Force | Out-Null
    Assert-DawnstrikeLockNoReparseComponents $lockRoot "Daily lock root"
    $activationLockPath = Join-Path $lockRoot "dawnstrike-runtime-activation.lock"
    $lockPath = Join-Path $lockRoot ("dawnstrike-daily-" + $MarketDate + ".lock")
    $activationBefore = Get-DawnstrikeLockSnapshot -Path $activationLockPath -Label "Runtime activation lock" -AllowMissing
    if ($Owner -notin @("runtime_activation", "runtime_rollback") -and $activationBefore.present) {
        return [pscustomobject]@{
            acquired = $false
            lock_path = $lockPath
            reason = "runtime_activation_lock"
            age_minutes = $null
        }
    }
    $existingDaily = Get-DawnstrikeLockSnapshot -Path $lockPath -Label "Daily run lock" -AllowMissing
    if ($existingDaily.present) {
        $age = ((Get-Date).ToUniversalTime() - (Get-Item -LiteralPath $lockPath).LastWriteTimeUtc).TotalMinutes
        $ownerActive = Test-DawnstrikeLockOwnerActive -LockPath $lockPath
        # Wall-clock age is diagnostic only.  A long-running owner must never
        # be evicted merely because it crossed StaleAfterMinutes; doing so
        # permits two daily runs to mutate the same research ledger.  Recovery
        # is allowed only when PID/start-time proof says the owner is dead.
        if (-not $ownerActive) {
            $again = Get-DawnstrikeLockSnapshot -Path $lockPath -Label "Daily run lock"
            if ($again.bytes_sha256 -ne $existingDaily.bytes_sha256) {
                return [pscustomobject]@{
                    acquired = $false
                    lock_path = $lockPath
                    reason = "concurrent_lock_change"
                    age_minutes = [math]::Round($age, 2)
                }
            }
            $null = Move-DawnstrikeDeadDailyLockToArchive `
                -Snapshot $again `
                -Label "Daily run lock"
        } else {
            return [pscustomobject]@{
                acquired = $false
                lock_path = $lockPath
                reason = "active_lock"
                age_minutes = [math]::Round($age, 2)
            }
        }
    }
    $foreignBefore = Resolve-DawnstrikeForeignDailyLocksCore `
        -LockRoot $lockRoot `
        -CurrentLockPath $lockPath
    if ($foreignBefore.blocked) {
        return [pscustomobject]@{
            acquired = $false
            lock_path = $lockPath
            reason = "active_foreign_daily_lock"
            age_minutes = $null
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
        research_only = $true
        broker_execution_enabled = $false
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
            $handle.Flush($true)
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
    Assert-DawnstrikeLockNoReparseComponents $lockPath "Daily run lock"
    $own = Get-DawnstrikeLockSnapshot -Path $lockPath -Label "Daily run lock"
    if (-not $own.present -or $own.lock_token -ne $lockToken) {
        throw "Daily run lock could not be read back with its own token."
    }
    # Cooperative two-lock handshake: the pre-create check is only a
    # fast-path.  The post-create same-byte snapshot is authoritative.  If a
    # runtime activation/rollback lock appeared in the race window, release
    # only this unchanged daily lock and fail before any stage work begins.
    $activationAfter = Get-DawnstrikeLockSnapshot -Path $activationLockPath -Label "Runtime activation lock" -AllowMissing
    if ($activationAfter.present -and $Owner -notin @("runtime_activation", "runtime_rollback")) {
        Remove-DawnstrikeOwnedLock -Lock $([pscustomobject]@{
                acquired = $true
                lock_path = $lockPath
                lock_token = $lockToken
                bytes_sha256 = $own.bytes_sha256
            }) -Label "Daily run lock"
        return [pscustomobject]@{
            acquired = $false
            lock_path = $lockPath
            reason = "runtime_activation_lock_race"
            age_minutes = $null
        }
    }
    if ($Owner -in @("runtime_activation", "runtime_rollback") -and -not $activationAfter.present) {
        Remove-DawnstrikeOwnedLock -Lock $([pscustomobject]@{
                acquired = $true
                lock_path = $lockPath
                lock_token = $lockToken
                bytes_sha256 = $own.bytes_sha256
            }) -Label "Daily run lock"
        throw "Runtime activation lock disappeared during the daily lock handshake."
    }
    try {
        $foreignAfter = Resolve-DawnstrikeForeignDailyLocksCore `
            -LockRoot $lockRoot `
            -CurrentLockPath $lockPath
    }
    catch {
        Remove-DawnstrikeOwnedLock -Lock $([pscustomobject]@{
                acquired = $true
                lock_path = $lockPath
                lock_token = $lockToken
                bytes_sha256 = $own.bytes_sha256
            }) -Label "Daily run lock"
        throw
    }
    if ($foreignAfter.blocked) {
        Remove-DawnstrikeOwnedLock -Lock $([pscustomobject]@{
                acquired = $true
                lock_path = $lockPath
                lock_token = $lockToken
                bytes_sha256 = $own.bytes_sha256
            }) -Label "Daily run lock"
        return [pscustomobject]@{
            acquired = $false
            lock_path = $lockPath
            reason = "active_foreign_daily_lock_race"
            age_minutes = $null
        }
    }
    return [pscustomobject]@{
        acquired = $true
        lock_path = $lockPath
        reason = "acquired"
        age_minutes = 0
        lock_token = $lockToken
        bytes_sha256 = $own.bytes_sha256
    }
}

function Enter-DawnstrikeDailyRunLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][string]$Owner,
        [int]$StaleAfterMinutes = 240
    )

    $mutex = Enter-DawnstrikeLockOperationMutex
    try {
        return Enter-DawnstrikeDailyRunLockCore @PSBoundParameters
    }
    finally {
        Exit-DawnstrikeLockOperationMutex $mutex
    }
}

function Confirm-DawnstrikeActivationDailyLockHandshakeCore {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][object]$ActivationLock,
        [Parameter(Mandatory = $true)][object]$DailyLock
    )

    if (-not $ActivationLock.acquired -or -not $DailyLock.acquired) {
        throw "Activation/daily lock handshake requires both owned locks."
    }
    $lockRoot = Join-Path $StateRoot "locks"
    Assert-DawnstrikeLockNoReparseComponents $lockRoot "Activation lock root"
    $activation = Get-DawnstrikeLockSnapshot `
        -Path ([string]$ActivationLock.path) `
        -Label "Runtime activation lock"
    if (
        $activation.lock_token -ne [string]$ActivationLock.token -or
        $activation.bytes_sha256 -ne [string]$ActivationLock.bytes_sha256
    ) {
        throw "Runtime activation lock changed during the lock handshake."
    }
    $daily = Get-DawnstrikeLockSnapshot `
        -Path ([string]$DailyLock.lock_path) `
        -Label "Daily run lock"
    if (
        $daily.lock_token -ne [string]$DailyLock.lock_token -or
        $daily.bytes_sha256 -ne [string]$DailyLock.bytes_sha256
    ) {
        throw "Daily run lock changed during the lock handshake."
    }
    $foreign = Resolve-DawnstrikeForeignDailyLocksCore `
        -LockRoot $lockRoot `
        -CurrentLockPath ([string]$DailyLock.lock_path)
    if ($foreign.blocked) {
        Remove-DawnstrikeOwnedLock -Lock $DailyLock -Label "Daily run lock"
        throw "An active counterpart daily run lock appeared during the activation handshake."
    }
    return $true
}

function Confirm-DawnstrikeActivationDailyLockHandshake {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][object]$ActivationLock,
        [Parameter(Mandatory = $true)][object]$DailyLock
    )

    $mutex = Enter-DawnstrikeLockOperationMutex
    try {
        return Confirm-DawnstrikeActivationDailyLockHandshakeCore @PSBoundParameters
    }
    finally {
        Exit-DawnstrikeLockOperationMutex $mutex
    }
}

function Test-DawnstrikeLockOwnerActive {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$LockPath)

    try {
        $snapshot = Get-DawnstrikeLockSnapshot -Path $LockPath -Label "Lock owner record"
        $payload = $snapshot.payload
        $processId = 0
        if (-not [int]::TryParse([string]$payload.process_id, [ref]$processId) -or $processId -le 0) {
            throw "Lock owner process_id is invalid."
        }
        $schemaVersion = [string]$payload.schema_version
        $recordedIdentity = [DateTimeOffset]::MinValue
        if ($schemaVersion -eq "dawnstrike.daily_run_lock.v3") {
            $startedProperty = $payload.PSObject.Properties["process_started_at_utc"]
            if (
                $null -eq $startedProperty -or
                [string]::IsNullOrWhiteSpace([string]$startedProperty.Value) -or
                -not [DateTimeOffset]::TryParse(
                    [string]$startedProperty.Value,
                    [ref]$recordedIdentity
                )
            ) {
                throw "Lock owner process-start identity is invalid."
            }
            # A reused PID has a different creation time.  Compare exact
            # process start identity rather than mutable lock-file age.
        }
        elseif ($schemaVersion -eq "dawnstrike.daily_run_lock.v2") {
            # v2 locks predate the exact process-start field.  Retain the old
            # acquired_at relationship for compatibility, but require that
            # timestamp to be valid before deciding whether an owner is dead.
            if (-not [DateTimeOffset]::TryParse(
                [string]$payload.acquired_at,
                [ref]$recordedIdentity
            )) {
                throw "Legacy lock owner acquisition identity is invalid."
            }
        }
        else {
            throw "Daily run lock schema is unsupported for owner recovery."
        }
        try {
            $ownerProcess = [System.Diagnostics.Process]::GetProcessById($processId)
        }
        catch [System.ArgumentException] {
            return $false
        }
        $processStarted = [DateTimeOffset]$ownerProcess.StartTime.ToUniversalTime()
        $recordedIdentity = $recordedIdentity.ToUniversalTime()
        if ($schemaVersion -eq "dawnstrike.daily_run_lock.v3") {
            return $processStarted.UtcDateTime.Ticks -eq $recordedIdentity.UtcDateTime.Ticks
        }
        return $processStarted.UtcDateTime.Ticks -le $recordedIdentity.UtcDateTime.AddTicks(
            [TimeSpan]::TicksPerSecond * 5
        ).Ticks
    }
    catch {
        throw "Lock owner identity is ambiguous; refusing stale recovery. $($_.Exception.Message)"
    }
}

function Exit-DawnstrikeDailyRunLockCore {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Lock)

    if (-not $Lock.acquired) { return }
    Remove-DawnstrikeOwnedLock -Lock $Lock -Label "Daily run lock"
}

function Exit-DawnstrikeDailyRunLock {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Lock)

    $mutex = Enter-DawnstrikeLockOperationMutex
    try {
        Exit-DawnstrikeDailyRunLockCore @PSBoundParameters
    }
    finally {
        Exit-DawnstrikeLockOperationMutex $mutex
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
