[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$StageRoot = "build\vercel-stage",
    [Parameter(Mandatory = $true)][string]$ExpectedSourceSha,
    [Parameter(Mandatory = $true)][string]$ExpectedSourceTree,
    [switch]$AllowDegraded
)

$ErrorActionPreference = "Stop"
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$codeRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$stage = Join-Path $resolvedRoot $StageRoot
. (Join-Path $PSScriptRoot "runtime_activation_lock.ps1")
. (Join-Path $PSScriptRoot "dawnstrike_process_runner.ps1")
$expectedCodeRoot = Get-DawnstrikeProtectedReleaseRoot -ExpectedSha $ExpectedSourceSha
if (-not [string]::Equals($codeRoot, $expectedCodeRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Vercel candidate verifier must execute from the protected exact-SHA release root."
}
$null = Assert-DawnstrikeProcessSourceBoundToHead `
    -ReleaseRoot $codeRoot -ExpectedSha $ExpectedSourceSha -EntryScript $PSCommandPath `
    -AdditionalSourceFiles @(
        'scripts/vercel_source_contract.ps1',
        'scripts/verify_public_artifact.py'
    )
. (Join-Path $PSScriptRoot "vercel_source_contract.ps1")
Assert-VercelGitSourceStable `
    -Root $resolvedRoot `
    -ExpectedSourceSha $ExpectedSourceSha `
    -ExpectedSourceTree $ExpectedSourceTree `
    -AllowedStageRoot $stage
$public = Join-Path $stage "public"
$config = Get-Content -Raw -LiteralPath (Join-Path $stage "vercel.json") | ConvertFrom-Json
if ($config.outputDirectory -ne "public") { throw "Unexpected Vercel output directory" }
$routesProperty = $config.PSObject.Properties["routes"]
$cronsProperty = $config.PSObject.Properties["crons"]
if (
    ($null -ne $routesProperty -and $routesProperty.Value) -or
    ($null -ne $cronsProperty -and $cronsProperty.Value)
) {
    throw "Legacy routes or crons remain in the candidate"
}
$expectedFunctions = @("api/health.py", "api/readiness.py")
$actualFunctions = @($config.functions.PSObject.Properties.Name)
if (@(Compare-Object -ReferenceObject ($expectedFunctions | Sort-Object) -DifferenceObject ($actualFunctions | Sort-Object)).Count -ne 0) {
    throw "Unexpected Vercel functions in candidate"
}
if ($ExpectedSourceSha -notmatch '^[0-9a-f]{40}$') {
    throw "ExpectedSourceSha must be the exact 40-character lowercase runtime commit SHA."
}
if ($ExpectedSourceTree -notmatch '^[0-9a-f]{40}$') {
    throw "ExpectedSourceTree must be the exact 40-character lowercase Git tree SHA."
}
Assert-VercelStagedSourceManifest `
    -StageRoot $stage `
    -ExpectedSourceSha $ExpectedSourceSha `
    -ExpectedSourceTree $ExpectedSourceTree
$verifyArgs = @(
    (Join-Path $codeRoot "scripts\verify_public_artifact.py"), "--root", $public,
    "--expected-source-sha", $ExpectedSourceSha
)
if ($AllowDegraded) {
    $verifyArgs += "--allow-degraded"
}
$approvedPython = Get-DawnstrikeApprovedLockInterpreter
$script:DawnstrikeExpectedReleaseSha = $ExpectedSourceSha
$verificationReceipt = Invoke-DawnstrikeNativeProcess `
    -FilePath ([string]$approvedPython.path) `
    -ArgumentList $verifyArgs `
    -LogRoot (Join-Path $resolvedRoot 'build\vercel-verification-logs') `
    -LogName 'verify_vercel_public_artifact' `
    -WorkingDirectory $resolvedRoot `
    -NoSite
if ([int]$verificationReceipt.exit_code -ne 0) {
    throw "Public artifact verification failed"
}
$scanRoots = @(
    $public,
    (Join-Path $stage "api")
)
$prebuiltRoots = @(
    Get-ChildItem -LiteralPath (Join-Path $stage ".vercel\output") -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq "functions" -or $_.Name -eq "static" }
)
$legacyPrebuiltRoots = @(
    Get-ChildItem -LiteralPath $stage -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like ".vercel-output*" }
)
$scanRoots += @($prebuiltRoots + $legacyPrebuiltRoots | ForEach-Object { $_.FullName })
$forbidden = foreach ($root in $scanRoots) {
    if (Test-Path -LiteralPath $root) {
        Get-ChildItem -LiteralPath $root -Recurse -File -Force |
            Where-Object { $_.Name -match 'sqlite|\.db$|telegram|scanner|ui\.py|\.env' }
    }
}
if ($forbidden) {
    throw "Forbidden runtime or secret file found in Vercel candidate"
}
Write-Output "Vercel candidate verified: $stage"
