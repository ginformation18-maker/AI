$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw 'Create venv first: py -3.12 -m venv .venv' }
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m mt5_agents.parquet_preflight --archive-root (Join-Path $Root 'runtime\tick_archive')
if ($LASTEXITCODE -ne 0) { Write-Error 'Real Parquet round-trip FAILED; DO NOT START COLLECTOR.'; exit 2 }
& $Python -m pytest tests -q
if ($LASTEXITCODE -ne 0) { Write-Error 'Test suite FAILED; DO NOT START COLLECTOR.'; exit 2 }
Write-Host 'LOCAL PRE-FLIGHT PASSED (real Parquet read/write). MT5 broker data still NOT validated.'
