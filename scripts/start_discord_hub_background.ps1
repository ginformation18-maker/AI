# Windows interactive logon startup for one Discord bot + read-only MT5 observer.
# Never auto-retries a risk/manifest/credential failure, to preserve fail-closed.
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw 'Python environment missing' }
if (-not (Test-Path (Join-Path $Root '.env'))) { throw '.env missing' }
Start-Sleep -Seconds 30 # MT5 terminal initialization after Windows logon
$LogDir = Join-Path $Root 'runtime\agent_logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ('discord_hub_' + (Get-Date -Format yyyyMMdd) + '.log')
"$(Get-Date -Format o) Starting Discord hub read-only" | Out-File -FilePath $Log -Append -Encoding utf8
& $Python -u -m mt5_agents.discord_hub *>> $Log
if ($LASTEXITCODE -ne 0) { throw "Discord hub failed ($LASTEXITCODE), check $Log. No forced unsafe restart." }
