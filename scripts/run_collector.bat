@echo off
setlocal
cd /d "%~dp0.."
rem Data collection ONLY; no trade execution and no infinite blind retry.
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0run_collector_supervised.ps1" %*
set "STATUS=%ERRORLEVEL%"
echo Collector terminated (exit %STATUS%). Inspect runtime\collector_logs before restarting.
exit /b %STATUS%
