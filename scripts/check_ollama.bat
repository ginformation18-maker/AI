@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv not present
  exit /b 2
)
".venv\Scripts\python.exe" -m mt5_agents.ollama_preflight
exit /b %ERRORLEVEL%
