$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$EvidenceDir = $PSScriptRoot
$ManifestPath = Join-Path $EvidenceDir "evidence-manifest.json"
$rows = foreach ($item in Get-ChildItem -LiteralPath $EvidenceDir -File | Sort-Object Name) {
    if ($item.FullName -eq $ManifestPath) {
        continue
    }
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $item.FullName
    [ordered]@{
        path = "docs/quant-refactor/evidence/wp005-c-20260815/$($item.Name)"
        bytes = $item.Length
        sha256 = $hash.Hash.ToLowerInvariant()
    }
}
$manifest = [ordered]@{
    schema_version = "wp005-c-evidence-manifest.v1"
    generated_at = [DateTimeOffset]::UtcNow.ToString("o")
    artifact_count = @($rows).Count
    artifacts = @($rows)
}
[System.IO.File]::WriteAllText(
    $ManifestPath,
    ($manifest | ConvertTo-Json -Depth 10) + [Environment]::NewLine
)
