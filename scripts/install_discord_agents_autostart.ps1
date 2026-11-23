# User confirmation is required: installs a per-user task on next interactive logon.
# It does NOT create or enable any order-execution job.
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Script = Join-Path $Root 'scripts\start_discord_agents_background.ps1'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw 'Missing virtualenv python. Run preflight first.' }
if (-not (Test-Path (Join-Path $Root '.env'))) { throw 'Missing .env' }
$TaskName = 'MT5 Discord Observation Agents (Demo Only)'
$PS = (Get-Command powershell.exe).Source
$Args = '-NoProfile -ExecutionPolicy Bypass -File "' + $Script + '"'
$Action = New-ScheduledTaskAction -Execute $PS -Argument $Args -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 0) -RestartCount 0 -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'Read-only MT5 Demo observation and Discord notifications; no trading orders' -Force | Out-Null
Write-Host "Task installed: $TaskName (runs when this user logs on; NOT a cloud process)"
Write-Host 'Start it manually from Task Scheduler or sign out and sign in.'
