param([string]$Date = (Get-Date -Format 'yyyy-MM-dd'))

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'omega_scheduler_common.ps1')
$Steps = @(
    @{ Name = 'market-session'; Arguments = @('-m', 'intraday_scanner.services.market_calendar', '--date', $Date) },
    @{ Name = 'watchdog'; Arguments = @('-m', 'intraday_scanner.v2.autonomous_runner', 'watchdog') },
    @{ Name = 'telegram-watchdog'; Arguments = @('-m', 'intraday_scanner.v2.telegram_intel', 'send', '--kind', 'watchdog', '--date', $Date) }
)
$ExitCode = Invoke-OmegaSchedulerRun -CommandName 'watchdog' -RunDate $Date -ScriptRoot $PSScriptRoot -Steps $Steps
exit $ExitCode
