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

function Write-JsonFile {
    param([string]$Path, [object]$Value)
    [System.IO.File]::WriteAllText(
        $Path,
        ($Value | ConvertTo-Json -Depth 20) + [Environment]::NewLine
    )
}

function Invoke-EvidenceCommand {
    param([string]$Name, [string]$Command, [scriptblock]$Action)
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

foreach ($gate in ("focused", "main", "affected")) {
    $result = Get-Content -LiteralPath (Join-Path $EvidenceDir "$gate.exit.json") -Raw |
        ConvertFrom-Json
    if ($result.exit_code -ne 0) {
        throw "cannot resume after non-green $gate gate"
    }
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
Write-JsonFile (Join-Path $EvidenceDir "source-hashes.post.json") $rows
git status --short --branch 2>&1 |
    Out-File -LiteralPath (Join-Path $EvidenceDir "status.post.txt") -Encoding utf8
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

$RunStarted = [DateTimeOffset]::Parse(
    (Get-Content -LiteralPath (Join-Path $EvidenceDir "run.started-at.txt") -Raw).Trim()
)
$RunEnded = [DateTimeOffset]::UtcNow
Write-JsonFile (Join-Path $EvidenceDir "run-summary.json") ([ordered]@{
    started_at = $RunStarted.ToString("o")
    ended_at = $RunEnded.ToString("o")
    duration_seconds = [Math]::Round(($RunEnded - $RunStarted).TotalSeconds, 3)
    focused_count = 19
    main_count = 656
    affected_count = 139
    implementation_repair_cycles = 2
    evidence_orchestration_attempts = 6
    source_hash_match = $HashMatch
    terminal_state = "PASS_CANDIDATE_FOR_SOL_ADJUDICATION"
})
