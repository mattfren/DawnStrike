@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."

rem Research and paper audit only. This wrapper has no broker/order command.
if not defined DAWNSTRIKE_DB_PATH set "DAWNSTRIKE_DB_PATH=data\shadow_real.sqlite"
if not defined DAWNSTRIKE_ALPHAOPS_RECONCILIATION_OUT set "DAWNSTRIKE_ALPHAOPS_RECONCILIATION_OUT=outputs\strategy_reconciliation"
if not defined DAWNSTRIKE_RUN_PAPEROPS_FLEET set "DAWNSTRIKE_RUN_PAPEROPS_FLEET=1"

if "%~1"=="" (
  for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "RUN_DATE=%%I"
) else (
  set "RUN_DATE=%~1"
)

if not exist logs mkdir logs
if not exist "%DAWNSTRIKE_ALPHAOPS_RECONCILIATION_OUT%" mkdir "%DAWNSTRIKE_ALPHAOPS_RECONCILIATION_OUT%"

echo [%DATE% %TIME%] AlphaOps paper-only EOD started for %RUN_DATE%.
py -m intraday_scanner.services.market_calendar --date %RUN_DATE%
set "CALENDAR_EXIT=%ERRORLEVEL%"
if "%CALENDAR_EXIT%"=="10" (
  echo Scheduled market closure. Nothing to reconcile.
  exit /b 0
)
if not "%CALENDAR_EXIT%"=="0" (
  echo Market calendar unavailable or invalid.
  exit /b 1
)

set "BARS_ARG="
if defined DAWNSTRIKE_ALPHAOPS_EOD_BARS_CSV (
  if not exist "%DAWNSTRIKE_ALPHAOPS_EOD_BARS_CSV%" (
    echo Configured AlphaOps EOD bars are absent: %DAWNSTRIKE_ALPHAOPS_EOD_BARS_CSV%
    exit /b 2
  )
  set "BARS_ARG=--bars-csv "%DAWNSTRIKE_ALPHAOPS_EOD_BARS_CSV%""
) else (
  if exist "data\v2_autodata\normalized\canonical\%RUN_DATE%_canonical_intraday.csv" set "BARS_ARG=--bars-csv "data\v2_autodata\normalized\canonical\%RUN_DATE%_canonical_intraday.csv""
  if not defined BARS_ARG if exist "data\v2_autodata\normalized\canonical" set "BARS_ARG=--bars-csv "data\v2_autodata\normalized\canonical""
)

echo Reconciling only the exact proven delivered official_telegram cohort.
py -m intraday_scanner.cli alpha-paper-reconcile --db-path "%DAWNSTRIKE_DB_PATH%" --market-date %RUN_DATE% %BARS_ARG% --out-dir "%DAWNSTRIKE_ALPHAOPS_RECONCILIATION_OUT%" --persist
set "RECONCILE_EXIT=%ERRORLEVEL%"
if "%RECONCILE_EXIT%"=="2" (
  echo AlphaOps reconciliation is blocked or incomplete. Missing truth remains N/A.
  exit /b 2
)
if not "%RECONCILE_EXIT%"=="0" (
  echo AlphaOps reconciliation failed operationally.
  exit /b 1
)

echo Refreshing AlphaOps learning from activation and after-cost return labels.
py -m intraday_scanner.cli alpha-learn --db-path "%DAWNSTRIKE_DB_PATH%" --market-date %RUN_DATE%
set "LEARN_EXIT=%ERRORLEVEL%"
if "%LEARN_EXIT%"=="2" (
  echo AlphaOps learning gate is incomplete.
  exit /b 2
)
if not "%LEARN_EXIT%"=="0" (
  echo AlphaOps learning failed operationally.
  exit /b 1
)

py -m intraday_scanner.cli alpha-report --db-path "%DAWNSTRIKE_DB_PATH%" --out-dir outputs\alpha_report
if errorlevel 1 exit /b 1

if "%DAWNSTRIKE_RUN_PAPEROPS_FLEET%"=="1" (
  call scripts\run_paperops_fleet_eod.bat %RUN_DATE%
  if errorlevel 1 exit /b 1
)

echo [%DATE% %TIME%] AlphaOps paper-only EOD completed for %RUN_DATE%.
exit /b 0
