param([string]$Date = (Get-Date -Format 'yyyy-MM-dd'))

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'omega_scheduler_common.ps1')

$Steps = @(
    @{ Name = 'market-session'; Arguments = @('-m', 'intraday_scanner.services.market_calendar', '--date', $Date) },
    @{ Name = 'alpha-producer'; Arguments = @('-m', 'intraday_scanner.v2.alpha_lab', 'demo') },
    @{ Name = 'forward-authoritative-producer'; Arguments = @('-m', 'intraday_scanner.v2.forward_autopilot', 'autopilot', '--date', $Date, '--no-fetch', '--require-authoritative-producer') },
    @{ Name = 'morning-check'; Arguments = @('-m', 'intraday_scanner.v2.omega_sentinel', 'morning-check', '--date', $Date, '--autodata', '--learn', '--market-masters') },
    @{ Name = 'telegram-morning'; Arguments = @('-m', 'intraday_scanner.v2.telegram_intel', 'send', '--kind', 'morning', '--date', $Date) }
)
$ExitCode = Invoke-OmegaSchedulerRun -CommandName 'morning_check' -RunDate $Date -ScriptRoot $PSScriptRoot -Steps $Steps
exit $ExitCode
