$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$EvidenceDir = $PSScriptRoot
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $EvidenceDir "..\..\..\..")).Path
Set-Location -LiteralPath $RepoRoot

$SourcePaths = @(
    "intraday_scanner/v2/opportunity/validation_robustness.py",
    "intraday_scanner/v2/opportunity/validation_robustness_contracts.py",
    "intraday_scanner/v2/opportunity/validation_robustness_controls.py",
    "intraday_scanner/v2/opportunity/validation_robustness_math.py",
    "intraday_scanner/v2/opportunity/validation_robustness_population.py",
    "intraday_scanner/v2/opportunity/validation_robustness_report.py",
    "tests/test_opportunity_validation_robustness.py"
)

$FocusedFiles = @("tests/test_opportunity_validation_robustness.py")
$MainFiles = @(
    "tests/test_opportunity_validation_metrics.py",
    "tests/test_opportunity_validation.py",
    "tests/test_opportunity_metric_persistence.py",
    "tests/test_opportunity_discovery_metrics.py",
    "tests/test_opportunity_miss_persistence.py",
    "tests/test_opportunity_missed.py",
    "tests/test_opportunity_outcomes.py",
    "tests/test_opportunity_outcome_persistence.py",
    "tests/test_opportunity_persistence.py",
    "tests/test_intraday_evidence_migration.py",
    "tests/test_opportunity_contracts.py",
    "tests/test_opportunity_features.py",
    "tests/test_opportunity_pipeline.py",
    "tests/test_opportunity_universe_risk.py"
)
$AffectedFiles = @(
    "tests/test_sqlite_read_only_store.py",
    "tests/test_no_persist_sqlite_semantics.py",
    "tests/test_v2_data_truth_paper_ops.py"
)

function Write-JsonFile {
    param([string]$Path, [object]$Value)
    $json = $Value | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($Path, $json + [Environment]::NewLine)
}

function Write-SourceHashes {
    param([string]$Name)
    $rows = foreach ($relative in $SourcePaths) {
        $full = Join-Path $RepoRoot $relative
        $item = Get-Item -LiteralPath $full
        $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $full
        [ordered]@{
            path = $relative
            bytes = $item.Length
            sha256 = $hash.Hash.ToLowerInvariant()
        }
    }
    Write-JsonFile (Join-Path $EvidenceDir $Name) $rows
}

function Invoke-EvidenceCommand {
    param(
        [string]$Name,
        [string]$Command,
        [scriptblock]$Action
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $EvidenceDir "$Name.command.txt"),
        $Command + [Environment]::NewLine
    )
    $started = [DateTimeOffset]::UtcNow
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    & $Action 2>&1 | Tee-Object -LiteralPath (Join-Path $EvidenceDir "$Name.log")
    $exitCode = $LASTEXITCODE
    $timer.Stop()
    $ended = [DateTimeOffset]::UtcNow
    Write-JsonFile (Join-Path $EvidenceDir "$Name.exit.json") ([ordered]@{
        command = $Command
        exit_code = $exitCode
        started_at = $started.ToString("o")
        ended_at = $ended.ToString("o")
        duration_seconds = [Math]::Round($timer.Elapsed.TotalSeconds, 3)
    })
    if ($exitCode -ne 0) {
        throw "$Name failed with exit code $exitCode"
    }
}

function Invoke-Collection {
    param([string]$Name, [string[]]$Files, [int]$Expected)
    $command = "py -m pytest " + ($Files -join " ") + " --collect-only -q -p no:cacheprovider"
    $logPath = Join-Path $EvidenceDir "$Name.collection.log"
    $started = [DateTimeOffset]::UtcNow
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    & py -m pytest @Files --collect-only -q -p no:cacheprovider 2>&1 |
        Tee-Object -LiteralPath $logPath
    $exitCode = $LASTEXITCODE
    $timer.Stop()
    $lines = Get-Content -LiteralPath $logPath
    $count = @($lines | Where-Object { $_ -match "::" }).Count
    if ($count -eq 0) {
        $summaries = $lines | Where-Object { $_ -match ":\s+(\d+)\s*$" }
        foreach ($summary in $summaries) {
            if ($summary -match ":\s+(\d+)\s*$") {
                $count += [int]$Matches[1]
            }
        }
    }
    Write-JsonFile (Join-Path $EvidenceDir "$Name.collection.json") ([ordered]@{
        command = $command
        exit_code = $exitCode
        expected_count = $Expected
        collected_count = $count
        started_at = $started.ToString("o")
        ended_at = [DateTimeOffset]::UtcNow.ToString("o")
        duration_seconds = [Math]::Round($timer.Elapsed.TotalSeconds, 3)
    })
    if ($exitCode -ne 0 -or $count -ne $Expected) {
        throw "$Name collection mismatch: expected $Expected, collected $count, exit $exitCode"
    }
}

$RunStarted = [DateTimeOffset]::UtcNow
[System.IO.File]::WriteAllText(
    (Join-Path $EvidenceDir "run.started-at.txt"),
    $RunStarted.ToString("o") + [Environment]::NewLine
)
[System.IO.File]::WriteAllText(
    (Join-Path $EvidenceDir "head.txt"),
    ((git rev-parse HEAD) -join [Environment]::NewLine) + [Environment]::NewLine
)
[System.IO.File]::WriteAllText(
    (Join-Path $EvidenceDir "branch.txt"),
    ((git branch --show-current) -join [Environment]::NewLine) + [Environment]::NewLine
)
git status --short --branch 2>&1 | Out-File -LiteralPath (Join-Path $EvidenceDir "status.pre.txt") -Encoding utf8
Write-SourceHashes "source-hashes.pre.json"

Invoke-Collection "focused" $FocusedFiles 19
Invoke-Collection "main" $MainFiles 656
Invoke-Collection "affected" $AffectedFiles 139

Invoke-EvidenceCommand "focused" "py -m pytest tests/test_opportunity_validation_robustness.py -q -p no:cacheprovider" {
    py -m pytest tests/test_opportunity_validation_robustness.py -q -p no:cacheprovider
}
Invoke-EvidenceCommand "main" ((Get-Content -LiteralPath "docs/quant-refactor/evidence/wp005-b-20260815/main.command.txt" -Raw).Trim()) {
    py -m pytest @MainFiles -q -p no:cacheprovider
}
Invoke-EvidenceCommand "affected" ((Get-Content -LiteralPath "docs/quant-refactor/evidence/wp005-b-20260815/affected.command.txt" -Raw).Trim()) {
    py -m pytest @AffectedFiles -q -p no:cacheprovider
}
Invoke-EvidenceCommand "ruff" "py -m ruff check ." { py -m ruff check . }
Invoke-EvidenceCommand "mypy" "py -m mypy intraday_scanner" { py -m mypy intraday_scanner }
Invoke-EvidenceCommand "compileall" "py -m compileall -q intraday_scanner scripts" {
    py -m compileall -q intraday_scanner scripts
}
Invoke-EvidenceCommand "diff-check" "git diff --check" { git diff --check }
Invoke-EvidenceCommand "import-firewall" "py docs/quant-refactor/evidence/wp005-c-20260815/import-firewall.py" {
    py docs/quant-refactor/evidence/wp005-c-20260815/import-firewall.py
}

Write-SourceHashes "source-hashes.post.json"
git status --short --branch 2>&1 | Out-File -LiteralPath (Join-Path $EvidenceDir "status.post.txt") -Encoding utf8
$PreHashes = Get-Content -LiteralPath (Join-Path $EvidenceDir "source-hashes.pre.json") -Raw
$PostHashes = Get-Content -LiteralPath (Join-Path $EvidenceDir "source-hashes.post.json") -Raw
$HashMatch = $PreHashes -eq $PostHashes
Write-JsonFile (Join-Path $EvidenceDir "source-hash-verification.json") ([ordered]@{
    match = $HashMatch
    source_count = $SourcePaths.Count
})
if (-not $HashMatch) {
    throw "WP005-C source hashes drifted during evidence run"
}

$RunEnded = [DateTimeOffset]::UtcNow
Write-JsonFile (Join-Path $EvidenceDir "run-summary.json") ([ordered]@{
    started_at = $RunStarted.ToString("o")
    ended_at = $RunEnded.ToString("o")
    duration_seconds = [Math]::Round(($RunEnded - $RunStarted).TotalSeconds, 3)
    focused_count = 19
    main_count = 656
    affected_count = 139
    repair_cycles = 2
    source_hash_match = $HashMatch
    terminal_state = "PASS_CANDIDATE_FOR_SOL_ADJUDICATION"
})
