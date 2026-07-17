@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."

if not defined DAWNSTRIKE_PAPER_OPS_ROOT set "DAWNSTRIKE_PAPER_OPS_ROOT=data\v2_paper_ops_live"
if not defined DAWNSTRIKE_PAPEROPS_MAX_ATTEMPTS set "DAWNSTRIKE_PAPEROPS_MAX_ATTEMPTS=12"
if not defined DAWNSTRIKE_PAPEROPS_RETRY_DELAY_SECONDS set "DAWNSTRIKE_PAPEROPS_RETRY_DELAY_SECONDS=300"
powershell -NoProfile -Command "$attempts=0; $delay=0; if (-not [int]::TryParse($env:DAWNSTRIKE_PAPEROPS_MAX_ATTEMPTS, [ref]$attempts) -or $attempts -lt 1 -or $attempts -gt 12 -or -not [int]::TryParse($env:DAWNSTRIKE_PAPEROPS_RETRY_DELAY_SECONDS, [ref]$delay) -or $delay -lt 1 -or $delay -gt 1800) { exit 2 }"
if errorlevel 1 (
  echo Invalid PaperOps retry policy. Attempts must be 1-12 and delay must be 1-1800 seconds.
  exit /b 2
)

if not exist logs mkdir logs
if not exist outputs\daily_movers mkdir outputs\daily_movers
if not exist outputs\daily_review mkdir outputs\daily_review
if not exist outputs\alpha_report mkdir outputs\alpha_report
if not exist outputs\return_attribution mkdir outputs\return_attribution
if not exist outputs\historical_report mkdir outputs\historical_report
if not exist outputs\calendar_report mkdir outputs\calendar_report
if not exist outputs\strategy_reconciliation mkdir outputs\strategy_reconciliation
if not exist outputs\strategy_fleet mkdir outputs\strategy_fleet

set EXITCODE=0
set OUTCOME_CAPTURE_OK=1
set PAPER_RECONCILIATION_OK=1
set DAILY_MOVERS_OK=1
set DAILY_REVIEW_OK=1
set PAPEROPS_FORWARD_OK=1
set PAPEROPS_VERIFY_OK=1
set PAPEROPS_SOURCE_TRUTH_OK=1
set PAPEROPS_SHADOW_OK=1
set PAPEROPS_SHADOW_ATTEMPTED=0
set POST_SHADOW_TRUTH_OK=1
set PAPEROPS_BLOTTER_OK=1
set CHALLENGER_EVAL_OK=1
set PAPEROPS_EVIDENCE_OK=1
set FLEET_REPORT_OK=1
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set RUN_DATE=%%I

echo [%DATE% %TIME%] Dawnstrike AlphaOps EOD full report started.

py -m intraday_scanner.services.market_calendar --date %RUN_DATE%
set CALENDAR_EXIT=%ERRORLEVEL%
if "%CALENDAR_EXIT%"=="10" (
  echo [%DATE% %TIME%] EOD chain skipped: scheduled US equities market closure.
  exit /b 0
)
if not "%CALENDAR_EXIT%"=="0" (
  echo [%DATE% %TIME%] EOD chain blocked: market calendar unavailable or invalid.
  exit /b %CALENDAR_EXIT%
)

echo [%DATE% %TIME%] Capturing sourced AlphaOps outcomes.
py -m intraday_scanner.cli alpha-capture-outcomes --db-path data\shadow_real.sqlite --market-date %RUN_DATE% --out-dir outputs\alpha_outcomes\%RUN_DATE% --persist
if errorlevel 1 (
  echo [%DATE% %TIME%] Outcome capture incomplete or failed; learning will be blocked.
  set EXITCODE=1
  set OUTCOME_CAPTURE_OK=0
)

if "%OUTCOME_CAPTURE_OK%"=="1" (
  echo [%DATE% %TIME%] Reconciling exact AlphaOps selections into paper trades.
  py -m intraday_scanner.cli alpha-paper-reconcile --db-path data\shadow_real.sqlite --market-date %RUN_DATE% --out-dir outputs\strategy_reconciliation --persist
  if errorlevel 1 (
    echo AlphaOps paper reconciliation failed or left unresolved selections.
    set EXITCODE=1
    set PAPER_RECONCILIATION_OK=0
  )
) else (
  echo AlphaOps paper reconciliation blocked because sourced outcome capture did not complete.
  set PAPER_RECONCILIATION_OK=0
)

echo [%DATE% %TIME%] Running the seven-strategy daily PaperOps fleet.
call :RUN_PAPEROPS_FORWARD_WITH_RETRY
if errorlevel 1 (
  echo Daily PaperOps fleet failed; strategy comparison evidence is incomplete.
  set EXITCODE=1
  set PAPEROPS_FORWARD_OK=0
)

if "%PAPEROPS_FORWARD_OK%"=="1" (
  echo [%DATE% %TIME%] Establishing pre-shadow PaperOps truth gates.
  py -m intraday_scanner.v2.paper_ops reconcile --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo Pre-shadow PaperOps reconciliation failed.
    set EXITCODE=1
    set PAPEROPS_VERIFY_OK=0
  )
  py -m intraday_scanner.v2.paper_ops verify-calendar --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo Pre-shadow PaperOps calendar truth failed.
    set EXITCODE=1
    set PAPEROPS_VERIFY_OK=0
  )
  py -m intraday_scanner.v2.paper_ops rebuild-ledger --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo Pre-shadow PaperOps ledger rebuild verification failed.
    set EXITCODE=1
    set PAPEROPS_VERIFY_OK=0
  )
  py -m intraday_scanner.v2.paper_ops verify-source-bars --mode forward --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo Pre-shadow PaperOps immutable source-bar truth failed.
    set EXITCODE=1
    set PAPEROPS_VERIFY_OK=0
    set PAPEROPS_SOURCE_TRUTH_OK=0
  )
) else (
  set PAPEROPS_VERIFY_OK=0
  set PAPEROPS_SHADOW_OK=0
  set POST_SHADOW_TRUTH_OK=0
  set PAPEROPS_BLOTTER_OK=0
  set CHALLENGER_EVAL_OK=0
  set PAPEROPS_SOURCE_TRUTH_OK=0
)

if "%PAPEROPS_VERIFY_OK%"=="1" (
  echo [%DATE% %TIME%] Running frozen paper-only shadow challengers on champion lineage.
  py -m intraday_scanner.v2.paper_ops shadow-init --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo Shadow challenger registry initialization failed.
    set EXITCODE=1
    set PAPEROPS_SHADOW_OK=0
    set PAPEROPS_VERIFY_OK=0
    set POST_SHADOW_TRUTH_OK=0
    set PAPEROPS_BLOTTER_OK=0
  )
) else (
  echo Shadow challenger execution blocked because pre-shadow truth gates failed.
  set PAPEROPS_SHADOW_OK=0
  set POST_SHADOW_TRUTH_OK=0
  set PAPEROPS_BLOTTER_OK=0
)

if "%PAPEROPS_SHADOW_OK%"=="1" (
  set PAPEROPS_SHADOW_ATTEMPTED=1
  py -m intraday_scanner.v2.paper_ops shadow-run --date %RUN_DATE% --mode forward --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo Frozen shadow challenger execution failed.
    set EXITCODE=1
    set PAPEROPS_SHADOW_OK=0
    set PAPEROPS_VERIFY_OK=0
    set POST_SHADOW_TRUTH_OK=0
  )
)

if "%PAPEROPS_SHADOW_ATTEMPTED%"=="1" (
  echo [%DATE% %TIME%] Rebuilding truth after shadow evidence writes.
  py -m intraday_scanner.v2.paper_ops reconcile --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo Post-shadow PaperOps reconciliation failed.
    set EXITCODE=1
    set PAPEROPS_VERIFY_OK=0
    set POST_SHADOW_TRUTH_OK=0
  )
  py -m intraday_scanner.v2.paper_ops verify-calendar --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo Post-shadow PaperOps calendar truth failed.
    set EXITCODE=1
    set PAPEROPS_VERIFY_OK=0
    set POST_SHADOW_TRUTH_OK=0
  )
  py -m intraday_scanner.v2.paper_ops rebuild-ledger --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo Post-shadow PaperOps ledger rebuild verification failed.
    set EXITCODE=1
    set PAPEROPS_VERIFY_OK=0
    set POST_SHADOW_TRUTH_OK=0
  )
  py -m intraday_scanner.v2.paper_ops verify-source-bars --mode forward --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo Post-shadow PaperOps immutable source-bar truth failed.
    set EXITCODE=1
    set PAPEROPS_VERIFY_OK=0
    set PAPEROPS_SOURCE_TRUTH_OK=0
    set POST_SHADOW_TRUTH_OK=0
  )
)

if "%POST_SHADOW_TRUTH_OK%"=="1" (
  echo [%DATE% %TIME%] Building and verifying exact PaperOps trade lifecycle blotter.
  py -m intraday_scanner.v2.paper_ops blotter --date %RUN_DATE% --mode forward --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo PaperOps trade blotter build failed.
    set EXITCODE=1
    set PAPEROPS_BLOTTER_OK=0
  )
  py -m intraday_scanner.v2.paper_ops verify-blotter --mode forward --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo PaperOps trade blotter verification failed.
    set EXITCODE=1
    set PAPEROPS_BLOTTER_OK=0
  )
) else (
  echo PaperOps trade blotter blocked because post-shadow truth is incomplete.
  set PAPEROPS_BLOTTER_OK=0
)

if "%POST_SHADOW_TRUTH_OK%"=="1" if "%PAPEROPS_BLOTTER_OK%"=="1" (
  py -m intraday_scanner.v2.paper_ops challenger-evaluate --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo Challenger evaluation failed its operational evidence gates.
    set EXITCODE=1
    set CHALLENGER_EVAL_OK=0
  )
) else (
  echo Challenger evaluation blocked because post-shadow truth or lifecycle blotter is incomplete.
  set CHALLENGER_EVAL_OK=0
)

if "%POST_SHADOW_TRUTH_OK%"=="1" if "%PAPEROPS_BLOTTER_OK%"=="1" (
  py -m intraday_scanner.v2.paper_ops evidence --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
  if errorlevel 1 (
    echo Daily strategy evidence scoring failed.
    set EXITCODE=1
    set PAPEROPS_EVIDENCE_OK=0
  )
) else (
  set PAPEROPS_EVIDENCE_OK=0
)

echo [%DATE% %TIME%] Writing the horizon-separated strategy fleet report.
py -m intraday_scanner.cli strategy-fleet-report --db-path data\shadow_real.sqlite --paper-ops-root "%DAWNSTRIKE_PAPER_OPS_ROOT%" --out-dir outputs\strategy_fleet --start %RUN_DATE% --end %RUN_DATE%
set FLEET_EXIT=%ERRORLEVEL%
if "%FLEET_EXIT%"=="2" echo Strategy fleet report is partial; Telegram digest will apply its sourced no-signal gate.
if not "%FLEET_EXIT%"=="0" if not "%FLEET_EXIT%"=="2" (
  echo Strategy fleet report failed; comparison evidence is unavailable.
  set EXITCODE=1
  set FLEET_REPORT_OK=0
)

set PAPEROPS_DIGEST_READY=1
if not "%PAPEROPS_FORWARD_OK%"=="1" set PAPEROPS_DIGEST_READY=0
if not "%PAPEROPS_VERIFY_OK%"=="1" set PAPEROPS_DIGEST_READY=0
if not "%PAPEROPS_SOURCE_TRUTH_OK%"=="1" set PAPEROPS_DIGEST_READY=0
if not "%PAPEROPS_SHADOW_OK%"=="1" set PAPEROPS_DIGEST_READY=0
if not "%POST_SHADOW_TRUTH_OK%"=="1" set PAPEROPS_DIGEST_READY=0
if not "%PAPEROPS_BLOTTER_OK%"=="1" set PAPEROPS_DIGEST_READY=0
if not "%CHALLENGER_EVAL_OK%"=="1" set PAPEROPS_DIGEST_READY=0
if not "%PAPEROPS_EVIDENCE_OK%"=="1" set PAPEROPS_DIGEST_READY=0
if not "%FLEET_REPORT_OK%"=="1" set PAPEROPS_DIGEST_READY=0

if "%PAPEROPS_DIGEST_READY%"=="1" (
  echo [%DATE% %TIME%] Sending verified forward PaperOps fleet digest through the durable outbox.
  py -m intraday_scanner.cli strategy-fleet-telegram --date %RUN_DATE% --db-path data\shadow_real.sqlite --paper-ops-root "%DAWNSTRIKE_PAPER_OPS_ROOT%" --fleet-report outputs\strategy_fleet\strategy_fleet_report.json --notify telegram --max-attempts 3
  if errorlevel 1 (
    echo PaperOps fleet Telegram delivery failed; durable outbox remains available for retry.
    set EXITCODE=1
  )
) else (
  echo PaperOps fleet Telegram blocked because forward, shadow, challenger, truth, evidence, or fleet-report gates did not complete.
)

echo [%DATE% %TIME%] Refreshing the read-only operator dashboard from canonical PaperOps truth.
py -m intraday_scanner.v2.command_center_x3 build --repo-root . --output-root data\v2_command_center_x3
if errorlevel 1 (
  echo Operator dashboard refresh failed.
  set EXITCODE=1
)

echo [%DATE% %TIME%] Collecting daily movers.
py -m intraday_scanner.cli collect-daily-movers --date %RUN_DATE% --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\daily_movers --persist
if errorlevel 1 (
  echo Daily movers step failed; dependent review stages will be blocked.
  set EXITCODE=1
  set DAILY_MOVERS_OK=0
)

if "%DAILY_MOVERS_OK%"=="1" (
  echo [%DATE% %TIME%] Running day review.
  py -m intraday_scanner.cli daily-review --date %RUN_DATE% --db-path data\shadow_real.sqlite --out-dir outputs\daily_review --persist
  if errorlevel 1 (
    echo Daily review step failed; dependent backfeed and notification stages will be blocked.
    set EXITCODE=1
    set DAILY_REVIEW_OK=0
  )
) else (
  echo Daily review blocked because daily movers did not complete.
  set DAILY_REVIEW_OK=0
)

if "%DAILY_REVIEW_OK%"=="1" (
  echo [%DATE% %TIME%] Creating learning backfeed proposals.
  py -m intraday_scanner.cli daily-review-learn --date %RUN_DATE% --db-path data\shadow_real.sqlite --out-dir outputs\daily_review --persist
  if errorlevel 1 (
    echo Daily review learning step failed.
    set EXITCODE=1
  )
) else (
  echo Daily review learning blocked because the daily review did not complete.
)

py -m intraday_scanner.cli attribute-returns --db-path data\shadow_real.sqlite --out-dir outputs\return_attribution --persist
if errorlevel 1 set EXITCODE=1

py -m intraday_scanner.cli historical-report --db-path data\shadow_real.sqlite --out-dir outputs\historical_report
if errorlevel 1 set EXITCODE=1

py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
if errorlevel 1 set EXITCODE=1

if /i "%DAWNSTRIKE_DAILY_REVIEW_NOTIFY%"=="telegram" (
  if "%DAILY_REVIEW_OK%"=="1" (
    py -m intraday_scanner.cli daily-review-telegram --date %RUN_DATE% --db-path data\shadow_real.sqlite --notify telegram
    if errorlevel 1 echo Daily review Telegram step skipped or failed. Continuing EOD chain.
  ) else (
    echo Daily review Telegram blocked because the daily review did not complete.
  )
)

py -m intraday_scanner.cli calendar-report --db-path data\shadow_real.sqlite --out-dir outputs\calendar_report
if errorlevel 1 set EXITCODE=1

if "%OUTCOME_CAPTURE_OK%"=="1" (
  if "%PAPER_RECONCILIATION_OK%"=="1" (
    py -m intraday_scanner.cli alpha-learn --db-path data\shadow_real.sqlite
    if errorlevel 1 (
      echo Alpha learning skipped or failed. Check output above.
      set EXITCODE=1
    )
  ) else (
    echo Alpha learning blocked because paper reconciliation did not complete.
  )
) else (
  echo Alpha learning blocked because sourced outcome capture did not complete.
)

echo [%DATE% %TIME%] Dawnstrike AlphaOps EOD full report finished with code %EXITCODE%.
exit /b %EXITCODE%

:RUN_PAPEROPS_FORWARD_WITH_RETRY
setlocal EnableExtensions EnableDelayedExpansion
set /a PAPEROPS_ATTEMPT=1

:PAPEROPS_FORWARD_RETRY_LOOP
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set PAPEROPS_CURRENT_DATE=%%I
if not "!PAPEROPS_CURRENT_DATE!"=="%RUN_DATE%" (
  echo [%DATE% %TIME%] PaperOps retry blocked because the market date changed from %RUN_DATE% to !PAPEROPS_CURRENT_DATE!.
  endlocal & exit /b 3
)
echo [%DATE% %TIME%] PaperOps forward attempt !PAPEROPS_ATTEMPT!/%DAWNSTRIKE_PAPEROPS_MAX_ATTEMPTS% for %RUN_DATE%.
py -m intraday_scanner.v2.paper_ops run-day --date %RUN_DATE% --mode forward --output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"
set PAPEROPS_ATTEMPT_EXIT=!ERRORLEVEL!
if "!PAPEROPS_ATTEMPT_EXIT!"=="0" endlocal & exit /b 0
if !PAPEROPS_ATTEMPT! GEQ %DAWNSTRIKE_PAPEROPS_MAX_ATTEMPTS% (
  echo [%DATE% %TIME%] PaperOps forward retries exhausted with code !PAPEROPS_ATTEMPT_EXIT!.
  endlocal & exit /b !PAPEROPS_ATTEMPT_EXIT!
)
echo [%DATE% %TIME%] PaperOps forward data is not ready; retrying the same date in %DAWNSTRIKE_PAPEROPS_RETRY_DELAY_SECONDS% seconds.
timeout /t %DAWNSTRIKE_PAPEROPS_RETRY_DELAY_SECONDS% /nobreak >nul
set /a PAPEROPS_ATTEMPT+=1
goto :PAPEROPS_FORWARD_RETRY_LOOP
