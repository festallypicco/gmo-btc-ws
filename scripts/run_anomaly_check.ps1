param()

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$LockPath = Join-Path $ScriptDir "run_anomaly_check.lock"
$LogDir = Join-Path $ProjectRoot "log"
$LogPath = Join-Path $LogDir ("anomaly_check_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
$CheckScriptPath = Join-Path $ScriptDir "check_trading_anomaly.py"

function Write-AnomalyCheckLog {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet("INFO", "WARN", "ERROR")][string]$Level = "INFO"
    )
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[{0}] [{1}] [anomaly_check] {2}" -f $timestamp, $Level, $Message
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
        Write-AnomalyCheckLog -Message ("Telegram notify failed in catch: {0}" -f $_.Exception.Message) -Level "WARN"
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
            Write-AnomalyCheckLog -Message "Lock file is fresh (<=30m). Another run may still be active; skipping." -Level "WARN"
            $exitCode = 1
        }
        else {
            Write-AnomalyCheckLog -Message ("Removing stale lock file: {0}" -f $LockPath) -Level "WARN"
            Remove-Item -Path $LockPath -Force -ErrorAction Stop
        }
    }

    if ($exitCode -eq 0) {
        Set-Content -Path $LockPath -Value (Get-Date -Format "o") -Encoding UTF8
        $lockAcquired = $true

        Write-AnomalyCheckLog -Message "Running check_trading_anomaly.py" -Level "INFO"
        $pythonExitCode = Invoke-PythonScriptAndLog -ScriptPath $CheckScriptPath
        if ($pythonExitCode -eq 0) {
            Write-AnomalyCheckLog -Message "check_trading_anomaly.py completed successfully" -Level "INFO"
            $exitCode = 0
        }
        else {
            Write-AnomalyCheckLog -Message ("ERROR: check_trading_anomaly.py failed (exit={0})" -f $pythonExitCode) -Level "ERROR"
            Send-WrapperTelegramAlert `
                -WrapperName "run_anomaly_check.ps1" `
                -ErrorText ("check_trading_anomaly.py exit={0}" -f $pythonExitCode)
            $exitCode = 1
        }
    }
}
catch {
    Write-AnomalyCheckLog -Message ("ERROR: run_anomaly_check.ps1 exception: {0}" -f $_.Exception.Message) -Level "ERROR"
    Send-WrapperTelegramAlert -WrapperName "run_anomaly_check.ps1" -ErrorText $_.Exception.Message
    $exitCode = 1
}
finally {
    if ($lockAcquired -and (Test-Path -Path $LockPath)) {
        Remove-Item -Path $LockPath -Force -ErrorAction SilentlyContinue
        Write-AnomalyCheckLog -Message "Lock file removed." -Level "INFO"
    }
}

exit $exitCode
