$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
$manifestPath = Join-Path $PSScriptRoot "evidence-manifest.json"
$sidecarPath = Join-Path $PSScriptRoot "evidence-manifest.sha256"
$reportPath = Join-Path $repo "docs/quant-refactor/24-final-independent-audit.md"

$paths = @(Get-ChildItem -LiteralPath $PSScriptRoot -File -Recurse |
    Where-Object {
        $_.FullName -notin @($manifestPath, $sidecarPath) -and
        $_.FullName -notlike (Join-Path $PSScriptRoot "pycache\*")
    } |
    Select-Object -ExpandProperty FullName)
if (Test-Path -LiteralPath $reportPath -PathType Leaf) { $paths += $reportPath }
$entries = @()
foreach ($path in @($paths | Sort-Object -Unique)) {
    $item = Get-Item -LiteralPath $path
    $relative = [System.IO.Path]::GetRelativePath($repo, $item.FullName).Replace("\", "/")
    $entries += [ordered]@{
        path = $relative
        length = [long]$item.Length
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $item.FullName).Hash.ToLowerInvariant()
    }
}
$payload = [ordered]@{
    schema_version = "dawnstrike.quant_refactor.final_audit_manifest.v1"
    created_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    verdict = "HASH_DRIFT"
    branch = (git -C $repo branch --show-current)
    head = (git -C $repo rev-parse HEAD)
    entry_count = $entries.Count
    entries = $entries
}
[System.IO.File]::WriteAllText(
    $manifestPath,
    (($payload | ConvertTo-Json -Depth 8) + "`n"),
    [System.Text.UTF8Encoding]::new($false)
)
$manifestHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $manifestPath).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText(
    $sidecarPath,
    "$manifestHash  evidence-manifest.json`n",
    [System.Text.UTF8Encoding]::new($false)
)
Write-Output "entries=$($entries.Count); manifest_sha256=$manifestHash"
