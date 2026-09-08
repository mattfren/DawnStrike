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
        "DAWNSTRIKE_STRATEGY_EVIDENCE_ENABLED",
        "DAWNSTRIKE_STRATEGY_EVIDENCE_SHADOW_ONLY",
        "DAWNSTRIKE_STRATEGY_EVIDENCE_MAX_CANDIDATES",
        # Bootstrap paper mode and the liquid-universe override are operator
        # switches read by the Python layer.  A key absent from this allowlist is
        # silently skipped below, so omitting them would leave both features
        # permanently unreachable in the runtime no matter what runtime.env says.
        "DAWNSTRIKE_BOOTSTRAP_PAPER_MODE",
        "DAWNSTRIKE_ALPHAOPS_LIQUID_UNIVERSE",
        # Alpaca PAPER execution switches and risk limits, read by
        # intraday_scanner/execution.  PAPER only: there is no live-trading key
        # here and the adapter cannot construct a live URL.  Omitting any of
        # these would leave the corresponding control unreachable.
        "DAWNSTRIKE_PAPER_EXECUTION_ENABLED",
        "DAWNSTRIKE_PAPER_ENTRIES_ENABLED",
        "DAWNSTRIKE_PAPER_KILL_SWITCH",
        "DAWNSTRIKE_PAPER_KILL_SWITCH_ENGAGED",
        "DAWNSTRIKE_PAPER_RISK_PCT",
        "DAWNSTRIKE_PAPER_MAX_POSITION_PCT",
        "DAWNSTRIKE_PAPER_MAX_CONCURRENT",
        "DAWNSTRIKE_PAPER_MAX_ENTRIES_PER_DAY",
        "DAWNSTRIKE_PAPER_DAILY_LOSS_LIMIT_PCT",
        "DAWNSTRIKE_PAPER_MAX_STALENESS_SECONDS",
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
