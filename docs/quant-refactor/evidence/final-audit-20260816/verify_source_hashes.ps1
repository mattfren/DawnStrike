$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
$sets = @(
    [ordered]@{ name = "wp005-b"; path = "docs/quant-refactor/evidence/wp005-b-20260815/preflight.json"; property = "expected_frozen_hashes" },
    [ordered]@{ name = "wp005-c-pre"; path = "docs/quant-refactor/evidence/wp005-c-20260815/source-hashes.pre.json"; property = "array" },
    [ordered]@{ name = "wp005-c-post"; path = "docs/quant-refactor/evidence/wp005-c-20260815/source-hashes.post.json"; property = "array" },
    [ordered]@{ name = "wp006"; path = "docs/quant-refactor/evidence/wp006-20260816/source-hashes.json"; property = "array" },
    [ordered]@{ name = "wp007"; path = "docs/quant-refactor/evidence/wp007-20260816/source-hashes.json"; property = "files" }
)
$results = @()
foreach ($set in $sets) {
    $raw = Get-Content -Raw -LiteralPath (Join-Path $repo $set.path) | ConvertFrom-Json
    $entries = @()
    if ($set.property -eq "expected_frozen_hashes") {
        foreach ($property in $raw.expected_frozen_hashes.PSObject.Properties) {
            $entries += [ordered]@{ path = $property.Name; expected_hash = [string]$property.Value; expected_length = $null }
        }
    }
    else {
        $sourceEntries = if ($set.property -eq "files") { @($raw.files) } else { @($raw) }
        foreach ($entry in $sourceEntries) {
            $propertyNames = @($entry.PSObject.Properties.Name)
            $length = if ($propertyNames -contains "length") { [long]$entry.length } elseif ($propertyNames -contains "bytes") { [long]$entry.bytes } else { $null }
            $entries += [ordered]@{ path = [string]$entry.path; expected_hash = [string]$entry.sha256; expected_length = $length }
        }
    }
    $mismatches = @()
    foreach ($entry in $entries) {
        $absolute = Join-Path $repo $entry.path
        if (-not (Test-Path -LiteralPath $absolute -PathType Leaf)) {
            $mismatches += [ordered]@{ path = $entry.path; kind = "missing" }
            continue
        }
        $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $absolute).Hash.ToLowerInvariant()
        $actualLength = (Get-Item -LiteralPath $absolute).Length
        if ($actualHash -ne $entry.expected_hash.ToLowerInvariant()) {
            $mismatches += [ordered]@{ path = $entry.path; kind = "sha256"; expected = $entry.expected_hash.ToLowerInvariant(); actual = $actualHash }
        }
        if ($null -ne $entry.expected_length -and $actualLength -ne $entry.expected_length) {
            $mismatches += [ordered]@{ path = $entry.path; kind = "length"; expected = $entry.expected_length; actual = $actualLength }
        }
    }
    $results += [ordered]@{ name = $set.name; source = $set.path; entry_count = $entries.Count; mismatch_count = $mismatches.Count; mismatches = $mismatches }
}
$totalEntries = 0
$totalMismatches = 0
foreach ($result in $results) {
    $totalEntries += $result.entry_count
    $totalMismatches += $result.mismatch_count
}
$payload = [ordered]@{
    captured_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    sets = $results
    total_entries = $totalEntries
    total_mismatches = $totalMismatches
}
$json = $payload | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot "accepted-source-rehash.json"), "$json`n", [System.Text.UTF8Encoding]::new($false))
foreach ($result in $results) { Write-Output ("{0}: entries={1}; mismatches={2}" -f $result.name, $result.entry_count, $result.mismatch_count) }
Write-Output ("total_entries={0}; total_mismatches={1}" -f $payload.total_entries, $payload.total_mismatches)
if ($payload.total_mismatches -ne 0) { exit 2 }
