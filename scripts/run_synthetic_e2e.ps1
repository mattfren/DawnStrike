[CmdletBinding()]
param(
    [ValidateSet(
        "All", "Fast", "Isolation",
        "ScenarioA", "ScenarioB", "ScenarioC", "ScenarioD", "ScenarioE", "ScenarioF",
        "ScenarioG", "ScenarioH", "ScenarioI", "ScenarioJ", "ScenarioK", "ScenarioL", "ScenarioM"
    )]
    [string]$Mode = "All"
)

# Runs the synthetic E2E rehearsal (isolation proofs + scenarios A-L). No
# placeholder editing required: it locates the repo root from its own path,
# uses the py launcher already installed on this machine, and writes
# evidence under C:\r\dsos-00-v3\e2e\<run-id> (created fresh by the harness
# itself on every run).
#
# "Fast" is the subset intended for routine future-change checks: it skips
# the slow/heavy real-subprocess scenarios (H's kill-and-restart child
# process, I's real EOD orchestration subprocess, J's competing OS-process
# lock contenders, M's real EOD orchestration subprocess under a valid
# universe) and runs everything else, which is still every in-process
# isolation/economic/risk/fault-reason property this harness proves.

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$AllFile = "tests/e2e/test_synthetic_rehearsal.py"
$PytestArgs = @(switch ($Mode) {
    "All"        { @($AllFile) }
    "Fast"       { @($AllFile, "-k", "not TestScenarioH and not TestScenarioI and not TestScenarioJ and not TestScenarioM") }
    "Isolation"  { @("$AllFile::TestIsolationProofs") }
    "ScenarioA"  { @("$AllFile::TestScenarioA") }
    "ScenarioB"  { @("$AllFile::TestScenarioB") }
    "ScenarioC"  { @("$AllFile::TestScenarioC") }
    "ScenarioD"  { @("$AllFile::TestScenarioD") }
    "ScenarioE"  { @("$AllFile::TestScenarioE") }
    "ScenarioF"  { @("$AllFile::TestScenarioF") }
    "ScenarioG"  { @("$AllFile::TestScenarioG") }
    "ScenarioH"  { @("$AllFile::TestScenarioH") }
    "ScenarioI"  { @("$AllFile::TestScenarioI") }
    "ScenarioJ"  { @("$AllFile::TestScenarioJ") }
    "ScenarioK"  { @("$AllFile::TestScenarioK") }
    "ScenarioL"  { @("$AllFile::TestScenarioL") }
    "ScenarioM"  { @("$AllFile::TestScenarioM") }
})
$NodeSelector = $PytestArgs -join " "

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

& py -3.13 -m pytest @PytestArgs -v
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
