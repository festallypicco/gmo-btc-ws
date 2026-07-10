function Test-EngineProcessAlive {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }

    $wmi = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $wmi) { return $false }

    $name = (($proc.ProcessName) + "").ToLowerInvariant()
    $cmd = (($wmi.CommandLine) + "").ToLowerInvariant()

    if (($name -notlike "python*") -and ($name -ne "py")) {
        return $false
    }
    if ($cmd -notmatch "trading_engine\.py") {
        return $false
    }
    return $true
}

function Find-AllEngineProcesses {
    $enginePids = @()
    $pythonNames = @("python.exe", "pythonw.exe", "py.exe")
    $candidates = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue

    foreach ($proc in $candidates) {
        $name = (($proc.Name) + "").ToLowerInvariant()
        $cmd = (($proc.CommandLine) + "").ToLowerInvariant()

        if ($pythonNames -notcontains $name) {
            continue
        }
        if ($cmd -notmatch "trading_engine\.py") {
            continue
        }
        $detectedPid = [int]$proc.ProcessId
        if ($detectedPid -gt 0) {
            $enginePids += $detectedPid
        }
    }

    return @($enginePids | Sort-Object -Unique)
}

function Stop-AllEngineProcesses {
    $enginePids = @(Find-AllEngineProcesses)
    if ($enginePids.Count -eq 0) {
        return $true
    }

    $allStopped = $true

    foreach ($enginePid in $enginePids) {
        cmd /c "taskkill /PID $enginePid /T" | Out-Null
        $stopped = $false

        for ($i = 0; $i -lt 5; $i++) {
            Start-Sleep -Seconds 1
            if (-not (Test-EngineProcessAlive -ProcessId $enginePid)) {
                $stopped = $true
                break
            }
        }

        if (-not $stopped) {
            cmd /c "taskkill /PID $enginePid /T /F" | Out-Null
            Start-Sleep -Seconds 3
            if (Test-EngineProcessAlive -ProcessId $enginePid) {
                $allStopped = $false
            }
        }
    }

    return $allStopped
}

function Start-EngineProcess {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $projectRootPath = (Resolve-Path $ProjectRoot).Path
    $engineScriptPath = Join-Path $projectRootPath "trading_engine.py"
    $pidPath = Join-Path $projectRootPath "trading_engine.pid"
    $logDir = Join-Path $projectRootPath "log"
    $launcherScriptPath = Join-Path $projectRootPath "scripts\process_launcher.py"

    if (-not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Path $logDir | Out-Null
    }
    if (Test-Path $pidPath) {
        Remove-Item -Path $pidPath -Force -ErrorAction SilentlyContinue
    }

    $logPath = Join-Path $logDir ("engine_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    $errLogPath = Join-Path $logDir ("engine_{0}.err.log" -f (Get-Date -Format "yyyy-MM-dd"))

    # Delegate spawn + append log redirection to cross-platform Python launcher.
    $launcherArgs = @(
        $launcherScriptPath,
        "--engine-script", $engineScriptPath,
        "--log-path", $logPath,
        "--err-log-path", $errLogPath,
        "--working-directory", $projectRootPath
    )
    $launcherOutput = & python @launcherArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ("process_launcher.py failed (exit={0}): {1}" -f $LASTEXITCODE, ($launcherOutput -join " "))
    }
}

function Wait-EngineHealthy {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [int]$TimeoutSec = 15
    )

    $projectRootPath = (Resolve-Path $ProjectRoot).Path
    $liveDbPath = Join-Path $projectRootPath "live_state.db"
    $pidPath = Join-Path $projectRootPath "trading_engine.pid"
    $checkScriptPath = Join-Path $projectRootPath "scripts\check_live_state.py"
    $deadline = (Get-Date).AddSeconds($TimeoutSec)

    while ((Get-Date) -lt $deadline) {
        $env:LIVE_DB = $liveDbPath
        python "$checkScriptPath"
        if ($LASTEXITCODE -eq 0) {
            if (Test-Path $pidPath) {
                $pidText = (Get-Content -Path $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
                if ($pidText -match '^\d+$') {
                    $pidVal = [int]$pidText
                    if (Test-EngineProcessAlive -ProcessId $pidVal) {
                        return $true
                    }
                }
            }
        }
        Start-Sleep -Seconds 1
    }

    return $false
}
