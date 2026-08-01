param()

# Task Scheduler registration example (every 5 minutes, LogonType=Password):
# schtasks /Create /TN "BTC_Crash_Loop_Check" /SC MINUTE /MO 5 `
#   /TR "powershell -NoProfile -ExecutionPolicy Bypass -File `"C:\Users\tai_m\Cursor\Projects\gmo-btc-ws\scripts\run_crash_loop_check.ps1`"" `
#   /RU "%USERDOMAIN%\%USERNAME%" /RP "<WindowsPassword>" /RL LIMITED /F
# Verify: schtasks /Query /TN "BTC_Crash_Loop_Check" /XML | findstr LogonType

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$ComposeFile = Join-Path $ProjectRoot "docker-compose.yml"
$CheckScriptPath = Join-Path $ScriptDir "check_engine_crash_loop.py"

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
        Write-Host ("[WARN] Telegram notify failed in catch: {0}" -f $_.Exception.Message)
    }
    finally {
        Remove-Item -Path $msgPath -Force -ErrorAction SilentlyContinue
    }
}

$exitCode = 0
try {
    Set-Location -Path $ProjectRoot
    & python $CheckScriptPath --compose-file $ComposeFile
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        Write-Host ("[ERROR] check_engine_crash_loop.py failed (exit={0})" -f $exitCode)
        Send-WrapperTelegramAlert `
            -WrapperName "run_crash_loop_check.ps1" `
            -ErrorText ("check_engine_crash_loop.py exit={0}" -f $exitCode)
    }
}
catch {
    Write-Host ("[ERROR] run_crash_loop_check.ps1 exception: {0}" -f $_.Exception.Message)
    Send-WrapperTelegramAlert -WrapperName "run_crash_loop_check.ps1" -ErrorText $_.Exception.Message
    $exitCode = 1
}

exit $exitCode
