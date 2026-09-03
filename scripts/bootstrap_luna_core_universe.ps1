[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
    [Parameter(Mandatory = $true)][ValidatePattern('^\d{4}-\d{2}-\d{2}$')][string]$MarketDate,
    [string]$RuntimeRoot = 'C:\r\dawnstrike-runtime',
    [string]$StateRoot = 'C:\r\dawnstrike-state',
    [switch]$ProtectedLauncherGrant
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$protectedLauncher = 'C:\Program Files\Dawnstrike\bin\dawnstrike_release_launcher.ps1'
$callerPath = [string]$MyInvocation.ScriptName
if (
    -not $ProtectedLauncherGrant -or
    [string]::IsNullOrWhiteSpace($callerPath) -or
    -not [string]::Equals(
        [IO.Path]::GetFullPath($callerPath),
        $protectedLauncher,
        [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw 'Core-universe bootstrap is restricted to the protected release launcher.'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Core-universe bootstrap requires an elevated administrator process.'
}

$runtime = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $RuntimeRoot).Path).TrimEnd('\')
$executingRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
if (-not [string]::Equals($runtime, $executingRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Core-universe bootstrap must execute from the exact mounted runtime root.'
}
$state = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $StateRoot).Path).TrimEnd('\')

. (Join-Path $PSScriptRoot 'dawnstrike_process_runner.ps1')
. (Join-Path $PSScriptRoot 'invoke_dawnstrike_stage.ps1')
. (Join-Path $PSScriptRoot 'protected_operation_contract.ps1')
$null = ConvertTo-DawnstrikeExactMarketDate -Value $MarketDate
$null = Assert-DawnstrikeProcessSourceBoundToHead `
    -ReleaseRoot $runtime `
    -ExpectedSha $ExpectedSha `
    -EntryScript $PSCommandPath `
        -AdditionalSourceFiles (@(
            'scripts/bootstrap_luna_core_universe.ps1',
            'scripts/invoke_dawnstrike_stage.ps1',
            'scripts/protected_operation_contract.ps1'
        ) + @(Get-DawnstrikeLunaCoreSourceFiles -ExpectedSha $ExpectedSha))
$state = Assert-DawnstrikeUniverseStateBoundary -StateRoot $state
$logBoundary = Open-DawnstrikeUniverseBootstrapLogBoundary `
    -StateRoot $state -MarketDate $MarketDate
$logRoot = [string]$logBoundary.path
try {
$releaseSha = Resolve-DawnstrikeReleaseSha `
    -RuntimeRoot $runtime -LogRoot $logRoot -ExpectedSha $ExpectedSha
if ($releaseSha -cne $ExpectedSha) {
    throw 'Core-universe bootstrap release identity changed during admission.'
}

function Assert-DawnstrikeUniverseBootstrapBoundary {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$RequestedMarketDate)

    $canonicalTaskNames = @(
    'Dawnstrike AlphaOps Morning',
    'Dawnstrike AlphaOps Monitor 5m',
    'Dawnstrike AlphaOps EOD Full Report',
    'Dawnstrike AlphaOps V6 Weekly Training',
        'Dawnstrike 10of10 Daily Finalize'
    )
    $snapshots = @()
    foreach ($taskName in $canonicalTaskNames) {
        $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
        if ($matches.Count -ne 1) {
            throw "Core-universe bootstrap requires a unique Ready canonical task: $taskName"
        }
        $task = $matches[0]
        $info = Get-ScheduledTaskInfo `
            -TaskName $taskName `
            -TaskPath ([string]$task.TaskPath) `
            -ErrorAction Stop
        $snapshots += [pscustomobject]@{
            name = $taskName
            state = [string]$task.State
            next_run_time = [DateTime]$info.NextRunTime
            last_run_time = [DateTime]$info.LastRunTime
        }
    }
    $capture = @(Get-ScheduledTask -TaskName 'Dawnstrike Delayed SIP Capture' -ErrorAction SilentlyContinue)
    $now = Get-Date
    $captureSnapshots = @($capture | ForEach-Object {
        [pscustomobject]@{ state = [string]$_.State }
    })
    $null = Assert-DawnstrikeUniverseBootstrapBoundarySnapshot `
        -RequestedMarketDate $RequestedMarketDate `
        -Now $now `
        -CanonicalTasks $snapshots `
        -CaptureTasks $captureSnapshots
}

Assert-DawnstrikeUniverseBootstrapBoundary -RequestedMarketDate $MarketDate
$dailyLock = Enter-DawnstrikeDailyRunLock `
    -StateRoot $state -MarketDate $MarketDate `
    -Owner 'luna_core_universe_bootstrap' -RetainHandle
if (-not $dailyLock.acquired) {
    throw "Core-universe bootstrap could not acquire the daily lock: $($dailyLock.reason)"
}
$null = Confirm-DawnstrikeRetainedDailyRunLock -Lock $dailyLock
try {
    Assert-DawnstrikeUniverseBootstrapBoundary -RequestedMarketDate $MarketDate
    $state = Assert-DawnstrikeUniverseStateBoundary -StateRoot $state
    Push-Location $runtime
    try {
        $refresh = Invoke-DawnstrikeNativeProcess `
            -FilePath 'py.exe' `
            -ArgumentList @(
                'scripts\refresh_luna_core_universe.py',
                '--state-root', $state,
                '--market-date', $MarketDate,
                '--bootstrap-state-street-proxy'
            ) `
            -LogRoot $logRoot `
            -LogName "luna_core_bootstrap-$MarketDate" `
            -WorkingDirectory $runtime `
            -TimeoutSeconds 300 `
            -SuppressConsoleReplay
    }
    finally { Pop-Location }
    if ($refresh.exit_code -ne 0) {
        throw "Core-universe bootstrap failed with exit code $($refresh.exit_code)."
    }
    try {
        $result = (Get-Content -LiteralPath $refresh.stdout_path -Raw) | ConvertFrom-Json
    }
    catch { throw 'Core-universe bootstrap returned invalid JSON.' }
    if (
        [string]$result.status -cne 'READY' -or
        $result.proxy_bootstrapped -ne $true -or
        [string]$result.market_date -cne $MarketDate -or
        [int]$result.ndx_member_count -ne 102 -or
        [int]$result.spy_member_count -ne 503 -or
        [string]$result.ndx_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$result.spy_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$result.generation_key -cnotmatch '^[0-9a-f]{64}$'
    ) {
        throw 'Core-universe bootstrap receipt is not the exact governed READY result.'
    }
    $expectedManifest = [IO.Path]::GetFullPath(
        (Join-Path $state 'config\luna_core_universe.json')
    )
    if (-not [string]::Equals(
        [IO.Path]::GetFullPath([string]$result.manifest),
        $expectedManifest,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Core-universe bootstrap receipt points outside the exact durable state path.'
    }
    $null = Confirm-DawnstrikeRetainedDailyRunLock -Lock $dailyLock
    $result | ConvertTo-Json -Depth 8 -Compress
}
finally {
    Exit-DawnstrikeDailyRunLock -Lock $dailyLock
}
}
finally {
    Close-DawnstrikeUniverseBootstrapLogBoundary -Boundary $logBoundary
}
