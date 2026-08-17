$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
Push-Location $repo
try {
    $changed = @(
        @(git diff --name-only --diff-filter=ACMR) +
        @(git ls-files -o --exclude-standard)
    ) |
        ForEach-Object { $_.Replace("\", "/") } |
        Where-Object {
            $_ -notlike "docs/quant-refactor/*" -and
            (Test-Path -LiteralPath $_ -PathType Leaf)
        } |
        Sort-Object -Unique
    Write-Output "changed_non_quant_doc_file_count=$($changed.Count)"
    $failed = $false
    for ($offset = 0; $offset -lt $changed.Count; $offset += 100) {
        $last = [Math]::Min($offset + 99, $changed.Count - 1)
        $batch = @($changed[$offset..$last])
        & py -m detect_secrets.pre_commit_hook --baseline .secrets.baseline @batch
        if ($LASTEXITCODE -ne 0) { $failed = $true }
    }
    if ($failed) { exit 1 }
    exit 0
}
finally {
    Pop-Location
}
