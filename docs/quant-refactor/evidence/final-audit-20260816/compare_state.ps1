$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

$beforeRepo = Get-Content -Raw -LiteralPath (Join-Path $root "repository-state-before.json") | ConvertFrom-Json
$afterRepo = Get-Content -Raw -LiteralPath (Join-Path $root "repository-state-after.json") | ConvertFrom-Json
$beforeFiles = @{}
$afterFiles = @{}
foreach ($file in $beforeRepo.files) { $beforeFiles[[string]$file.path] = $file }
foreach ($file in $afterRepo.files) { $afterFiles[[string]$file.path] = $file }
$added = @($afterFiles.Keys | Where-Object { -not $beforeFiles.ContainsKey($_) } | Sort-Object)
$removed = @($beforeFiles.Keys | Where-Object { -not $afterFiles.ContainsKey($_) } | Sort-Object)
$changed = @()
foreach ($path in $beforeFiles.Keys) {
    if (-not $afterFiles.ContainsKey($path)) { continue }
    if (
        [string]$beforeFiles[$path].sha256 -ne [string]$afterFiles[$path].sha256 -or
        [long]$beforeFiles[$path].length -ne [long]$afterFiles[$path].length
    ) {
        $changed += [ordered]@{
            path = $path
            before_sha256 = [string]$beforeFiles[$path].sha256
            after_sha256 = [string]$afterFiles[$path].sha256
            before_length = [long]$beforeFiles[$path].length
            after_length = [long]$afterFiles[$path].length
        }
    }
}
$repoPayload = [ordered]@{
    compared_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    excluded_paths = @(
        "docs/quant-refactor/evidence/final-audit-20260816/*",
        "docs/quant-refactor/24-final-independent-audit.md"
    )
    branch_unchanged = ([string]$beforeRepo.branch -eq [string]$afterRepo.branch)
    head_unchanged = ([string]$beforeRepo.head -eq [string]$afterRepo.head)
    origin_main_unchanged = ([string]$beforeRepo.origin_main -eq [string]$afterRepo.origin_main)
    added_count = $added.Count
    removed_count = $removed.Count
    changed_count = $changed.Count
    added = $added
    removed = $removed
    changed = $changed
    invariant = (
        [string]$beforeRepo.branch -eq [string]$afterRepo.branch -and
        [string]$beforeRepo.head -eq [string]$afterRepo.head -and
        $added.Count -eq 0 -and $removed.Count -eq 0 -and $changed.Count -eq 0
    )
}
[System.IO.File]::WriteAllText(
    (Join-Path $root "repository-state-invariance.json"),
    (($repoPayload | ConvertTo-Json -Depth 8) + "`n"),
    [System.Text.UTF8Encoding]::new($false)
)

$beforeActive = Get-Content -Raw -LiteralPath (Join-Path $root "active-state-before.json") | ConvertFrom-Json
$afterActive = Get-Content -Raw -LiteralPath (Join-Path $root "active-state-after.json") | ConvertFrom-Json
$beforeIdentity = $beforeActive.before_read | ConvertTo-Json -Depth 8 -Compress
$afterIdentity = $afterActive.after_read | ConvertTo-Json -Depth 8 -Compress
$activePayload = [ordered]@{
    compared_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    path = [string]$beforeActive.path
    sqlite_uri_mode_before = [string]$beforeActive.sqlite_uri_mode
    sqlite_uri_mode_after = [string]$afterActive.sqlite_uri_mode
    query_only_before = [int]$beforeActive.query_only
    query_only_after = [int]$afterActive.query_only
    quick_check_before = [string]$beforeActive.quick_check
    quick_check_after = [string]$afterActive.quick_check
    before_identity = $beforeActive.before_read
    after_identity = $afterActive.after_read
    invariant = ($beforeIdentity -eq $afterIdentity)
}
[System.IO.File]::WriteAllText(
    (Join-Path $root "active-state-invariance.json"),
    (($activePayload | ConvertTo-Json -Depth 8) + "`n"),
    [System.Text.UTF8Encoding]::new($false)
)

$repoPayload | ConvertTo-Json -Depth 8
$activePayload | ConvertTo-Json -Depth 8
if (-not $repoPayload.invariant -or -not $activePayload.invariant) { exit 2 }
