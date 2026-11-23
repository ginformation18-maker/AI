@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Missing virtualenv. Run preflight_windows.ps1 first.
  exit /b 2
)
echo Starting read-only MT5 AI discussion hub in one Discord channel.
echo NOTE: This bot never places orders. MODE must be DRY_RUN, account must be DEMO.
".venv\Scripts\python.exe" -u -m mt5_agents.discord_hub
exit /b %ERRORLEVEL%
