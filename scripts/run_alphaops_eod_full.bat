@echo off
setlocal EnableExtensions

rem The legacy AlphaOps engine is an explicit upstream dependency. This owned
rem wrapper records its real exit status for the canonical finalize chain.
set "SOURCE_ROOT=%~1"
if not defined SOURCE_ROOT set "SOURCE_ROOT=%DAWNSTRIKE_ALPHAOPS_SOURCE_ROOT%"
if not defined SOURCE_ROOT (
  echo DAWNSTRIKE_ALPHAOPS_SOURCE_ROOT is required for the upstream AlphaOps engine.
  exit /b 2
)
for %%I in ("%SOURCE_ROOT%") do set "SOURCE_ROOT=%%~fI"
for %%I in ("%~dp0..") do set "OWNED_ROOT=%%~fI"
if /I "%SOURCE_ROOT%"=="%OWNED_ROOT%" (
  echo Refusing to recurse into the owned AlphaOps wrapper without an explicit legacy source root.
  exit /b 2
)
set "LEGACY_RUNNER=%SOURCE_ROOT%\scripts\run_alphaops_eod_full.bat"
if not exist "%LEGACY_RUNNER%" (
  echo Legacy AlphaOps runner not found: %LEGACY_RUNNER%
  exit /b 2
)
pushd "%SOURCE_ROOT%"
call "%LEGACY_RUNNER%"
set "EXITCODE=%ERRORLEVEL%"
popd

set "STATUS=failed"
if "%EXITCODE%"=="0" set "STATUS=complete"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "RUN_DATE=%%I"
py "%~dp0record_automation_stage.py" ^
  --db-path "%SOURCE_ROOT%\data\shadow_real.sqlite" ^
  --market-date "%RUN_DATE%" ^
  --status "%STATUS%" ^
  --exit-code "%EXITCODE%" ^
  --source-root "%SOURCE_ROOT%" ^
  --out-dir "outputs\alpha_report"
exit /b %EXITCODE%
