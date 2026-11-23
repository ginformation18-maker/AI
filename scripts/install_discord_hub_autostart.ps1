# Run ONLY after local tests and successful Discord app/channel setup.
# Creates per-user Task Scheduler job at interactive Windows logon.
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Script = Join-Path $Root 'scripts\start_discord_hub_background.ps1'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw 'Missing .venv python' }
if (-not (Test-Path (Join-Path $Root '.env'))) { throw 'Missing .env' }
$TaskName = 'MT5 One-Channel Discord AI Observers (No Trades)'
$PS = (Get-Command powershell.exe).Source
$Args = '-NoProfile -ExecutionPolicy Bypass -File "' + $Script + '"'
$Action = New-ScheduledTaskAction -Execute $PS -Argument $Args -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 0) -RestartCount 0 -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'Read-only MT5 Demo and Discord single-channel discussion; no orders' -Force | Out-Null
Write-Host "Installed: $TaskName. Starts after interactive logon, only while this PC is powered on."
Write-Host 'Do NOT run the older discord_observer job at the same time (same archive).'
