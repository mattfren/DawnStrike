$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$stdout = Get-Content -Raw -LiteralPath (Join-Path $root "full-pytest.stdout.txt")
$clean = [regex]::Replace($stdout, '\s*\[\s*\d+%\]\r?\n', '')
$markers = @($clean.ToCharArray() | Where-Object { $_ -in @('.', 'F', 's', 'x', 'X', 'E') })
$failurePositions = @()
for ($index = 0; $index -lt $markers.Count; $index++) {
    if ($markers[$index] -eq 'F') { $failurePositions += ($index + 1) }
}

$collectionLines = Get-Content -LiteralPath (Join-Path $root "collect-only.stdout.txt")
$rows = @()
$cumulative = 0
foreach ($line in $collectionLines) {
    if ($line -match '^(tests/.+\.py): (\d+)$') {
        $count = [int]$Matches[2]
        $start = $cumulative + 1
        $cumulative += $count
        $rows += [pscustomobject]@{
            file = $Matches[1]
            count = $count
            start = $start
            end = $cumulative
        }
    }
}
$knownNodes = @{
    'tests/test_daily_publish_gate.py#2' = 'test_artifact_gate_accepts_clean_explicit_no_trade_fixture'
    'tests/test_daily_publish_gate.py#3' = 'test_artifact_gate_accepts_only_explicitly_approved_degraded_fixture'
    'tests/test_opportunity_validation_persistence.py#4' = 'test_invalid_retrospective_reused_missing_and_nonpredeclared_fail_closed[no_durable_evidence-True-True-retrospective]'
}
$mapped = @()
foreach ($position in $failurePositions) {
    $row = $rows | Where-Object { $position -ge $_.start -and $position -le $_.end }
    $ordinal = $position - $row.start + 1
    $key = "$($row.file)#$ordinal"
    $mapped += [ordered]@{
        marker_position = $position
        file = $row.file
        file_ordinal = $ordinal
        node = $knownNodes[$key]
        isolated_rerun_result = if ($position -in @(252, 253)) { 'FAILED' } else { 'PASSED' }
    }
}
$payload = [ordered]@{
    captured_at_utc = [DateTimeOffset]::UtcNow.ToString('o')
    collected_test_count = $cumulative
    observed_marker_count_before_bounded_termination = $markers.Count
    observed_progress_fraction = [Math]::Round($markers.Count / $cumulative, 6)
    failure_marker_count = $failurePositions.Count
    failures = $mapped
    interpretation = 'Two daily publish failures reproduce in isolation; the validation-persistence marker passes in isolation and remains an order-dependent or cross-test-interference proof gap.'
}
[System.IO.File]::WriteAllText(
    (Join-Path $root 'full-pytest.partial-failure-map.json'),
    (($payload | ConvertTo-Json -Depth 8) + "`n"),
    [System.Text.UTF8Encoding]::new($false)
)
$payload | ConvertTo-Json -Depth 8
