param(
    [string]$ProjectRoot = (Split-Path -Path $PSScriptRoot -Parent),
    [int]$StartupTimeoutSec = 15
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$UtilsScriptPath = Join-Path $PSScriptRoot "engine_process_utils.ps1"
. "$UtilsScriptPath"

$EngineScriptPath = Join-Path $ProjectRoot "trading_engine.py"
$LogDir = Join-Path $ProjectRoot "log"
$ManualStopFlagPath = Join-Path $ProjectRoot "manual_stop.flag"

if (-not (Test-Path $EngineScriptPath)) {
    Write-Host "[ERROR] trading_engine.py not found: $EngineScriptPath"
    exit 1
}

if (Test-Path $ManualStopFlagPath) {
    Write-Host ("[INFO] manual stop active. skipped startup (flag: {0})" -f $ManualStopFlagPath)
    exit 0
}

$runningPids = @(Find-AllEngineProcesses)
if ($runningPids.Count -ge 1) {
    Write-Host ("[INFO] existing trading_engine process is running; skipped startup (pid: {0})" -f (($runningPids | Sort-Object) -join ", "))
    exit 0
}

Write-Host "[INFO] starting trading_engine.py ..."
Start-EngineProcess -ProjectRoot $ProjectRoot
if (Wait-EngineHealthy -ProjectRoot $ProjectRoot -TimeoutSec $StartupTimeoutSec) {
    Write-Host "[INFO] engine startup confirmed (live_state.db updated)."
    exit 0
}

$logPath = Join-Path $LogDir ("engine_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
Write-Host "[ERROR] trading_engine startup timeout. live_state.db updated_at was not refreshed within $StartupTimeoutSec seconds."
Write-Host "[ERROR] check engine log: $logPath"
exit 1
