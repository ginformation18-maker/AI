# Task Scheduler entry point: logs output and returns nonzero on unsafe failure.
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw 'Missing .venv python executable' }
$LogDir = Join-Path $Root 'runtime\agent_logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ('discord_agents_' + (Get-Date -Format yyyyMMdd) + '.log')
"$(Get-Date -Format o) Starting supervised observer (no order execution)" | Out-File -FilePath $Log -Encoding utf8 -Append
& $Python -u -m mt5_agents.discord_observer --forever *>> $Log
if ($LASTEXITCODE -ne 0) { throw "Observer failed exit=$LASTEXITCODE. Inspect $Log; no automatic unsafe resume." }
