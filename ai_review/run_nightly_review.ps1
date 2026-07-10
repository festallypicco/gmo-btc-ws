param()

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$LockPath = Join-Path $ScriptDir "run_nightly_review.lock"
$LogDir = Join-Path $ProjectRoot "log"
$LogPath = Join-Path $LogDir ("ai_review_run_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
$ChangeOutcomesScriptPath = Join-Path $ScriptDir "build_change_outcomes.py"
$SummaryScriptPath = Join-Path $ScriptDir "build_ai_review_summary.py"
$ReviewPipelinePath = Join-Path $ScriptDir "review_pipeline.py"

function Write-NightlyReviewLog {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet("INFO", "WARN", "ERROR")][string]$Level = "INFO"
    )
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[{0}] [{1}] [nightly_review] {2}" -f $timestamp, $Level, $Message
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

$exitCode = 0
$lockAcquired = $false

try {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

    if (Test-Path -Path $LockPath) {
        $lockItem = Get-Item -Path $LockPath -ErrorAction Stop
        $lockAge = (Get-Date) - $lockItem.LastWriteTime

        if ($lockAge.TotalHours -le 2) {
            Write-NightlyReviewLog -Message "Lock file is fresh (<=2h). Another run may still be active; skipping." -Level "WARN"
            $exitCode = 1
        }
        else {
            Write-NightlyReviewLog -Message ("Removing stale lock file: {0}" -f $LockPath) -Level "WARN"
            Remove-Item -Path $LockPath -Force -ErrorAction Stop
        }
    }

    if ($exitCode -eq 0) {
        Set-Content -Path $LockPath -Value (Get-Date -Format "o") -Encoding UTF8
        $lockAcquired = $true

        Write-NightlyReviewLog -Message "Running build_change_outcomes.py" -Level "INFO"
        $changeOutcomesExitCode = Invoke-PythonScriptAndLog -ScriptPath $ChangeOutcomesScriptPath
        if ($changeOutcomesExitCode -eq 0) {
            Write-NightlyReviewLog -Message "build_change_outcomes.py completed successfully" -Level "INFO"
        }
        else {
            Write-NightlyReviewLog -Message ("ERROR: build_change_outcomes.py failed (exit={0})" -f $changeOutcomesExitCode) -Level "ERROR"
            Write-NightlyReviewLog -Message "Continuing nightly review pipeline despite build_change_outcomes.py failure." -Level "WARN"
        }
        Write-NightlyReviewLog -Message "Running build_ai_review_summary.py" -Level "INFO"
        $pythonExitCode = Invoke-PythonScriptAndLog -ScriptPath $SummaryScriptPath

        if ($pythonExitCode -eq 0) {
            Write-NightlyReviewLog -Message "build_ai_review_summary.py completed successfully" -Level "INFO"

            Write-NightlyReviewLog -Message "Running review_pipeline.py" -Level "INFO"
            $reviewExitCode = Invoke-PythonScriptAndLog -ScriptPath $ReviewPipelinePath
            if ($reviewExitCode -eq 0) {
                Write-NightlyReviewLog -Message "review_pipeline.py completed successfully" -Level "INFO"
                $exitCode = 0
            }
            else {
                Write-NightlyReviewLog -Message ("ERROR: review_pipeline.py failed (exit={0})" -f $reviewExitCode) -Level "ERROR"
                $exitCode = 1
            }
        }
        else {
            Write-NightlyReviewLog -Message ("ERROR: build_ai_review_summary.py failed (exit={0})" -f $pythonExitCode) -Level "ERROR"
            Write-NightlyReviewLog -Message "Skipping review_pipeline.py because summary build failed." -Level "WARN"
            $exitCode = 1
        }
    }
}
catch {
    Write-NightlyReviewLog -Message ("ERROR: run_nightly_review.ps1 exception: {0}" -f $_.Exception.Message) -Level "ERROR"
    $exitCode = 1
}
finally {
    if ($lockAcquired -and (Test-Path -Path $LockPath)) {
        Remove-Item -Path $LockPath -Force -ErrorAction SilentlyContinue
        Write-NightlyReviewLog -Message "Lock file removed." -Level "INFO"
    }
}

exit $exitCode
