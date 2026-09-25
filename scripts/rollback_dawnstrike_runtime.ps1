[CmdletBinding()]
param(
    [string]$ActivationReceipt = "",
    [string]$ContractRoot = "",
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$BackupRoot = "C:\r\dawnstrike-state-backups",
    [ValidateRange(30, 1800)][int]$ProcessTimeoutSeconds = 300,
    [pscredential]$RunAsCredential,
    [ValidatePattern('^$|^[0-9a-f]{32}$')][string]$StateBoundaryTaskMutationOperationId = "",
    [switch]$StateBoundaryTerminalReconciliationRequired,
    [string]$TestNowUtc = ""
)

$global:PSModuleAutoLoadingPreference = 'None'
$env:PSModulePath = 'C:\Windows\System32\WindowsPowerShell\v1.0\Modules'
. ([IO.Path]::Combine($PSScriptRoot, 'powershell_module_boundary.ps1'))

$ErrorActionPreference = "Stop"
if (
    [int]$PSVersionTable.PSVersion.Major -lt 5 -or
    [string]$PSVersionTable.PSEdition -ne "Desktop"
) {
    throw "Dawnstrike rollback requires Windows PowerShell 5.1 or later (Desktop edition)."
}
$rollbackRuntimeRoot = $RuntimeRoot
$rollbackStateRoot = $StateRoot
$rollbackBackupRoot = $BackupRoot
$rollbackTimeout = $ProcessTimeoutSeconds
$rollbackRunAsCredential = $RunAsCredential
$rollbackStateBoundaryOperationId = $StateBoundaryTaskMutationOperationId
$rollbackStateBoundaryTerminalReconciliationRequired = $StateBoundaryTerminalReconciliationRequired
$rollbackTestNowUtc = $TestNowUtc
. (Join-Path $PSScriptRoot "activate_dawnstrike_runtime.ps1")
$RuntimeRoot = $rollbackRuntimeRoot
$StateRoot = $rollbackStateRoot
$BackupRoot = $rollbackBackupRoot
$RunAsCredential = $rollbackRunAsCredential
$StateBoundaryTaskMutationOperationId = $rollbackStateBoundaryOperationId
$StateBoundaryTerminalReconciliationRequired = $rollbackStateBoundaryTerminalReconciliationRequired
$ProcessTimeoutSeconds = $rollbackTimeout
$TestNowUtc = $rollbackTestNowUtc

function Get-DawnstrikeRollbackBoundaryNowUtc {
    [CmdletBinding()]
    param([string]$TestNowUtc = "")

    if (-not [string]::IsNullOrWhiteSpace($TestNowUtc)) {
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CLOCK -ne "1") {
            throw "Rollback clock override is test-only."
        }
        try {
            $parsed = [DateTimeOffset]::Parse(
                $TestNowUtc,
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind
            )
        }
        catch {
            throw "Rollback clock override is invalid."
        }
        if ($parsed.Offset -ne [TimeSpan]::Zero) {
            throw "Rollback clock override must be UTC."
        }
        return $parsed.ToUniversalTime()
    }
    return [DateTimeOffset]::UtcNow
}

function Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][DateTimeOffset]$NowUtc,
        [Parameter(Mandatory = $true)][string]$RollbackTargetMarketDate,
        [Parameter(Mandatory = $true)][string]$RequiredCompletedMarketDate,
        [Parameter(Mandatory = $true)][object[]]$TaskSnapshots,
        [ValidateSet('PROGRESS','EXPIRED_NO_RUN','RECOVERY_WITH_RUN')][string]$BoundaryMode = 'PROGRESS',
        [switch]$ProtectedInFlight,
        [switch]$AllowRecoveryEnablePrefix,
        [switch]$AllowRecoveryDisablePrefix
    )

    if ($BoundaryMode -ne 'PROGRESS' -and -not $ProtectedInFlight) {
        throw 'Rollback recovery modes require exact protected in-flight evidence.'
    }
    try {
        $targetDate = [DateTime]::ParseExact(
            $RollbackTargetMarketDate, 'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None
        ).Date
        $requiredCompletedDate = [DateTime]::ParseExact(
            $RequiredCompletedMarketDate, 'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None
        ).Date
    }
    catch { throw 'Runtime rollback boundary market date is invalid.' }
    if ($requiredCompletedDate -ge $targetDate) {
        throw 'Runtime rollback preceding market date is invalid.'
    }

    $expected = @($script:DawnstrikeCanonicalTaskNames)
    if (@($TaskSnapshots).Count -ne $expected.Count) {
        throw "Runtime rollback requires exactly five canonical task snapshots."
    }
    $byName = @{}
    foreach ($snapshot in @($TaskSnapshots)) {
        $name = [string]$snapshot.name
        if ($name -cnotin $expected -or $byName.ContainsKey($name)) {
            throw "Runtime rollback post-Finalizer task snapshot is unknown or duplicated."
        }
        if ([string]$snapshot.state -cnotin @("Ready", "Disabled")) {
            throw 'Runtime rollback requires every canonical task to be quiescent.'
        }
        if (-not ($snapshot.PSObject.Properties.Name -contains 'last_task_result')) {
            throw 'Runtime rollback task completion result is missing.'
        }
        try { $null = [int]$snapshot.last_task_result }
        catch { throw 'Runtime rollback task completion result is invalid.' }
        if ($BoundaryMode -ne 'PROGRESS' -and
            -not $AllowRecoveryEnablePrefix -and -not $AllowRecoveryDisablePrefix -and
            [string]$snapshot.state -cne 'Disabled') {
            throw 'Expired rollback recovery requires every canonical task to remain Disabled.'
        }
        $byName[$name] = $snapshot
    }

    # Canonical enablement is intentionally one task at a time.  The only
    # mixed Ready/Disabled state that can be produced by that transaction is a
    # Ready prefix in the exact canonical order.  Enforce that shape at every
    # boundary recheck so a foreign subset cannot borrow protected recovery
    # evidence.
    if ($AllowRecoveryEnablePrefix) {
        $disabledSeen = $false
        foreach ($name in $expected) {
            $state = [string]$byName[$name].state
            if ($state -ceq 'Disabled') {
                $disabledSeen = $true
            }
            elseif ($disabledSeen) {
                throw 'Runtime rollback canonical task enablement is not an exact recovery prefix.'
            }
        }
    }
    if ($AllowRecoveryDisablePrefix) {
        $readySeen = $false
        foreach ($name in $expected) {
            $state = [string]$byName[$name].state
            if ($state -ceq 'Ready') {
                $readySeen = $true
            }
            elseif ($readySeen) {
                throw 'Runtime rollback canonical task disablement is not an exact recovery prefix.'
            }
        }
    }

    $nowLocal = $NowUtc.ToLocalTime().DateTime
    if ($BoundaryMode -eq 'PROGRESS') {
        if ($targetDate -lt $nowLocal.Date) {
            throw 'Fresh rollback target is stale.'
        }
        if ($targetDate -eq $nowLocal.Date) {
            try {
                $eastern = [TimeZoneInfo]::FindSystemTimeZoneById('Eastern Standard Time')
                $morningLocal = [DateTime]::SpecifyKind($targetDate.AddHours(9), [DateTimeKind]::Unspecified)
                $morningUtc = [DateTimeOffset]::new(
                    [TimeZoneInfo]::ConvertTimeToUtc($morningLocal, $eastern),
                    [TimeSpan]::Zero
                )
            }
            catch { throw 'Runtime rollback Morning boundary could not be resolved.' }
            if ($NowUtc -ge $morningUtc.AddSeconds(-30)) {
                throw 'Fresh rollback PROGRESS boundary expired before Morning.'
            }
        }
    }

    foreach ($name in $expected) {
        $snapshot = $byName[$name]
        $nextRun = [DateTime]$snapshot.next_run_time
        if ([string]$snapshot.state -ceq 'Ready' -and $nextRun -eq [DateTime]::MinValue) {
            throw "Runtime rollback requires every Ready canonical task to expose a future trigger: $name"
        }
        if ($BoundaryMode -ceq 'RECOVERY_WITH_RUN' -and $nextRun -eq [DateTime]::MinValue) {
            throw "Rollback recovery-with-run requires every canonical trigger to have advanced: $name"
        }
        if ($nextRun -ne [DateTime]::MinValue -and
            ($nextRun -le $nowLocal -or $nextRun.Date -lt $targetDate)) {
            throw "Runtime rollback is blocked by a pending pre-target or overdue trigger: $name"
        }
        $lastRun = [DateTime]$snapshot.last_run_time
        if ($BoundaryMode -eq 'PROGRESS' -and $lastRun -ne [DateTime]::MinValue -and
            $lastRun.Date -ge $targetDate) {
            throw "Fresh rollback cannot cross any target-date task progress: $name"
        }
    }

    $eodLast = [DateTime]$byName["Dawnstrike AlphaOps EOD Full Report"].last_run_time
    $finalizerLast = [DateTime]$byName["Dawnstrike 10of10 Daily Finalize"].last_run_time
    if (
        $eodLast -eq [DateTime]::MinValue -or
        $finalizerLast -eq [DateTime]::MinValue -or
        $eodLast -gt $finalizerLast -or
        $finalizerLast -gt $nowLocal -or
        [int]$byName["Dawnstrike AlphaOps EOD Full Report"].last_task_result -ne 0 -or
        [int]$byName["Dawnstrike 10of10 Daily Finalize"].last_task_result -ne 0
    ) {
        throw 'Runtime rollback requires a successful ordered EOD-to-Finalizer boundary.'
    }

    if ($BoundaryMode -in @('PROGRESS','EXPIRED_NO_RUN')) {
        if ($eodLast.Date -lt $requiredCompletedDate -or
            $finalizerLast.Date -lt $requiredCompletedDate -or
            $eodLast.Date -ge $targetDate -or
            $finalizerLast.Date -ge $targetDate) {
            throw 'Runtime rollback progress history is outside its preceding-open boundary.'
        }
    }
    elseif ($eodLast.Date -ne $targetDate -or $finalizerLast.Date -ne $targetDate) {
        throw 'Rollback recovery-with-run requires target EOD and Finalizer completion.'
    }

    if ($BoundaryMode -eq 'EXPIRED_NO_RUN') {
        foreach ($name in $expected) {
            $snapshot = $byName[$name]
            $lastRun = [DateTime]$snapshot.last_run_time
            if ($lastRun -ne [DateTime]::MinValue -and $lastRun.Date -ge $targetDate) {
                throw "Expired no-run rollback recovery observed target progress: $name"
            }
            # Canonical tasks deliberately use StartWhenAvailable=true and a
            # Disabled task may report MinValue for NextRunTime.  This mode is
            # therefore detection-only: the caller must retain both locks,
            # normalize every task Disabled, and refuse automatic enablement.
            # An automatic late enable would be able to launch the missed
            # target occurrence.
            if (-not ($snapshot.PSObject.Properties.Name -contains 'start_when_available') -or
                -not [bool]$snapshot.start_when_available) {
                throw "Expired no-run rollback recovery canonical catch-up semantics drifted: $name"
            }
        }
    }
    elseif ($BoundaryMode -eq 'RECOVERY_WITH_RUN') {
        foreach ($name in @(
            'Dawnstrike AlphaOps Morning',
            'Dawnstrike AlphaOps Monitor 5m'
        )) {
            $lastRun = [DateTime]$byName[$name].last_run_time
            if ($lastRun.Date -ne $targetDate -or $lastRun -gt $nowLocal -or
                [int]$byName[$name].last_task_result -ne 0) {
                throw "Rollback recovery-with-run requires successful target task completion: $name"
            }
        }
    }

    # Weekly is scheduled Monday 21:00 host time. A post-session Monday fresh
    # rollback targeting Tuesday must wait for that pending occurrence; Sunday
    # before Monday and Monday pre-Morning instead use the prior elapsed Monday.
    $weeklyOccurrenceDate = $nowLocal.Date
    while ($weeklyOccurrenceDate.DayOfWeek -ne [DayOfWeek]::Monday) {
        $weeklyOccurrenceDate = $weeklyOccurrenceDate.AddDays(-1)
    }
    $weeklyOccurrence = $weeklyOccurrenceDate.AddHours(21)
    if ($weeklyOccurrence -gt $nowLocal) {
        if ($nowLocal.DayOfWeek -eq [DayOfWeek]::Monday -and $targetDate -gt $nowLocal.Date) {
            throw 'Runtime rollback cannot pass Monday before the pending Weekly task.'
        }
        $weeklyOccurrence = $weeklyOccurrence.AddDays(-7)
    }
    if ($BoundaryMode -eq 'RECOVERY_WITH_RUN' -and
        $targetDate.DayOfWeek -eq [DayOfWeek]::Monday) {
        $targetWeekly = $targetDate.AddHours(21)
        if ($nowLocal -lt $targetWeekly) {
            throw 'Rollback recovery-with-run must wait for target Monday Weekly.'
        }
        $weeklyOccurrence = $targetWeekly
    }
    while ($BoundaryMode -ne 'RECOVERY_WITH_RUN' -and $weeklyOccurrence.Date -ge $targetDate) {
        $weeklyOccurrence = $weeklyOccurrence.AddDays(-7)
    }
    $weeklyLast = [DateTime]$byName['Dawnstrike AlphaOps V6 Weekly Training'].last_run_time
    if ($weeklyLast -eq [DateTime]::MinValue -or
        $weeklyLast.Date -ne $weeklyOccurrence.Date -or
        $weeklyLast -gt $nowLocal -or
        [int]$byName['Dawnstrike AlphaOps V6 Weekly Training'].last_task_result -ne 0) {
        throw 'Runtime rollback requires the latest elapsed canonical Weekly task to complete successfully.'
    }
    return $true
}

function Assert-DawnstrikeRollbackPostFinalizerMutationWindow {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RollbackTargetMarketDate,
        [Parameter(Mandatory = $true)][string]$RequiredCompletedMarketDate,
        [ValidateSet('PROGRESS','EXPIRED_NO_RUN','RECOVERY_WITH_RUN')][string]$BoundaryMode = 'PROGRESS',
        [switch]$ProtectedInFlight,
        [switch]$AllowRecoveryEnablePrefix,
        [switch]$AllowRecoveryDisablePrefix,
        [string]$TestNowUtc = ""
    )

    $snapshots = @()
    foreach ($taskName in @(
        "Dawnstrike AlphaOps Morning",
        "Dawnstrike AlphaOps Monitor 5m",
        "Dawnstrike AlphaOps EOD Full Report",
        "Dawnstrike AlphaOps V6 Weekly Training",
        "Dawnstrike 10of10 Daily Finalize"
    )) {
        $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
        if ($matches.Count -ne 1) {
            throw "Runtime rollback post-Finalizer task is not unique: $taskName"
        }
        $taskPath = [string]$matches[0].TaskPath
        if ([string]::IsNullOrWhiteSpace($taskPath)) { $taskPath = "\" }
        $info = Get-ScheduledTaskInfo `
            -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
        $snapshots += [pscustomobject]@{
            name = $taskName
            state = [string]$matches[0].State
            last_run_time = [DateTime]$info.LastRunTime
            last_task_result = [int]$info.LastTaskResult
            next_run_time = [DateTime]$info.NextRunTime
            start_when_available = [bool]$matches[0].Settings.StartWhenAvailable
        }
    }
    $nowUtc = Get-DawnstrikeRollbackBoundaryNowUtc -TestNowUtc $TestNowUtc
    return Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
        -NowUtc $nowUtc -RollbackTargetMarketDate $RollbackTargetMarketDate `
        -RequiredCompletedMarketDate $RequiredCompletedMarketDate `
        -TaskSnapshots $snapshots -BoundaryMode $BoundaryMode `
        -ProtectedInFlight:$ProtectedInFlight `
        -AllowRecoveryEnablePrefix:$AllowRecoveryEnablePrefix `
        -AllowRecoveryDisablePrefix:$AllowRecoveryDisablePrefix
}

function Get-DawnstrikeRollbackSessionContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [string]$RollbackTargetMarketDate = '',
        [string]$TestNowUtc = ''
    )

    if ([string]::IsNullOrWhiteSpace($RollbackTargetMarketDate)) {
        $nowUtc = Get-DawnstrikeRollbackBoundaryNowUtc -TestNowUtc $TestNowUtc
        $contract = Invoke-DawnstrikeContractCli `
            -PythonPath $PythonPath -CandidateRoot $CandidateRoot `
            -Arguments @('resolve-rollback-session','--now-utc',$nowUtc.ToString('o')) `
            -Label 'Fresh rollback session resolution' -TimeoutSeconds $TimeoutSeconds
        if ([string]$contract.status -cne 'PASS' -or
            [string]$contract.rollback_target_market_date -cnotmatch '^\d{4}-\d{2}-\d{2}$' -or
            [string]$contract.required_completed_market_date -cnotmatch '^\d{4}-\d{2}-\d{2}$' -or
            $contract.research_only -ne $true -or $contract.broker_execution_enabled -ne $false) {
            throw 'Fresh rollback session contract is invalid.'
        }
        return $contract
    }

    # Recovery never resolves a new date. It only validates the protected
    # journal/receipt target against this exact candidate's governed calendar
    # and recomputes the preceding-open lower bound from that same calendar.
    $contract = Invoke-DawnstrikeContractCli `
        -PythonPath $PythonPath -CandidateRoot $CandidateRoot `
        -Arguments @('resolve-activation-session','--market-date',$RollbackTargetMarketDate) `
        -Label 'Sealed rollback session validation' -TimeoutSeconds $TimeoutSeconds
    if ([string]$contract.status -cne 'PASS' -or
        [string]$contract.market_date -cne $RollbackTargetMarketDate -or
        [string]$contract.required_completed_market_date -cnotmatch '^\d{4}-\d{2}-\d{2}$' -or
        $contract.research_only -ne $true -or $contract.broker_execution_enabled -ne $false) {
        throw 'Sealed rollback target is invalid under the exact candidate calendar.'
    }
    return [pscustomobject]@{
        status = 'PASS'
        rollback_target_market_date = $RollbackTargetMarketDate
        required_completed_market_date = [string]$contract.required_completed_market_date
        research_only = $true
        broker_execution_enabled = $false
    }
}

function Resolve-DawnstrikeProtectedRollbackBoundaryMode {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RollbackTargetMarketDate,
        [Parameter(Mandatory = $true)][string]$RequiredCompletedMarketDate,
        [switch]$AllowRecoveryEnablePrefix,
        [switch]$AllowRecoveryDisablePrefix,
        [string]$TestNowUtc = ''
    )

    $failures = @()
    foreach ($mode in @('PROGRESS','RECOVERY_WITH_RUN','EXPIRED_NO_RUN')) {
        try {
            $null = Assert-DawnstrikeRollbackPostFinalizerMutationWindow `
                -RollbackTargetMarketDate $RollbackTargetMarketDate `
                -RequiredCompletedMarketDate $RequiredCompletedMarketDate `
                -BoundaryMode $mode -ProtectedInFlight `
                -AllowRecoveryEnablePrefix:$AllowRecoveryEnablePrefix `
                -AllowRecoveryDisablePrefix:$AllowRecoveryDisablePrefix `
                -TestNowUtc $TestNowUtc
            return $mode
        }
        catch { $failures += "${mode}:$($_.Exception.Message)" }
    }
    throw ('Protected rollback recovery is outside every admitted boundary: ' + ($failures -join ' | '))
}

function Assert-DawnstrikeProtectedRollbackEnablePrefix {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedTaskDefinitionContractSha256,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedTaskActionContractSha256
    )

    $disabledSeen = $false
    $readyCount = 0
    $disabledCount = 0
    foreach ($taskName in @($script:DawnstrikeCanonicalTaskNames)) {
        $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
        if ($matches.Count -ne 1) {
            throw "Protected rollback enable-prefix task is not unique: $taskName"
        }
        $state = [string]$matches[0].State
        if ($state -ceq 'Ready') {
            if ($disabledSeen) {
                throw 'Protected rollback task state is not the exact canonical Ready prefix.'
            }
            $readyCount += 1
        }
        elseif ($state -ceq 'Disabled') {
            $disabledSeen = $true
            $disabledCount += 1
        }
        else {
            throw "Protected rollback enable-prefix task is not quiescent: $taskName state=$state"
        }
    }
    $contract = Get-DawnstrikeTaskContract $RuntimeRoot $StateRoot -AllowDisabled
    if (
        [int]$contract.enabled_count -ne $readyCount -or
        [int]$contract.disabled_count -ne $disabledCount -or
        ($readyCount + $disabledCount) -ne $script:DawnstrikeCanonicalTaskNames.Count -or
        [string]$contract.task_definition_contract_sha256 -cne $ExpectedTaskDefinitionContractSha256 -or
        [string]$contract.task_action_contract_sha256 -cne $ExpectedTaskActionContractSha256
    ) {
        throw 'Protected rollback enable prefix does not match its sealed task inventory and action contract.'
    }
    return [pscustomobject]@{
        ready_count = $readyCount
        disabled_count = $disabledCount
        task_contract = $contract
    }
}

function Assert-DawnstrikeProtectedRollbackDisablePrefix {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedTaskDefinitionContractSha256,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedTaskActionContractSha256
    )

    $readySeen = $false
    $readyCount = 0
    $disabledCount = 0
    foreach ($taskName in @($script:DawnstrikeCanonicalTaskNames)) {
        $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
        if ($matches.Count -ne 1) {
            throw "Protected rollback disable-prefix task is not unique: $taskName"
        }
        $state = [string]$matches[0].State
        if ($state -ceq 'Disabled') {
            if ($readySeen) {
                throw 'Protected rollback task state is not the exact canonical Disabled prefix.'
            }
            $disabledCount += 1
        }
        elseif ($state -ceq 'Ready') {
            $readySeen = $true
            $readyCount += 1
        }
        else {
            throw "Protected rollback disable-prefix task is not quiescent: $taskName state=$state"
        }
    }
    $contract = Get-DawnstrikeTaskContract $RuntimeRoot $StateRoot -AllowDisabled
    if (
        [int]$contract.enabled_count -ne $readyCount -or
        [int]$contract.disabled_count -ne $disabledCount -or
        ($readyCount + $disabledCount) -ne $script:DawnstrikeCanonicalTaskNames.Count -or
        [string]$contract.task_definition_contract_sha256 -cne $ExpectedTaskDefinitionContractSha256 -or
        [string]$contract.task_action_contract_sha256 -cne $ExpectedTaskActionContractSha256
    ) {
        throw 'Protected rollback disable prefix does not match its sealed task inventory and action contract.'
    }
    return [pscustomobject]@{
        ready_count = $readyCount
        disabled_count = $disabledCount
        task_contract = $contract
    }
}

function Assert-DawnstrikeRollbackStateBoundaryTerminalRecoveryAuthorization {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{32}$')][string]$OperationId
    )

    foreach ($name in @(
        'Assert-DawnstrikeStateRootBoundary',
        'Get-DawnstrikeStateBoundaryTaskMutationIntent',
        'Assert-DawnstrikeStateBoundaryTaskMutationIntent'
    )) {
        if ($null -eq (Get-Command $name -ErrorAction SilentlyContinue)) {
            throw 'Disabled terminal rollback recovery requires the installed protected StateRoot boundary helper.'
        }
    }
    $boundary = Assert-DawnstrikeStateRootBoundary `
        -StateRoot $StateRoot `
        -AllowedTaskMutationOperationId $OperationId `
        -AllowTaskDefinitionDrift
    try {
        $intent = Get-DawnstrikeStateBoundaryTaskMutationIntent `
            -EvidenceRoot 'C:\ProgramData\Dawnstrike'
        if ($null -eq $intent -or [string]$intent.payload.operation_id -cne $OperationId) {
            throw 'Disabled terminal rollback recovery has no exact protected operation identity.'
        }
        $null = Assert-DawnstrikeStateBoundaryTaskMutationIntent `
            -Intent $intent -StateRoot $StateRoot -Mode Rollback `
            -ExpectedSha $CandidateSha -ExpectedTree $CandidateTree
        if (
            [string]$boundary.candidate_sha -cne $CandidateSha -or
            [string]$boundary.candidate_tree -cne $CandidateTree -or
            [string]$boundary.receipt_sha256 -cne [string]$intent.payload.old_current_receipt_sha256 -or
            [string]$boundary.receipt.task_binding_sha256 -cne [string]$intent.payload.old_task_binding_sha256 -or
            $boundary.research_only -ne $true -or
            $boundary.broker_execution_enabled -ne $false
        ) {
            throw 'Disabled terminal rollback recovery is not bound to the protected predecessor receipt.'
        }
        $canonical = @(
            $boundary.receipt.task_definitions_and_principals | Where-Object { [bool]$_.canonical }
        )
        if ($canonical.Count -ne $script:DawnstrikeCanonicalTaskNames.Count -or
            @($canonical | Where-Object { [string]$_.state -cne 'Ready' }).Count -ne 0) {
            throw 'Disabled terminal rollback recovery predecessor canonical inventory is not exact Ready.'
        }
        $first = $canonical[0]
        $capture = @(
            $boundary.receipt.task_definitions_and_principals | Where-Object {
                [string]$_.task_name -ceq $script:DawnstrikeAuxiliaryCaptureTaskName -and
                -not [bool]$_.canonical
            }
        )
        if ($capture.Count -gt 1) {
            throw 'Disabled terminal rollback recovery predecessor capture inventory is ambiguous.'
        }
        $authorization = [pscustomobject]@{
            operation_id = $OperationId
            canonical_task_contract_sha256 = [string]$first.canonical_task_contract_sha256
            canonical_task_definition_contract_sha256 =
                [string]$first.canonical_task_definition_contract_sha256
            canonical_task_action_contract_sha256 =
                [string]$first.canonical_task_action_contract_sha256
            auxiliary_present = $capture.Count -eq 1
            auxiliary_state = if ($capture.Count -eq 1) { [string]$capture[0].state } else { 'ABSENT' }
            auxiliary_definition_sha256 = if ($capture.Count -eq 1) {
                [string]$capture[0].definition_sha256
            } else { '' }
            auxiliary_definition_contract_sha256 = if ($capture.Count -eq 1) {
                [string]$capture[0].definition_contract_sha256
            } else { '' }
            auxiliary_action_contract_sha256 = if ($capture.Count -eq 1) {
                [string]$capture[0].action_contract_sha256
            } else { '' }
        }
    }
    finally {
        foreach ($lock in @($boundary.locks)) {
            if ($null -ne $lock) { $lock.Dispose() }
        }
    }
    return $authorization
}

function Get-DawnstrikeRollbackProtectedRuntimeAuthorizations {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$CandidateSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$CandidateTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{32}$')]
        [string]$OperationId
    )

    foreach ($name in @(
        'Assert-DawnstrikeStateRootBoundary',
        'Get-DawnstrikeStateBoundaryTaskMutationIntentPath',
        'Read-DawnstrikeStateBoundaryProtectedJson',
        'Assert-DawnstrikeStateBoundaryTaskMutationIntent',
        'Get-DawnstrikeStateBoundaryRuntimeAuthorization'
    )) {
        if ($null -eq (Get-Command $name -ErrorAction SilentlyContinue)) {
            throw 'Rollback requires the installed protected StateRoot runtime-authorization boundary.'
        }
    }
    $boundary = $null
    $intentRead = $null
    $returnLocks = $false
    try {
        $boundary = Assert-DawnstrikeStateRootBoundary `
            -StateRoot $StateRoot -AllowedTaskMutationOperationId $OperationId `
            -AllowTaskDefinitionDrift
        $intentPath = Get-DawnstrikeStateBoundaryTaskMutationIntentPath `
            -EvidenceRoot 'C:\ProgramData\Dawnstrike'
        $intentRead = Read-DawnstrikeStateBoundaryProtectedJson -Path $intentPath
        $intent = [pscustomobject]@{
            path = $intentPath
            payload = $intentRead.payload
            sha256 = $intentRead.sha256
        }
        $null = Assert-DawnstrikeStateBoundaryTaskMutationIntent `
            -Intent $intent -StateRoot $StateRoot -Mode Rollback `
            -ExpectedSha $CandidateSha -ExpectedTree $CandidateTree
        if ([string]$boundary.receipt_sha256 -cne
                [string]$intent.payload.old_current_receipt_sha256 -or
            [string]$boundary.receipt.task_binding_sha256 -cne
                [string]$intent.payload.old_task_binding_sha256) {
            throw 'Rollback protected StateRoot predecessor changed after intent admission.'
        }
        $current = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $boundary.receipt -Kind current -StateRoot $StateRoot
        $rollback = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $boundary.receipt -Kind rollback -StateRoot $StateRoot
        $intentCurrent = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $intent.payload -Kind current -StateRoot $StateRoot
        $intentRollback = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $intent.payload -Kind rollback -StateRoot $StateRoot
        if ([string]$current.status -cne 'AUTHORIZED' -or
            [string]$rollback.status -cne 'AUTHORIZED' -or
            [string]$current.sha256 -cne [string]$intentCurrent.sha256 -or
            [string]$rollback.sha256 -cne [string]$intentRollback.sha256) {
            throw 'Rollback requires exact protected current and predecessor runtime authorizations.'
        }
        $locks = @($boundary.locks) + @($intentRead.stream)
        $boundary.locks = @()
        $intentRead.stream = $null
        $returnLocks = $true
        return [pscustomobject]@{
            current = $current
            rollback = $rollback
            intent = $intent
            boundary = $boundary
            locks = $locks
        }
    }
    finally {
        if (-not $returnLocks) {
            if ($null -ne $intentRead -and $null -ne $intentRead.stream) {
                $intentRead.stream.Dispose()
            }
            if ($null -ne $boundary) {
                foreach ($lock in @($boundary.locks)) {
                    if ($null -ne $lock) { $lock.Dispose() }
                }
            }
        }
    }
}

function Get-DawnstrikeActivationAuxiliaryRecoveryContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Activation,
        [Parameter(Mandatory = $true)][string]$StateRoot
    )

    if (-not [bool]$Activation.auxiliary_capture_present) {
        return [pscustomobject]@{
            present = $false
            task_path = "NONE"
            xml = ""
            xml_sha256 = Get-DawnstrikeSha256Text ""
            enabled = $false
        }
    }
    $backupName = [string]$Activation.scheduler_backup_name
    $manifestPath = Join-Path $StateRoot "scheduler-backups\$backupName\manifest.json"
    Assert-DawnstrikeNoReparseComponents $manifestPath "Activation scheduler backup manifest"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Activation auxiliary capture XML backup is missing."
    }
    if ((Get-DawnstrikeSha256File $manifestPath) -ne [string]$Activation.scheduler_backup_manifest_sha256) {
        throw "Activation auxiliary capture XML backup manifest hash does not match the receipt."
    }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Activation auxiliary capture XML backup manifest is invalid JSON."
    }
    $auxiliary = $manifest.auxiliary_capture
    if ($null -eq $auxiliary -or $auxiliary.present -ne $true) {
        throw "Activation auxiliary capture XML backup does not attest the governed task."
    }
    $xmlPath = Join-Path (Split-Path -Parent $manifestPath) ([string]$auxiliary.file_name)
    Assert-DawnstrikeNoReparseComponents $xmlPath "Activation auxiliary XML backup"
    if (-not (Test-Path -LiteralPath $xmlPath -PathType Leaf)) {
        throw "Activation auxiliary capture XML file is missing."
    }
    $xml = [System.IO.File]::ReadAllText($xmlPath)
    Assert-DawnstrikeNoReparseComponents $xmlPath "Activation auxiliary XML backup"
    if ((Get-DawnstrikeSha256File $xmlPath) -ne [string]$auxiliary.xml_file_sha256) {
        throw "Activation auxiliary capture XML backup changed during read."
    }
    if (
        (Get-DawnstrikeSha256File $xmlPath) -ne [string]$auxiliary.xml_file_sha256 -or
        (Get-DawnstrikeSha256Text $xml) -ne [string]$auxiliary.xml_sha256 -or
        [string]$auxiliary.xml_sha256 -ne [string]$Activation.auxiliary_capture_xml_sha256 -or
        [string]$auxiliary.xml_file_sha256 -ne [string]$Activation.auxiliary_capture_xml_file_sha256
    ) {
        throw "Activation auxiliary capture XML backup does not match the receipt."
    }
    return [pscustomobject]@{
        present = $true
        task_path = [string]$auxiliary.task_path
        xml = $xml
        xml_sha256 = [string]$auxiliary.xml_sha256
        xml_file_sha256 = [string]$auxiliary.xml_file_sha256
        definition_contract_sha256 = [string]$auxiliary.definition_contract_sha256
        action_contract_sha256 = [string]$auxiliary.action_contract_sha256
        enabled = ([string]$Activation.auxiliary_capture_state_before -eq "Ready")
    }
}

function Get-DawnstrikeRollbackTerminalAuxiliaryRecovery {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Activation,
        [Parameter(Mandatory = $true)][object]$Receipt,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [switch]$AllowStateBoundaryDisabledTerminal
    )

    $current = Get-DawnstrikeAuxiliaryCaptureTask $RuntimeRoot $StateRoot -AllowDisabled
    $hasPresence = $Receipt.PSObject.Properties.Name -contains 'auxiliary_capture_present'
    if (-not $hasPresence) {
        if ($current.present) {
            throw 'Legacy terminal rollback receipt requires the auxiliary capture task to be absent.'
        }
        return [pscustomobject]@{
            present = $false
            intended_state = 'ABSENT'
            requires_enable = $false
            current = $current
        }
    }
    if ($Receipt.auxiliary_capture_present -isnot [bool]) {
        throw 'Terminal rollback receipt auxiliary presence is invalid.'
    }
    if (-not [bool]$Receipt.auxiliary_capture_present) {
        if ([string]$Receipt.auxiliary_capture_action -cne 'RESTORED_EXACT' -or $current.present) {
            throw 'Terminal rollback receipt does not bind an exact absent auxiliary task.'
        }
        return [pscustomobject]@{
            present = $false
            intended_state = 'ABSENT'
            requires_enable = $false
            current = $current
        }
    }

    $expected = Get-DawnstrikeActivationAuxiliaryRecoveryContract `
        -Activation $Activation -StateRoot $StateRoot
    $intendedState = [string]$Receipt.auxiliary_capture_state_after
    if (
        -not $expected.present -or
        [string]$Receipt.auxiliary_capture_action -cne 'RESTORED_EXACT' -or
        $intendedState -cnotin @('Ready', 'Disabled') -or
        [string]$Receipt.auxiliary_capture_xml_sha256 -cne [string]$expected.xml_sha256 -or
        [string]$Receipt.auxiliary_capture_definition_contract_sha256 -cne
            [string]$expected.definition_contract_sha256 -or
        [string]$Receipt.auxiliary_capture_action_contract_sha256 -cne
            [string]$expected.action_contract_sha256 -or
        [bool]$expected.enabled -ne ($intendedState -ceq 'Ready')
    ) {
        throw 'Terminal rollback receipt auxiliary contract does not match the sealed activation backup.'
    }
    if (-not $current.present -or [string]$current.task_path -cne [string]$expected.task_path -or
        [string]$current.definition_contract_sha256 -cne [string]$expected.definition_contract_sha256 -or
        [string]$current.action_contract_sha256 -cne [string]$expected.action_contract_sha256) {
        throw 'Live terminal rollback auxiliary definition does not match the sealed activation backup.'
    }
    $requiresEnable = $false
    if ($intendedState -ceq 'Ready') {
        if ([string]$current.state -ceq 'Ready') {
            if ([string]$current.xml_sha256 -cne [string]$expected.xml_sha256) {
                throw 'Live terminal rollback Ready auxiliary XML is not exact.'
            }
        }
        elseif ($AllowStateBoundaryDisabledTerminal -and [string]$current.state -ceq 'Disabled') {
            $requiresEnable = $true
        }
        else {
            throw 'Live terminal rollback auxiliary state is not the receipt-bound Ready state.'
        }
    }
    elseif ([string]$current.state -cne 'Disabled' -or
        [string]$current.xml_sha256 -cne [string]$expected.xml_sha256) {
        throw 'Live terminal rollback Disabled auxiliary XML is not exact.'
    }
    return [pscustomobject]@{
        present = $true
        intended_state = $intendedState
        requires_enable = $requiresEnable
        current = $current
        expected = $expected
    }
}

function Assert-DawnstrikeCapturePreparedRecovery {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Activation,
        [Parameter(Mandatory = $true)][object]$Auxiliary,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][string]$PythonPath
    )

    if (-not $Auxiliary.present -or $Auxiliary.state -ne "Ready") {
        throw "Prepared capture-task recovery requires the current auxiliary task to be Ready."
    }
    $receiptRoot = Join-Path $StateRoot "receipts\capture-task"
    Assert-DawnstrikeNoReparseComponents $receiptRoot "Capture-task receipt root"
    $preparedPath = Join-Path $receiptRoot ("capture-task-rebind-" + $CandidateSha + ".prepared.json")
    Assert-DawnstrikeNoReparseComponents $preparedPath "Capture-task prepared recovery record"
    if (-not (Test-Path -LiteralPath $preparedPath -PathType Leaf)) {
        throw "Ready auxiliary capture task has no COMPLETE or PREPARED recovery chain."
    }
    $captureContract = Join-Path $PSScriptRoot "capture_task_contract.py"
    $activationPath = Join-Path $StateRoot ("receipts\runtime-activation\runtime-activation-" + [string]$Activation.activation_id + ".json")
    Assert-DawnstrikeNoReparseComponents $activationPath "Prepared recovery activation receipt"
    if (-not (Test-Path -LiteralPath $activationPath -PathType Leaf)) {
        throw "Capture-task PREPARED recovery activation receipt is missing."
    }
    $activationItem = Get-Item -LiteralPath $activationPath -Force -ErrorAction Stop
    $hashFields = @(
        "activation_receipt_sha256", "xml_before_sha256", "action_before_sha256",
        "definition_before_sha256", "normalized_definition_before_sha256",
        "principal_sha256", "trigger_sha256", "settings_sha256",
        "symbols_manifest_sha256", "entitlement_receipt_sha256", "source_config_sha256"
    )
    try {
        $preparedResult = Invoke-DawnstrikeActivationProcess $PythonPath @(
            $captureContract, "verify-prepared", "--prepared", $preparedPath,
            "--candidate-sha", $CandidateSha, "--candidate-tree", $CandidateTree
        ) $PSScriptRoot "Capture-task PREPARED recovery verification" $ProcessTimeoutSeconds
        $prepared = [string]$preparedResult.Stdout | ConvertFrom-Json
    }
    catch {
        throw "Capture-task PREPARED recovery record is not a strict self-hashed contract."
    }
    foreach ($field in $hashFields) {
        if ([string]$prepared.$field -notmatch '^[0-9a-f]{64}$') {
            throw "Capture-task PREPARED recovery hash is invalid: $field"
        }
    }
    if (
        [string]$prepared.schema_version -ne "dawnstrike.capture_task_rebind_prepared.v1" -or
        [string]$prepared.status -ne "PREPARED" -or
        [string]$prepared.task_name -ne $script:DawnstrikeAuxiliaryCaptureTaskName -or
        [string]$prepared.candidate_sha -ne $CandidateSha -or
        [string]$prepared.candidate_tree -ne $CandidateTree -or
        [string]$Activation.candidate_sha -ne $CandidateSha -or
        [string]$Activation.candidate_tree -ne $CandidateTree -or
        [string]$prepared.activation_id -ne [string]$Activation.activation_id -or
        [string]$prepared.activation_receipt_name -ne [string]$activationItem.Name -or
        [string]$prepared.activation_receipt_sha256 -ne (Get-DawnstrikeSha256File $activationItem) -or
        [string]$prepared.xml_before_sha256 -ne [string]$Activation.auxiliary_capture_xml_sha256 -or
        [string]$prepared.action_before_sha256 -ne [string]$Activation.auxiliary_capture_action_contract_sha256 -or
        [string]$prepared.definition_before_sha256 -ne [string]$Activation.auxiliary_capture_definition_contract_sha256 -or
        [string]$prepared.enablement_before -ne "Disabled" -or
        [string]$prepared.compensation -ne "RESTORE_EXACT_XML_AND_DISABLED" -or
        $prepared.research_only -ne $true -or
        $prepared.broker_execution_enabled -ne $false
    ) {
        throw "Capture-task PREPARED recovery record does not bind to the activation receipt."
    }
    if ([string]$prepared.previous_candidate_sha -notmatch '^[0-9a-f]{40}$') {
        throw "Capture-task PREPARED previous candidate SHA is invalid."
    }
    $original = Get-DawnstrikeActivationAuxiliaryRecoveryContract -Activation $Activation -StateRoot $StateRoot
    if (
        [string]$prepared.xml_before_sha256 -ne [string]$original.xml_sha256 -or
        [string]$prepared.action_before_sha256 -ne [string]$original.action_contract_sha256 -or
        [string]$prepared.definition_before_sha256 -ne [string]$original.definition_contract_sha256
    ) { throw "Capture-task PREPARED original XML is not bound to the activation backup." }
    foreach ($input in @(
        @("symbols_manifest_path", "symbols_manifest_sha256"),
        @("entitlement_receipt_path", "entitlement_receipt_sha256"),
        @("source_config_path", "source_config_sha256")
    )) {
        $inputPath = [string]$prepared.($input[0])
        Assert-DawnstrikeNoReparseComponents $inputPath "Capture-task PREPARED input"
        $inputItem = Get-Item -LiteralPath $inputPath -Force -ErrorAction Stop
        if ($inputItem.PSIsContainer -or ($inputItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Capture-task PREPARED input is not a regular file."
        }
        if ((Get-DawnstrikeSha256File $inputPath) -ne [string]$prepared.($input[1])) {
            throw "Capture-task PREPARED input hash does not match the supplied file."
        }
    }
    $candidateOccurrences = @([regex]::Matches([string]$Auxiliary.xml, [regex]::Escape($CandidateSha))).Count
    if (
        $candidateOccurrences -ne 1 -or
        [string]$Auxiliary.action_contract_sha256 -eq [string]$prepared.action_before_sha256 -or
        (Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Principal") -ne [string]$prepared.principal_sha256 -or
        (Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Triggers") -ne [string]$prepared.trigger_sha256 -or
        (Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Settings") -ne [string]$prepared.settings_sha256
    ) {
        throw "Ready auxiliary task does not prove the exact post-mutation PREPARED boundary."
    }
    return $prepared
}

function Archive-DawnstrikeTerminalRollbackAttemptBackup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$ActivationId,
        [Parameter(Mandatory = $true)][string]$JournalSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedManifestSha256,
        [switch]$AllowMissing
    )

    if ($JournalSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "Terminal rollback attempt journal hash is invalid."
    }
    if ($ExpectedManifestSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "Terminal rollback attempt backup hash is invalid."
    }
    if ($ActivationId -notmatch '^[0-9a-f]{24}$') {
        throw "Terminal rollback attempt activation id is invalid."
    }
    $backupRoot = Join-Path $StateRoot ("scheduler-backups\runtime-rollback-" + $ActivationId)
    $manifestPath = Join-Path $backupRoot "manifest.json"
    $archiveRoot = Join-Path $StateRoot "scheduler-backups\archive"
    $archiveRootPath = Join-Path $archiveRoot ("runtime-rollback-" + $ActivationId + "-" + $JournalSha256)
    $archiveManifestPath = Join-Path $archiveRootPath "manifest.json"
    Assert-DawnstrikeNoReparseComponents $backupRoot "Terminal rollback attempt backup"
    Assert-DawnstrikeNoReparseComponents $manifestPath "Terminal rollback attempt backup manifest"
    Assert-DawnstrikeNoReparseComponents $archiveRoot "Terminal rollback backup archive root"
    Assert-DawnstrikeNoReparseComponents $archiveRootPath "Terminal rollback backup archive"
    Assert-DawnstrikeNoReparseComponents $archiveManifestPath "Terminal rollback backup archive manifest"

    $sourceExists = Test-Path -LiteralPath $backupRoot -PathType Container
    $archiveExists = Test-Path -LiteralPath $archiveRootPath -PathType Container
    if (-not $sourceExists -and -not $archiveExists) {
        if ($AllowMissing) {
            return [pscustomobject]@{
                path = $null
                manifest_path = $null
                manifest_sha256 = $ExpectedManifestSha256
                archived = $false
            }
        }
        throw "Terminal rollback attempt backup is missing before compensation tombstone cleanup."
    }
    if ($sourceExists) {
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
            throw "Terminal rollback attempt backup manifest is missing."
        }
        if ((Get-DawnstrikeSha256File $manifestPath) -ne $ExpectedManifestSha256) {
            throw "Terminal rollback attempt backup manifest changed before archival."
        }
        if ($archiveExists) {
            throw "Terminal rollback attempt backup has both live and archived copies."
        }
        New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
        [IO.Directory]::Move($backupRoot, $archiveRootPath)
        if (Test-Path -LiteralPath $backupRoot) {
            throw "Terminal rollback attempt backup remained after archival."
        }
    }
    if (-not (Test-Path -LiteralPath $archiveManifestPath -PathType Leaf)) {
        throw "Terminal rollback attempt backup archive manifest is missing."
    }
    if ((Get-DawnstrikeSha256File $archiveManifestPath) -ne $ExpectedManifestSha256) {
        throw "Terminal rollback attempt backup archive manifest was not proven."
    }
    return [pscustomobject]@{
        path = $archiveRootPath
        manifest_path = $archiveManifestPath
        manifest_sha256 = $ExpectedManifestSha256
        archived = $sourceExists
    }
}

function Assert-DawnstrikeRollbackCompleteTerminal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Journal,
        [Parameter(Mandatory = $true)][object]$Activation,
        [Parameter(Mandatory = $true)][string]$ReceiptPath,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][string]$PreviousSha,
        [Parameter(Mandatory = $true)][string]$PreviousTree,
        [Parameter(Mandatory = $true)][string]$OriginIdentity,
        [Parameter(Mandatory = $true)][string]$ActivationMarketDate,
        [Parameter(Mandatory = $true)][string]$RollbackTargetMarketDate,
        [Parameter(Mandatory = $true)][object]$StateDeclaration,
        [switch]$AllowStateBoundaryDisabledTerminal
    )

    if ([string]$Journal.payload.operation -ne "runtime_rollback" -or
        [string]$Journal.payload.phase -ne "COMPLETE" -or
        [string]$Journal.payload.candidate_sha -ne $CandidateSha -or
        [string]$Journal.payload.candidate_tree -ne $CandidateTree -or
        [string]$Journal.payload.current_sha -ne $PreviousSha -or
        [string]$Journal.payload.current_tree -ne $PreviousTree -or
        [string]$Journal.payload.origin_identity -ne $OriginIdentity -or
        (($Journal.payload.PSObject.Properties.Name -contains 'rollback_target_market_date') -and
            [string]$Journal.payload.rollback_target_market_date -cne $RollbackTargetMarketDate)) {
        throw "Complete rollback journal identity is not exact."
    }
    Assert-DawnstrikeNoReparseComponents $ReceiptPath "Complete rollback receipt"
    if (-not (Test-Path -LiteralPath $ReceiptPath -PathType Leaf)) {
        throw "Complete rollback journal has no exact complete receipt."
    }
    $statePrefix = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\') + '\'
    $receiptFull = [IO.Path]::GetFullPath($ReceiptPath)
    if (-not $receiptFull.StartsWith($statePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Complete rollback receipt escaped StateRoot."
    }
    $receiptRelative = ($receiptFull.Substring($statePrefix.Length) -replace '\\','/')
    if ([string]$Journal.payload.complete_receipt_relative_path -cne $receiptRelative -or
        [string]$Journal.payload.complete_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$Journal.payload.complete_receipt_sha256 -cne (Get-DawnstrikeSha256File $ReceiptPath)) {
        throw "Complete rollback journal is not bound to the exact receipt bytes."
    }
    $verified = Invoke-DawnstrikeContractCli $PythonPath $CandidateRoot `
        -Arguments @("verify-receipt", "--receipt", $ReceiptPath, "--expected-status", "ROLLED_BACK") `
        -Label "Complete rollback receipt terminal validation" -TimeoutSeconds $TimeoutSeconds
    if ([string]$verified.candidate_sha -ne $CandidateSha -or
        [string]$verified.candidate_tree -ne $CandidateTree -or
        [string]$verified.previous_sha -ne $PreviousSha -or
        [string]$verified.previous_tree -ne $PreviousTree) {
        throw "Complete rollback receipt is not bound to the exact terminal identity."
    }
    if ($verified.PSObject.Properties.Name -contains 'activation_market_date') {
        if ([string]$verified.activation_market_date -cne $ActivationMarketDate -or
            [string]$verified.rollback_target_market_date -cne $RollbackTargetMarketDate) {
            throw 'Complete rollback receipt date identity is not exact.'
        }
    }
    elseif ([string]$verified.market_date -cne $ActivationMarketDate) {
        throw 'Legacy complete rollback receipt activation date is not exact.'
    }
    $live = Get-DawnstrikeGitContract $GitPath $RuntimeRoot $TimeoutSeconds $PreviousSha
    if ($live.tree -ne $PreviousTree) { throw "Complete rollback runtime HEAD/tree is not exact." }
    $liveOrigin = Get-DawnstrikeGitValue $GitPath $RuntimeRoot @("remote", "get-url", "origin") `
        "Complete rollback runtime origin validation" $TimeoutSeconds
    Assert-DawnstrikeSafeOrigin $liveOrigin
    $receiptOriginHash = [string]$verified.runtime_origin_sha256
    if ($receiptOriginHash -notmatch '^[0-9a-f]{64}$' -or
        (Convert-DawnstrikeCanonicalOriginIdentity $liveOrigin) -ne $OriginIdentity -or
        (Get-DawnstrikeSha256Text $liveOrigin) -ne $receiptOriginHash -or
        [string]$verified.runtime_origin_sha256 -ne [string]$Activation.runtime_origin_sha256) {
        throw "Complete rollback runtime origin is not receipt/journal-bound."
    }
    $activationTaskBackup = Get-DawnstrikeTaskXmlBackupManifest `
        -StateRoot $StateRoot -BackupName ([string]$Activation.scheduler_backup_name) `
        -ExpectedManifestSha256 ([string]$Activation.scheduler_backup_manifest_sha256)
    $null = Assert-DawnstrikeTaskXmlBackup `
        -StateRoot $StateRoot -BackupName ([string]$Activation.scheduler_backup_name) `
        -ExpectedManifestSha256 ([string]$Activation.scheduler_backup_manifest_sha256) `
        -ExpectedTaskContractSha256 ([string]$activationTaskBackup.task_contract_sha256) `
        -ExpectedTaskDefinitionContractSha256 ([string]$activationTaskBackup.task_definition_contract_sha256) `
        -ExpectedTaskActionContractSha256 ([string]$activationTaskBackup.task_action_contract_sha256)
    $tasks = Get-DawnstrikeTaskContract `
        $RuntimeRoot $StateRoot -AllowDisabled:$AllowStateBoundaryDisabledTerminal
    if ($AllowStateBoundaryDisabledTerminal) {
        if (
            [string]$tasks.task_definition_contract_sha256 -cne [string]$verified.task_definition_contract_sha256 -or
            [string]$tasks.task_action_contract_sha256 -cne [string]$verified.task_action_contract_sha256 -or
            [string]$tasks.task_definition_contract_sha256 -cne [string]$activationTaskBackup.task_definition_contract_sha256 -or
            [string]$tasks.task_action_contract_sha256 -cne [string]$activationTaskBackup.task_action_contract_sha256 -or
            [int]$tasks.enabled_count -ne 0 -or [int]$tasks.disabled_count -ne 5
        ) {
            throw "Complete rollback Disabled canonical recovery contract is not exact."
        }
        $null = Assert-DawnstrikeCanonicalTaskSemantics `
            -RuntimeRoot $RuntimeRoot -StateRoot $StateRoot `
            -ExpectedSha $PreviousSha -AllowDisabled
    }
    elseif ([string]$tasks.task_contract_sha256 -ne [string]$verified.task_contract_sha256 -or
        [string]$tasks.task_definition_contract_sha256 -ne [string]$verified.task_definition_contract_sha256 -or
        [string]$tasks.task_action_contract_sha256 -ne [string]$verified.task_action_contract_sha256 -or
        [string]$tasks.task_contract_sha256 -ne [string]$activationTaskBackup.task_contract_sha256 -or
        [string]$tasks.task_definition_contract_sha256 -ne [string]$activationTaskBackup.task_definition_contract_sha256 -or
        [string]$tasks.task_action_contract_sha256 -ne [string]$activationTaskBackup.task_action_contract_sha256 -or
        [int]$tasks.enabled_count -ne 5 -or [int]$tasks.disabled_count -ne 0) {
        throw "Complete rollback canonical task contract is not exact."
    }
    # The scheduler backup captures the candidate-bound task contract before
    # rollback.  The terminal receipt carries the *restored* previous-SHA
    # contract, so never compare those two different identities directly.
    # Archive/move is compensation-only; consuming this directory here would
    # make the validator unable to prove the same receipt on a crash/retry.
    $backupManifest = Get-DawnstrikeTaskXmlBackupManifest `
        -StateRoot $StateRoot -BackupName ([string]$verified.scheduler_backup_name) `
        -ExpectedManifestSha256 ([string]$verified.scheduler_backup_manifest_sha256)
    $null = Assert-DawnstrikeTaskXmlBackup `
        -StateRoot $StateRoot -BackupName ([string]$verified.scheduler_backup_name) `
        -ExpectedManifestSha256 ([string]$verified.scheduler_backup_manifest_sha256) `
        -ExpectedTaskContractSha256 ([string]$backupManifest.task_contract_sha256) `
        -ExpectedTaskDefinitionContractSha256 ([string]$backupManifest.task_definition_contract_sha256) `
        -ExpectedTaskActionContractSha256 ([string]$backupManifest.task_action_contract_sha256)
    $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
        -Receipt $Activation -StateRoot $StateRoot -BackupRoot $BackupRoot `
        -ToolRoot $CandidateRoot -GitPath $GitPath -PythonPath $PythonPath `
        -TimeoutSeconds $TimeoutSeconds
    if ($StateDeclaration.required -and $Activation.PSObject.Properties.Name -contains "state_preparation_receipt_sha256") {
        $proof = Get-DawnstrikeStatePreparationProof `
            -CandidateRoot $CandidateRoot -StateRoot $StateRoot -BackupRoot $BackupRoot `
            -CandidateSha $CandidateSha -CandidateTree $CandidateTree `
            -PythonPath $PythonPath -TimeoutSeconds $TimeoutSeconds
        if ([string]$verified.state_preparation_receipt_sha256 -ne [string]$proof.receipt_sha256 -or
            [string]$verified.state_preparation_after_db_sha256 -ne [string]$proof.after_db_sha256 -or
            [string]$verified.state_preparation_inventory_sha256 -ne [string]$proof.inventory_sha256) {
            throw "Complete rollback state-preparation lineage is not exact."
        }
    }
    return $verified
}

function Get-DawnstrikeTrustedRollbackTerminalEnvelope {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Activation,
        [Parameter(Mandatory = $true)][string]$ReceiptPath,
        [Parameter(Mandatory = $true)][string]$JournalPath,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$PythonSha256,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$CandidateSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$CandidateTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$PreviousSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$PreviousTree,
        [Parameter(Mandatory = $true)][string]$OriginIdentity,
        [Parameter(Mandatory = $true)][string]$ActivationMarketDate,
        [Parameter(Mandatory = $true)][string]$RollbackTargetMarketDate,
        [Parameter(Mandatory = $true)][object]$StateDeclaration,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{32}$')]
        [string]$OperationId
    )

    $receiptGuard = $null
    $journalGuard = $null
    try {
        $receiptGuard = Open-DawnstrikeActivationReceiptGuard `
            -Path $ReceiptPath -ExpectedStatus ROLLED_BACK -PythonPath $PythonPath `
            -ToolRoot $CandidateRoot -TimeoutSeconds $TimeoutSeconds
        $journalGuard = Open-DawnstrikeRuntimeJournalGuard `
            -Path $JournalPath -PythonPath $PythonPath -PythonSha256 $PythonSha256
        $verified = Assert-DawnstrikeRollbackCompleteTerminal `
            -Journal $journalGuard.journal -Activation $Activation `
            -ReceiptPath $ReceiptPath -CandidateRoot $CandidateRoot `
            -RuntimeRoot $RuntimeRoot -StateRoot $StateRoot -BackupRoot $BackupRoot `
            -GitPath $GitPath -PythonPath $PythonPath -TimeoutSeconds $TimeoutSeconds `
            -CandidateSha $CandidateSha -CandidateTree $CandidateTree `
            -PreviousSha $PreviousSha -PreviousTree $PreviousTree `
            -OriginIdentity $OriginIdentity -ActivationMarketDate $ActivationMarketDate `
            -RollbackTargetMarketDate $RollbackTargetMarketDate `
            -StateDeclaration $StateDeclaration
        if ([string]$verified.receipt_sha256 -cne [string]$receiptGuard.receipt.receipt_sha256) {
            throw 'Rollback terminal guard and deep proof returned different receipts.'
        }
        $null = Confirm-DawnstrikeActivationReceiptGuard $receiptGuard
        $null = Confirm-DawnstrikeRuntimeJournalGuard $journalGuard
        return New-DawnstrikeStateBoundaryTerminalEnvelope `
            -Terminal $receiptGuard.receipt -ReceiptSha256 ([string]$receiptGuard.sha256) `
            -JournalSha256 ([string]$journalGuard.sha256) -OperationId $OperationId `
            -StateRoot $StateRoot -Mode Rollback -ExpectedSha $CandidateSha `
            -ExpectedTree $CandidateTree
    }
    finally {
        Close-DawnstrikeRuntimeJournalGuard $journalGuard
        Close-DawnstrikeActivationReceiptGuard $receiptGuard
    }
}

function Invoke-DawnstrikeRuntimeRollback {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ActivationReceipt,
        [Parameter(Mandatory = $true)][string]$ContractRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][int]$ProcessTimeoutSeconds,
        [pscredential]$RunAsCredential,
        [ValidatePattern('^$|^[0-9a-f]{32}$')][string]$StateBoundaryTaskMutationOperationId = "",
        [switch]$StateBoundaryTerminalReconciliationRequired,
        [string]$TestNowUtc = ""
    )

    $contract = Resolve-DawnstrikeActivationRoot $ContractRoot "ContractRoot"
    $state = Resolve-DawnstrikeActivationRoot $StateRoot "StateRoot"
    $safeBackupRoot = Resolve-DawnstrikeActivationRoot $BackupRoot "BackupRoot"
    $runtime = Get-DawnstrikeFutureActivationRoot $RuntimeRoot "RuntimeRoot"
    Assert-DawnstrikeRootIsolation $safeBackupRoot @($contract, $runtime, $state) "BackupRoot"
    Assert-DawnstrikeNoReparseComponents $ActivationReceipt "Activation receipt"
    $receiptPath = [System.IO.Path]::GetFullPath($ActivationReceipt)
    $approvedReceiptRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $state "receipts\runtime-activation")
    ).TrimEnd('\') + '\'
    if (-not $receiptPath.StartsWith($approvedReceiptRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Activation receipt must be inside the durable activation receipt root."
    }
    Assert-DawnstrikeNoReparseComponents $receiptPath "Activation receipt"
    $receiptItem = Get-Item -LiteralPath $receiptPath -Force
    if (
        $receiptItem.PSIsContainer -or
        ($receiptItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Activation receipt cannot be a reparse point."
    }
    Assert-DawnstrikeNoReparseComponents $receiptPath "Activation receipt"

    try {
        $receiptHint = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Activation receipt is not valid JSON."
    }
    if (
        [string]$receiptHint.schema_version -notin @("dawnstrike.runtime_activation_receipt.v1", "dawnstrike.runtime_activation_receipt.v2") -or
        [string]$receiptHint.candidate_sha -notmatch '^[0-9a-f]{40}$' -or
        [string]$receiptHint.candidate_tree -notmatch '^[0-9a-f]{40}$'
    ) {
        throw "Activation receipt cannot identify an exact candidate checkout."
    }
    $rollbackAdmissionLocks = Assert-DawnstrikeActivationSourceAdmission `
        -CandidateRoot $contract `
        -ExpectedSha ([string]$receiptHint.candidate_sha)
    try {
    . (Join-Path $PSScriptRoot "runtime_activation_lock.ps1")
    $gitPath = (Get-DawnstrikeApprovedGit).path
    $pythonPath = (Get-DawnstrikeApprovedLockInterpreter).path
    $toolRoot = Resolve-DawnstrikeActivationRoot (Join-Path $PSScriptRoot "..") "ToolRoot"
    if (-not [string]::Equals(
        $contract,
        $toolRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "ContractRoot must be the exact checkout containing the rollback tool."
    }
    . (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")

    $contractGit = Get-DawnstrikeGitContract `
        $gitPath `
        $contract `
        $ProcessTimeoutSeconds `
        ([string]$receiptHint.candidate_sha)
    if ($contractGit.tree -ne [string]$receiptHint.candidate_tree) {
        throw "ContractRoot tree does not match the activation receipt candidate."
    }
    $null = Assert-DawnstrikeHelpersBoundToHead `
        -GitPath $gitPath -Root $contract -TimeoutSeconds $ProcessTimeoutSeconds
    . (Join-Path $PSScriptRoot "capture_task_safety.ps1")
    . (Join-Path $PSScriptRoot "invoke_dawnstrike_stage.ps1")

    $activation = Invoke-DawnstrikeContractCli `
        -PythonPath $pythonPath `
        -CandidateRoot $contract `
        -Arguments @("verify-receipt", "--receipt", $receiptPath) `
        -Label "Activation receipt verification" `
        -TimeoutSeconds $ProcessTimeoutSeconds
    if ($activation.schema_version -notin @("dawnstrike.runtime_activation_receipt.v1", "dawnstrike.runtime_activation_receipt.v2")) {
        throw "Rollback requires an activation receipt."
    }
    if (
        $contractGit.head -ne [string]$activation.candidate_sha -or
        $contractGit.tree -ne [string]$activation.candidate_tree
    ) {
        throw "Validated ContractRoot does not match the sealed activation receipt."
    }
    $stateBoundaryTerminalRecoveryAuthorized = $false
    $stateBoundaryTerminalRecoveryAuthorization = $null
    if ($StateBoundaryTerminalReconciliationRequired) {
        if ($StateBoundaryTaskMutationOperationId -notmatch '^[0-9a-f]{32}$') {
            throw "Disabled terminal rollback recovery requires its exact protected StateRoot operation identity."
        }
        $stateBoundaryTerminalRecoveryAuthorization = Assert-DawnstrikeRollbackStateBoundaryTerminalRecoveryAuthorization `
            -StateRoot $state -CandidateSha ([string]$activation.candidate_sha) `
            -CandidateTree ([string]$activation.candidate_tree) `
            -OperationId $StateBoundaryTaskMutationOperationId
        $stateBoundaryTerminalRecoveryAuthorized = $true
    }
    $activationId = [string]$activation.activation_id
    $candidateSha = [string]$activation.candidate_sha
    $previousSha = [string]$activation.previous_sha
    $previousTree = [string]$activation.previous_tree
    $activationMarketDate = [string]$activation.market_date
    $activationReceiptFileSha256 = Get-DawnstrikeSha256File $receiptPath
    $assertProtectedActivationLineage = {
        $null = Assert-DawnstrikeStateBoundaryActivationLineage `
            -StateRoot $state -ActivationReceiptPath $receiptPath `
            -ActivationReceiptSha256 $activationReceiptFileSha256 `
            -ExpectedActivationId $activationId
    }.GetNewClosure()
    & $assertProtectedActivationLineage
    if ($stateBoundaryTerminalRecoveryAuthorized -and (
        [string]$activation.task_contract_sha256 -cne
            [string]$stateBoundaryTerminalRecoveryAuthorization.canonical_task_contract_sha256 -or
        [string]$activation.task_definition_contract_sha256 -cne
            [string]$stateBoundaryTerminalRecoveryAuthorization.canonical_task_definition_contract_sha256 -or
        [string]$activation.task_action_contract_sha256 -cne
            [string]$stateBoundaryTerminalRecoveryAuthorization.canonical_task_action_contract_sha256
    )) {
        throw "Disabled terminal rollback recovery activation task contract is not the protected predecessor contract."
    }
    $rollbackRoot = Join-Path $state "runtime-rollbacks\$activationId"
    $rollbackCheckout = Join-Path $rollbackRoot "previous-runtime"
    $rollbackBundle = Join-Path $rollbackRoot "previous-runtime.bundle"
    $rollbackStage = "$runtime.rollback-stage-$activationId"
    $deactivatedCandidate = Join-Path $rollbackRoot "deactivated-candidate-runtime"
    $rollbackReceiptRoot = Join-Path $state "receipts\runtime-rollback"
    $rollbackReceipt = Join-Path $rollbackReceiptRoot "runtime-rollback-$activationId.json"
    $rollbackReadyReceipt = Join-Path $rollbackReceiptRoot "runtime-rollback-$activationId.ready.json"
    $rollbackSchedulerBackupName = "runtime-rollback-$activationId"
    $rollbackSchedulerBackupPath = Join-Path $state "scheduler-backups\$rollbackSchedulerBackupName"
    $operationJournalPath = Join-Path $state "receipts\runtime-operation\runtime-rollback-$activationId.json"
    $journalPreparedRelativePath = "receipts/runtime-activation/runtime-activation-$activationId.json"
    $journalCompleteRelativePath = "receipts/runtime-rollback/runtime-rollback-$activationId.json"
    $journalReadyRelativePath = "receipts/runtime-rollback/runtime-rollback-$activationId.ready.json"
    $journalEmptySha256 = Get-DawnstrikeSha256Text ""
    Assert-DawnstrikeNoReparseComponents $rollbackReceiptRoot "Rollback receipt root"
    Assert-DawnstrikeNoReparseComponents $rollbackReceipt "Rollback receipt"
    Assert-DawnstrikeNoReparseComponents $rollbackReadyReceipt "Rollback ready receipt"
    Assert-DawnstrikeNoReparseComponents $operationJournalPath "Rollback operation journal"
    Assert-DawnstrikeSameVolume @($runtime, $rollbackStage, $rollbackRoot)
    $approvedJournalInterpreter = Get-DawnstrikeApprovedLockInterpreter
    $contractOrigin = Get-DawnstrikeGitValue `
        $gitPath $contract @("remote", "get-url", "origin") `
        "Rollback contract origin verification" $ProcessTimeoutSeconds
    Assert-DawnstrikeSafeOrigin $contractOrigin
    $contractOriginIdentity = Convert-DawnstrikeCanonicalOriginIdentity $contractOrigin
    $entryRollbackJournal = $null
    $rollbackTargetMarketDate = ''
    if (Test-Path -LiteralPath $operationJournalPath -PathType Leaf) {
        $entryRollbackJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
            $operationJournalPath $approvedJournalInterpreter.path $approvedJournalInterpreter.sha256
        if ([string]$entryRollbackJournal.payload.operation -cne 'runtime_rollback') {
            throw 'Rollback journal path belongs to a different operation.'
        }
        if ($entryRollbackJournal.payload.PSObject.Properties.Name -contains 'rollback_target_market_date') {
            $rollbackTargetMarketDate = [string]$entryRollbackJournal.payload.rollback_target_market_date
        }
        elseif ([string]$entryRollbackJournal.payload.phase -notin @('COMPLETE','COMPENSATED')) {
            throw 'Legacy in-flight rollback journal has no immutable target.'
        }
        else {
            # Completed legacy evidence remains verifiable for cleanup only.
            $rollbackTargetMarketDate = $activationMarketDate
        }
    }
    $rollbackSessionContract = Get-DawnstrikeRollbackSessionContract `
        -PythonPath $pythonPath -CandidateRoot $contract `
        -TimeoutSeconds $ProcessTimeoutSeconds `
        -RollbackTargetMarketDate $rollbackTargetMarketDate `
        -TestNowUtc $TestNowUtc
    $rollbackTargetMarketDate = [string]$rollbackSessionContract.rollback_target_market_date
    $requiredCompletedMarketDate = [string]$rollbackSessionContract.required_completed_market_date
    # Resolve the declaration before any existing COMPLETE receipt fast path
    # so every terminal return uses the same exact candidate contract.  This
    # is read-only and does not authorize a stale/nonterminal rollback.
    $stateDeclaration = Get-DawnstrikeStatePreparationDeclaration `
        -CandidateRoot $contract `
        -GitPath $gitPath `
        -CandidateSha $candidateSha `
        -CandidateTree $activation.candidate_tree `
        -PythonPath $pythonPath `
        -TimeoutSeconds $ProcessTimeoutSeconds
    $null = Assert-DawnstrikeCandidateIdentityAndDeclaration `
        -GitPath $gitPath `
        -CandidateRoot $contract `
        -CandidateSha $candidateSha `
        -CandidateTree $activation.candidate_tree `
        -Declaration $stateDeclaration `
        -TimeoutSeconds $ProcessTimeoutSeconds

    # A runtime that merely happens to have the recorded previous SHA is not a
    # reusable rollback target.  The writer-owned activation receipt is only a
    # hint until the protected ProgramData receipt and mutation intent bind the
    # exact current (C) and predecessor (P) runtime authorizations.
    if (
        -not ($activation.PSObject.Properties.Name -contains "previous_runtime_rollback_authorized") -or
        $activation.previous_runtime_rollback_authorized -ne $true -or
        [string]$activation.previous_runtime_disposition -ne "AUTHORIZED_PROTECTED_CURRENT_RUNTIME"
    ) {
        throw "Rollback denied: the prior runtime is quarantined and lacks a protected runtime authorization."
    }
    if ($StateBoundaryTaskMutationOperationId -notmatch '^[0-9a-f]{32}$') {
        throw 'Rollback requires its exact protected StateRoot mutation operation identity.'
    }
    $protectedRuntimeAuthorizations = Get-DawnstrikeRollbackProtectedRuntimeAuthorizations `
        -StateRoot $state -CandidateSha $candidateSha `
        -CandidateTree ([string]$activation.candidate_tree) `
        -OperationId $StateBoundaryTaskMutationOperationId
    $rollbackAdmissionLocks += @($protectedRuntimeAuthorizations.locks)
    $protectedRuntimeAuthorizations.locks = @()
    $protectedCurrentAuthorization = $protectedRuntimeAuthorizations.current
    $protectedRollbackAuthorization = $protectedRuntimeAuthorizations.rollback
    $expectedActivationReceiptRelative =
        "receipts/runtime-activation/runtime-activation-$activationId.json"
    if (
        [string]$protectedCurrentAuthorization.runtime_sha -cne $candidateSha -or
        [string]$protectedCurrentAuthorization.runtime_tree -cne
            [string]$activation.candidate_tree -or
        [string]$protectedCurrentAuthorization.terminal_id -cne $activationId -or
        [string]$protectedCurrentAuthorization.contract.terminal_receipt_relative_path -cne
            $expectedActivationReceiptRelative -or
        [string]$protectedCurrentAuthorization.contract.terminal_receipt_sha256 -cne
            $activationReceiptFileSha256 -or
        [string]$protectedCurrentAuthorization.material.canonical_task_definition_contract_sha256 -cne
            [string]$activation.task_definition_contract_sha256 -or
        [string]$protectedCurrentAuthorization.material.canonical_task_action_contract_sha256 -cne
            [string]$activation.task_action_contract_sha256 -or
        [string]$protectedRollbackAuthorization.runtime_sha -cne $previousSha -or
        [string]$protectedRollbackAuthorization.runtime_tree -cne $previousTree -or
        [string]$protectedRollbackAuthorization.contract.terminal_receipt_sha256 -cne
            [string]$activation.previous_runtime_authorization_receipt_sha256 -or
        [string]$protectedRollbackAuthorization.contract.terminal_journal_sha256 -cne
            [string]$activation.previous_runtime_authorization_journal_sha256
    ) {
        throw 'Rollback protected runtime authorization is stale, remapped, or not bound to this activation.'
    }
    $currentAuthorizationGuard = Assert-DawnstrikeProtectedCurrentRuntimeAuthorization `
        -Authorization $protectedCurrentAuthorization `
        -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $candidateSha `
        -ExpectedTree ([string]$activation.candidate_tree) `
        -ExpectedOriginIdentity $contractOriginIdentity `
        -ExpectedOriginSha256 ([string]$activation.runtime_origin_sha256) `
        -GitPath $gitPath -TimeoutSeconds $ProcessTimeoutSeconds -SkipLiveTaskProof
    $rollbackAdmissionLocks += @($currentAuthorizationGuard.locks)
    $predecessorAuthorizationGuard = Assert-DawnstrikeProtectedCurrentRuntimeAuthorization `
        -Authorization $protectedRollbackAuthorization `
        -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $previousSha `
        -ExpectedTree $previousTree -ExpectedOriginIdentity $contractOriginIdentity `
        -ExpectedOriginSha256 ([string]$activation.runtime_origin_sha256) `
        -GitPath $gitPath -TimeoutSeconds $ProcessTimeoutSeconds -SkipLiveTaskProof
    $rollbackAdmissionLocks += @($predecessorAuthorizationGuard.locks)
    $getTrustedRollbackTerminalEnvelope = {
        param([Parameter(Mandatory = $true)][string]$TerminalOriginIdentity)
        Get-DawnstrikeTrustedRollbackTerminalEnvelope `
            -Activation $activation -ReceiptPath $rollbackReceipt `
            -JournalPath $operationJournalPath -CandidateRoot $contract `
            -RuntimeRoot $runtime -StateRoot $state -BackupRoot $safeBackupRoot `
            -GitPath $gitPath -PythonPath $pythonPath `
            -PythonSha256 ([string]$approvedJournalInterpreter.sha256) `
            -TimeoutSeconds $ProcessTimeoutSeconds -CandidateSha $candidateSha `
            -CandidateTree ([string]$activation.candidate_tree) `
            -PreviousSha $previousSha -PreviousTree $previousTree `
            -OriginIdentity $TerminalOriginIdentity `
            -ActivationMarketDate $activationMarketDate `
            -RollbackTargetMarketDate $rollbackTargetMarketDate `
            -StateDeclaration $stateDeclaration `
            -OperationId $StateBoundaryTaskMutationOperationId
    }.GetNewClosure()

    $existingRollbackReceipt = $null
    if (Test-Path -LiteralPath $rollbackReceipt -PathType Leaf) {
        Assert-DawnstrikeNoReparseComponents $rollbackReceipt "Existing rollback receipt"
        $existingRollbackReceipt = Invoke-DawnstrikeContractCli $pythonPath $contract @("verify-receipt", "--receipt", $rollbackReceipt, "--expected-status", "ROLLED_BACK") "Existing rollback receipt verification" $ProcessTimeoutSeconds
        $newRollbackDateContract = (
            $existingRollbackReceipt.PSObject.Properties.Name -contains 'activation_market_date' -and
            $existingRollbackReceipt.PSObject.Properties.Name -contains 'rollback_target_market_date'
        )
        if ($newRollbackDateContract) {
            if ([string]$existingRollbackReceipt.activation_market_date -cne $activationMarketDate -or
                [string]$existingRollbackReceipt.rollback_target_market_date -cne $rollbackTargetMarketDate) {
                throw 'Existing rollback receipt date identity is not exact.'
            }
        }
        elseif ([string]$existingRollbackReceipt.market_date -cne $activationMarketDate) {
            throw 'Legacy completed rollback receipt activation date is not exact.'
        }
        if (-not (Test-Path -LiteralPath $operationJournalPath -PathType Leaf)) {
            throw "Existing rollback receipt has no durable operation journal."
        }
        $approvedJournalInterpreter = Get-DawnstrikeApprovedLockInterpreter
        $existingJournal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $approvedJournalInterpreter.path $approvedJournalInterpreter.sha256
        if (
            [string]$existingJournal.payload.operation -ne "runtime_rollback" -or
            [string]$existingJournal.payload.candidate_sha -ne $candidateSha -or
            [string]$existingJournal.payload.candidate_tree -ne [string]$activation.candidate_tree -or
            [string]$existingJournal.payload.previous_sha -ne $previousSha -or
            [string]$existingJournal.payload.previous_tree -ne $previousTree -or
            ($newRollbackDateContract -and
                [string]$existingJournal.payload.rollback_target_market_date -cne $rollbackTargetMarketDate) -or
            [string]$existingJournal.payload.complete_receipt_relative_path -ne $journalCompleteRelativePath -or
            (
                [string]$existingJournal.payload.phase -eq "COMPLETE" -and
                [string]$existingJournal.payload.complete_receipt_sha256 -ne (Get-DawnstrikeSha256File $rollbackReceipt)
            )
        ) { throw "Existing rollback receipt is not bound to the exact rollback journal." }
        if ([string]$existingJournal.payload.phase -ne "COMPLETE") {
            if ([string]$existingJournal.payload.phase -cne "POST_SWAP_READY") {
                throw "Existing rollback receipt has an invalid non-COMPLETE journal phase."
            }
        }
        if ([string]$existingJournal.payload.phase -eq "COMPLETE") {
        $rollbackTerminalRecoveryJournalPath = if ($stateBoundaryTerminalRecoveryAuthorized) {
            Get-DawnstrikeTerminalRecoveryJournalPath `
                -StateRoot $state -OperationId $StateBoundaryTaskMutationOperationId
        }
        else { '' }
        $rollbackTerminalRecoveryJournalPending = (
            -not [string]::IsNullOrWhiteSpace($rollbackTerminalRecoveryJournalPath) -and
            (Test-Path -LiteralPath $rollbackTerminalRecoveryJournalPath -PathType Leaf)
        )
        $existingTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
        $disabledTerminalRecovery = (
            $stateBoundaryTerminalRecoveryAuthorized -and
            [int]$existingTasks.enabled_count -eq 0 -and
            [int]$existingTasks.disabled_count -eq $script:DawnstrikeCanonicalTaskNames.Count -and
            [string]$existingTasks.task_definition_contract_sha256 -ceq
                [string]$existingRollbackReceipt.task_definition_contract_sha256 -and
            [string]$existingTasks.task_action_contract_sha256 -ceq
                [string]$existingRollbackReceipt.task_action_contract_sha256
        )
        if (-not $rollbackTerminalRecoveryJournalPending -and -not $disabledTerminalRecovery -and (
            [string]$existingTasks.task_contract_sha256 -cne
                [string]$existingRollbackReceipt.task_contract_sha256 -or
            [string]$existingTasks.task_definition_contract_sha256 -cne
                [string]$existingRollbackReceipt.task_definition_contract_sha256 -or
            [string]$existingTasks.task_action_contract_sha256 -cne
                [string]$existingRollbackReceipt.task_action_contract_sha256 -or
            [int]$existingTasks.enabled_count -ne $script:DawnstrikeCanonicalTaskNames.Count -or
            [int]$existingTasks.disabled_count -ne 0
        )) {
            throw "Rollback receipt exists but the live canonical task contract is not an authorized terminal boundary."
        }
        $terminalAuxiliary = if ($rollbackTerminalRecoveryJournalPending) { $null } else {
            Get-DawnstrikeRollbackTerminalAuxiliaryRecovery `
                -Activation $activation -Receipt $existingRollbackReceipt `
                -RuntimeRoot $runtime -StateRoot $state `
                -AllowStateBoundaryDisabledTerminal:$disabledTerminalRecovery
        }
        $terminalValidationArguments = @{
            Journal = $existingJournal
            Activation = $activation
            ReceiptPath = $rollbackReceipt
            CandidateRoot = $contract
            RuntimeRoot = $runtime
            StateRoot = $state
            BackupRoot = $safeBackupRoot
            GitPath = $gitPath
            PythonPath = $pythonPath
            TimeoutSeconds = $ProcessTimeoutSeconds
            CandidateSha = $candidateSha
            CandidateTree = [string]$activation.candidate_tree
            PreviousSha = $previousSha
            PreviousTree = $previousTree
            OriginIdentity = [string]$existingJournal.payload.origin_identity
            ActivationMarketDate = $activationMarketDate
            RollbackTargetMarketDate = $rollbackTargetMarketDate
            StateDeclaration = $stateDeclaration
        }
        if (-not $rollbackTerminalRecoveryJournalPending) {
            $null = Assert-DawnstrikeRollbackCompleteTerminal `
                @terminalValidationArguments `
                -AllowStateBoundaryDisabledTerminal:$disabledTerminalRecovery
        }
        if (-not (Test-Path -LiteralPath $runtime -PathType Container)) {
            throw "Rollback receipt exists but the runtime is missing."
        }
        $current = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $previousSha
        if ($current.tree -ne $previousTree) {
            throw "Rollback receipt exists but the runtime tree does not match."
        }
        $currentOrigin = Get-DawnstrikeGitValue `
            $gitPath `
            $runtime `
            @("remote", "get-url", "origin") `
            "Existing rollback origin verification" `
            $ProcessTimeoutSeconds
        Assert-DawnstrikeSafeOrigin $currentOrigin
        if ((Get-DawnstrikeSha256Text $currentOrigin) -ne [string]$existingRollbackReceipt.runtime_origin_sha256) {
            throw "Rollback receipt exists but the runtime origin does not match."
        }
        $null = Assert-DawnstrikeTaskXmlBackup `
            -StateRoot $state `
            -BackupName ([string]$existingRollbackReceipt.scheduler_backup_name) `
            -ExpectedManifestSha256 ([string]$existingRollbackReceipt.scheduler_backup_manifest_sha256) `
            -ExpectedTaskContractSha256 ([string](Get-DawnstrikeTaskXmlBackupManifest `
                -StateRoot $state -BackupName ([string]$existingRollbackReceipt.scheduler_backup_name) `
                -ExpectedManifestSha256 ([string]$existingRollbackReceipt.scheduler_backup_manifest_sha256)).task_contract_sha256) `
            -ExpectedTaskDefinitionContractSha256 ([string](Get-DawnstrikeTaskXmlBackupManifest `
                -StateRoot $state -BackupName ([string]$existingRollbackReceipt.scheduler_backup_name) `
                -ExpectedManifestSha256 ([string]$existingRollbackReceipt.scheduler_backup_manifest_sha256)).task_definition_contract_sha256) `
            -ExpectedTaskActionContractSha256 ([string](Get-DawnstrikeTaskXmlBackupManifest `
                -StateRoot $state -BackupName ([string]$existingRollbackReceipt.scheduler_backup_name) `
                -ExpectedManifestSha256 ([string]$existingRollbackReceipt.scheduler_backup_manifest_sha256)).task_action_contract_sha256)
        $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
            -Receipt $existingRollbackReceipt `
            -StateRoot $state `
            -BackupRoot $safeBackupRoot `
            -ToolRoot $contract `
            -GitPath $gitPath `
            -PythonPath $pythonPath `
            -TimeoutSeconds $ProcessTimeoutSeconds
        $completeLockPath = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
        $completeLock = $null
        $completeDailyLock = $null
        $completeLockRoot = Join-Path $state "locks"
        Assert-DawnstrikeNoReparseComponents $completeLockRoot "Completed rollback lock root"
        $expectedCompleteDailyPath = Join-Path $completeLockRoot ("dawnstrike-daily-" + $rollbackTargetMarketDate + ".lock")
        $completeDailyPaths = @(
            Get-ChildItem -LiteralPath $completeLockRoot -Filter "dawnstrike-daily-*.lock" -File -Force -ErrorAction SilentlyContinue |
                ForEach-Object { [System.IO.Path]::GetFullPath($_.FullName) }
        )
        $unexpectedCompleteDaily = @($completeDailyPaths | Where-Object {
            $_ -ne [System.IO.Path]::GetFullPath($expectedCompleteDailyPath)
        })
        if ($unexpectedCompleteDaily.Count -gt 0) {
            throw "Completed rollback has a foreign or multiple daily lock set."
        }
        if ((Test-Path -LiteralPath $completeLockPath -PathType Leaf) -and
            -not $rollbackTerminalRecoveryJournalPending) {
            $completeInterpreter = Get-DawnstrikeApprovedLockInterpreter
            $completeLockSnapshot = Get-DawnstrikeStrictRuntimeLock $completeLockPath $completeInterpreter.path $completeInterpreter.sha256
            if ([string]$completeLockSnapshot.payload.operation -ne "runtime_rollback") {
                throw "Completed rollback lock belongs to a different operation."
            }
            if ([string]$completeLockSnapshot.payload.rollback_target_market_date -cne $rollbackTargetMarketDate) {
                throw 'Completed rollback lock target identity changed.'
            }
            $completeLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
                -StateRoot $state -JournalPath $operationJournalPath `
                -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -OriginIdentity (Convert-DawnstrikeCanonicalOriginIdentity $currentOrigin) `
                -PythonPath $completeInterpreter.path -PythonSha256 $completeInterpreter.sha256
            $completeJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
                $operationJournalPath $completeInterpreter.path $completeInterpreter.sha256
            if (
                [string]$completeJournal.payload.phase -ne "COMPLETE" -or
                [string]$completeJournal.payload.prepared_receipt_sha256 -ne (Get-DawnstrikeSha256File $receiptPath) -or
                [string]$completeJournal.payload.backup_contract_sha256 -ne [string]$existingRollbackReceipt.scheduler_backup_manifest_sha256 -or
                [string]$completeJournal.payload.complete_receipt_sha256 -ne (Get-DawnstrikeSha256File $rollbackReceipt)
            ) { throw "Completed rollback lock recovery changed the sealed journal." }
            if (Test-Path -LiteralPath $expectedCompleteDailyPath -PathType Leaf) {
                $completeDailyLock = Enter-DawnstrikeDailyRunLock `
                    -StateRoot $state -MarketDate $rollbackTargetMarketDate `
                    -Owner "runtime_rollback" -RetainHandle
                if (-not $completeDailyLock.acquired) {
                    throw "Completed rollback could not reacquire its exact daily lock."
                }
                Confirm-DawnstrikeActivationDailyLockHandshake `
                    -StateRoot $state -ActivationLock $completeLock -DailyLock $completeDailyLock | Out-Null
                Exit-DawnstrikeDailyRunLock -Lock $completeDailyLock
                if (Test-Path -LiteralPath $expectedCompleteDailyPath -PathType Leaf) {
                    throw "Completed rollback daily lock release was not proven."
                }
                $completeDailyLock = $null
            }
            Exit-DawnstrikeGovernedRuntimeLock $completeLock
            if (Test-Path -LiteralPath $completeLockPath -PathType Leaf) {
                throw "Completed rollback runtime lock release was not proven."
            }
            $completeLock = $null
        }
        elseif (-not $rollbackTerminalRecoveryJournalPending) {
            Assert-DawnstrikeNoDailyLocks $state
        }
        if ($disabledTerminalRecovery -or $rollbackTerminalRecoveryJournalPending) {
            $terminalRecoveryLock = $null
            $terminalRecoveryDailyLock = $null
            $releaseTerminalRecoveryLocks = $false
            try {
                # StateRoot may have isolated the terminal after the original
                # COMPLETE lock was released.  Re-establish both retained
                # exclusion layers before trusting or enabling any task.
                $terminalRecoveryInterpreter = Get-DawnstrikeApprovedLockInterpreter
                $terminalOriginIdentity = Convert-DawnstrikeCanonicalOriginIdentity $currentOrigin
                $terminalTaskContractSha256 = [string]$existingTasks.task_contract_sha256
                if ($rollbackTerminalRecoveryJournalPending) {
                    $pendingTerminalJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
                        $rollbackTerminalRecoveryJournalPath `
                        $terminalRecoveryInterpreter.path $terminalRecoveryInterpreter.sha256
                    if (
                        [string]$pendingTerminalJournal.payload.operation -cne 'runtime_rollback' -or
                        [string]$pendingTerminalJournal.payload.phase -cne 'INIT' -or
                        [string]$pendingTerminalJournal.payload.candidate_sha -cne $previousSha -or
                        [string]$pendingTerminalJournal.payload.candidate_tree -cne $previousTree -or
                        [string]$pendingTerminalJournal.payload.current_sha -cne $previousSha -or
                        [string]$pendingTerminalJournal.payload.current_tree -cne $previousTree -or
                        [string]$pendingTerminalJournal.payload.previous_sha -cne $previousSha -or
                        [string]$pendingTerminalJournal.payload.previous_tree -cne $previousTree -or
                        [string]$pendingTerminalJournal.payload.origin_identity -cne $terminalOriginIdentity -or
                        [string]$pendingTerminalJournal.payload.rollback_target_market_date -cne $rollbackTargetMarketDate -or
                        [string]$pendingTerminalJournal.payload.prepared_receipt_relative_path -cne $journalCompleteRelativePath -or
                        [string]$pendingTerminalJournal.payload.complete_receipt_relative_path -cne $journalCompleteRelativePath
                    ) { throw 'Terminal rollback recovery journal identity is not exact.' }
                    $terminalTaskContractSha256 = [string]$pendingTerminalJournal.payload.task_contract_sha256
                    if (Test-Path -LiteralPath $completeLockPath -PathType Leaf) {
                        $terminalRecoveryLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
                            -StateRoot $state -JournalPath $rollbackTerminalRecoveryJournalPath `
                            -CandidateSha $previousSha -CandidateTree $previousTree `
                            -OriginIdentity $terminalOriginIdentity `
                            -PythonPath $terminalRecoveryInterpreter.path `
                            -PythonSha256 $terminalRecoveryInterpreter.sha256
                    }
                    else {
                        $terminalRecoveryLock = Enter-DawnstrikeGovernedRuntimeLockWithJournal `
                            -StateRoot $state -JournalPath $rollbackTerminalRecoveryJournalPath `
                            -Operation runtime_rollback `
                            -CandidateSha $previousSha -CandidateTree $previousTree `
                            -CurrentSha $previousSha -CurrentTree $previousTree `
                            -PreviousSha $previousSha -PreviousTree $previousTree `
                            -OriginIdentity $terminalOriginIdentity `
                            -PreparedReceiptRelativePath $journalCompleteRelativePath `
                            -CompleteReceiptRelativePath $journalCompleteRelativePath `
                            -TaskContractSha256 $terminalTaskContractSha256 `
                            -RollbackTargetMarketDate $rollbackTargetMarketDate `
                            -PythonPath $terminalRecoveryInterpreter.path `
                            -PythonSha256 $terminalRecoveryInterpreter.sha256 `
                            -ProcessTimeoutSeconds $ProcessTimeoutSeconds
                    }
                }
                else {
                    $terminalRecoveryLock = Enter-DawnstrikeGovernedRuntimeLockWithJournal `
                        -StateRoot $state -JournalPath $rollbackTerminalRecoveryJournalPath `
                        -Operation runtime_rollback `
                        -CandidateSha $previousSha -CandidateTree $previousTree `
                        -CurrentSha $previousSha -CurrentTree $previousTree `
                        -PreviousSha $previousSha -PreviousTree $previousTree `
                        -OriginIdentity $terminalOriginIdentity `
                        -PreparedReceiptRelativePath $journalCompleteRelativePath `
                        -CompleteReceiptRelativePath $journalCompleteRelativePath `
                        -TaskContractSha256 $terminalTaskContractSha256 `
                        -RollbackTargetMarketDate $rollbackTargetMarketDate `
                        -PythonPath $terminalRecoveryInterpreter.path `
                        -PythonSha256 $terminalRecoveryInterpreter.sha256 `
                        -ProcessTimeoutSeconds $ProcessTimeoutSeconds
                }
                $terminalRecoveryDailyLock = Enter-DawnstrikeDailyRunLock `
                    -StateRoot $state -MarketDate $rollbackTargetMarketDate `
                    -Owner 'runtime_rollback' -RetainHandle
                if (-not $terminalRecoveryDailyLock.acquired) {
                    throw 'Disabled terminal rollback recovery could not acquire its exact daily lock.'
                }
                Confirm-DawnstrikeActivationDailyLockHandshake `
                    -StateRoot $state -ActivationLock $terminalRecoveryLock `
                    -DailyLock $terminalRecoveryDailyLock | Out-Null
                $lockedTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
                if (
                    [int]$lockedTasks.enabled_count + [int]$lockedTasks.disabled_count -ne
                        $script:DawnstrikeCanonicalTaskNames.Count -or
                    [string]$lockedTasks.task_definition_contract_sha256 -cne
                        [string]$existingRollbackReceipt.task_definition_contract_sha256 -or
                    [string]$lockedTasks.task_action_contract_sha256 -cne
                        [string]$existingRollbackReceipt.task_action_contract_sha256
                ) {
                    throw "Terminal rollback recovery task definitions changed before reconciliation."
                }
                $null = Assert-DawnstrikeCanonicalTaskSemantics `
                    -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $previousSha -AllowDisabled
                # A terminal retry may only observe the exact Ready prefix
                # produced by Enable-DawnstrikeCanonicalTasks.  Bind that
                # transient state to the completed receipt before normalizing
                # anything, so a foreign Ready subset cannot borrow the
                # protected terminal journal and retained locks.
                $null = Assert-DawnstrikeProtectedRollbackEnablePrefix `
                    -RuntimeRoot $runtime -StateRoot $state `
                    -ExpectedTaskDefinitionContractSha256 ([string]$existingRollbackReceipt.task_definition_contract_sha256) `
                    -ExpectedTaskActionContractSha256 ([string]$existingRollbackReceipt.task_action_contract_sha256)
                $terminalAuxiliary = Get-DawnstrikeRollbackTerminalAuxiliaryRecovery `
                    -Activation $activation -Receipt $existingRollbackReceipt `
                    -RuntimeRoot $runtime -StateRoot $state `
                    -AllowStateBoundaryDisabledTerminal
                if ([bool]$terminalAuxiliary.present -ne
                    [bool]$stateBoundaryTerminalRecoveryAuthorization.auxiliary_present) {
                    throw "Disabled terminal rollback recovery auxiliary presence differs from the protected predecessor."
                }
                $canonicalReadyExact = (
                    [int]$lockedTasks.enabled_count -eq $script:DawnstrikeCanonicalTaskNames.Count -and
                    [int]$lockedTasks.disabled_count -eq 0 -and
                    [string]$lockedTasks.task_contract_sha256 -ceq
                        [string]$existingRollbackReceipt.task_contract_sha256
                )
                if ($canonicalReadyExact -and -not [bool]$terminalAuxiliary.requires_enable) {
                    $null = Assert-DawnstrikeRollbackCompleteTerminal @terminalValidationArguments
                    $releaseTerminalRecoveryLocks = $true
                    return & $getTrustedRollbackTerminalEnvelope `
                        ([string]$existingJournal.payload.origin_identity)
                }

                # A kill may leave any canonical enable prefix and may occur
                # before or after the auxiliary enable. Normalize the entire
                # protected set under retained exclusion, then replay exactly.
                if ([int]$lockedTasks.enabled_count -ne 0) {
                    $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
                }
                if (
                    $terminalAuxiliary.present -and
                    [string]$terminalAuxiliary.intended_state -ceq 'Ready' -and
                    [string]$terminalAuxiliary.current.state -ceq 'Ready'
                ) {
                    $null = Disable-DawnstrikeAuxiliaryCaptureTask $runtime $state
                }
                Confirm-DawnstrikeActivationDailyLockHandshake `
                    -StateRoot $state -ActivationLock $terminalRecoveryLock `
                    -DailyLock $terminalRecoveryDailyLock | Out-Null
                $terminalRollbackBoundaryMode = Resolve-DawnstrikeProtectedRollbackBoundaryMode `
                    -RollbackTargetMarketDate $rollbackTargetMarketDate `
                    -RequiredCompletedMarketDate $requiredCompletedMarketDate `
                    -TestNowUtc $TestNowUtc
                $null = Assert-DawnstrikeRollbackCompleteTerminal `
                    @terminalValidationArguments -AllowStateBoundaryDisabledTerminal
                $disabledTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
                if (
                    [int]$disabledTasks.enabled_count -ne 0 -or
                    [int]$disabledTasks.disabled_count -ne $script:DawnstrikeCanonicalTaskNames.Count
                ) { throw 'Terminal rollback recovery could not prove exact Disabled normalization.' }
                $terminalAuxiliary = Get-DawnstrikeRollbackTerminalAuxiliaryRecovery `
                    -Activation $activation -Receipt $existingRollbackReceipt `
                    -RuntimeRoot $runtime -StateRoot $state `
                    -AllowStateBoundaryDisabledTerminal
                if ($terminalRollbackBoundaryMode -ceq 'EXPIRED_NO_RUN') {
                    # Canonical StartWhenAvailable=true means an automatic late
                    # enable can queue the missed target occurrence.  Without a
                    # separately journaled, live-proven catch-up-neutralization
                    # protocol, the only safe terminal state is exact Disabled
                    # under both retained locks.  Preserve the journal and both
                    # locks for governed operator recovery.
                    Confirm-DawnstrikeActivationDailyLockHandshake `
                        -StateRoot $state -ActivationLock $terminalRecoveryLock `
                        -DailyLock $terminalRecoveryDailyLock | Out-Null
                    $null = Assert-DawnstrikeRollbackPostFinalizerMutationWindow `
                        -RollbackTargetMarketDate $rollbackTargetMarketDate `
                        -RequiredCompletedMarketDate $requiredCompletedMarketDate `
                        -BoundaryMode EXPIRED_NO_RUN -ProtectedInFlight `
                        -TestNowUtc $TestNowUtc
                    if (
                        $terminalAuxiliary.present -and
                        [string]$terminalAuxiliary.current.state -cne 'Disabled'
                    ) {
                        throw 'Expired no-run terminal rollback recovery could not prove the auxiliary task Disabled.'
                    }
                    throw 'Expired no-run terminal rollback recovery is safely retained Disabled under both locks; governed operator recovery is required.'
                }
                $terminalEnableBoundary = {
                    Confirm-DawnstrikeActivationDailyLockHandshake `
                        -StateRoot $state -ActivationLock $terminalRecoveryLock `
                        -DailyLock $terminalRecoveryDailyLock | Out-Null
                    $null = Assert-DawnstrikeRollbackPostFinalizerMutationWindow `
                        -RollbackTargetMarketDate $rollbackTargetMarketDate `
                        -RequiredCompletedMarketDate $requiredCompletedMarketDate `
                        -BoundaryMode $terminalRollbackBoundaryMode -ProtectedInFlight `
                        -AllowRecoveryEnablePrefix `
                        -TestNowUtc $TestNowUtc
                }.GetNewClosure()
                Enable-DawnstrikeCanonicalTasks -BeforeEachEnable $terminalEnableBoundary
                if ($terminalAuxiliary.present -and $terminalAuxiliary.requires_enable) {
                    & $terminalEnableBoundary
                    Enable-ScheduledTask `
                        -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName `
                        -TaskPath ([string]$terminalAuxiliary.current.task_path) `
                        -ErrorAction Stop | Out-Null
                }
                & $terminalEnableBoundary
                $readyTasks = Get-DawnstrikeTaskContract $runtime $state
                if (
                    [int]$readyTasks.enabled_count -ne $script:DawnstrikeCanonicalTaskNames.Count -or
                    [int]$readyTasks.disabled_count -ne 0 -or
                    [string]$readyTasks.task_contract_sha256 -cne
                        [string]$existingRollbackReceipt.task_contract_sha256 -or
                    [string]$readyTasks.task_definition_contract_sha256 -cne
                        [string]$existingRollbackReceipt.task_definition_contract_sha256 -or
                    [string]$readyTasks.task_action_contract_sha256 -cne
                        [string]$existingRollbackReceipt.task_action_contract_sha256
                ) {
                    throw "Recovered terminal rollback canonical Ready contract is not exact."
                }
                $null = Get-DawnstrikeRollbackTerminalAuxiliaryRecovery `
                    -Activation $activation -Receipt $existingRollbackReceipt `
                    -RuntimeRoot $runtime -StateRoot $state
                $null = Assert-DawnstrikeRollbackCompleteTerminal @terminalValidationArguments
                $releaseTerminalRecoveryLocks = $true
            }
            catch {
                $terminalRecoveryFailure = $_
                if ($null -ne $terminalRecoveryDailyLock -and $terminalRecoveryDailyLock.acquired) {
                    try { $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state } catch { }
                    try { $null = Disable-DawnstrikeAuxiliaryCaptureTask $runtime $state } catch { }
                    $failedClosedTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
                    $failedClosedAuxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state -AllowDisabled
                    if (
                        [int]$failedClosedTasks.enabled_count -ne 0 -or
                        [int]$failedClosedTasks.disabled_count -ne $script:DawnstrikeCanonicalTaskNames.Count -or
                        ($failedClosedAuxiliary.present -and [string]$failedClosedAuxiliary.state -cne 'Disabled')
                    ) {
                        throw "Disabled terminal rollback recovery failed and every affected task could not be held Disabled."
                    }
                }
                throw $terminalRecoveryFailure
            }
            finally {
                if ($releaseTerminalRecoveryLocks -and $null -ne $terminalRecoveryDailyLock -and $terminalRecoveryDailyLock.acquired) {
                    Exit-DawnstrikeDailyRunLock -Lock $terminalRecoveryDailyLock
                }
                if ($releaseTerminalRecoveryLocks -and $null -ne $terminalRecoveryLock -and $terminalRecoveryLock.acquired) {
                    Exit-DawnstrikeGovernedTerminalRecoveryLockWithJournal `
                        -StateRoot $state -JournalPath $rollbackTerminalRecoveryJournalPath `
                        -Lock $terminalRecoveryLock -Operation runtime_rollback `
                        -CandidateSha $previousSha -CandidateTree $previousTree `
                        -OriginIdentity (Convert-DawnstrikeCanonicalOriginIdentity $currentOrigin) `
                        -PythonPath $terminalRecoveryInterpreter.path `
                        -PythonSha256 $terminalRecoveryInterpreter.sha256 `
                        -RollbackTargetMarketDate $rollbackTargetMarketDate
                }
            }
        }
        return & $getTrustedRollbackTerminalEnvelope `
            ([string]$existingJournal.payload.origin_identity)
        }
    }

    # A failed rollback may restore the activated candidate and exact Ready
    # tasks.  That is a terminal compensation boundary, not POST_SWAP retry
    # state (POST_SWAP requires the previous runtime and Disabled tasks).
    if (Test-Path -LiteralPath $operationJournalPath -PathType Leaf) {
        $compensatedJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
            $operationJournalPath $approvedJournalInterpreter.path $approvedJournalInterpreter.sha256
        if ([string]$compensatedJournal.payload.phase -eq "COMPENSATED") {
            $compensatedRelative = [string]$compensatedJournal.payload.compensation_receipt_relative_path
            $compensatedPath = Join-Path $state ($compensatedRelative.Replace('/', '\'))
            $compensationCheck = Invoke-DawnstrikeActivationProcess `
                -FilePath $approvedJournalInterpreter.path `
                -ArgumentList @(
                    "-I", "-B", "-S",
                    (Join-Path $PSScriptRoot "runtime_operation_journal.py"),
                    "verify-compensation", "--receipt", $compensatedPath,
                    "--state-root", $state
                ) `
                -WorkingDirectory $PSScriptRoot `
                -Label "Compensated rollback receipt strict validation" `
                -TimeoutSeconds $ProcessTimeoutSeconds
            try {
                $compensationPayload = ([string]$compensationCheck.Stdout | ConvertFrom-Json).payload
            }
            catch {
                throw "Compensated rollback receipt validation returned invalid JSON."
            }
            $compensatedRuntime = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $candidateSha
            $compensationOrigin = Get-DawnstrikeGitValue `
                $gitPath $runtime @("remote", "get-url", "origin") `
                "Compensated rollback origin verification" $ProcessTimeoutSeconds
            Assert-DawnstrikeSafeOrigin $compensationOrigin
            if ((Get-DawnstrikeSha256Text $compensationOrigin) -ne [string]$activation.runtime_origin_sha256) {
                throw "Compensated rollback origin does not match the activation receipt."
            }
            $compensationOriginIdentity = Convert-DawnstrikeCanonicalOriginIdentity $compensationOrigin
            $compensatedTasks = Get-DawnstrikeTaskContract $runtime $state
            if (
                $compensationPayload.operation -ne "runtime_rollback" -or
                $compensationPayload.candidate_sha -ne $candidateSha -or
                $compensationPayload.candidate_tree -ne [string]$activation.candidate_tree -or
                $compensationPayload.prior_journal_file_sha256 -ne [string]$compensatedJournal.payload.prior_journal_file_sha256 -or
                $compensationPayload.task_state -ne "Ready" -or
                $compensatedRuntime.tree -ne [string]$activation.candidate_tree -or
                $compensatedTasks.task_contract_sha256 -ne [string]$compensatedJournal.payload.task_contract_sha256 -or
                $compensatedTasks.task_contract_sha256 -ne [string]$compensationPayload.task_contract_sha256 -or
                $compensatedTasks.task_contract_sha256 -ne [string]$compensationPayload.task_xml_sha256 -or
                $compensatedTasks.task_action_contract_sha256 -ne [string]$compensationPayload.task_action_contract_sha256 -or
                $compensatedTasks.task_definition_contract_sha256 -ne [string]$compensationPayload.task_definition_contract_sha256 -or
                $compensatedJournal.payload.compensation_receipt_sha256 -ne (Get-DawnstrikeSha256File $compensatedPath)
            ) { throw "Compensated rollback tombstone does not attest the exact restored boundary." }
            # Terminal compensation cleanup changes only protected evidence;
            # it never enables tasks and therefore does not consume a fresh
            # schedule window.
            $compensationLock = $null
            $compensationLockPath = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
            if (Test-Path -LiteralPath $compensationLockPath -PathType Leaf) {
                $compensationLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
                    -StateRoot $state -JournalPath $operationJournalPath -CandidateSha $candidateSha `
                    -CandidateTree ([string]$activation.candidate_tree) -OriginIdentity $compensationOriginIdentity `
                    -PythonPath $approvedJournalInterpreter.path -PythonSha256 $approvedJournalInterpreter.sha256
            }
            if (@(Get-ChildItem -LiteralPath (Join-Path $state "locks") -Filter "dawnstrike-daily-*.lock" -File -Force -ErrorAction SilentlyContinue).Count -gt 0) {
                if ($null -eq $compensationLock) { throw "Compensated rollback has a daily lock without its runtime lock." }
                $compensationDaily = Enter-DawnstrikeDailyRunLock `
                    -StateRoot $state -MarketDate $rollbackTargetMarketDate `
                    -Owner "runtime_rollback" -RetainHandle
                if (-not $compensationDaily.acquired) { throw "Compensated rollback could not recover its daily lock." }
                Exit-DawnstrikeDailyRunLock $compensationDaily
            }
            # The failed cycle's scheduler backup is terminal evidence.  A
            # recursive retry must not encounter it as a live fresh-attempt
            # backup (the cycle-1 -> cycle-2 poison).  Move it under an
            # immutable prior-journal-hash name only after proving its exact
            # sealed manifest; the helper is idempotent for a crash between
            # archival and tombstone cleanup.
            $archiveAttemptArgs = @{
                StateRoot = $state
                ActivationId = $activationId
                JournalSha256 = [string]$compensatedJournal.raw_file_sha256
                ExpectedManifestSha256 = [string]$compensatedJournal.payload.backup_contract_sha256
            }
            # The COMPENSATED branch runs before the new attempt snapshots
            # live tasks, so process-local $tasksInitiallyEnabled is not valid
            # recovery evidence here.  The sealed backup hash identifies the
            # exact topology: equality with the activation backup means the
            # failed attempt began from an already-Disabled contract and did
            # not create a rollback-attempt backup.  Every other hash requires
            # the rollback backup (or its exact archive) to remain present.
            if (
                [string]$compensatedJournal.payload.backup_contract_sha256 -ceq
                    [string]$activation.scheduler_backup_manifest_sha256
            ) {
                $archiveAttemptArgs.AllowMissing = $true
            }
            # Invoke on every terminal retry.  The helper proves source/archive
            # exclusivity and the sealed manifest even when the source was
            # already moved by a previous interrupted cleanup.
            $null = Archive-DawnstrikeTerminalRollbackAttemptBackup @archiveAttemptArgs
            if ($null -ne $compensationLock) { Exit-DawnstrikeGovernedRuntimeLock $compensationLock }
            Clear-DawnstrikeCompensatedJournalTombstone -StateRoot $state -JournalPath $operationJournalPath `
                -Operation runtime_rollback -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -OriginIdentity $compensationOriginIdentity -PythonPath $approvedJournalInterpreter.path -PythonSha256 $approvedJournalInterpreter.sha256
            return Invoke-DawnstrikeRuntimeRollback @PSBoundParameters
        }
    }
    if (-not (Test-Path -LiteralPath $rollbackBundle -PathType Leaf)) {
        throw "Integrity-sealed rollback bundle is missing."
    }
    if ((Get-DawnstrikeSha256File $rollbackBundle) -ne [string]$activation.rollback_bundle_sha256) {
        throw "Rollback bundle hash does not match the activation receipt."
    }
    $null = Invoke-DawnstrikeActivationProcess $gitPath @("bundle", "verify", $rollbackBundle) $contract "Rollback bundle verification" $ProcessTimeoutSeconds

    $currentContract = $null
    if (Test-Path -LiteralPath $runtime -PathType Container) {
        $currentContract = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds
        if ($currentContract.head -notin @($candidateSha, $previousSha)) {
            throw "Active runtime is neither the activated candidate nor the recorded previous SHA."
        }
    }
    if ($null -ne $currentContract -and $currentContract.head -eq $previousSha) {
        if ($currentContract.tree -ne $previousTree) {
            throw "Restored runtime tree does not match the activation receipt."
        }
    }
    elseif (-not (Test-Path -LiteralPath $rollbackCheckout -PathType Container)) {
        throw "Previous runtime checkout is missing; rollback cannot recover its approved origin."
    }

    $origin = ""
    if (Test-Path -LiteralPath $rollbackCheckout -PathType Container) {
        $previous = Get-DawnstrikeGitContract $gitPath $rollbackCheckout $ProcessTimeoutSeconds $previousSha
        if ($previous.tree -ne $previousTree) {
            throw "Previous runtime checkout tree does not match the activation receipt."
        }
        $origin = Get-DawnstrikeGitValue $gitPath $rollbackCheckout @("remote", "get-url", "origin") "Rollback origin verification" $ProcessTimeoutSeconds
    }
    elseif ($null -ne $currentContract -and $currentContract.head -eq $previousSha) {
        $origin = Get-DawnstrikeGitValue $gitPath $runtime @("remote", "get-url", "origin") "Restored origin verification" $ProcessTimeoutSeconds
    }
    Assert-DawnstrikeSafeOrigin $origin
    if ((Get-DawnstrikeSha256Text $origin) -ne [string]$activation.runtime_origin_sha256) {
        throw "Rollback origin does not match the activation receipt."
    }

    # The activation backup is the immutable pre-activation (rollback target)
    # task inventory.  Load it before classifying any live mixed state so a
    # protected retry is compared to sealed bytes, never merely to counts.
    $activationTaskBackupManifest = Get-DawnstrikeTaskXmlBackupManifest `
        -StateRoot $state `
        -BackupName ([string]$activation.scheduler_backup_name) `
        -ExpectedManifestSha256 ([string]$activation.scheduler_backup_manifest_sha256)
    if (
        [string]$activationTaskBackupManifest.task_definition_contract_sha256 -cne
            [string]$protectedRollbackAuthorization.material.canonical_task_definition_contract_sha256 -or
        [string]$activationTaskBackupManifest.task_action_contract_sha256 -cne
            [string]$protectedRollbackAuthorization.material.canonical_task_action_contract_sha256
    ) {
        throw 'Activation scheduler backup is not the exact protected predecessor task contract.'
    }
    $taskBefore = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
    # Always inventory the auxiliary.  A task present during rollback without
    # an explicit governed sidecar declaration is an ungoverned task and must
    # fail closed rather than being silently carried through a legacy path.
    $auxiliaryBefore = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
    if (-not $stateDeclaration.required -and $auxiliaryBefore.present) {
        throw "Auxiliary capture task is present but the activation candidate did not declare its governed sidecar contract."
    }
    if ($stateDeclaration.required -and $activation.PSObject.Properties.Name -contains "auxiliary_capture_present") {
        if ([bool]$activation.auxiliary_capture_present -ne [bool]$auxiliaryBefore.present) {
            throw "Rollback auxiliary capture presence does not match the activation receipt."
        }
        if ($auxiliaryBefore.present) {
            if ($auxiliaryBefore.state -eq "Disabled") {
                if (
                    $auxiliaryBefore.definition_contract_sha256 -ne [string]$activation.auxiliary_capture_definition_contract_sha256 -or
                    $auxiliaryBefore.action_contract_sha256 -ne [string]$activation.auxiliary_capture_action_contract_sha256
                ) { throw "Rollback auxiliary capture task is not the exact disabled activation task." }
            }
            elseif ($auxiliaryBefore.state -eq "Ready") {
                $captureReceiptPath = Join-Path $state ("receipts\capture-task\capture-task-rebind-" + $candidateSha + ".json")
                Assert-DawnstrikeNoReparseComponents $captureReceiptPath "Capture-task complete receipt"
                if (Test-Path -LiteralPath $captureReceiptPath -PathType Leaf) {
                    $null = Assert-DawnstrikeCaptureRebindChain `
                        -ActivationReceipt $activation -Auxiliary $auxiliaryBefore `
                        -CandidateRoot $contract -StateRoot $state -CandidateSha $candidateSha `
                        -CandidateTree $activation.candidate_tree -PythonPath $pythonPath `
                        -TimeoutSeconds $ProcessTimeoutSeconds
                }
                else {
                    $null = Assert-DawnstrikeCapturePreparedRecovery `
                        -Activation $activation -Auxiliary $auxiliaryBefore `
                        -StateRoot $state -CandidateSha $candidateSha `
                        -CandidateTree $activation.candidate_tree -PythonPath $pythonPath
                }
            }
            else { throw "Rollback auxiliary capture task is in an ambiguous state." }
        }
    }
    $readyHint = $null
    if (Test-Path -LiteralPath $rollbackReadyReceipt -PathType Leaf) {
        $readyHint = Invoke-DawnstrikeContractCli $pythonPath $contract `
            @("verify-receipt", "--receipt", $rollbackReadyReceipt, "--expected-status", "PREPARED") `
            "Rollback ready receipt preflight verification" $ProcessTimeoutSeconds
        if (
            [string]$readyHint.activation_id -ne $activationId -or
            [string]$readyHint.candidate_sha -ne $candidateSha -or
            [string]$readyHint.candidate_tree -ne [string]$activation.candidate_tree -or
            [string]$readyHint.previous_sha -ne $previousSha -or
            [string]$readyHint.previous_tree -ne $previousTree -or
            [string]$readyHint.activation_market_date -cne $activationMarketDate -or
            [string]$readyHint.rollback_target_market_date -cne $rollbackTargetMarketDate
        ) { throw "Rollback ready receipt is not bound to the exact activation." }
    }
    $entryRollbackPhase = if ($null -eq $entryRollbackJournal) {
        ''
    }
    else { [string]$entryRollbackJournal.payload.phase }
    $expectedLiveAuthorization = if ($entryRollbackPhase -in @('POST_SWAP', 'POST_SWAP_READY')) {
        $protectedRollbackAuthorization
    }
    else { $protectedCurrentAuthorization }
    if (
        [string]$taskBefore.task_definition_contract_sha256 -cne
            [string]$expectedLiveAuthorization.material.canonical_task_definition_contract_sha256 -or
        [string]$taskBefore.task_action_contract_sha256 -cne
            [string]$expectedLiveAuthorization.material.canonical_task_action_contract_sha256
    ) {
        throw 'Live canonical tasks differ from the protected runtime authorization for this rollback phase.'
    }
    $protectedRecoveryEnablePrefix = $false
    $protectedRecoveryDisablePrefix = $false
    $protectedRecoveryAllReady = $false
    if ($entryRollbackPhase -ceq 'POST_SWAP_READY') {
        if ($null -eq $readyHint) {
            throw 'POST_SWAP_READY rollback recovery has no sealed ready receipt.'
        }
        $readyBackupName = [string]$readyHint.scheduler_backup_name
        if ($readyBackupName -cnotin @(
            $rollbackSchedulerBackupName,
            [string]$activation.scheduler_backup_name
        )) {
            throw 'POST_SWAP_READY rollback recovery names a foreign scheduler backup.'
        }
        $null = Get-DawnstrikeTaskXmlBackupManifest `
            -StateRoot $state -BackupName $readyBackupName `
            -ExpectedManifestSha256 ([string]$readyHint.scheduler_backup_manifest_sha256)
        if (
            [string]$entryRollbackJournal.payload.prepared_receipt_relative_path -cne $journalPreparedRelativePath -or
            [string]$entryRollbackJournal.payload.prepared_receipt_sha256 -cne (Get-DawnstrikeSha256File $receiptPath) -or
            [string]$entryRollbackJournal.payload.complete_receipt_relative_path -cne $journalReadyRelativePath -or
            [string]$entryRollbackJournal.payload.complete_receipt_sha256 -cne (Get-DawnstrikeSha256File $rollbackReadyReceipt) -or
            [string]$readyHint.scheduler_backup_manifest_sha256 -cne [string]$entryRollbackJournal.payload.backup_contract_sha256 -or
            [string]$readyHint.task_definition_contract_sha256 -cne [string]$activationTaskBackupManifest.task_definition_contract_sha256 -or
            [string]$readyHint.task_action_contract_sha256 -cne [string]$activationTaskBackupManifest.task_action_contract_sha256
        ) {
            throw 'POST_SWAP_READY rollback recovery evidence is not the exact sealed target inventory.'
        }
        $protectedPrefixProof = Assert-DawnstrikeProtectedRollbackEnablePrefix `
            -RuntimeRoot $runtime -StateRoot $state `
            -ExpectedTaskDefinitionContractSha256 ([string]$readyHint.task_definition_contract_sha256) `
            -ExpectedTaskActionContractSha256 ([string]$readyHint.task_action_contract_sha256)
        $protectedRecoveryEnablePrefix = $true
        $protectedRecoveryAllReady = (
            [int]$protectedPrefixProof.ready_count -eq $script:DawnstrikeCanonicalTaskNames.Count -and
            [int]$protectedPrefixProof.disabled_count -eq 0 -and
            [string]$protectedPrefixProof.task_contract.task_contract_sha256 -ceq
                [string]$activationTaskBackupManifest.task_contract_sha256
        )
        if ($protectedRecoveryAllReady) {
            $null = Get-DawnstrikeRollbackTerminalAuxiliaryRecovery `
                -Activation $activation -Receipt $readyHint `
                -RuntimeRoot $runtime -StateRoot $state
        }
    }
    elseif ($entryRollbackPhase -ceq 'POST_SWAP') {
        if (
            [string]$taskBefore.task_definition_contract_sha256 -cne [string]$activationTaskBackupManifest.task_definition_contract_sha256 -or
            [string]$taskBefore.task_action_contract_sha256 -cne [string]$activationTaskBackupManifest.task_action_contract_sha256 -or
            [int]$taskBefore.enabled_count -ne 0 -or [int]$taskBefore.disabled_count -ne 5
        ) {
            throw 'POST_SWAP rollback recovery is not the exact sealed Disabled target inventory.'
        }
    }
    elseif (
        [string]$taskBefore.task_definition_contract_sha256 -cne [string]$activation.task_definition_contract_sha256 -or
        [string]$taskBefore.task_action_contract_sha256 -cne [string]$activation.task_action_contract_sha256
    ) {
        throw 'Canonical tasks do not match the sealed activation inventory before rollback.'
    }
    $tasksInitiallyEnabled = $taskBefore.enabled_count -eq 5 -and $taskBefore.disabled_count -eq 0
    $tasksInitiallyDisabled = $taskBefore.disabled_count -eq 5 -and $taskBefore.enabled_count -eq 0
    if ($entryRollbackPhase -cin @('INIT','PRE_SWAP')) {
        if (-not $tasksInitiallyEnabled -and -not $tasksInitiallyDisabled) {
            $null = Assert-DawnstrikeProtectedRollbackDisablePrefix `
                -RuntimeRoot $runtime -StateRoot $state `
                -ExpectedTaskDefinitionContractSha256 ([string]$activation.task_definition_contract_sha256) `
                -ExpectedTaskActionContractSha256 ([string]$activation.task_action_contract_sha256)
        }
        # All-Ready and all-Disabled are respectively the zero- and full-length
        # members of the same protected disable prefix.
        $protectedRecoveryDisablePrefix = $true
    }
    if (-not $tasksInitiallyEnabled -and -not $tasksInitiallyDisabled -and
        -not $protectedRecoveryEnablePrefix -and -not $protectedRecoveryDisablePrefix) {
        throw "Canonical tasks have a mixed or ambiguous state before rollback."
    }
    if (
        $tasksInitiallyEnabled -and
        -not $protectedRecoveryEnablePrefix -and
        $taskBefore.task_contract_sha256 -ne [string]$activation.task_contract_sha256
    ) {
        throw "Enabled task XML does not match the activation receipt."
    }
    if ($tasksInitiallyDisabled -and
        -not $protectedRecoveryEnablePrefix -and -not $protectedRecoveryDisablePrefix -and
        $entryRollbackPhase -cnotin @('POST_SWAP')) {
        $activationTaskBackup = Join-Path $state ("scheduler-backups\" + [string]$activation.scheduler_backup_name + "\manifest.json")
        if (
            -not (Test-Path -LiteralPath $activationTaskBackup -PathType Leaf) -or
            (Get-DawnstrikeSha256File $activationTaskBackup) -ne
                [string]$activation.scheduler_backup_manifest_sha256
        ) {
            throw "Disabled-task crash recovery lacks its exact activation XML backup."
        }
        $null = Assert-DawnstrikeTaskXmlBackup `
            -StateRoot $state `
            -BackupName ([string]$activation.scheduler_backup_name) `
            -ExpectedManifestSha256 ([string]$activation.scheduler_backup_manifest_sha256) `
            -ExpectedTaskContractSha256 ([string]$activation.task_contract_sha256) `
            -ExpectedTaskDefinitionContractSha256 ([string]$activation.task_definition_contract_sha256) `
            -ExpectedTaskActionContractSha256 ([string]$activation.task_action_contract_sha256)
    }
    elseif ([string]::IsNullOrWhiteSpace($entryRollbackPhase) -and
        (Test-Path -LiteralPath $rollbackSchedulerBackupPath)) {
        throw "Rollback scheduler backup already exists and requires review."
    }
    # The activation backup is also the only compatible action source when the
    # previous runtime is a legacy checkout without guarded runner parameters.
    $dbPath = Join-Path $state "shadow_real.sqlite"
    $stateInfo = Invoke-DawnstrikeContractCli $pythonPath $contract @("inspect-state", "--db-path", $dbPath) "Rollback state validation" $ProcessTimeoutSeconds
    if ([int]$stateInfo.schema_version -ne [int]$activation.state_schema_version) {
        throw "Current durable state schema is incompatible with the previous runtime."
    }

    $activationLock = $null
    $dailyLock = $null
    $candidateMoved = $false
    $previousInstalled = $false
    $tasksDisabled = $tasksInitiallyDisabled
    $auxiliaryDisabled = $false
    $taskBackup = $null
    $preserveLocks = $false
    $expiredNoRunSafeStop = $false
    $journalPhase = "INIT"
    $operationJournal = $null
    $journalTaskContractSha256 = [string]$taskBefore.task_contract_sha256
    $stageContractSha256 = Get-DawnstrikeSha256Text ("dawnstrike-runtime-rollback-stage.v1|" + $activationId + "|" + $candidateSha + "|" + [string]$activation.candidate_tree + "|" + $previousSha + "|" + $previousTree + "|" + $rollbackStage)
    try {
        $lockOrigin = Convert-DawnstrikeCanonicalOriginIdentity $origin
        $lockInterpreter = Get-DawnstrikeApprovedLockInterpreter
        $enterJournalArgs = @{
            StateRoot = $state
            JournalPath = $operationJournalPath
            Operation = "runtime_rollback"
            CandidateSha = $candidateSha
            CandidateTree = [string]$activation.candidate_tree
            CurrentSha = $candidateSha
            CurrentTree = [string]$activation.candidate_tree
            PreviousSha = $previousSha
            PreviousTree = $previousTree
            OriginIdentity = $lockOrigin
            PreparedReceiptRelativePath = $journalPreparedRelativePath
            CompleteReceiptRelativePath = $journalCompleteRelativePath
            TaskContractSha256 = $journalTaskContractSha256
            PythonPath = $lockInterpreter.path
            PythonSha256 = $lockInterpreter.sha256
            RollbackTargetMarketDate = $rollbackTargetMarketDate
            ProcessTimeoutSeconds = $ProcessTimeoutSeconds
        }
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -in @("after_init", "after_lock")) {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            $enterJournalArgs.TestCrashPoint = $env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT
        }
        $operationLockPath = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
        $hasJournal = Test-Path -LiteralPath $operationJournalPath -PathType Leaf
        $hasOperationLock = Test-Path -LiteralPath $operationLockPath -PathType Leaf
        if ($hasJournal) {
            $preexistingJournal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
            if (
                [string]$preexistingJournal.payload.operation -ne "runtime_rollback" -or
                [string]$preexistingJournal.payload.candidate_sha -ne $candidateSha -or
                [string]$preexistingJournal.payload.candidate_tree -ne [string]$activation.candidate_tree -or
                [string]$preexistingJournal.payload.previous_sha -ne $previousSha -or
                [string]$preexistingJournal.payload.previous_tree -ne $previousTree -or
                [string]$preexistingJournal.payload.origin_identity -ne $lockOrigin -or
                [string]$preexistingJournal.payload.rollback_target_market_date -cne $rollbackTargetMarketDate
            ) { throw "Existing rollback journal source identity is invalid." }
            $journalTaskContractSha256 = [string]$preexistingJournal.payload.task_contract_sha256
            $enterJournalArgs.TaskContractSha256 = $journalTaskContractSha256
        }
        $rollbackBoundaryMode = if ($hasJournal) {
            Resolve-DawnstrikeProtectedRollbackBoundaryMode `
                -RollbackTargetMarketDate $rollbackTargetMarketDate `
                -RequiredCompletedMarketDate $requiredCompletedMarketDate `
                -AllowRecoveryEnablePrefix:$protectedRecoveryEnablePrefix `
                -AllowRecoveryDisablePrefix:$protectedRecoveryDisablePrefix `
                -TestNowUtc $TestNowUtc
        }
        else { 'PROGRESS' }
        $null = Assert-DawnstrikeRollbackPostFinalizerMutationWindow `
            -RollbackTargetMarketDate $rollbackTargetMarketDate `
            -RequiredCompletedMarketDate $requiredCompletedMarketDate `
            -BoundaryMode $rollbackBoundaryMode -ProtectedInFlight:$hasJournal `
            -AllowRecoveryEnablePrefix:$protectedRecoveryEnablePrefix `
            -AllowRecoveryDisablePrefix:$protectedRecoveryDisablePrefix `
            -TestNowUtc $TestNowUtc
        if ($hasJournal -and $hasOperationLock) {
            # A crash after INIT leaves both artifacts. Adopt the exact stale
            # lock first; Enter-DawnstrikeDailyRunLock then performs the
            # governed same-date dead-owner recovery and rejects foreign or
            # active daily locks.
            $activationLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
                -StateRoot $state -JournalPath $operationJournalPath `
                -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -OriginIdentity $lockOrigin -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
        }
        else {
            if ($hasJournal -and -not $hasOperationLock) {
                $orphan = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
                if ([string]$orphan.payload.phase -ne "INIT") { throw "Rollback journal exists without its exact runtime lock." }
            }
            Assert-DawnstrikeNoDailyLocks $state
            $activationLock = Enter-DawnstrikeGovernedRuntimeLockWithJournal @enterJournalArgs
        }
        $operationJournal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
        $journalPhase = [string]$operationJournal.payload.phase
        $preserveReadyPostSwap = (
            $journalPhase -ceq 'POST_SWAP_READY' -and
            $protectedRecoveryAllReady -and
            $rollbackBoundaryMode -cne 'EXPIRED_NO_RUN'
        )
        $liveRollbackLock = Confirm-DawnstrikeGovernedRuntimeLock $activationLock
        if ([string]$operationJournal.payload.rollback_target_market_date -cne $rollbackTargetMarketDate -or
            [string]$liveRollbackLock.payload.rollback_target_market_date -cne $rollbackTargetMarketDate) {
            throw 'Runtime rollback lock/journal target identity changed.'
        }
        if (
            [string]$operationJournal.payload.prepared_receipt_sha256 -ne (Get-DawnstrikeSha256File $receiptPath) -and
            $journalPhase -ne "INIT"
        ) { throw "Rollback journal is not bound to the exact activation receipt." }
        if (
            [string]$operationJournal.payload.runtime_stage_contract_sha256 -notin @($journalEmptySha256, $stageContractSha256)
        ) { throw "Rollback journal stage identity is invalid." }
        $dailyLock = Enter-DawnstrikeDailyRunLock `
            -StateRoot $state -MarketDate $rollbackTargetMarketDate `
            -Owner "runtime_rollback" -RetainHandle
        if (-not $dailyLock.acquired) {
            throw "Runtime rollback could not acquire the daily run lock."
        }
        Confirm-DawnstrikeActivationDailyLockHandshake `
            -StateRoot $state -ActivationLock $activationLock -DailyLock $dailyLock | Out-Null
        $null = Assert-DawnstrikeRollbackPostFinalizerMutationWindow `
            -RollbackTargetMarketDate $rollbackTargetMarketDate `
            -RequiredCompletedMarketDate $requiredCompletedMarketDate `
            -BoundaryMode $rollbackBoundaryMode -ProtectedInFlight:$hasJournal `
            -AllowRecoveryEnablePrefix:$protectedRecoveryEnablePrefix `
            -AllowRecoveryDisablePrefix:$protectedRecoveryDisablePrefix `
            -TestNowUtc $TestNowUtc
        $taskLocked = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
        if ($taskLocked.task_action_contract_sha256 -ne $taskBefore.task_action_contract_sha256) {
            throw "Task definitions changed during rollback preflight."
        }
        if ($journalPhase -ne "INIT") {
            $taskBackup = [pscustomobject]@{
                backup_name = if (Test-Path -LiteralPath $rollbackSchedulerBackupPath) { $rollbackSchedulerBackupName } else { [string]$activation.scheduler_backup_name }
                manifest_sha256 = [string]$operationJournal.payload.backup_contract_sha256
            }
        }
        elseif ($tasksInitiallyEnabled) {
            if ($taskLocked.task_contract_sha256 -ne $taskBefore.task_contract_sha256) {
                throw "Task XML changed during rollback preflight."
            }
            $taskBackup = New-DawnstrikeTaskXmlBackup `
                -StateRoot $state `
                -BackupName $rollbackSchedulerBackupName `
                -ActivationId $activationId `
                -TaskContract $taskLocked `
                -AuxiliaryCapture $auxiliaryBefore
            $tasksDisabled = $false
        }
        elseif ($tasksInitiallyDisabled) {
            $taskBackup = [pscustomobject]@{
                backup_name = [string]$activation.scheduler_backup_name
                manifest_sha256 = [string]$activation.scheduler_backup_manifest_sha256
            }
        }
        else { throw "Rollback task backup state is ambiguous." }
        $taskBackupManifest = Get-DawnstrikeTaskXmlBackupManifest `
            -StateRoot $state -BackupName $taskBackup.backup_name `
            -ExpectedManifestSha256 $taskBackup.manifest_sha256
        if ($journalPhase -eq "INIT") {
            $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                -Lock $activationLock -Operation runtime_rollback -Phase PRE_SWAP `
                -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -CurrentSha $candidateSha -CurrentTree ([string]$activation.candidate_tree) `
                -PreviousSha $previousSha -PreviousTree $previousTree -OriginIdentity $lockOrigin `
                -PreparedReceiptRelativePath $journalPreparedRelativePath `
                -PreparedReceiptSha256 (Get-DawnstrikeSha256File $receiptPath) `
                -CompleteReceiptRelativePath $journalCompleteRelativePath `
                -CompleteReceiptSha256 $journalEmptySha256 `
                -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
                -TaskContractSha256 $journalTaskContractSha256 `
                -RuntimeStageContractSha256 $stageContractSha256 `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256 | Out-Null
            $journalPhase = "PRE_SWAP"
        }
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_pre_swap") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        if (($journalPhase -in @("POST_SWAP", "POST_SWAP_READY") -or
            $protectedRecoveryDisablePrefix) -and -not $preserveReadyPostSwap) {
            $recoveryTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
            if ($recoveryTasks.enabled_count -gt 0) {
                $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
            }
            $tasksDisabled = $true
        }
        elseif ($tasksInitiallyEnabled -and -not $tasksDisabled) {
            Disable-DawnstrikeCanonicalTasks
            $tasksDisabled = $true
        }
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_task_disable") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_THROW_POINT -eq "after_task_disable") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback failure injection is test-only." }
            throw "Injected ordinary rollback failure after PRE_SWAP task disable."
        }
        $taskSwapBoundary = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
        $expectedSwapDefinitionContract = if ($journalPhase -in @('POST_SWAP','POST_SWAP_READY')) {
            [string]$activationTaskBackupManifest.task_definition_contract_sha256
        }
        else { [string]$activation.task_definition_contract_sha256 }
        $expectedSwapActionContract = if ($journalPhase -in @('POST_SWAP','POST_SWAP_READY')) {
            [string]$activationTaskBackupManifest.task_action_contract_sha256
        }
        else { [string]$activation.task_action_contract_sha256 }
        if ($preserveReadyPostSwap) {
            if (
                $taskSwapBoundary.enabled_count -ne 5 -or
                $taskSwapBoundary.disabled_count -ne 0 -or
                $taskSwapBoundary.task_contract_sha256 -ne [string]$activationTaskBackupManifest.task_contract_sha256 -or
                $taskSwapBoundary.task_definition_contract_sha256 -ne $expectedSwapDefinitionContract -or
                $taskSwapBoundary.task_action_contract_sha256 -ne $expectedSwapActionContract
            ) {
                throw 'POST_SWAP_READY no-mutation recovery lost its exact Ready task contract.'
            }
        }
        elseif (
            $taskSwapBoundary.disabled_count -ne 5 -or
            $taskSwapBoundary.enabled_count -ne 0 -or
            $taskSwapBoundary.task_definition_contract_sha256 -ne $expectedSwapDefinitionContract -or
            $taskSwapBoundary.task_action_contract_sha256 -ne $expectedSwapActionContract
        ) {
            throw "Canonical tasks did not enter the exact disabled rollback boundary."
        }
        if ($auxiliaryBefore.present -and -not $preserveReadyPostSwap) {
            $auxiliaryDisabled = $true
            $null = Disable-DawnstrikeAuxiliaryCaptureTask $runtime $state
        }
        if ($rollbackBoundaryMode -ceq 'EXPIRED_NO_RUN') {
            # Canonical StartWhenAvailable=true is part of the sealed task
            # contract.  Without a separately journaled, integration-proven
            # catch-up neutralization protocol, a late enable could launch the
            # missed target occurrence.  Stop safely at the retained-lock,
            # exact Disabled boundary and leave the nonterminal journal for a
            # governed operator recovery instead of claiming completion.
            Confirm-DawnstrikeActivationDailyLockHandshake `
                -StateRoot $state -ActivationLock $activationLock -DailyLock $dailyLock | Out-Null
            $null = Assert-DawnstrikeRollbackPostFinalizerMutationWindow `
                -RollbackTargetMarketDate $rollbackTargetMarketDate `
                -RequiredCompletedMarketDate $requiredCompletedMarketDate `
                -BoundaryMode EXPIRED_NO_RUN -ProtectedInFlight `
                -TestNowUtc $TestNowUtc
            $expiredNoRunSafeStop = $true
            $preserveLocks = $true
            throw 'Expired no-run rollback recovery is safely retained Disabled; automatic catch-up-neutralized enablement is not certified.'
        }
        $null = Assert-DawnstrikeTaskXmlBackup `
            -StateRoot $state `
            -BackupName $taskBackup.backup_name `
            -ExpectedManifestSha256 $taskBackup.manifest_sha256 `
            -ExpectedTaskContractSha256 ([string]$taskBackupManifest.task_contract_sha256) `
            -ExpectedTaskDefinitionContractSha256 ([string]$taskBackupManifest.task_definition_contract_sha256) `
            -ExpectedTaskActionContractSha256 ([string]$taskBackupManifest.task_action_contract_sha256)

        if ($journalPhase -eq "POST_SWAP") {
            $previousInstalled = $true
        }
        elseif ($null -eq $currentContract) {
            if (Test-Path -LiteralPath $rollbackStage -PathType Container) {
                $staged = Get-DawnstrikeGitContract $gitPath $rollbackStage $ProcessTimeoutSeconds $previousSha
                if ($staged.tree -ne $previousTree) { throw "Rollback stage tree does not match the activation receipt." }
                [System.IO.Directory]::Move($rollbackStage, $runtime)
                $previousInstalled = $true
            }
            elseif (Test-Path -LiteralPath $rollbackCheckout -PathType Container) {
                [System.IO.Directory]::Move($rollbackCheckout, $runtime)
                $previousInstalled = $true
            }
            else { throw "Rollback journal PRE_SWAP has no previous-runtime stage or checkout." }
        }
        elseif ($currentContract.head -eq $candidateSha) {
            if (-not (Test-Path -LiteralPath $rollbackStage -PathType Container)) {
                $null = Invoke-DawnstrikeActivationProcess $gitPath @("clone", "--no-checkout", "--quiet", $rollbackBundle, $rollbackStage) (Split-Path -Parent $runtime) "Previous runtime staging" $ProcessTimeoutSeconds
                $null = Invoke-DawnstrikeActivationProcess $gitPath @("-C", $rollbackStage, "checkout", "--detach", "--quiet", $previousSha) $rollbackStage "Previous runtime checkout" $ProcessTimeoutSeconds
                $null = Invoke-DawnstrikeActivationProcess $gitPath @("-C", $rollbackStage, "remote", "set-url", "origin", $origin) $rollbackStage "Previous origin binding" $ProcessTimeoutSeconds
            }
            $staged = Get-DawnstrikeGitContract $gitPath $rollbackStage $ProcessTimeoutSeconds $previousSha
            if ($staged.tree -ne $previousTree) { throw "Rollback stage tree does not match the activation receipt." }
            if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_stage_prepare") {
                if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
                Stop-Process -Id $PID -Force
            }
            if (Test-Path -LiteralPath $deactivatedCandidate) {
                throw "Deactivated candidate preservation path already exists."
            }
            [System.IO.Directory]::Move($runtime, $deactivatedCandidate)
            $candidateMoved = $true
            if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_first_rename") {
                if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
                Stop-Process -Id $PID -Force
            }
            [System.IO.Directory]::Move($rollbackStage, $runtime)
            $previousInstalled = $true
            if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_second_rename") {
                if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
                Stop-Process -Id $PID -Force
            }
        }

        $restored = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $previousSha
        if ($restored.tree -ne $previousTree) {
            throw "Restored runtime tree does not match the activation receipt."
        }
        $restoredOrigin = Get-DawnstrikeGitValue $gitPath $runtime @("remote", "get-url", "origin") "Restored origin verification" $ProcessTimeoutSeconds
        if ((Get-DawnstrikeSha256Text $restoredOrigin) -ne [string]$activation.runtime_origin_sha256) {
            throw "Restored runtime origin does not match the activation receipt."
        }
        # The restored runtime must be disabled and rebound to the previous
        # runtime SHA before any task can be enabled.  The pre-rollback backup
        # remains candidate-bound evidence; it is intentionally distinct from
        # this final previous-SHA task contract.
        if ($preserveReadyPostSwap) {
            $taskAfterDisabled = $taskSwapBoundary
        }
        else {
            $null = Restore-DawnstrikeCanonicalTasksFromXmlBackup `
                -RuntimeRoot $runtime -StateRoot $state `
                -BackupName ([string]$activation.scheduler_backup_name) `
                -ExpectedManifestSha256 ([string]$activation.scheduler_backup_manifest_sha256) `
                -ExpectedTaskContractSha256 ([string]$activationTaskBackupManifest.task_contract_sha256) `
                -ExpectedTaskDefinitionContractSha256 ([string]$activationTaskBackupManifest.task_definition_contract_sha256) `
                -ExpectedTaskActionContractSha256 ([string]$activationTaskBackupManifest.task_action_contract_sha256)
            $taskAfterDisabled = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
            if (
                $taskAfterDisabled.disabled_count -ne 5 -or
                $taskAfterDisabled.enabled_count -ne 0
            ) {
                throw "Canonical tasks were not rebound to the exact disabled previous-SHA boundary."
            }
        }
        $null = Assert-DawnstrikeCanonicalTaskSemantics `
            -RuntimeRoot $runtime -StateRoot $state `
            -ExpectedSha $previousSha -AllowDisabled
        if (
            [string]$taskAfterDisabled.task_definition_contract_sha256 -cne
                [string]$protectedRollbackAuthorization.material.canonical_task_definition_contract_sha256 -or
            [string]$taskAfterDisabled.task_action_contract_sha256 -cne
                [string]$protectedRollbackAuthorization.material.canonical_task_action_contract_sha256
        ) {
            throw 'Restored canonical tasks differ from the protected predecessor runtime authorization.'
        }
        if ($journalPhase -eq "PRE_SWAP") {
            $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                -Lock $activationLock -Operation runtime_rollback -Phase POST_SWAP `
                -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -CurrentSha $previousSha -CurrentTree $previousTree -PreviousSha $previousSha -PreviousTree $previousTree `
                -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
                -PreparedReceiptSha256 (Get-DawnstrikeSha256File $receiptPath) `
                -CompleteReceiptRelativePath $journalCompleteRelativePath -CompleteReceiptSha256 $journalEmptySha256 `
                -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
                -TaskContractSha256 $journalTaskContractSha256 `
                -RuntimeStageContractSha256 $stageContractSha256 `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256 | Out-Null
            $journalPhase = "POST_SWAP"
        }
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_post_swap") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        if ($preserveReadyPostSwap) {
            $null = Get-DawnstrikeRollbackTerminalAuxiliaryRecovery `
                -Activation $activation -Receipt $readyHint `
                -RuntimeRoot $runtime -StateRoot $state
        }
        else {
            $auxiliaryAfterDisabled = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
            if ($auxiliaryBefore.present -and (
                $auxiliaryAfterDisabled.state -ne "Disabled" -or
                $auxiliaryAfterDisabled.definition_contract_sha256 -ne $auxiliaryBefore.definition_contract_sha256
            )) { throw "Auxiliary capture task changed across the rollback swap." }
        }
        $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
            -Receipt $activation `
            -StateRoot $state `
            -BackupRoot $safeBackupRoot `
            -ToolRoot $contract `
            -GitPath $gitPath `
            -PythonPath $pythonPath `
            -TimeoutSeconds $ProcessTimeoutSeconds
        if ($stateDeclaration.required -and $activation.PSObject.Properties.Name -contains "auxiliary_capture_present") {
            $expectedAuxiliary = Get-DawnstrikeActivationAuxiliaryRecoveryContract `
                -Activation $activation `
                -StateRoot $state
            if ($expectedAuxiliary.present -and -not $preserveReadyPostSwap) {
                Confirm-DawnstrikeActivationDailyLockHandshake `
                    -StateRoot $state -ActivationLock $activationLock -DailyLock $dailyLock | Out-Null
                $null = Assert-DawnstrikeRollbackPostFinalizerMutationWindow `
                    -RollbackTargetMarketDate $rollbackTargetMarketDate `
                    -RequiredCompletedMarketDate $requiredCompletedMarketDate `
                    -BoundaryMode $rollbackBoundaryMode -ProtectedInFlight:$hasJournal `
                    -TestNowUtc $TestNowUtc
                $null = Restore-DawnstrikeAuxiliaryCaptureTask `
                    -Expected $expectedAuxiliary `
                    -RuntimeRoot $runtime `
                    -StateRoot $state `
                    -RunAsCredential $RunAsCredential
            }
            $auxiliaryDisabled = $false
        }
        $taskAfter = $taskAfterDisabled
        $payload = $null
        if ($journalPhase -eq "POST_SWAP_READY") {
            if (-not (Test-Path -LiteralPath $rollbackReadyReceipt -PathType Leaf)) {
                throw "POST_SWAP_READY rollback journal has no exact ready receipt."
            }
            $sealedRollback = Invoke-DawnstrikeContractCli $pythonPath $contract `
                @("verify-receipt", "--receipt", $rollbackReadyReceipt, "--expected-status", "PREPARED") `
                "Rollback ready receipt verification" $ProcessTimeoutSeconds
            $payload = [ordered]@{}
            foreach ($property in $sealedRollback.PSObject.Properties) {
                if ($property.Name -ne "receipt_sha256") { $payload[$property.Name] = $property.Value }
            }
        }
        if ($null -eq $existingRollbackReceipt -and $journalPhase -ne "POST_SWAP_READY") {
        $payload = [ordered]@{
            schema_version = "dawnstrike.runtime_rollback_receipt.v2"
            status = "PREPARED"
            activation_id = $activationId
            activation_market_date = $activationMarketDate
            rollback_target_market_date = $rollbackTargetMarketDate
            candidate_sha = $candidateSha
            candidate_tree = [string]$activation.candidate_tree
            previous_sha = $previousSha
            previous_tree = $previousTree
            restored_sha = $previousSha
            ci_evidence_sha256 = [string]$activation.ci_evidence_sha256
            sol_evidence_sha256 = [string]$activation.sol_evidence_sha256
            state_backup_id = [string]$activation.state_backup_id
            state_backup_db_sha256 = [string]$activation.state_backup_db_sha256
            state_schema_version = [int]$stateInfo.schema_version
            state_quick_check = [string]$stateInfo.quick_check
            rollback_bundle_sha256 = [string]$activation.rollback_bundle_sha256
            task_count = [int]$taskAfter.task_count
            task_contract_sha256 = [string]$taskAfter.task_contract_sha256
            task_definition_contract_sha256 = [string]$taskAfter.task_definition_contract_sha256
            task_action_contract_sha256 = [string]$taskAfter.task_action_contract_sha256
            task_paths_unchanged = $true
            task_enablement_restored = $false
            scheduler_backup_name = [string]$taskBackup.backup_name
            scheduler_backup_manifest_sha256 = [string]$taskBackup.manifest_sha256
            runtime_origin_sha256 = [string]$activation.runtime_origin_sha256
            swap_contract = "same_volume_two_rename_with_immediate_restore"
            prepared_at_utc = [string]$activation.prepared_at_utc
            completed_at_utc = $null
            research_only = $true
            broker_execution_enabled = $false
        }
    if ($stateDeclaration.required -and $activation.PSObject.Properties.Name -contains "auxiliary_capture_present") {
            $payload.previous_runtime_rollback_authorized = [bool]$activation.previous_runtime_rollback_authorized
            $payload.previous_runtime_disposition = [string]$activation.previous_runtime_disposition
            $payload.previous_runtime_authorization_receipt_sha256 = [string]$activation.previous_runtime_authorization_receipt_sha256
            $payload.previous_runtime_authorization_journal_sha256 = [string]$activation.previous_runtime_authorization_journal_sha256
            $payload.state_preparation_required = $true
            $payload.state_preparation_contract = [string]$activation.state_preparation_contract
            $payload.state_preparation_receipt_sha256 = [string]$activation.state_preparation_receipt_sha256
            $payload.state_preparation_after_db_sha256 = [string]$activation.state_preparation_after_db_sha256
            $payload.state_preparation_after_wal_sha256 = [string]$activation.state_preparation_after_wal_sha256
            $payload.state_preparation_after_shm_sha256 = [string]$activation.state_preparation_after_shm_sha256
            $payload.state_preparation_after_logical_snapshot_sha256 = [string]$activation.state_preparation_after_logical_snapshot_sha256
            $payload.state_preparation_inventory_sha256 = [string]$activation.state_preparation_inventory_sha256
            $payload.state_preparation_backup_id = [string]$activation.state_preparation_backup_id
            $payload.state_preparation_backup_bundle_path = [string]$activation.state_preparation_backup_bundle_path
            $payload.state_preparation_backup_db_sha256 = [string]$activation.state_preparation_backup_db_sha256
            $payload.state_preparation_backup_manifest_sha256 = [string]$activation.state_preparation_backup_manifest_sha256
            $payload.state_preparation_backup_manifest_file_sha256 = [string]$activation.state_preparation_backup_manifest_file_sha256
            $payload.state_backup_bundle_path = [string]$activation.state_backup_bundle_path
            $payload.state_backup_manifest_sha256 = [string]$activation.state_backup_manifest_sha256
            $payload.state_backup_logical_snapshot_sha256 = [string]$activation.state_backup_logical_snapshot_sha256
            $payload.state_backup_source_logical_snapshot_sha256 = [string]$activation.state_backup_source_logical_snapshot_sha256
            $payload.auxiliary_capture_present = [bool]$activation.auxiliary_capture_present
            $payload.auxiliary_capture_state_before = "Disabled"
            $payload.auxiliary_capture_state_after = [string]$activation.auxiliary_capture_state_before
            $payload.auxiliary_capture_action = "RESTORED_EXACT"
            $payload.auxiliary_capture_xml_sha256 = [string]$activation.auxiliary_capture_xml_sha256
            $payload.auxiliary_capture_xml_file_sha256 = [string]$activation.auxiliary_capture_xml_file_sha256
            $payload.auxiliary_capture_definition_contract_sha256 = [string]$activation.auxiliary_capture_definition_contract_sha256
            $payload.auxiliary_capture_action_contract_sha256 = [string]$activation.auxiliary_capture_action_contract_sha256
            $payload.auxiliary_capture_backup_name = [string]$activation.auxiliary_capture_backup_name
            $payload.auxiliary_capture_backup_manifest_sha256 = [string]$activation.auxiliary_capture_backup_manifest_sha256
            $payload.capture_hardening_receipt_relative_path = [string]$activation.capture_hardening_receipt_relative_path
            $payload.capture_hardening_receipt_raw_sha256 = [string]$activation.capture_hardening_receipt_raw_sha256
            $payload.capture_hardening_receipt_sha256 = [string]$activation.capture_hardening_receipt_sha256
            $payload.capture_hardening_xml_sha256 = [string]$activation.capture_hardening_xml_sha256
            $payload.capture_hardening_action_sha256 = [string]$activation.capture_hardening_action_sha256
            $payload.capture_hardening_principal_sha256 = [string]$activation.capture_hardening_principal_sha256
            $payload.capture_hardening_trigger_sha256 = [string]$activation.capture_hardening_trigger_sha256
            $payload.capture_hardening_settings_sha256 = [string]$activation.capture_hardening_settings_sha256
            $payload.capture_hardening_runner_before_sha256 = [string]$activation.capture_hardening_runner_before_sha256
            $payload.capture_hardening_runner_target_sha256 = [string]$activation.capture_hardening_runner_target_sha256
        }
        $input = Join-Path $rollbackReceiptRoot ".$activationId.input.json"
        Write-DawnstrikeActivationJson $payload $input
        try {
            $sealedRollback = Invoke-DawnstrikeContractCli $pythonPath $contract @("seal-receipt", "--input", $input, "--output", $rollbackReadyReceipt) "Rollback ready receipt sealing" $ProcessTimeoutSeconds
            if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_receipt") {
                if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
                Stop-Process -Id $PID -Force
            }
            # The temporary seal input is no longer recovery evidence.  Remove
            # it before the terminal journal transition so terminal cleanup
            # cannot turn a committed rollback into compensation.
            if (Test-Path -LiteralPath $input -PathType Leaf) {
                Remove-Item -LiteralPath $input -Force
            }
            $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                -Lock $activationLock -Operation runtime_rollback -Phase POST_SWAP_READY `
                -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -CurrentSha $previousSha -CurrentTree $previousTree -PreviousSha $previousSha -PreviousTree $previousTree `
                -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
                -PreparedReceiptSha256 (Get-DawnstrikeSha256File $receiptPath) `
                -CompleteReceiptRelativePath $journalReadyRelativePath -CompleteReceiptSha256 (Get-DawnstrikeSha256File $rollbackReadyReceipt) `
                -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
                -TaskContractSha256 $journalTaskContractSha256 `
                -RuntimeStageContractSha256 $stageContractSha256 `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256 | Out-Null
            $journalPhase = "POST_SWAP_READY"
        }
        finally {
            if (Test-Path -LiteralPath $input -PathType Leaf) {
                Remove-Item -LiteralPath $input -Force -ErrorAction SilentlyContinue
            }
        }
        }
        else {
            $sealedRollback = $existingRollbackReceipt
        }
        if ($journalPhase -eq "POST_SWAP") {
            $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                -Lock $activationLock -Operation runtime_rollback -Phase POST_SWAP_READY `
                -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -CurrentSha $previousSha -CurrentTree $previousTree -PreviousSha $previousSha -PreviousTree $previousTree `
                -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
                -PreparedReceiptSha256 (Get-DawnstrikeSha256File $receiptPath) `
                -CompleteReceiptRelativePath $journalReadyRelativePath -CompleteReceiptSha256 (Get-DawnstrikeSha256File $rollbackReadyReceipt) `
                -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
                -TaskContractSha256 $journalTaskContractSha256 `
                -RuntimeStageContractSha256 $stageContractSha256 `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256 | Out-Null
            $journalPhase = "POST_SWAP_READY"
        }
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_ready") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        $rollbackEnableBoundary = {
            Confirm-DawnstrikeActivationDailyLockHandshake `
                -StateRoot $state -ActivationLock $activationLock -DailyLock $dailyLock | Out-Null
            & $assertProtectedActivationLineage
            $null = Assert-DawnstrikeRollbackPostFinalizerMutationWindow `
                -RollbackTargetMarketDate $rollbackTargetMarketDate `
                -RequiredCompletedMarketDate $requiredCompletedMarketDate `
                -BoundaryMode $rollbackBoundaryMode -ProtectedInFlight:$hasJournal `
                -AllowRecoveryEnablePrefix `
                -TestNowUtc $TestNowUtc
            $null = Assert-DawnstrikeCanonicalTaskSemantics `
                -RuntimeRoot $runtime -StateRoot $state `
                -ExpectedSha $previousSha -AllowDisabled
            $authorizedTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
            if (
                [string]$authorizedTasks.task_definition_contract_sha256 -cne
                    [string]$protectedRollbackAuthorization.material.canonical_task_definition_contract_sha256 -or
                [string]$authorizedTasks.task_action_contract_sha256 -cne
                    [string]$protectedRollbackAuthorization.material.canonical_task_action_contract_sha256
            ) {
                throw 'Rollback enable boundary lost the protected predecessor task contract.'
            }
        }.GetNewClosure()
        if (-not $preserveReadyPostSwap) {
            Enable-DawnstrikeCanonicalTasks -BeforeEachEnable $rollbackEnableBoundary
        }
        & $rollbackEnableBoundary
        $taskAfter = Get-DawnstrikeTaskContract $runtime $state
        $null = Assert-DawnstrikeCanonicalTaskSemantics `
            -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $previousSha
        if (
            $taskAfter.enabled_count -ne 5 -or
            $taskAfter.disabled_count -ne 0 -or
            $taskAfter.task_contract_sha256 -ne [string]$activationTaskBackupManifest.task_contract_sha256 -or
            $taskAfter.task_definition_contract_sha256 -ne [string]$activationTaskBackupManifest.task_definition_contract_sha256 -or
            $taskAfter.task_action_contract_sha256 -ne [string]$activationTaskBackupManifest.task_action_contract_sha256
        ) {
            throw "Task XML was not restored exactly to the previous-SHA Ready boundary."
        }
        if (
            [string]$taskAfter.task_definition_contract_sha256 -cne
                [string]$protectedRollbackAuthorization.material.canonical_task_definition_contract_sha256 -or
            [string]$taskAfter.task_action_contract_sha256 -cne
                [string]$protectedRollbackAuthorization.material.canonical_task_action_contract_sha256
        ) {
            throw 'Rollback terminal tasks differ from the protected predecessor runtime authorization.'
        }
        $tasksDisabled = $false
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_enable") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        $payload.schema_version = "dawnstrike.runtime_rollback_receipt.v1"
        $payload.status = "ROLLED_BACK"
        $payload.task_count = [int]$taskAfter.task_count
        $payload.task_contract_sha256 = [string]$taskAfter.task_contract_sha256
        $payload.task_definition_contract_sha256 = [string]$taskAfter.task_definition_contract_sha256
        $payload.task_action_contract_sha256 = [string]$taskAfter.task_action_contract_sha256
        $payload.task_enablement_restored = $true
        $payload.completed_at_utc = if ($null -ne $existingRollbackReceipt) {
            [string]$existingRollbackReceipt.completed_at_utc
        }
        else { [DateTime]::UtcNow.ToString("o") }
        $input = Join-Path $rollbackReceiptRoot ".$activationId.complete.input.json"
        if ($null -ne $existingRollbackReceipt) {
            # Power loss can occur after the immutable terminal receipt was
            # linked but before POST_SWAP_READY -> COMPLETE.  Accept only the
            # exact field-for-field terminal derivation of the journal-bound
            # ready receipt and current Ready task proof; never overwrite or
            # recompute the already sealed completion time.
            $existingNames = @($existingRollbackReceipt.PSObject.Properties.Name)
            if ($existingNames.Count -ne ($payload.Keys.Count + 1) -or
                'receipt_sha256' -cnotin $existingNames) {
                throw 'Existing rollback terminal receipt fields are not exact for POST_SWAP_READY recovery.'
            }
            foreach ($field in @($payload.Keys)) {
                if ($field -cnotin $existingNames -or
                    [string]$existingRollbackReceipt.$field -cne [string]$payload[$field]) {
                    throw "Existing rollback terminal receipt is not the exact ready-receipt derivation: $field"
                }
            }
            $sealedRollback = $existingRollbackReceipt
        }
        else {
            Write-DawnstrikeActivationJson $payload $input
            try {
                $sealedRollback = Invoke-DawnstrikeContractCli $pythonPath $contract `
                    @("seal-receipt", "--input", $input, "--output", $rollbackReceipt) `
                    "Rollback terminal receipt sealing" $ProcessTimeoutSeconds
                if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq 'after_complete_receipt') {
                    if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne '1') { throw 'Rollback crash injection is test-only.' }
                    Stop-Process -Id $PID -Force
                }
            }
            finally {
                if (Test-Path -LiteralPath $input -PathType Leaf) {
                    Remove-Item -LiteralPath $input -Force -ErrorAction SilentlyContinue
                }
            }
        }
        $completeReceiptHash = Get-DawnstrikeSha256File $rollbackReceipt
        $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
            -Lock $activationLock -Operation runtime_rollback -Phase COMPLETE `
            -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
            -CurrentSha $previousSha -CurrentTree $previousTree -PreviousSha $previousSha -PreviousTree $previousTree `
            -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
            -PreparedReceiptSha256 (Get-DawnstrikeSha256File $receiptPath) `
            -CompleteReceiptRelativePath $journalCompleteRelativePath -CompleteReceiptSha256 $completeReceiptHash `
            -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
            -TaskContractSha256 $journalTaskContractSha256 `
            -RuntimeStageContractSha256 $stageContractSha256 `
            -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256 | Out-Null
        $journalPhase = "COMPLETE"
        Confirm-DawnstrikeActivationDailyLockHandshake `
            -StateRoot $state -ActivationLock $activationLock -DailyLock $dailyLock | Out-Null
        $null = Assert-DawnstrikeRollbackPostFinalizerMutationWindow `
            -RollbackTargetMarketDate $rollbackTargetMarketDate `
            -RequiredCompletedMarketDate $requiredCompletedMarketDate `
            -BoundaryMode $rollbackBoundaryMode -ProtectedInFlight:$hasJournal `
            -AllowRecoveryEnablePrefix `
            -TestNowUtc $TestNowUtc
        $null = Assert-DawnstrikeRollbackCompleteTerminal `
            -Journal $operationJournal -Activation $activation -ReceiptPath $rollbackReceipt `
            -CandidateRoot $contract -RuntimeRoot $runtime -StateRoot $state `
            -BackupRoot $safeBackupRoot -GitPath $gitPath -PythonPath $pythonPath `
            -TimeoutSeconds $ProcessTimeoutSeconds -CandidateSha $candidateSha `
            -CandidateTree ([string]$activation.candidate_tree) -PreviousSha $previousSha `
            -PreviousTree $previousTree -OriginIdentity $lockOrigin `
            -ActivationMarketDate $activationMarketDate `
            -RollbackTargetMarketDate $rollbackTargetMarketDate `
            -StateDeclaration $stateDeclaration
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_complete") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        return & $getTrustedRollbackTerminalEnvelope $lockOrigin
    }
    catch {
        $failure = $_
        if ($expiredNoRunSafeStop) {
            $preserveLocks = $true
            throw $failure
        }
        # COMPLETE is an irreversible commit.  A cleanup/output fault can be
        # raised after the journal file was durably replaced, so reconcile the
        # exact terminal receipt before considering any compensation.  The
        # stale local POST_SWAP value must never authorize rollback of a
        # committed operation.
        $terminalJournal = $null
        try {
            $terminalJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
                $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
            $journalPhase = [string]$terminalJournal.payload.phase
        }
        catch {
            $preserveLocks = $true
            throw "Runtime rollback journal phase could not be reconciled; operator recovery is required."
        }
        if ($journalPhase -eq "COMPLETE") {
            try {
                $terminalRollbackReceipt = Assert-DawnstrikeRollbackCompleteTerminal `
                    -Journal $terminalJournal -Activation $activation -ReceiptPath $rollbackReceipt `
                    -CandidateRoot $contract -RuntimeRoot $runtime -StateRoot $state `
                    -BackupRoot $safeBackupRoot -GitPath $gitPath -PythonPath $pythonPath `
                    -TimeoutSeconds $ProcessTimeoutSeconds -CandidateSha $candidateSha `
                    -CandidateTree ([string]$activation.candidate_tree) -PreviousSha $previousSha `
                    -PreviousTree $previousTree -OriginIdentity ([string]$terminalJournal.payload.origin_identity) `
                    -ActivationMarketDate $activationMarketDate `
                    -RollbackTargetMarketDate $rollbackTargetMarketDate `
                    -StateDeclaration $stateDeclaration
                return & $getTrustedRollbackTerminalEnvelope `
                    ([string]$terminalJournal.payload.origin_identity)
            }
            catch {
                $preserveLocks = $true
                throw "Complete rollback evidence could not be reconciled; operator recovery is required."
            }
        }
        # Never mutate Task Scheduler from a failure path unless the exact
        # runtime/daily lock pair was successfully retained.  In particular,
        # a daily-lock acquisition race can mean a scheduled process is live;
        # disabling or restoring tasks in that state would cross the very
        # execution boundary the daily lock is meant to protect.  Preserve any
        # acquired runtime lock plus its INIT journal for governed adoption.
        if (
            $null -eq $activationLock -or -not [bool]$activationLock.acquired -or
            $null -eq $dailyLock -or -not [bool]$dailyLock.acquired
        ) {
            if ($null -ne $activationLock -and [bool]$activationLock.acquired) {
                $preserveLocks = $true
            }
            throw "Runtime rollback failed without its exact retained lock pair; no further task mutation is permitted. Original failure: $($failure.Exception.Message)"
        }
        try {
            Confirm-DawnstrikeActivationDailyLockHandshake `
                -StateRoot $state -ActivationLock $activationLock -DailyLock $dailyLock | Out-Null
        }
        catch {
            $preserveLocks = $true
            throw "Runtime rollback retained lock-pair proof failed; no further task mutation is permitted. Original failure: $($failure.Exception.Message)"
        }
        if ($candidateMoved -or $previousInstalled -or $tasksDisabled) {
            try {
                $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
                $tasksDisabled = $true
                if ($auxiliaryBefore.present) {
                    $null = Disable-DawnstrikeAuxiliaryCaptureTask $runtime $state
                    $auxiliaryDisabled = $true
                }
            }
            catch {
                $preserveLocks = $true
                throw "Runtime rollback failed and exact task quiescence could not be proven; runtime recovery was not attempted."
            }
        }
        try {
            $failedAttemptJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
                $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
            if ([string]$failedAttemptJournal.payload.phase -notin @("PRE_SWAP", "POST_SWAP", "POST_SWAP_READY")) {
                throw "Failed previous-runtime preservation lacks a nonterminal journal."
            }
            $failedAttemptKey = [string]$failedAttemptJournal.raw_file_sha256
            $failedPrevious = Join-Path $rollbackRoot "failed-previous-runtime-$failedAttemptKey"

            # Process-local rename flags are not recovery authority.  A retry
            # starts with them false, so derive the exact swap shape from the
            # journal-bound Git identities and restore the candidate runtime
            # before touching candidate-bound task actions.
            if (Test-Path -LiteralPath $runtime -PathType Container) {
                $runtimeIsCandidate = $false
                try {
                    $runtimeCandidate = Get-DawnstrikeGitContract `
                        $gitPath $runtime $ProcessTimeoutSeconds $candidateSha
                    $runtimeIsCandidate = $runtimeCandidate.tree -eq [string]$activation.candidate_tree
                }
                catch {
                    $runtimePrevious = Get-DawnstrikeGitContract `
                        $gitPath $runtime $ProcessTimeoutSeconds $previousSha
                    if ($runtimePrevious.tree -ne $previousTree) {
                        throw "Automatic rollback recovery found a foreign runtime tree."
                    }
                    if (-not (Test-Path -LiteralPath $deactivatedCandidate -PathType Container)) {
                        throw "Automatic rollback recovery has no preserved candidate runtime."
                    }
                    $preservedCandidate = Get-DawnstrikeGitContract `
                        $gitPath $deactivatedCandidate $ProcessTimeoutSeconds $candidateSha
                    if ($preservedCandidate.tree -ne [string]$activation.candidate_tree) {
                        throw "Automatic rollback recovery preserved candidate tree is invalid."
                    }
                    if (Test-Path -LiteralPath $failedPrevious) {
                        throw "Failed previous-runtime preservation path already exists while runtime is previous."
                    }
                    [System.IO.Directory]::Move($runtime, $failedPrevious)
                }
                if ($runtimeIsCandidate -and (Test-Path -LiteralPath $deactivatedCandidate)) {
                    throw "Automatic rollback recovery found duplicate candidate runtimes."
                }
            }
            if (-not (Test-Path -LiteralPath $runtime)) {
                if (-not (Test-Path -LiteralPath $deactivatedCandidate -PathType Container)) {
                    throw "Automatic rollback recovery has no candidate runtime to restore."
                }
                $preservedCandidate = Get-DawnstrikeGitContract `
                    $gitPath $deactivatedCandidate $ProcessTimeoutSeconds $candidateSha
                if ($preservedCandidate.tree -ne [string]$activation.candidate_tree) {
                    throw "Automatic rollback recovery candidate preservation is invalid."
                }
                [System.IO.Directory]::Move($deactivatedCandidate, $runtime)
            }
            if ($tasksDisabled) {
                if ($env:DAWNSTRIKE_TEST_ROLLBACK_THROW_POINT -eq "during_compensation") {
                    if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback failure injection is test-only." }
                    throw "Injected rollback compensation failure."
                }
                $recoveredRuntime = Get-DawnstrikeGitContract `
                    $gitPath $runtime $ProcessTimeoutSeconds $candidateSha
                if ($recoveredRuntime.tree -ne [string]$activation.candidate_tree) {
                    throw "Automatic rollback failure recovery did not restore the candidate tree."
                }
                $null = Assert-DawnstrikeTaskXmlBackup `
                    -StateRoot $state `
                    -BackupName $taskBackup.backup_name `
                    -ExpectedManifestSha256 $taskBackup.manifest_sha256 `
                    -ExpectedTaskContractSha256 ([string]$activation.task_contract_sha256) `
                    -ExpectedTaskDefinitionContractSha256 ([string]$activation.task_definition_contract_sha256) `
                    -ExpectedTaskActionContractSha256 ([string]$activation.task_action_contract_sha256)
                $recoveredDisabledTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
                if (
                    $recoveredDisabledTasks.disabled_count -ne 5 -or
                    $recoveredDisabledTasks.enabled_count -ne 0 -or
                    $recoveredDisabledTasks.task_definition_contract_sha256 -ne
                        [string]$activation.task_definition_contract_sha256
                ) {
                    throw "Automatic rollback recovery did not recover exact disabled task definitions."
                }
                $compensationEnableBoundary = {
                    Confirm-DawnstrikeActivationDailyLockHandshake `
                        -StateRoot $state -ActivationLock $activationLock -DailyLock $dailyLock | Out-Null
                    $null = Assert-DawnstrikeRollbackPostFinalizerMutationWindow `
                        -RollbackTargetMarketDate $rollbackTargetMarketDate `
                        -RequiredCompletedMarketDate $requiredCompletedMarketDate `
                        -BoundaryMode $rollbackBoundaryMode -ProtectedInFlight `
                        -AllowRecoveryEnablePrefix `
                        -TestNowUtc $TestNowUtc
                }.GetNewClosure()
                if ($auxiliaryBefore.present) {
                    & $compensationEnableBoundary
                    $null = Restore-DawnstrikeAuxiliaryCaptureTask `
                        -Expected $auxiliaryBefore `
                        -RuntimeRoot $runtime `
                        -StateRoot $state `
                        -RunAsCredential $RunAsCredential
                    $auxiliaryDisabled = $false
                }
                # Compensation restores the original candidate boundary.  The
                # rollback transaction may have rebound actions to the
                # previous SHA, so restore the candidate binding while still
                # Disabled before re-enabling anything.
                Set-DawnstrikeCanonicalTaskExpectedSha `
                    -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $candidateSha
                $null = Assert-DawnstrikeCanonicalTaskSemantics `
                    -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $candidateSha -AllowDisabled
                Enable-DawnstrikeCanonicalTasks -BeforeEachEnable $compensationEnableBoundary
                & $compensationEnableBoundary
                $recoveredTasks = Get-DawnstrikeTaskContract $runtime $state
                if ($recoveredTasks.task_contract_sha256 -ne [string]$activation.task_contract_sha256) {
                    throw "Automatic rollback failure recovery did not restore task XML."
                }
                $tasksDisabled = $false
            }
            # The original candidate and Ready task contract are now restored.
            # Seal this as terminal compensation so a retry cannot interpret
            # POST_SWAP/POST_SWAP_READY as proof that the previous runtime is installed.
            if ($journalPhase -in @("PRE_SWAP", "POST_SWAP", "POST_SWAP_READY") -and -not $tasksDisabled) {
                $compensatedRuntime = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $candidateSha
                $compensatedTasks = Get-DawnstrikeTaskContract $runtime $state
                if ($compensatedRuntime.tree -ne [string]$activation.candidate_tree -or
                    $compensatedTasks.task_contract_sha256 -ne [string]$activation.task_contract_sha256) {
                    throw "Automatic rollback restore did not prove the exact original Ready boundary."
                }
                $journalBefore = Get-DawnstrikeStrictRuntimeOperationJournal `
                    $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
                $compensationAttemptKey = [string]$journalBefore.raw_file_sha256
                $compensationReceiptRelative = "receipts/runtime-rollback/runtime-rollback-$activationId.compensated-$compensationAttemptKey.json"
                $compensationReceipt = Join-Path $state ($compensationReceiptRelative.Replace('/', '\'))
                $failureReceipt = Join-Path $rollbackReceiptRoot "runtime-rollback-$activationId.failed-$compensationAttemptKey.json"
                Assert-DawnstrikeNoReparseComponents $compensationReceipt "Rollback compensation receipt"
                Assert-DawnstrikeNoReparseComponents $failureReceipt "Rollback failure receipt"
                $empty = Get-DawnstrikeSha256Text ""
                $statePrefix = [System.IO.Path]::GetFullPath($state).TrimEnd('\') + '\'
                $receiptFullPath = [System.IO.Path]::GetFullPath($receiptPath)
                if (-not $receiptFullPath.StartsWith($statePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw "Rollback activation receipt is outside StateRoot."
                }
                $priorReceiptRelative = $receiptFullPath.Substring($statePrefix.Length) -replace '\\','/'
                $priorReceiptHash = Get-DawnstrikeSha256File $receiptPath
                $failurePayload = [ordered]@{
                    schema_version = "dawnstrike.runtime_rollback_failure.v1"
                    status = "FAILED_RESTORED_EXACT_READY"
                    activation_id = $activationId
                    candidate_sha = $candidateSha
                    candidate_tree = [string]$activation.candidate_tree
                    restored_sha = $candidateSha
                    restored_tree = [string]$activation.candidate_tree
                    restored_task_contract_sha256 = [string]$compensatedTasks.task_contract_sha256
                    failure_phase = $journalPhase
                    failure_type = $failure.Exception.GetType().Name
                    recovery_evidence = "EXACT_CANDIDATE_RUNTIME_AND_READY_TASKS"
                    research_only = $true
                    broker_execution_enabled = $false
                }
                Write-DawnstrikeActivationJson $failurePayload $failureReceipt
                $compensationInput = "$compensationReceipt.$([guid]::NewGuid().ToString('N')).input.json"
                $compensationPayload = [ordered]@{
                    schema_version = "dawnstrike.runtime_compensation_receipt.v1"
                    status = "COMPENSATED"
                    operation = "runtime_rollback"
                    candidate_sha = $candidateSha
                    candidate_tree = [string]$activation.candidate_tree
                    prior_journal_file_sha256 = [string]$journalBefore.raw_file_sha256
                    task_contract_sha256 = [string]$compensatedTasks.task_contract_sha256
                    task_state = "Ready"
                    task_xml_sha256 = [string]$compensatedTasks.task_contract_sha256
                    task_action_contract_sha256 = [string]$compensatedTasks.task_action_contract_sha256
                    task_definition_contract_sha256 = [string]$compensatedTasks.task_definition_contract_sha256
                    prior_receipt_relative_path = $priorReceiptRelative
                    prior_receipt_sha256 = $priorReceiptHash
                    failure_type = $failure.Exception.GetType().Name
                    research_only = $true
                    broker_execution_enabled = $false
                }
                try {
                    Write-DawnstrikeActivationJson $compensationPayload $compensationInput
                    $null = Invoke-DawnstrikeActivationProcess `
                        -FilePath $lockInterpreter.path `
                        -ArgumentList @(
                            "-I", "-B", "-S",
                            (Join-Path $PSScriptRoot "runtime_operation_journal.py"),
                            "seal-compensation", "--input", $compensationInput,
                            "--output", $compensationReceipt, "--state-root", $state,
                            "--reuse-existing"
                        ) `
                        -WorkingDirectory $PSScriptRoot `
                        -Label "Rollback compensation receipt strict sealing" `
                        -TimeoutSeconds $ProcessTimeoutSeconds
                }
                finally { if (Test-Path -LiteralPath $compensationInput) { Remove-Item -LiteralPath $compensationInput -Force } }
                $compensationHash = Get-DawnstrikeSha256File $compensationReceipt
                $null = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                    -Lock $activationLock -Operation runtime_rollback -Phase COMPENSATED `
                    -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                    -CurrentSha $candidateSha -CurrentTree ([string]$activation.candidate_tree) `
                    -PreviousSha $previousSha -PreviousTree $previousTree -OriginIdentity $lockOrigin `
                    -PreparedReceiptRelativePath $journalPreparedRelativePath -PreparedReceiptSha256 (Get-DawnstrikeSha256File $receiptPath) `
                    -CompleteReceiptRelativePath $journalCompleteRelativePath -CompleteReceiptSha256 $empty `
                    -BackupContractSha256 ([string]$journalBefore.payload.backup_contract_sha256) `
                    -TaskContractSha256 ([string]$compensatedTasks.task_contract_sha256) -RuntimeStageContractSha256 $empty `
                    -CompensationReceiptRelativePath $compensationReceiptRelative -CompensationReceiptSha256 $compensationHash `
                    -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
                $journalPhase = "COMPENSATED"
            }
        }
        catch {
            try {
                $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
            }
            catch {
                $preserveLocks = $true
                throw "Runtime rollback and automatic candidate restore failed; exact task state is unverified and operator recovery is required."
            }
            # Compensation failed, but the best-effort Disabled boundary was
            # proven. Keep both exact locks while PRE_SWAP/POST_SWAP/POST_SWAP_READY evidence
            # remains so the next invocation can adopt and retry safely.
            if ($journalPhase -in @("PRE_SWAP", "POST_SWAP", "POST_SWAP_READY")) {
                if ($null -eq $activationLock -or $null -eq $dailyLock) {
                    $preserveLocks = $true
                    throw "Nonterminal rollback compensation lacks its adoptable lock pair."
                }
                $preserveLocks = $true
            }
            throw "Runtime rollback failed and automatic candidate restore could not be completed; canonical tasks are proven Disabled and all rollback artifacts must be preserved. Original failure: $($failure.Exception.Message)"
        }
        # A caught failure after a nonterminal journal transition must leave
        # both lock artifacts adoptable by the next invocation.  Releasing
        # them here would strand PRE_SWAP/POST_SWAP/POST_SWAP_READY evidence with no legal
        # owner and make recovery permanently fail closed.
        if ($journalPhase -in @("PRE_SWAP", "POST_SWAP", "POST_SWAP_READY")) {
            if ($null -eq $activationLock -or $null -eq $dailyLock) {
                throw "Nonterminal rollback recovery lacks its adoptable lock pair."
            }
            $preserveLocks = $true
        }
        throw $failure
    }
    finally {
        if (-not $preserveLocks) {
            if ($null -ne $dailyLock) { Exit-DawnstrikeDailyRunLock -Lock $dailyLock }
            Exit-DawnstrikeGovernedRuntimeLock $activationLock
        }
    }
    }
    finally {
        foreach ($stream in @($rollbackAdmissionLocks)) {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    if ([string]::IsNullOrWhiteSpace($ActivationReceipt)) {
        throw "ActivationReceipt is required."
    }
    if ([string]::IsNullOrWhiteSpace($ContractRoot)) {
        $ContractRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    }
    $result = Invoke-DawnstrikeRuntimeRollback `
        -ActivationReceipt $ActivationReceipt `
        -ContractRoot $ContractRoot `
        -RuntimeRoot $RuntimeRoot `
        -StateRoot $StateRoot `
        -BackupRoot $BackupRoot `
        -ProcessTimeoutSeconds $ProcessTimeoutSeconds `
        -RunAsCredential $RunAsCredential `
        -StateBoundaryTaskMutationOperationId $StateBoundaryTaskMutationOperationId `
        -StateBoundaryTerminalReconciliationRequired:$StateBoundaryTerminalReconciliationRequired `
        -TestNowUtc $TestNowUtc
    $result | ConvertTo-Json -Depth 12
}
