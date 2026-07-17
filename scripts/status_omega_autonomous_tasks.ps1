Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $RepoRoot
py -m intraday_scanner.v2.autonomous_runner status
$ExitCode = $LASTEXITCODE
exit $ExitCode
