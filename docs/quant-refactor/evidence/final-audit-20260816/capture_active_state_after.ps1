$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
$capture = Join-Path $repo "docs/quant-refactor/evidence/wp007-20260816/capture_active_state.py"
$database = "C:\r\dawnstrike-state\shadow_real.sqlite"
$output = & py $capture $database
$exitCode = $LASTEXITCODE
[System.IO.File]::WriteAllText(
    (Join-Path $PSScriptRoot "active-state-after.json"),
    (($output -join "`n") + "`n"),
    [System.Text.UTF8Encoding]::new($false)
)
Write-Output ($output -join "`n")
exit $exitCode
