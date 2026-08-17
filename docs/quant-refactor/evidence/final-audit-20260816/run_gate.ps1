param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "full-pytest",
        "collect-only",
        "observed-failures",
        "ruff",
        "mypy",
        "compileall",
        "pip-check",
        "node-check",
        "powershell-parse",
        "bandit",
        "detect-secrets",
        "detect-secrets-candidate",
        "detect-secrets-candidate-2",
        "detect-secrets-changed",
        "cyclonedx",
        "safety-contracts",
        "todo-scan",
        "todo-scan-final",
        "diff-check"
    )]
    [string]$Gate
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
$evidence = $PSScriptRoot
$pycache = Join-Path $evidence "pycache"
[void](New-Item -ItemType Directory -Force -Path $pycache)

$filePath = $null
$arguments = @()
$commandText = $null

switch ($Gate) {
    "full-pytest" {
        $filePath = "py"
        $arguments = @("-m", "pytest", "-q", "-p", "no:cacheprovider")
        $commandText = "py -m pytest -q -p no:cacheprovider"
    }
    "collect-only" {
        $filePath = "py"
        $arguments = @("-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider")
        $commandText = "py -m pytest --collect-only -q -p no:cacheprovider"
    }
    "observed-failures" {
        $filePath = "py"
        $arguments = @(
            "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "tests/test_daily_publish_gate.py::test_artifact_gate_accepts_clean_explicit_no_trade_fixture",
            "tests/test_daily_publish_gate.py::test_artifact_gate_accepts_only_explicitly_approved_degraded_fixture",
            "tests/test_opportunity_validation_persistence.py::test_invalid_retrospective_reused_missing_and_nonpredeclared_fail_closed[no_durable_evidence-True-True-retrospective]"
        )
        $commandText = "py -m pytest -q -p no:cacheprovider tests/test_daily_publish_gate.py::test_artifact_gate_accepts_clean_explicit_no_trade_fixture tests/test_daily_publish_gate.py::test_artifact_gate_accepts_only_explicitly_approved_degraded_fixture tests/test_opportunity_validation_persistence.py::test_invalid_retrospective_reused_missing_and_nonpredeclared_fail_closed[no_durable_evidence-True-True-retrospective]"
    }
    "ruff" {
        $filePath = "py"
        $arguments = @("-m", "ruff", "check", ".")
        $commandText = "py -m ruff check ."
    }
    "mypy" {
        $filePath = "py"
        $arguments = @("-m", "mypy", "intraday_scanner")
        $commandText = "py -m mypy intraday_scanner"
    }
    "compileall" {
        $filePath = "py"
        $arguments = @("-m", "compileall", "-q", "intraday_scanner", "scripts")
        $commandText = "py -m compileall -q intraday_scanner scripts"
    }
    "pip-check" {
        $filePath = "py"
        $arguments = @("-m", "pip", "check")
        $commandText = "py -m pip check"
    }
    "node-check" {
        $filePath = "node"
        $arguments = @("--check", "web/assets/dawnstrike.js")
        $commandText = "node --check web/assets/dawnstrike.js"
    }
    "powershell-parse" {
        $filePath = "pwsh"
        $arguments = @("-NoProfile", "-File", (Join-Path $evidence "verify_powershell_parse.ps1"))
        $commandText = "pwsh -NoProfile -File docs/quant-refactor/evidence/final-audit-20260816/verify_powershell_parse.ps1"
    }
    "bandit" {
        $filePath = "py"
        $arguments = @("-m", "bandit", "-r", "intraday_scanner", "scripts", "-ll", "-b", "config/security/bandit-baseline.json")
        $commandText = "py -m bandit -r intraday_scanner scripts -ll -b config/security/bandit-baseline.json"
    }
    "detect-secrets" {
        $filePath = "pwsh"
        $arguments = @("-NoProfile", "-File", (Join-Path $evidence "verify_detect_secrets.ps1"))
        $commandText = "pwsh -NoProfile -File docs/quant-refactor/evidence/final-audit-20260816/verify_detect_secrets.ps1"
    }
    "detect-secrets-candidate" {
        $filePath = "pwsh"
        $arguments = @("-NoProfile", "-File", (Join-Path $evidence "verify_detect_secrets_candidate.ps1"))
        $commandText = "pwsh -NoProfile -File docs/quant-refactor/evidence/final-audit-20260816/verify_detect_secrets_candidate.ps1"
    }
    "detect-secrets-candidate-2" {
        $filePath = "pwsh"
        $arguments = @("-NoProfile", "-File", (Join-Path $evidence "verify_detect_secrets_candidate.ps1"))
        $commandText = "pwsh -NoProfile -File docs/quant-refactor/evidence/final-audit-20260816/verify_detect_secrets_candidate.ps1"
    }
    "detect-secrets-changed" {
        $filePath = "pwsh"
        $arguments = @("-NoProfile", "-File", (Join-Path $evidence "verify_detect_secrets_changed.ps1"))
        $commandText = "pwsh -NoProfile -File docs/quant-refactor/evidence/final-audit-20260816/verify_detect_secrets_changed.ps1"
    }
    "cyclonedx" {
        $filePath = "py"
        $arguments = @("-m", "cyclonedx_py", "environment", "--pyproject", "pyproject.toml", "--output-reproducible", "--output-file", (Join-Path $evidence "sbom.cdx.json"))
        $commandText = "py -m cyclonedx_py environment --pyproject pyproject.toml --output-reproducible --output-file docs/quant-refactor/evidence/final-audit-20260816/sbom.cdx.json"
    }
    "safety-contracts" {
        $filePath = "py"
        $arguments = @(
            "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "tests/test_network_safety.py",
            "tests/test_sql_safety.py",
            "tests/test_opportunity_contracts.py",
            "tests/test_opportunity_validation_robustness.py"
        )
        $commandText = "py -m pytest -q -p no:cacheprovider tests/test_network_safety.py tests/test_sql_safety.py tests/test_opportunity_contracts.py tests/test_opportunity_validation_robustness.py"
    }
    "todo-scan" {
        $filePath = "pwsh"
        $arguments = @("-NoProfile", "-File", (Join-Path $evidence "scan_todos.ps1"))
        $commandText = "pwsh -NoProfile -File docs/quant-refactor/evidence/final-audit-20260816/scan_todos.ps1"
    }
    "todo-scan-final" {
        $filePath = "pwsh"
        $arguments = @("-NoProfile", "-File", (Join-Path $evidence "scan_todos.ps1"))
        $commandText = "pwsh -NoProfile -File docs/quant-refactor/evidence/final-audit-20260816/scan_todos.ps1"
    }
    "diff-check" {
        $filePath = "git"
        $arguments = @("diff", "--check")
        $commandText = "git diff --check"
    }
}

[System.IO.File]::WriteAllText((Join-Path $evidence "$Gate.command.txt"), "$commandText`n", [System.Text.UTF8Encoding]::new($false))

$start = [DateTimeOffset]::UtcNow
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $filePath
$psi.WorkingDirectory = $repo
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true
$psi.Environment["PYTHONDONTWRITEBYTECODE"] = "1"
$psi.Environment["PYTHONPYCACHEPREFIX"] = $pycache
foreach ($argument in $arguments) {
    [void]$psi.ArgumentList.Add($argument)
}

$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $psi
[void]$process.Start()
$stdoutTask = $process.StandardOutput.ReadToEndAsync()
$stderrTask = $process.StandardError.ReadToEndAsync()
$process.WaitForExit()
$stdout = $stdoutTask.GetAwaiter().GetResult()
$stderr = $stderrTask.GetAwaiter().GetResult()
$stopwatch.Stop()
$end = [DateTimeOffset]::UtcNow

[System.IO.File]::WriteAllText((Join-Path $evidence "$Gate.stdout.txt"), $stdout, [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText((Join-Path $evidence "$Gate.stderr.txt"), $stderr, [System.Text.UTF8Encoding]::new($false))
$record = [ordered]@{
    gate = $Gate
    command = $commandText
    cwd = $repo
    started_at_utc = $start.ToString("o")
    ended_at_utc = $end.ToString("o")
    elapsed_seconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 6)
    exit_code = $process.ExitCode
    stdout_bytes = ([System.Text.UTF8Encoding]::new($false)).GetByteCount($stdout)
    stderr_bytes = ([System.Text.UTF8Encoding]::new($false)).GetByteCount($stderr)
}
[System.IO.File]::WriteAllText((Join-Path $evidence "$Gate.exit.json"), (($record | ConvertTo-Json -Depth 4) + "`n"), [System.Text.UTF8Encoding]::new($false))

if ($stdout) { Write-Output $stdout }
if ($stderr) { [Console]::Error.Write($stderr) }
exit $process.ExitCode
