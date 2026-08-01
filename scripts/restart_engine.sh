#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.yml"
MANUAL_STOP_FLAG="$PROJECT_ROOT/runtime/manual_stop.flag"

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

log() {
    local level="$1"
    local message="$2"
    local timestamp
    timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$timestamp] [$level] [restart_engine] $message"
}

if [ ! -f "$COMPOSE_FILE" ]; then
    log "ERROR" "compose file not found: $COMPOSE_FILE"
    exit 1
fi

cd "$PROJECT_ROOT" || exit 1

if [ -f "$MANUAL_STOP_FLAG" ]; then
    log "SKIP" "manual_stop.flag exists, restart skipped ($MANUAL_STOP_FLAG)"
    exit 0
fi

log "INFO" "restarting engine service (compose-file=$COMPOSE_FILE)"
if docker compose -f "$COMPOSE_FILE" restart engine; then
    log "INFO" "engine restart succeeded"
    exit 0
fi

log "ERROR" "engine restart failed"
exit 1
