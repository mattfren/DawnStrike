@echo off
setlocal EnableExtensions

cd /d "%~dp0\.."

if not defined DAWNSTRIKE_PAPER_OPS_ROOT set "DAWNSTRIKE_PAPER_OPS_ROOT=data\v2_paper_ops_live"
if not defined DAWNSTRIKE_DB_PATH set "DAWNSTRIKE_DB_PATH=data\shadow_real.sqlite"
if not defined DAWNSTRIKE_PAPEROPS_NOTIFY set "DAWNSTRIKE_PAPEROPS_NOTIFY=telegram"
if not defined DAWNSTRIKE_PAPEROPS_MAX_ATTEMPTS set "DAWNSTRIKE_PAPEROPS_MAX_ATTEMPTS=3"

if "%~1"=="" (
  for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "RUN_DATE=%%I"
) else (
  set "RUN_DATE=%~1"
)

if /I not "%DAWNSTRIKE_PAPEROPS_NOTIFY%"=="telegram" if /I not "%DAWNSTRIKE_PAPEROPS_NOTIFY%"=="console" (
  echo Invalid DAWNSTRIKE_PAPEROPS_NOTIFY. Expected telegram or console.
  exit /b 2
)

echo [%DATE% %TIME%] Running date-scoped PaperOps fleet for %RUN_DATE%.
py -m intraday_scanner.v2.paper_ops run-day --date %RUN_DATE% --mode forward --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
if errorlevel 1 goto :TRUTH_FAILED

py -m intraday_scanner.v2.paper_ops reconcile --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
if errorlevel 1 goto :TRUTH_FAILED

py -m intraday_scanner.v2.paper_ops verify-calendar --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
if errorlevel 1 goto :TRUTH_FAILED

py -m intraday_scanner.v2.paper_ops rebuild-ledger --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
if errorlevel 1 goto :TRUTH_FAILED

py -m intraday_scanner.v2.paper_ops verify-source-bars --mode forward --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
if errorlevel 1 goto :TRUTH_FAILED

py -m intraday_scanner.v2.paper_ops blotter --date %RUN_DATE% --mode forward --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
if errorlevel 1 goto :TRUTH_FAILED

py -m intraday_scanner.v2.paper_ops verify-blotter --mode forward --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
if errorlevel 1 goto :TRUTH_FAILED

py -m intraday_scanner.v2.paper_ops evidence --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
if errorlevel 1 goto :TRUTH_FAILED

echo [%DATE% %TIME%] Writing the date-scoped strategy fleet report.
py -m intraday_scanner.cli strategy-fleet-report --db-path "%DAWNSTRIKE_DB_PATH%" --paper-ops-root "%DAWNSTRIKE_PAPER_OPS_ROOT%" --out-dir outputs\strategy_fleet --start %RUN_DATE% --end %RUN_DATE%
set "FLEET_EXIT=%ERRORLEVEL%"
if "%FLEET_EXIT%"=="0" goto :SEND_DIGEST
if "%FLEET_EXIT%"=="2" goto :SEND_DIGEST
echo Strategy fleet report failed; digest delivery is blocked.
goto :TRUTH_FAILED

:SEND_DIGEST
echo [%DATE% %TIME%] Sending verified PaperOps fleet digest through the durable outbox.
py -m intraday_scanner.cli strategy-fleet-telegram --date %RUN_DATE% --db-path "%DAWNSTRIKE_DB_PATH%" --paper-ops-root "%DAWNSTRIKE_PAPER_OPS_ROOT%" --fleet-report outputs\strategy_fleet\strategy_fleet_report.json --notify %DAWNSTRIKE_PAPEROPS_NOTIFY% --max-attempts %DAWNSTRIKE_PAPEROPS_MAX_ATTEMPTS%
if errorlevel 1 (
  echo PaperOps fleet delivery failed; durable outbox remains available for retry.
  exit /b 1
)

echo [%DATE% %TIME%] PaperOps fleet EOD completed for %RUN_DATE%.
exit /b 0

:TRUTH_FAILED
echo PaperOps fleet truth gate failed; no digest was sent.
exit /b 1
