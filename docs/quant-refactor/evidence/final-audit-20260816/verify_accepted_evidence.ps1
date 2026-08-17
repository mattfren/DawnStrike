$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
$manifestPaths = @(
    "docs/quant-refactor/evidence/wp005-b-20260815/evidence-manifest.json",
    "docs/quant-refactor/evidence/wp005-c-20260815/evidence-manifest.json",
    "docs/quant-refactor/evidence/wp006-20260816/evidence-manifest.json",
    "docs/quant-refactor/evidence/wp007-20260816/evidence-manifest.json"
)

function Get-ManifestEntries {
    param([object]$Manifest)
    $entries = @()
    if ($null -ne $Manifest.entries) {
        foreach ($entry in $Manifest.entries) {
            $entries += [ordered]@{ path = [string]$entry.path; length = [long]$entry.length; sha256 = [string]$entry.sha256 }
        }
    }
    elseif ($Manifest.artifacts -is [System.Array]) {
        foreach ($entry in $Manifest.artifacts) {
            $entries += [ordered]@{ path = [string]$entry.path; length = [long]$entry.bytes; sha256 = [string]$entry.sha256 }
        }
    }
    elseif ($null -ne $Manifest.artifacts) {
        foreach ($property in $Manifest.artifacts.PSObject.Properties) {
            $entries += [ordered]@{ path = [string]$property.Name; length = [long]$property.Value.size_bytes; sha256 = [string]$property.Value.sha256 }
        }
    }
    return $entries
}

$results = @()
foreach ($manifestRelative in $manifestPaths) {
    $manifestAbsolute = Join-Path $repo $manifestRelative
    $manifest = Get-Content -Raw -LiteralPath $manifestAbsolute | ConvertFrom-Json
    $entries = @(Get-ManifestEntries -Manifest $manifest)
    $manifestDirectory = Split-Path -Parent $manifestAbsolute
    $mismatches = @()
    foreach ($entry in $entries) {
        $entryPath = $entry.path.Replace("\", "/")
        if ($entryPath.StartsWith("docs/")) {
            $absolute = Join-Path $repo $entryPath
        }
        else {
            $absolute = Join-Path $manifestDirectory $entryPath
        }
        if (-not (Test-Path -LiteralPath $absolute -PathType Leaf)) {
            $mismatches += [ordered]@{ path = $entryPath; kind = "missing" }
            continue
        }
        $actualLength = (Get-Item -LiteralPath $absolute).Length
        $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $absolute).Hash.ToLowerInvariant()
        if ($actualLength -ne $entry.length) {
            $mismatches += [ordered]@{ path = $entryPath; kind = "length"; expected = $entry.length; actual = $actualLength }
        }
        if ($actualHash -ne $entry.sha256.ToLowerInvariant()) {
            $mismatches += [ordered]@{ path = $entryPath; kind = "sha256"; expected = $entry.sha256.ToLowerInvariant(); actual = $actualHash }
        }
    }
    $sidecar = Join-Path $manifestDirectory "evidence-manifest.sha256"
    $sidecarResult = $null
    if (Test-Path -LiteralPath $sidecar) {
        $expectedManifestHash = ((Get-Content -Raw -LiteralPath $sidecar).Trim() -split "\s+")[0].ToLowerInvariant()
        $actualManifestHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $manifestAbsolute).Hash.ToLowerInvariant()
        $sidecarResult = [ordered]@{
            expected = $expectedManifestHash
            actual = $actualManifestHash
            match = ($expectedManifestHash -eq $actualManifestHash)
        }
    }
    $results += [ordered]@{
        manifest = $manifestRelative
        manifest_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $manifestAbsolute).Hash.ToLowerInvariant()
        entry_count = $entries.Count
        mismatch_count = $mismatches.Count
        mismatches = $mismatches
        external_seal = $sidecarResult
    }
}

$totalEntries = 0
$totalMismatches = 0
foreach ($result in $results) {
    $totalEntries += [int]$result.entry_count
    $totalMismatches += [int]$result.mismatch_count
}
$payload = [ordered]@{
    captured_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    manifests = $results
    total_entries = $totalEntries
    total_mismatches = $totalMismatches
    all_entries_match = ($totalMismatches -eq 0)
    all_external_seals_match = (@($results | Where-Object { $null -ne $_.external_seal -and -not $_.external_seal.match }).Count -eq 0)
}
$json = $payload | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot "accepted-evidence-rehash-v2.json"), "$json`n", [System.Text.UTF8Encoding]::new($false))
$payload | Select-Object captured_at_utc, total_entries, total_mismatches, all_entries_match, all_external_seals_match | Format-List
foreach ($result in $results) {
    Write-Output ("{0}: entries={1}; mismatches={2}; manifest_sha256={3}; seal_match={4}" -f $result.manifest, $result.entry_count, $result.mismatch_count, $result.manifest_sha256, $result.external_seal.match)
}
if (-not $payload.all_entries_match -or -not $payload.all_external_seals_match) { exit 2 }
