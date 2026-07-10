param(
    [string]$ProjectRoot = (Split-Path -Path $PSScriptRoot -Parent)
)

# Task Scheduler registration example (hourly):
# schtasks /create /tn "BTC_Detect_Orphan_Engines" /sc hourly /mo 1 /tr "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\tai_m\Cursor\Projects\gmo-btc-ws\scripts\detect_orphan_engines.ps1" /f

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$UtilsScriptPath = Join-Path $PSScriptRoot "engine_process_utils.ps1"
. "$UtilsScriptPath"

$PidPath = Join-Path $ProjectRoot "trading_engine.pid"
$LogDir = Join-Path $ProjectRoot "log"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$LogPath = Join-Path $LogDir ("orphan_check_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

function Write-OrphanLog {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet("INFO", "WARN", "ERROR")][string]$Level = "INFO"
    )
    $line = "[{0}] [{1}] [orphan_check] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    Write-Host $line
}

function Get-PidFromFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -Path $Path)) {
        return @()
    }

    $raw = (Get-Content -Path $Path -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($raw -match '^\d+$') {
        return @([int]$raw)
    }

    return @()
}

$actualPids = @(Find-AllEngineProcesses)
$filePids = @(Get-PidFromFile -Path $PidPath)

$actualSet = @($actualPids | Sort-Object -Unique)
$fileSet = @($filePids | Sort-Object -Unique)
$actualCsv = if ($actualSet.Count -gt 0) { $actualSet -join ", " } else { "(none)" }
$fileCsv = if ($fileSet.Count -gt 0) { $fileSet -join ", " } else { "(none)" }

$isMismatch = (($actualSet -join ",") -ne ($fileSet -join ","))
$hasMultiple = ($actualSet.Count -ge 2)

if ($hasMultiple -or $isMismatch) {
    if ($hasMultiple) {
        Write-OrphanLog -Message ("Multiple engine processes detected: PID {0}" -f $actualCsv) -Level "WARN"
    }
    if ($isMismatch) {
        Write-OrphanLog -Message ("PID mismatch detected. pid_file={0}, actual={1}" -f $fileCsv, $actualCsv) -Level "WARN"
    }
}

exit 0
