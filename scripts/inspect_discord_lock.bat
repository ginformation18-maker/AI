@echo off
setlocal
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m mt5_agents.discord_lock_health %*
exit /b %ERRORLEVEL%
