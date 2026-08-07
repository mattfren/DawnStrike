function Import-DawnstrikeEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$StateRoot
    )

    $environmentPath = Join-Path $StateRoot "secrets\runtime.env"
    if (-not (Test-Path -LiteralPath $environmentPath -PathType Leaf)) {
        return
    }

    $allowedKeys = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($key in @(
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "INTRADAY_TELEGRAM_BOT_TOKEN",
        "INTRADAY_TELEGRAM_CHAT_ID",
        "OPENAI_API_KEY",
        "DAWNSTRIKE_OPENAI_MODEL",
        "DAWNSTRIKE_SCENARIO_INTELLIGENCE_ENABLED",
        "DAWNSTRIKE_SCENARIO_OPENAI_TIMEOUT_SECONDS",
        "DAWNSTRIKE_SCENARIO_MAX_ARTICLES_PER_RUN",
        "DAWNSTRIKE_SCENARIO_ARTICLE_MAX_CHARS",
        "DAWNSTRIKE_INDETERMINATE_RESEARCH_ENABLED",
        "DAWNSTRIKE_INDETERMINATE_RESEARCH_MAX_SYMBOLS",
        "DAWNSTRIKE_INDETERMINATE_RESEARCH_TIMEOUT_SECONDS",
        "DAWNSTRIKE_INDETERMINATE_RESEARCH_MAX_TOOL_CALLS",
        "DAWNSTRIKE_FORWARD_GAP_HMAC_KEY",
        "ALPACA_API_KEY_ID",
        "ALPACA_API_SECRET_KEY",
        "ALPACA_DATA_FEED",
        "INTRADAY_OUTCOME_CAPTURE_PROVIDER_ORDER",
        "POLYGON_API_KEY",
        "DATABENTO_API_KEY",
        "NEWS_API_KEY",
        "BENZINGA_API_KEY",
        "FINNHUB_API_KEY",
        "VERCEL_TOKEN",
        "VERCEL_ORG_ID",
        "VERCEL_PROJECT_ID"
    )) {
        [void]$allowedKeys.Add($key)
    }

    $lineNumber = 0
    foreach ($rawLine in Get-Content -LiteralPath $environmentPath) {
        $lineNumber += 1
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }

        $separator = $line.IndexOf("=")
        if ($separator -le 0) {
            throw "Malformed runtime environment entry at line $lineNumber."
        }
        $name = $line.Substring(0, $separator).Trim()
        if (-not $allowedKeys.Contains($name)) {
            continue
        }
        $value = $line.Substring($separator + 1).Trim()
        if (
            $value.Length -ge 2 -and
            (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))
            )
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw "Runtime environment key $name is empty."
        }

        $existing = [Environment]::GetEnvironmentVariable($name, "Process")
        if ([string]::IsNullOrWhiteSpace($existing)) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}
