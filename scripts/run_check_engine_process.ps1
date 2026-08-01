param()

# Task Scheduler registration (hourly, same cadence family as anomaly check).
# LogonType=Password = run whether user is logged on or not:
#
#   schtasks /Create /TN "BTC_Engine_Process_Check" /SC HOURLY /MO 1 `
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File `"C:\Users\tai_m\Cursor\Projects\gmo-btc-ws\scripts\run_check_engine_process.ps1`"" `
#     /RU "%USERDOMAIN%\%USERNAME%" /RP "<WindowsPassword>" /RL LIMITED /F
#
# Verify:
#   schtasks /Query /TN "BTC_Engine_Process_Check" /XML | findstr LogonType

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$LockPath = Join-Path $ScriptDir "run_check_engine_process.lock"
$LogDir = Join-Path $ProjectRoot "log"
$LogPath = Join-Path $LogDir ("engine_process_check_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
$CheckScriptPath = Join-Path $ScriptDir "check_engine_process.py"

function Write-EngineProcessCheckLog {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet("INFO", "WARN", "ERROR")][string]$Level = "INFO"
    )
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[{0}] [{1}] [engine_process_check] {2}" -f $timestamp, $Level, $Message
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    Write-Host $line
}

function Invoke-PythonScriptAndLog {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath
    )

    $tmpOut = [System.IO.Path]::GetTempFileName()
    $tmpErr = [System.IO.Path]::GetTempFileName()
    try {
        $proc = Start-Process `
            -FilePath "python" `
            -ArgumentList @($ScriptPath) `
            -NoNewWindow `
            -PassThru `
            -Wait `
            -RedirectStandardOutput $tmpOut `
            -RedirectStandardError $tmpErr

        if (Test-Path -Path $tmpOut) {
            $outText = Get-Content -Path $tmpOut -Raw -ErrorAction SilentlyContinue
            if ($outText) {
                Add-Content -Path $LogPath -Value $outText -Encoding UTF8
            }
        }
        if (Test-Path -Path $tmpErr) {
            $errText = Get-Content -Path $tmpErr -Raw -ErrorAction SilentlyContinue
            if ($errText) {
                Add-Content -Path $LogPath -Value $errText -Encoding UTF8
            }
        }
        return $proc.ExitCode
    }
    finally {
        Remove-Item -Path $tmpOut -Force -ErrorAction SilentlyContinue
        Remove-Item -Path $tmpErr -Force -ErrorAction SilentlyContinue
    }
}

function Send-WrapperTelegramAlert {
    param(
        [Parameter(Mandatory = $true)][string]$WrapperName,
        [Parameter(Mandatory = $true)][string]$ErrorText
    )
    $msgPath = [System.IO.Path]::GetTempFileName()
    try {
        $body = "[ALERT] wrapper exception`nscript=$WrapperName`nerror=$ErrorText"
        Set-Content -Path $msgPath -Value $body -Encoding UTF8
        & python -c "import sys; from pathlib import Path; sys.path.insert(0, r'$ScriptDir'); from telegram_notifier import send_telegram_message; p = Path(sys.argv[1]); send_telegram_message(p.read_text(encoding='utf-8'))" $msgPath | Out-Null
    }
    catch {
        Write-EngineProcessCheckLog -Message ("Telegram notify failed in catch: {0}" -f $_.Exception.Message) -Level "WARN"
    }
    finally {
        Remove-Item -Path $msgPath -Force -ErrorAction SilentlyContinue
    }
}

$exitCode = 0
$lockAcquired = $false

try {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

    if (Test-Path -Path $LockPath) {
        $lockItem = Get-Item -Path $LockPath -ErrorAction Stop
        $lockAge = (Get-Date) - $lockItem.LastWriteTime

        if ($lockAge.TotalMinutes -le 30) {
            Write-EngineProcessCheckLog -Message "Lock file is fresh (<=30m). Another run may still be active; skipping." -Level "WARN"
            $exitCode = 1
        }
        else {
            Write-EngineProcessCheckLog -Message ("Removing stale lock file: {0}" -f $LockPath) -Level "WARN"
            Remove-Item -Path $LockPath -Force -ErrorAction Stop
        }
    }

    if ($exitCode -eq 0) {
        Set-Content -Path $LockPath -Value (Get-Date -Format "o") -Encoding UTF8
        $lockAcquired = $true

        Write-EngineProcessCheckLog -Message "Running check_engine_process.py" -Level "INFO"
        $pythonExitCode = Invoke-PythonScriptAndLog -ScriptPath $CheckScriptPath
        if ($pythonExitCode -eq 0) {
            Write-EngineProcessCheckLog -Message "check_engine_process.py completed successfully" -Level "INFO"
            $exitCode = 0
        }
        elseif ($pythonExitCode -eq 2) {
            Write-EngineProcessCheckLog -Message "check_engine_process.py: down and recovery failed (exit=2)" -Level "ERROR"
            $exitCode = 2
        }
        else {
            Write-EngineProcessCheckLog -Message ("ERROR: check_engine_process.py failed (exit={0})" -f $pythonExitCode) -Level "ERROR"
            Send-WrapperTelegramAlert `
                -WrapperName "run_check_engine_process.ps1" `
                -ErrorText ("check_engine_process.py exit={0}" -f $pythonExitCode)
            $exitCode = 1
        }
    }
}
catch {
    Write-EngineProcessCheckLog -Message ("ERROR: run_check_engine_process.ps1 exception: {0}" -f $_.Exception.Message) -Level "ERROR"
    Send-WrapperTelegramAlert -WrapperName "run_check_engine_process.ps1" -ErrorText $_.Exception.Message
    $exitCode = 1
}
finally {
    if ($lockAcquired -and (Test-Path -Path $LockPath)) {
        Remove-Item -Path $LockPath -Force -ErrorAction SilentlyContinue
        Write-EngineProcessCheckLog -Message "Lock file removed." -Level "INFO"
    }
}

exit $exitCode
