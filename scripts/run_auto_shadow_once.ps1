param(
    [string]$InputPath = "data\inbox\screener\latest.csv",
    [string]$DbPath = "data\shadow.sqlite",
    [string]$OutDir = "outputs\auto_shadow\manual_run",
    [ValidateSet("none", "codex-cli", "openai-api")]
    [string]$AiNormalizer = "none"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    py -m intraday_scanner.cli auto-shadow-from-screener `
        --input $InputPath `
        --db-path $DbPath `
        --out-dir $OutDir `
        --ai-normalizer $AiNormalizer `
        --persist `
        --print
}
finally {
    Pop-Location
}
