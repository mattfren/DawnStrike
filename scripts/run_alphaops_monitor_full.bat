@echo off
setlocal EnableExtensions
set "SOURCE_ROOT=%~1"
if not defined SOURCE_ROOT set "SOURCE_ROOT=%DAWNSTRIKE_ALPHAOPS_SOURCE_ROOT%"
if not defined SOURCE_ROOT (
  echo DAWNSTRIKE_ALPHAOPS_SOURCE_ROOT is required for the upstream AlphaOps engine.
  exit /b 2
)
for %%I in ("%SOURCE_ROOT%") do set "SOURCE_ROOT=%%~fI"
set "LEGACY_RUNNER=%SOURCE_ROOT%\scripts\run_alphaops_monitor_full.bat"
if not exist "%LEGACY_RUNNER%" (
  echo Legacy AlphaOps monitor not found: %LEGACY_RUNNER%
  exit /b 2
)
if /I "%LEGACY_RUNNER%"=="%~f0" (
  echo Refusing to recurse into the owned AlphaOps monitor wrapper.
  exit /b 2
)
pushd "%SOURCE_ROOT%"
call "%LEGACY_RUNNER%"
set "EXITCODE=%ERRORLEVEL%"
popd
exit /b %EXITCODE%
