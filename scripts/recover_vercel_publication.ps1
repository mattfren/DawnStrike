[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
    [Parameter(Mandatory = $true)][ValidatePattern('^\d{4}-\d{2}-\d{2}$')][string]$MarketDate,
    [string]$RuntimeRoot = 'C:\r\dawnstrike-runtime',
    [string]$StateRoot = 'C:\r\dawnstrike-state',
    [string]$ProjectId = 'prj_5pef3EZF1u5YadebEz3dFjnkWOXy',
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
    throw 'Vercel publication recovery is restricted to the protected release launcher.'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Vercel publication recovery requires an elevated administrator process.'
}
if ($ProjectId -cne 'prj_5pef3EZF1u5YadebEz3dFjnkWOXy') {
    throw 'Vercel publication recovery project is not the governed production project.'
}

. (Join-Path $PSScriptRoot 'protected_operation_contract.ps1')
$null = ConvertTo-DawnstrikeExactMarketDate -Value $MarketDate
$runtime = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $RuntimeRoot).Path).TrimEnd('\')
$executingRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
if (-not [string]::Equals($runtime, $executingRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Vercel publication recovery must execute from the exact mounted runtime root.'
}
$state = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $StateRoot).Path).TrimEnd('\')

. (Join-Path $PSScriptRoot 'runtime_activation_lock.ps1')
$state = Assert-DawnstrikeRuntimeLockStateRoot $state
Assert-DawnstrikeSharedLockNoReparse `
    (Join-Path $state 'secrets\runtime.env') 'Vercel recovery environment file'
. (Join-Path $PSScriptRoot 'import_dawnstrike_environment.ps1')
foreach ($name in @('VERCEL_TOKEN', 'VERCEL_ORG_ID', 'VERCEL_PROJECT_ID')) {
    [Environment]::SetEnvironmentVariable($name, $null, 'Process')
}
Import-DawnstrikeEnvironment -StateRoot $state
if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable('VERCEL_TOKEN', 'Process'))) {
    throw 'Vercel publication recovery credential is unavailable in durable runtime.env.'
}

$publisher = Join-Path $PSScriptRoot 'publish_vercel_public.ps1'
$output = @(
    & $publisher `
        -ProjectRoot $runtime `
        -ProjectId $ProjectId `
        -StateRoot $state `
        -ExpectedSha $ExpectedSha `
        -ExpectedMarketDate $MarketDate `
        -RecoveryOnly `
        -SuppressNativeConsoleReplay
)
try {
    if ($output.Count -ne 1) {
        throw 'Vercel publication recovery returned an unexpected output count.'
    }
    $result = if ($output[0] -is [string]) {
        ([string]$output[0]) | ConvertFrom-Json
    }
    else { $output[0] }
}
catch { throw 'Vercel publication recovery returned invalid JSON.' }
$expectedAliases = @(
    'https://dawnstrike-command-center-x3-mattfren-mattfrens-projects.vercel.app',
    'https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app',
    'https://dawnstrike-command-center-x3.vercel.app'
)
$null = Assert-DawnstrikeVercelRecoveryResult `
    -Result $result `
    -ExpectedSha $ExpectedSha `
    -ExpectedMarketDate $MarketDate `
    -ExpectedProjectId $ProjectId `
    -ExpectedProjectName 'dawnstrike-command-center-x3' `
    -ExpectedProviderScope 'mattfrens-projects' `
    -ExpectedAliases $expectedAliases
$result | ConvertTo-Json -Depth 40 -Compress
