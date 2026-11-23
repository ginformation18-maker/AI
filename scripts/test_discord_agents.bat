@echo off
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m mt5_agents.discord_observer --test-discord
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m mt5_agents.discord_observer --once
