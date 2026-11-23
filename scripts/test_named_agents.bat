@echo off
setlocal
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m mt5_agents.discord_agent_webhook --test
exit /b %ERRORLEVEL%
