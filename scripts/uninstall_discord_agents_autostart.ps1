$ErrorActionPreference = 'Stop'
$TaskName = 'MT5 Discord Observation Agents (Demo Only)'
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed task: $TaskName. Running agent process (if any) must be stopped separately."
