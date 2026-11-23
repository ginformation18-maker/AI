param([switch]$Apply)
# Windows settings are NEVER changed by preflight or the .bat launcher.
# Use only on a plugged-in dedicated demo collection PC after user review.
if (-not $Apply) {
    Write-Host 'DRY RUN: Use -Apply to disable AC sleep, AC hibernate timeout and AC disk idle timeout.'
    Write-Host 'Power plan query:'
    powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE
    exit 0
}
Write-Warning 'Changing AC power policy on THIS machine. Will NOT disable Windows updates.'
powercfg /change standby-timeout-ac 0
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
powercfg /change hibernate-timeout-ac 0
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
powercfg /change disk-timeout-ac 0
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host 'AC sleep/disk idle disabled. Windows Update: configure active hours and maintenance window separately.'
