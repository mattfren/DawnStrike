param([string]$Date = (Get-Date -Format 'yyyy-MM-dd'))

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'omega_scheduler_common.ps1')

$Steps = @(
    @{ Name = 'market-session'; Arguments = @('-m', 'intraday_scanner.services.market_calendar', '--date', $Date) },
    @{ Name = 'forward-authoritative-producer'; Arguments = @('-m', 'intraday_scanner.v2.forward_autopilot', 'autopilot', '--date', $Date, '--no-fetch', '--require-authoritative-producer') },
    @{ Name = 'after-close'; Arguments = @('-m', 'intraday_scanner.v2.omega_sentinel', 'after-close', '--date', $Date, '--autodata', '--learn', '--market-masters') },
    @{ Name = 'telegram-after-close'; Arguments = @('-m', 'intraday_scanner.v2.telegram_intel', 'send', '--kind', 'after-close', '--date', $Date) }
)
$ExitCode = Invoke-OmegaSchedulerRun -CommandName 'after_close' -RunDate $Date -ScriptRoot $PSScriptRoot -Steps $Steps
exit $ExitCode
