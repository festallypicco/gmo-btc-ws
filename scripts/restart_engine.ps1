# restart_engine.ps1
# --------------------------------------------
# trading_engine.py を安全に再起動するスクリプト
#
# タスクスケジューラ登録例（毎日 06:00）:
# schtasks /create /tn "GMOTradingEngineRestart" /sc daily /st 06:00 /tr "powershell -ExecutionPolicy Bypass -File C:\Users\tai_m\Cursor\Projects\gmo-btc-ws\scripts\restart_engine.ps1"

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Path $PSScriptRoot -Parent
$UtilsScriptPath = Join-Path $PSScriptRoot "engine_process_utils.ps1"
. "$UtilsScriptPath"
$LockPath = Join-Path $PSScriptRoot "restart_engine.lock"

$PidPath = Join-Path $ProjectRoot "runtime\trading_engine.pid"
$ManualStopFlagPath = Join-Path $ProjectRoot "runtime\manual_stop.flag"
$LogDir = Join-Path $ProjectRoot "log"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$RestartLogPath = Join-Path $LogDir ("restart_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

function Write-RestartLog {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet("INFO", "WARN", "ERROR")][string]$Level = "INFO"
    )
    $line = "[{0}] [{1}] [restart] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Write-Host $line
    Add-Content -Path $RestartLogPath -Value $line -Encoding UTF8
}

Write-RestartLog "project root: $ProjectRoot" -Level "INFO"

$exitCode = 0
$lockAcquired = $false

try {
    if (Test-Path -Path $LockPath) {
        $lockItem = Get-Item -Path $LockPath -ErrorAction Stop
        $lockAge = (Get-Date) - $lockItem.LastWriteTime

        if ($lockAge.TotalMinutes -le 5) {
            Write-RestartLog "Lock file is fresh (<=5m). Another restart_engine.ps1 may be running; aborting restart." -Level "WARN"
            $exitCode = 1
        }
        else {
            Write-RestartLog ("Removing stale lock file: {0}" -f $LockPath) -Level "WARN"
            Remove-Item -Path $LockPath -Force -ErrorAction Stop
        }
    }

    if ($exitCode -eq 0) {
        Set-Content -Path $LockPath -Value (Get-Date -Format "o") -Encoding UTF8
        $lockAcquired = $true

        $enginePids = @(Find-AllEngineProcesses)
        if ($enginePids.Count -ge 2) {
            Write-RestartLog ("複数の孤立プロセスを検出しました: PID {0}" -f (($enginePids | Sort-Object) -join ", ")) -Level "WARN"
        } elseif ($enginePids.Count -eq 1) {
            Write-RestartLog ("existing engine process detected: PID {0}" -f $enginePids[0]) -Level "INFO"
        } else {
            Write-RestartLog "no running engine process found. start from clean state." -Level "INFO"
        }

        if (-not (Stop-AllEngineProcesses)) {
            Write-RestartLog "failed to stop one or more existing engine processes. abort restart to prevent dual-run." -Level "ERROR"
            $exitCode = 1
        }

        if ($exitCode -eq 0) {
            $manualStopRequested = Test-Path -Path $ManualStopFlagPath
            if ($manualStopRequested) {
                Write-RestartLog ("manual stop flag detected: {0}" -f $ManualStopFlagPath) -Level "WARN"
            }

            if ($manualStopRequested) {
                Write-RestartLog "手動停止中のため起動をスキップしました" -Level "WARN"
            } else {
                Write-RestartLog "starting engine process..." -Level "INFO"
                # Spawn via scripts/process_launcher.py (append log redirection; cross-platform).
                Start-EngineProcess -ProjectRoot $ProjectRoot

                if (-not (Wait-EngineHealthy -ProjectRoot $ProjectRoot -TimeoutSec 15)) {
                    Write-RestartLog "engine failed to become healthy within 15s" -Level "ERROR"
                    $exitCode = 1
                }
                else {
                    $newPid = ""
                    if (Test-Path $PidPath) {
                        $newPid = (Get-Content -Path $PidPath -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
                    }
                    Write-RestartLog "engine restarted successfully (pid=$newPid)" -Level "INFO"
                }
            }
        }
    }
}
catch {
    Write-RestartLog ("restart_engine.ps1 exception: {0}" -f $_.Exception.Message) -Level "ERROR"
    $exitCode = 1
}
finally {
    if ($lockAcquired -and (Test-Path -Path $LockPath)) {
        Remove-Item -Path $LockPath -Force -ErrorAction SilentlyContinue
        Write-RestartLog "Lock file removed." -Level "INFO"
    }
}

exit $exitCode
