$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
Push-Location $repo
try {
    $status = @(git status --porcelain=v1)
    $candidateStatus = @($status | Where-Object { $_ -notmatch '^\?\? docs/quant-refactor/evidence/final-audit-20260816/' })
    $trackedChanges = @($candidateStatus | Where-Object { $_ -notmatch '^\?\? ' })
    $untracked = @(git ls-files -o --exclude-standard |
        Where-Object { $_ -notlike 'docs/quant-refactor/evidence/final-audit-20260816/*' })
    $untrackedImplementation = @($untracked | Where-Object { $_ -notlike 'docs/quant-refactor/*' })

    $requirements = @(Get-Content -LiteralPath "docs/quant-refactor/02-requirements-ledger.md" |
        Where-Object { $_ -match '^\| REQ-[A-Z]+-[0-9]+' })
    $audit = Get-Content -LiteralPath "docs/quant-refactor/03-audit-ledger.md"
    $severityCounts = [ordered]@{}
    foreach ($severity in @("BLOCKER", "CRITICAL", "HIGH", "MEDIUM", "LOW")) {
        $severityCounts[$severity] = @($audit | Where-Object { $_ -match "^SEVERITY: $severity\s*$" }).Count
    }

    $reachability = [ordered]@{}
    foreach ($pattern in @(
        "prepare_opportunity_pipeline\(",
        "run_opportunity_pipeline\(",
        "\.append_run\("
    )) {
        $matches = @(& rg -n --glob "*.py" $pattern intraday_scanner 2>$null)
        if ($LASTEXITCODE -gt 1) { throw "rg failed for $pattern" }
        $reachability[$pattern] = $matches
    }

    $payload = [ordered]@{
        captured_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        branch = (git branch --show-current)
        head = (git rev-parse HEAD)
        tracked_change_count = $trackedChanges.Count
        untracked_candidate_count = $untracked.Count
        untracked_non_quant_docs_count = $untrackedImplementation.Count
        requirement_count = $requirements.Count
        requirement_open_count = @($requirements | Where-Object { $_ -match '\| OPEN \|$' }).Count
        audit_open_count = @($audit | Where-Object { $_ -match '^STATUS: OPEN\s*$' }).Count
        audit_severity_counts = $severityCounts
        canonical_return_truth_module_exists = Test-Path -LiteralPath "intraday_scanner/alpha/canonical_return_truth.py"
        wp006_evidence_packet_exists = Test-Path -LiteralPath "docs/quant-refactor/evidence/wp006-20260816/evidence-packet.md"
        reachability = $reachability
    }
    $json = $payload | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText((Join-Path $PSScriptRoot "candidate-analysis-v2.json"), "$json`n", [System.Text.UTF8Encoding]::new($false))
    Write-Output $json
}
finally {
    Pop-Location
}
