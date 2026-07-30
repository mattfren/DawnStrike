[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$StageRoot = "build\vercel-stage"
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
& py.exe scripts\verify_public_artifact.py --root $public
if ($LASTEXITCODE -ne 0) { throw "Public artifact verification failed" }
if (Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object { $_.Name -match 'sqlite|\.db$|telegram|scanner|ui\.py|\.env' }) {
    throw "Forbidden runtime or secret file found in Vercel candidate"
}
Write-Output "Vercel candidate verified: $stage"
