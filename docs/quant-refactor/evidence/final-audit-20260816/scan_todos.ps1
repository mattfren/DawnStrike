$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
Push-Location $repo
try {
    & rg -n -i --glob "!docs/quant-refactor/evidence/**" --glob "!docs/quant-refactor/24-final-independent-audit.md" --glob "*.py" --glob "*.md" --glob "*.ps1" --glob "*.js" "TODO|FIXME|PLACEHOLDER|NOT IMPLEMENTED|not_in_package" intraday_scanner tests scripts api app.py docs/quant-refactor
    if ($LASTEXITCODE -gt 1) { exit $LASTEXITCODE }
    exit 0
}
finally {
    Pop-Location
}
