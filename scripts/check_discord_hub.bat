@echo off
setlocal
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m mt5_agents.discord_hub --check-config
if errorlevel 1 exit /b %ERRORLEVEL%
".venv\Scripts\python.exe" -m mt5_agents.ollama_preflight
if errorlevel 1 exit /b %ERRORLEVEL%
".venv\Scripts\python.exe" -m mt5_agents.parquet_preflight --archive-root runtime\tick_archive
if errorlevel 1 exit /b %ERRORLEVEL%
".venv\Scripts\python.exe" -m mt5_agents.collector_resume_safety --archive-root runtime\tick_archive
exit /b %ERRORLEVEL%
