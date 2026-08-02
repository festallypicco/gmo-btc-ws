#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="$PROJECT_ROOT/runtime"
LOCK_PATH="$SCRIPT_DIR/run_daily_report.lock"
LOG_DIR="$PROJECT_ROOT/log"
LOG_PATH="$LOG_DIR/daily_report_$(date +%Y-%m-%d).log"
CHECK_SCRIPT="$SCRIPT_DIR/build_daily_report.py"
PYTHON_BIN="$PROJECT_ROOT/.venv-host/bin/python"
LOCK_STALE_SEC=1800
WRAPPER_NAME="run_daily_report.sh"

to_python_path() {
    local path="$1"
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$path"
    else
        printf '%s\n' "$path"
    fi
}

log() {
    local level="$1"
    local message="$2"
    local timestamp
    timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
    local line="[$timestamp] [$level] [daily_report] $message"
    echo "$line" >> "$LOG_PATH"
    echo "$line"
}

run_and_log() {
    local script_path="$1"
    local script_path_py
    local tmp_out
    local tmp_err
    local py_exit
    script_path_py="$(to_python_path "$script_path")"
    tmp_out="$(mktemp)"
    tmp_err="$(mktemp)"
    "$PYTHON_BIN" "$script_path_py" >"$tmp_out" 2>"$tmp_err"
    py_exit=$?
    if [ -s "$tmp_out" ]; then
        cat "$tmp_out" >> "$LOG_PATH"
    fi
    if [ -s "$tmp_err" ]; then
        cat "$tmp_err" >> "$LOG_PATH"
    fi
    rm -f "$tmp_out" "$tmp_err"
    return $py_exit
}

send_wrapper_telegram_alert() {
    local wrapper_name="$1"
    local error_text="$2"
    local msg_path
    local msg_path_py
    local script_dir_py
    local tg_err
    msg_path="$(mktemp)"
    tg_err="$(mktemp)"
    {
        printf '%s\n' "[ALERT] wrapper exception"
        printf '%s\n' "script=$wrapper_name"
        printf '%s\n' "error=$error_text"
    } > "$msg_path"
    msg_path_py="$(to_python_path "$msg_path")"
    script_dir_py="$(to_python_path "$SCRIPT_DIR")"
    if ! "$PYTHON_BIN" -c "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from telegram_notifier import send_telegram_message; send_telegram_message(Path(sys.argv[2]).read_text(encoding='utf-8'))" "$script_dir_py" "$msg_path_py" >"$tg_err" 2>&1; then
        log "WARN" "Telegram notify failed in catch: $(tr '\n' ' ' < "$tg_err")"
    fi
    rm -f "$msg_path" "$tg_err"
}

exit_code=0
lock_acquired=0

cleanup() {
    if [ "$lock_acquired" -eq 1 ] && [ -f "$LOCK_PATH" ]; then
        rm -f "$LOCK_PATH"
        log "INFO" "Lock file removed."
    fi
}
trap cleanup EXIT

mkdir -p "$LOG_DIR"
mkdir -p "$RUNTIME_DIR"

if [ -f "$LOCK_PATH" ]; then
    lock_age_sec=$(( $(date +%s) - $(stat -c %Y "$LOCK_PATH") ))
    if [ "$lock_age_sec" -le "$LOCK_STALE_SEC" ]; then
        log "WARN" "Lock file is fresh (<=30m). Another run may still be active; skipping."
        exit_code=1
    else
        log "WARN" "Removing stale lock file: $LOCK_PATH"
        rm -f "$LOCK_PATH"
    fi
fi

if [ "$exit_code" -eq 0 ]; then
    date -Iseconds > "$LOCK_PATH"
    lock_acquired=1

    log "INFO" "Running build_daily_report.py"
    run_and_log "$CHECK_SCRIPT"
    python_exit=$?
    if [ "$python_exit" -eq 0 ]; then
        log "INFO" "build_daily_report.py completed successfully"
        exit_code=0
    else
        log "ERROR" "ERROR: build_daily_report.py failed (exit=$python_exit)"
        send_wrapper_telegram_alert \
            "$WRAPPER_NAME" \
            "build_daily_report.py exit=$python_exit"
        exit_code=1
    fi
fi

exit "$exit_code"
