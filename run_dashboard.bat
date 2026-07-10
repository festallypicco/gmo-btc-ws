@echo off
setlocal

set "ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\ensure_engine_running.ps1" -ProjectRoot "%ROOT%" -StartupTimeoutSec 15
if errorlevel 1 (
    echo.
    echo Engine startup check failed.
    pause
    exit /b 1
)

cd /d "%ROOT%btc_trading_tool"
echo Starting Streamlit dashboard...
streamlit run dashboard.py

if errorlevel 1 (
    echo.
    echo Failed to start dashboard.
    pause
)
