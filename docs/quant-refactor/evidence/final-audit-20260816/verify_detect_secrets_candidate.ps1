$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
Push-Location $repo
try {
    $candidateFiles = @(git ls-files -co --exclude-standard) |
        ForEach-Object { $_.Replace("\", "/") } |
        Where-Object {
            $_ -notlike "docs/quant-refactor/evidence/final-audit-20260816/*" -and
            (Test-Path -LiteralPath $_ -PathType Leaf)
        }
    $failed = $false
    for ($offset = 0; $offset -lt $candidateFiles.Count; $offset += 100) {
        $last = [Math]::Min($offset + 99, $candidateFiles.Count - 1)
        $batch = @($candidateFiles[$offset..$last])
        Write-Output "candidate_batch=$offset-$last"
        & py -m detect_secrets.pre_commit_hook --baseline .secrets.baseline @batch
        if ($LASTEXITCODE -ne 0) { $failed = $true }
    }
    if ($failed) { exit 1 }
    exit 0
}
finally {
    Pop-Location
}
