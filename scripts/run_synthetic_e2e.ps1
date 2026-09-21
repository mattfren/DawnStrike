[CmdletBinding()]
param(
    [ValidateSet("All", "Isolation", "ScenarioA", "ScenarioB", "ScenarioC", "ScenarioD")]
    [string]$Mode = "All"
)

# Runs the synthetic E2E rehearsal (isolation proofs + happy-path scenarios
# A-D). No placeholder editing required: it locates the repo root from its
# own path, uses the py launcher already installed on this machine, and
# writes evidence under C:\r\dsos-00-v3\e2e\<run-id> (created fresh by the
# harness itself on every run).

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$NodeSelector = switch ($Mode) {
    "All"        { "tests/e2e/test_synthetic_rehearsal.py" }
    "Isolation"  { "tests/e2e/test_synthetic_rehearsal.py::TestIsolationProofs" }
    "ScenarioA"  { "tests/e2e/test_synthetic_rehearsal.py::TestScenarioA" }
    "ScenarioB"  { "tests/e2e/test_synthetic_rehearsal.py::TestScenarioB" }
    "ScenarioC"  { "tests/e2e/test_synthetic_rehearsal.py::TestScenarioC" }
    "ScenarioD"  { "tests/e2e/test_synthetic_rehearsal.py::TestScenarioD" }
}

$PytestManifest = Join-Path $env:TEMP "dawnstrike_e2e_pytest_manifest_$([Guid]::NewGuid().ToString('N')).json"
$env:DAWNSTRIKE_E2E_PYTEST_MANIFEST = $PytestManifest
$env:DAWNSTRIKE_TEST_ACTIVE_PATH_GUARD = "1"

Write-Host "Dawnstrike synthetic E2E rehearsal - Mode=$Mode"
Write-Host "Repo root: $RepoRoot"
Write-Host "Node selector: $NodeSelector"

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if (-not $pyLauncher) {
    throw "The 'py' launcher was not found on PATH; install Python 3.13 or adjust this script."
}

& py -3.13 -m pytest $NodeSelector -v --maxfail=1
$exitCode = $LASTEXITCODE

if (Test-Path -LiteralPath $PytestManifest) {
    Write-Host "Structured pytest manifest: $PytestManifest"
}

$latestRun = Get-ChildItem -Path "C:\r\dsos-00-v3\e2e" -Directory -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($latestRun) {
    Write-Host "Latest sandbox run: $($latestRun.FullName)"
    $manifestPath = Join-Path $latestRun.FullName "artifacts\manifest.json"
    if (Test-Path -LiteralPath $manifestPath) {
        Write-Host "Evidence manifest: $manifestPath"
    }
}

exit $exitCode
