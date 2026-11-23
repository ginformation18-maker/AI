@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Python venv missing. Run preflight_windows.ps1 first.
  exit /b 2
)
if not exist "runtime" mkdir "runtime"
echo Starting read-only MT5 agents. Discord webhook must be set in .env
".venv\Scripts\python.exe" -u -m mt5_agents.discord_observer --forever
set "CODE=%ERRORLEVEL%"
echo Observer exited with code %CODE%. Verify logs/state before retry. NEVER auto override a fatal stop.
exit /b %CODE%
