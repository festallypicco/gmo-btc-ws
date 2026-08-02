#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.yml"
CHECK_SCRIPT="$SCRIPT_DIR/check_engine_crash_loop.py"
PYTHON_BIN="$PROJECT_ROOT/.venv-host/bin/python"
LOCK_PATH="$SCRIPT_DIR/run_crash_loop_check.lock"
# 5分間隔の監視向け。nightly(2h)より短く、csv整合性チェック(30m)に合わせる。
LOCK_STALE_SEC=1800

usage() {
    echo "Usage: $0 [--compose-file|-f PATH]"
    echo "  Default compose file: $PROJECT_ROOT/docker-compose.yml"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --compose-file|-f)
            if [ $# -lt 2 ]; then
                echo "[ERROR] $1 requires a path argument" >&2
                usage >&2
                exit 1
            fi
            COMPOSE_FILE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[ERROR] unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ "$COMPOSE_FILE" != /* ]]; then
    COMPOSE_FILE="$PROJECT_ROOT/$COMPOSE_FILE"
fi

# Git Bash 上の Windows Python 向けにパスを変換。Linux ではそのまま。
to_python_path() {
    local path="$1"
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$path"
    else
        printf '%s\n' "$path"
    fi
}

send_wrapper_telegram_alert() {
    local wrapper_name="$1"
    local error_text="$2"
    local msg_path
    local msg_path_py
    local script_dir_py
    msg_path="$(mktemp)"
    {
        printf '%s\n' "[ALERT] wrapper exception"
        printf '%s\n' "script=$wrapper_name"
        printf '%s\n' "error=$error_text"
    } > "$msg_path"
    msg_path_py="$(to_python_path "$msg_path")"
    script_dir_py="$(to_python_path "$SCRIPT_DIR")"
    if ! "$PYTHON_BIN" -c "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from telegram_notifier import send_telegram_message; send_telegram_message(Path(sys.argv[2]).read_text(encoding='utf-8'))" "$script_dir_py" "$msg_path_py" >/dev/null 2>&1; then
        echo "[WARN] Telegram notify failed in catch"
    fi
    rm -f "$msg_path"
}

exit_code=0
lock_acquired=0

cleanup() {
    if [ "$lock_acquired" -eq 1 ] && [ -f "$LOCK_PATH" ]; then
        rm -f "$LOCK_PATH"
        echo "[INFO] Lock file removed."
    fi
}
trap cleanup EXIT

if [ -f "$LOCK_PATH" ]; then
    lock_age_sec=$(( $(date +%s) - $(stat -c %Y "$LOCK_PATH") ))
    if [ "$lock_age_sec" -le "$LOCK_STALE_SEC" ]; then
        echo "[WARN] Lock file is fresh (<=30m). Another run may still be active; skipping."
        exit_code=1
        exit "$exit_code"
    else
        echo "[WARN] Removing stale lock file: $LOCK_PATH"
        rm -f "$LOCK_PATH"
    fi
fi

date -Iseconds > "$LOCK_PATH"
lock_acquired=1

if ! cd "$PROJECT_ROOT"; then
    msg="failed to cd to $PROJECT_ROOT"
    echo "[ERROR] run_crash_loop_check.sh exception: $msg"
    send_wrapper_telegram_alert "run_crash_loop_check.sh" "$msg"
    exit_code=1
    exit "$exit_code"
fi

CHECK_SCRIPT_PY="$(to_python_path "$CHECK_SCRIPT")"
COMPOSE_FILE_PY="$(to_python_path "$COMPOSE_FILE")"
"$PYTHON_BIN" "$CHECK_SCRIPT_PY" --compose-file "$COMPOSE_FILE_PY"
exit_code=$?

if [ "$exit_code" -ne 0 ]; then
    echo "[ERROR] check_engine_crash_loop.py failed (exit=$exit_code)"
    send_wrapper_telegram_alert \
        "run_crash_loop_check.sh" \
        "check_engine_crash_loop.py exit=$exit_code"
fi

exit "$exit_code"
