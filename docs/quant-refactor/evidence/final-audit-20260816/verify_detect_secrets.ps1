$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
Push-Location $repo
try {
    $trackedFiles = git ls-files
    & py -m detect_secrets.pre_commit_hook --baseline .secrets.baseline @trackedFiles
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
