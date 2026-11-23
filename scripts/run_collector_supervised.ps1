param(
    [ValidatePattern('^[A-Za-z0-9_.#-]{1,40}$')][string]$Symbol = 'XAUUSD',
    [ValidateSet(10,20,30,60,120)][int]$WindowSeconds = 60,
    [ValidateRange(0,12)][int]$MaxTransientRestarts = 8,
    [ValidateRange(10,300)][int]$RestartDelaySeconds = 30,
    [string]$TerminalPath = ''
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Archive = Join-Path $Root 'runtime\tick_archive'
$Logs = Join-Path $Root 'runtime\collector_logs'
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
if (-not (Test-Path $Python)) { throw 'Missing venv. Run: py -3.12 -m venv .venv' }
& $Python -m mt5_agents.parquet_preflight --archive-root $Archive
if ($LASTEXITCODE -ne 0) { throw 'Parquet preflight failed. NO COLLECTION ALLOWED.' }
$Attempts = 0
while ($true) {
    $LogFile = Join-Path $Logs ("collector_{0}_{1}.log" -f $Symbol, (Get-Date -Format 'yyyyMMdd'))
    $ArgList = @('-u','-m','mt5_agents.tick_collector','--symbol',$Symbol,'--window-seconds',"$WindowSeconds",'--forever')
    if ($TerminalPath) { $ArgList += @('--terminal-path',$TerminalPath) }
    ("{0} START attempt={1}" -f (Get-Date -Format 'o'),$Attempts) | Tee-Object -FilePath $LogFile -Append
    & $Python @ArgList 2>&1 | Tee-Object -FilePath $LogFile -Append
    $Code = $LASTEXITCODE
    ("{0} EXIT code={1}" -f (Get-Date -Format 'o'),$Code) | Tee-Object -FilePath $LogFile -Append
    if ($Code -eq 0) {
        Write-Host 'Collector exited normally; no auto-restart.'
        exit 0
    }
    # Even an abrupt crash can be retried, but only after an actual readback of
    # recent files, SQLite integrity_check, and checks for locks/orphans/temps.
    # This still cannot prove that the broker will return available ticks.
    & $Python -m mt5_agents.collector_resume_safety --archive-root $Archive
    if ($LASTEXITCODE -ne 0) {
        Write-Error 'UNSAFE restart state. Human inspection required; NEVER clear locks/manifest automatically.'
        exit 2
    }
    $Attempts++
    if ($Attempts -gt $MaxTransientRestarts) {
        Write-Error "Transient restart budget exhausted ($Attempts). NO further attempts; inspect data gaps."
        exit 75
    }
    Write-Warning "Collector failure (exit=$Code). Integrity preflight passed; bounded restart $Attempts/$MaxTransientRestarts in $RestartDelaySeconds sec. NO gap is deemed complete."
    Start-Sleep -Seconds $RestartDelaySeconds
    # Full Parquet preflight is repeated after any reconnect, never inherited from older run.
    & $Python -m mt5_agents.parquet_preflight --archive-root $Archive
    if ($LASTEXITCODE -ne 0) { throw 'Parquet preflight failed on reconnect. STOP.' }
}
