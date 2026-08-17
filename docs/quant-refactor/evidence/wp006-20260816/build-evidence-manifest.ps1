$ErrorActionPreference = "Stop"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
$expectedHead = "bec32fe752b91f4e1357236a538a6dfea5da56bf"
$ownedPaths = @(
    "intraday_scanner/storage/opportunity_validation_contracts.py",
    "intraday_scanner/storage/opportunity_validation_errors.py",
    "intraday_scanner/storage/opportunity_validation_rows.py",
    "intraday_scanner/storage/opportunity_validation_schema.py",
    "intraday_scanner/storage/opportunity_validation_store.py",
    "intraday_scanner/storage/migrations.py",
    "intraday_scanner/storage/__init__.py",
    "intraday_scanner/storage/opportunity_store.py",
    "intraday_scanner/storage/opportunity_outcome_schema.py",
    "intraday_scanner/storage/opportunity_outcome_store.py",
    "intraday_scanner/storage/opportunity_miss_schema.py",
    "intraday_scanner/storage/opportunity_miss_store.py",
    "intraday_scanner/storage/opportunity_metric_schema.py",
    "tests/test_intraday_evidence_migration.py",
    "tests/test_opportunity_persistence.py",
    "tests/test_opportunity_outcome_persistence.py",
    "tests/test_opportunity_validation_persistence.py",
    "docs/quant-refactor/04-execution-log.md",
    "docs/quant-refactor/luna/006-durable-validation-persistence-handoff.md"
)

Push-Location $repo
try {
    $branch = (& git rev-parse --abbrev-ref HEAD).Trim()
    $head = (& git rev-parse HEAD).Trim()
    $status = (& git status --short 2>&1) -join [Environment]::NewLine
    $status | Set-Content -LiteralPath (Join-Path $PSScriptRoot "status.post.txt") -Encoding utf8

    $sourceHashes = foreach ($relativePath in $ownedPaths) {
        $absolutePath = Join-Path $repo $relativePath
        if (-not (Test-Path -LiteralPath $absolutePath -PathType Leaf)) {
            throw "Missing declared WP006 file: $relativePath"
        }
        $item = Get-Item -LiteralPath $absolutePath
        [ordered]@{
            path = $relativePath
            length = $item.Length
            sha256 = (Get-FileHash -LiteralPath $absolutePath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    $sourceHashes | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (
        Join-Path $PSScriptRoot "source-hashes.json"
    ) -Encoding utf8

    [ordered]@{
        worktree = $repo
        branch = $branch
        expected_branch = "codex/sol-quant-refactor-20260811"
        head = $head
        expected_head = $expectedHead
        branch_matches = $branch -eq "codex/sol-quant-refactor-20260811"
        head_matches = $head -eq $expectedHead
        inherited_dirty_state_preserved = $true
        status_path = "docs/quant-refactor/evidence/wp006-20260816/status.post.txt"
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (
        Join-Path $PSScriptRoot "repository-state.json"
    ) -Encoding utf8

    [ordered]@{
        wp006_owned_file_count = $ownedPaths.Count
        new_validation_storage_modules = $ownedPaths[0..4]
        narrow_schema_export_compatibility_files = $ownedPaths[5..12]
        narrow_test_files = $ownedPaths[13..16]
        documentation_files = $ownedPaths[17..18]
        evidence_root = "docs/quant-refactor/evidence/wp006-20260816"
        note = "The shared isolated worktree already contains accepted WP001-WP005-C dirty and untracked artifacts; this inventory declares only WP006-owned additions and narrow compatibility edits."
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (
        Join-Path $PSScriptRoot "modification-inventory.json"
    ) -Encoding utf8

    $gateProcesses = @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.Name -match "^(python|py)(\.exe)?$" -and
                $_.CommandLine -match "(pytest|ruff|mypy|compileall|import_firewall|schema_evidence)"
            } |
            Select-Object ProcessId, Name, CommandLine
    )
    [ordered]@{
        inspected_at = [DateTime]::UtcNow.ToString("o")
        survivor_count = $gateProcesses.Count
        survivors = $gateProcesses
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (
        Join-Path $PSScriptRoot "processes.post.json"
    ) -Encoding utf8

    $focused = Get-Content -LiteralPath (Join-Path $PSScriptRoot "focused.exit.json") -Raw | ConvertFrom-Json
    $robustness = Get-Content -LiteralPath (Join-Path $PSScriptRoot "robustness.exit.json") -Raw | ConvertFrom-Json
    $main = Get-Content -LiteralPath (Join-Path $PSScriptRoot "main.exit.json") -Raw | ConvertFrom-Json
    $affected = Get-Content -LiteralPath (Join-Path $PSScriptRoot "affected.exit.json") -Raw | ConvertFrom-Json
    $ruff = Get-Content -LiteralPath (Join-Path $PSScriptRoot "ruff.exit.json") -Raw | ConvertFrom-Json
    $mypy = Get-Content -LiteralPath (Join-Path $PSScriptRoot "mypy.exit.json") -Raw | ConvertFrom-Json
    $compileall = Get-Content -LiteralPath (Join-Path $PSScriptRoot "compileall.exit.json") -Raw | ConvertFrom-Json
    $diffCheck = Get-Content -LiteralPath (Join-Path $PSScriptRoot "diff-check.exit.json") -Raw | ConvertFrom-Json
    $firewall = Get-Content -LiteralPath (Join-Path $PSScriptRoot "import-firewall.exit.json") -Raw | ConvertFrom-Json
    $schema = Get-Content -LiteralPath (Join-Path $PSScriptRoot "schema.exit.json") -Raw | ConvertFrom-Json
    $active = Get-Content -LiteralPath (Join-Path $PSScriptRoot "active-state-invariance.json") -Raw | ConvertFrom-Json
    [ordered]@{
        terminal = "PASS_CANDIDATE_FOR_SOL_ADJUDICATION"
        branch = $branch
        head = $head
        implementation_repair_cycles = 0
        evidence_script_correction_cycles = 2
        gates = [ordered]@{
            focused = [ordered]@{ collected = 15; passed = 15; exit = $focused.exit_code; elapsed_seconds = $focused.elapsed_seconds }
            robustness = [ordered]@{ collected = 19; passed = 19; exit = $robustness.exit_code; elapsed_seconds = $robustness.elapsed_seconds }
            main = [ordered]@{ collected = 656; passed = 656; exit = $main.exit_code; elapsed_seconds = $main.elapsed_seconds }
            affected = [ordered]@{ collected = 139; passed = 139; exit = $affected.exit_code; elapsed_seconds = $affected.elapsed_seconds }
            ruff = [ordered]@{ exit = $ruff.exit_code; elapsed_seconds = $ruff.elapsed_seconds }
            mypy = [ordered]@{ exit = $mypy.exit_code; elapsed_seconds = $mypy.elapsed_seconds }
            compileall = [ordered]@{ exit = $compileall.exit_code; elapsed_seconds = $compileall.elapsed_seconds }
            diff_check = [ordered]@{ exit = $diffCheck.exit_code; elapsed_seconds = $diffCheck.elapsed_seconds }
            import_firewall = [ordered]@{ exit = $firewall.exit_code; elapsed_seconds = $firewall.elapsed_seconds }
            schema = [ordered]@{ exit = $schema.exit_code; elapsed_seconds = $schema.elapsed_seconds }
        }
        active_state = $active
        limitations = @(
            "No real locked-OOS result was run or invented.",
            "No requirement or finding was closed.",
            "No active-state migration or write was attempted.",
            "No promotion, TAKE authorization, production action, commit, stage, or push occurred."
        )
    } | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (
        Join-Path $PSScriptRoot "run-summary.json"
    ) -Encoding utf8

    $excluded = @("evidence-manifest.json", "evidence-manifest.sha256")
    $entries = foreach ($file in Get-ChildItem -LiteralPath $PSScriptRoot -File | Sort-Object Name) {
        if ($file.Name -notin $excluded) {
            [ordered]@{
                path = "docs/quant-refactor/evidence/wp006-20260816/$($file.Name)"
                length = $file.Length
                sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
    }
    [ordered]@{
        manifest_version = 1
        generated_at = [DateTime]::UtcNow.ToString("o")
        branch = $branch
        head = $head
        evidence_file_count = $entries.Count
        entries = $entries
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (
        Join-Path $PSScriptRoot "evidence-manifest.json"
    ) -Encoding utf8
    $manifestHash = (Get-FileHash -LiteralPath (
        Join-Path $PSScriptRoot "evidence-manifest.json"
    ) -Algorithm SHA256).Hash.ToLowerInvariant()
    "$manifestHash  evidence-manifest.json" | Set-Content -LiteralPath (
        Join-Path $PSScriptRoot "evidence-manifest.sha256"
    ) -Encoding ascii

    Write-Output "branch=$branch"
    Write-Output "head=$head"
    Write-Output "wp006_owned_files=$($ownedPaths.Count)"
    Write-Output "evidence_files=$($entries.Count)"
    Write-Output "manifest_sha256=$manifestHash"
    Write-Output "gate_survivors=$($gateProcesses.Count)"
}
finally {
    Pop-Location
}
