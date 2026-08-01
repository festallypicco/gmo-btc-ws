@echo off
setlocal

set "ROOT=%~dp0"
rem %~dp0 ends with \, which escapes the closing quote when passed to powershell -File.
rem Strip it so -ProjectRoot and -StartupTimeoutSec stay separate arguments.
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\ensure_engine_running.ps1" -ProjectRoot "%ROOT%" -StartupTimeoutSec 15
if errorlevel 1 (
    echo.
    echo Engine startup check failed.
    pause
    exit /b 1
)

cd /d "%ROOT%\btc_trading_tool"
echo Starting Streamlit dashboard...
streamlit run dashboard.py

if errorlevel 1 (
    echo.
    echo Failed to start dashboard.
    pause
)
