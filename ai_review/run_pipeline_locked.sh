#!/bin/bash
# run_pipeline_locked.sh
#
# review_pipeline.py を夜間レビューと同じロックのもとで実行するラッパー。
# 提案役をじじが担う運用で、じじ側ジョブが提案ファイルを作成した後に
# パイプライン（懐疑役・最終決定役・config適用）を起動するために使う。
# 引数はそのまま review_pipeline.py へ渡す。
#
#   bash ai_review/run_pipeline_locked.sh \
#     --target-date 2026-11-22 \
#     --proposer-file /app/log/ai_review_proposer_output_2026-11-22.txt
#
# ロックが新鮮（2時間以内）な場合は別の実行が進行中とみなし exit 1。
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_ROOT/log"
LOCK_PATH="$LOG_DIR/run_nightly_review.lock"
LOG_PATH="$LOG_DIR/ai_review_run_$(date +%Y-%m-%d).log"
REVIEW_PIPELINE_SCRIPT="$SCRIPT_DIR/review_pipeline.py"

mkdir -p "$LOG_DIR"

log() {
    local level="$1"
    local message="$2"
    local timestamp
    timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
    local line="[$timestamp] [$level] [pipeline_locked] $message"
    echo "$line" >> "$LOG_PATH"
    echo "$line"
}

if [ -f "$LOCK_PATH" ]; then
    lock_age_sec=$(( $(date +%s) - $(stat -c %Y "$LOCK_PATH") ))
    if [ "$lock_age_sec" -le 7200 ]; then
        log "WARN" "Lock file is fresh (<=2h). Another run may still be active; skipping."
        exit 1
    fi
    log "WARN" "Removing stale lock file: $LOCK_PATH"
    rm -f "$LOCK_PATH"
fi

date -Iseconds > "$LOCK_PATH"
log "INFO" "Running review_pipeline.py $*"
python "$REVIEW_PIPELINE_SCRIPT" "$@"
exit_code=$?
rm -f "$LOCK_PATH"
if [ "$exit_code" -eq 0 ]; then
    log "INFO" "review_pipeline.py completed successfully"
else
    log "ERROR" "review_pipeline.py failed (exit=$exit_code)"
fi
exit $exit_code
