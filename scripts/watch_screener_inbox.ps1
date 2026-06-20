param(
    [string]$Inbox = "data\inbox\screener",
    [string]$DbPath = "data\shadow.sqlite",
    [string]$OutRoot = "outputs\auto_shadow",
    [int]$PollSeconds = 10,
    [ValidateSet("none", "codex-cli", "openai-api")]
    [string]$AiNormalizer = "none"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    py -m intraday_scanner.cli watch-screener-inbox `
        --inbox $Inbox `
        --db-path $DbPath `
        --out-root $OutRoot `
        --ai-normalizer $AiNormalizer `
        --poll-seconds $PollSeconds
}
finally {
    Pop-Location
}
