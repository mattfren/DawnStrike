[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$StageRoot = "build\vercel-stage",
    [Parameter(Mandatory = $true)][string]$ExpectedSourceSha,
    [switch]$AllowDegraded
)

$ErrorActionPreference = "Stop"
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$stage = Join-Path $resolvedRoot $StageRoot
$public = Join-Path $stage "public"
$config = Get-Content -Raw -LiteralPath (Join-Path $stage "vercel.json") | ConvertFrom-Json
if ($config.outputDirectory -ne "public") { throw "Unexpected Vercel output directory" }
if ($config.routes -or $config.crons) { throw "Legacy routes or crons remain in the candidate" }
$expectedFunctions = @("api/health.py", "api/readiness.py")
$actualFunctions = @($config.functions.PSObject.Properties.Name)
if (@(Compare-Object -ReferenceObject ($expectedFunctions | Sort-Object) -DifferenceObject ($actualFunctions | Sort-Object)).Count -ne 0) {
    throw "Unexpected Vercel functions in candidate"
}
if ($ExpectedSourceSha -notmatch '^[0-9a-f]{40}$') {
    throw "ExpectedSourceSha must be the exact 40-character lowercase runtime commit SHA."
}
$verifyArgs = @(
    "scripts\verify_public_artifact.py", "--root", $public,
    "--expected-source-sha", $ExpectedSourceSha
)
if ($AllowDegraded) {
    $verifyArgs += "--allow-degraded"
}
& py.exe @verifyArgs
if ($LASTEXITCODE -ne 0) { throw "Public artifact verification failed" }
$scanRoots = @(
    $public,
    (Join-Path $stage "api")
)
$prebuiltRoots = @(
    Get-ChildItem -LiteralPath $stage -Directory -Force |
        Where-Object { $_.Name -like ".vercel-output*" }
)
$scanRoots += @($prebuiltRoots | ForEach-Object { $_.FullName })
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
