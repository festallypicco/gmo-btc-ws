#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCK_PATH="$SCRIPT_DIR/run_nightly_review.lock"
LOG_DIR="$PROJECT_ROOT/log"
LOG_PATH="$LOG_DIR/ai_review_run_$(date +%Y-%m-%d).log"
CHANGE_OUTCOMES_SCRIPT="$SCRIPT_DIR/build_change_outcomes.py"
SUMMARY_SCRIPT="$SCRIPT_DIR/build_ai_review_summary.py"
REVIEW_PIPELINE_SCRIPT="$SCRIPT_DIR/review_pipeline.py"

mkdir -p "$LOG_DIR"

log() {
    local level="$1"
    local message="$2"
    local timestamp
    timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
    local line="[$timestamp] [$level] [nightly_review] $message"
    echo "$line" >> "$LOG_PATH"
    echo "$line"
}

run_and_log() {
    local script_path="$1"
    local tmp_out
    local tmp_err
    tmp_out="$(mktemp)"
    tmp_err="$(mktemp)"
    python "$script_path" >"$tmp_out" 2>"$tmp_err"
    local exit_code=$?
    if [ -s "$tmp_out" ]; then
        cat "$tmp_out" >> "$LOG_PATH"
    fi
    if [ -s "$tmp_err" ]; then
        cat "$tmp_err" >> "$LOG_PATH"
    fi
    rm -f "$tmp_out" "$tmp_err"
    return $exit_code
}

exit_code=0
lock_acquired=0

if [ -f "$LOCK_PATH" ]; then
    lock_age_sec=$(( $(date +%s) - $(stat -c %Y "$LOCK_PATH") ))
    if [ "$lock_age_sec" -le 7200 ]; then
        log "WARN" "Lock file is fresh (<=2h). Another run may still be active; skipping."
        exit_code=1
    else
        log "WARN" "Removing stale lock file: $LOCK_PATH"
        rm -f "$LOCK_PATH"
    fi
fi

if [ "$exit_code" -eq 0 ]; then
    date -Iseconds > "$LOCK_PATH"
    lock_acquired=1

    log "INFO" "Running build_change_outcomes.py"
    run_and_log "$CHANGE_OUTCOMES_SCRIPT"
    change_outcomes_exit=$?
    if [ "$change_outcomes_exit" -eq 0 ]; then
        log "INFO" "build_change_outcomes.py completed successfully"
    else
        log "ERROR" "build_change_outcomes.py failed (exit=$change_outcomes_exit)"
        log "WARN" "Continuing nightly review pipeline despite build_change_outcomes.py failure."
    fi

    log "INFO" "Running build_ai_review_summary.py"
    run_and_log "$SUMMARY_SCRIPT"
    summary_exit=$?
    if [ "$summary_exit" -eq 0 ]; then
        log "INFO" "build_ai_review_summary.py completed successfully"
        log "INFO" "Running review_pipeline.py"
        run_and_log "$REVIEW_PIPELINE_SCRIPT"
        review_exit=$?
        if [ "$review_exit" -eq 0 ]; then
            log "INFO" "review_pipeline.py completed successfully"
            exit_code=0
        else
            log "ERROR" "review_pipeline.py failed (exit=$review_exit)"
            exit_code=1
        fi
    else
        log "ERROR" "build_ai_review_summary.py failed (exit=$summary_exit)"
        log "WARN" "Skipping review_pipeline.py because summary build failed."
        exit_code=1
    fi
fi

if [ "$lock_acquired" -eq 1 ] && [ -f "$LOCK_PATH" ]; then
    rm -f "$LOCK_PATH"
    log "INFO" "Lock file removed."
fi

exit $exit_code
