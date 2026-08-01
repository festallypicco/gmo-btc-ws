"""CSV exit count vs live_state.db win/loss counter consistency check."""
from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional, Set

from telegram_notifier import send_telegram_message

ROOT_DIR = Path(__file__).resolve().parent.parent
LIVE_STATE_DB_PATH = ROOT_DIR / "runtime" / "live_state.db"
PID_PATH = ROOT_DIR / "runtime" / "trading_engine.pid"
STATE_PATH = ROOT_DIR / "runtime" / "csv_db_consistency_state.json"
HEARTBEATS_PATH = ROOT_DIR / "runtime" / "monitor_heartbeats.json"
HEARTBEAT_KEY = "check_csv_db_consistency"
LOG_DIR = ROOT_DIR / "log"

EXIT_REASONS: Set[str] = {"TAKE_PROFIT", "STOP_LOSS", "FORCE_CLOSE_MAINTENANCE"}

LOGGER = logging.getLogger("csv_db_consistency")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [csv_db_consistency] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _record_monitor_heartbeat() -> None:
    HEARTBEATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {}
    if HEARTBEATS_PATH.exists():
        try:
            loaded = json.loads(HEARTBEATS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception as exc:
            LOGGER.warning("Failed to read heartbeats file; recreating: %s", exc)
    data[HEARTBEAT_KEY] = datetime.now().isoformat(timespec="seconds")
    tmp_path = HEARTBEATS_PATH.with_suffix(HEARTBEATS_PATH.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, HEARTBEATS_PATH)


def _csv_path_for_day(day: date, log_dir: Path = LOG_DIR) -> Path:
    return log_dir / f"realtime_trading_log_{day.isoformat()}.csv"


def _read_db_exit_count(db_path: Path = LIVE_STATE_DB_PATH) -> int:
    if not db_path.exists():
        raise FileNotFoundError(f"live_state.db not found: {db_path}")

    with sqlite3.connect(db_path, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT win_count, loss_count
            FROM live_state
            WHERE id = 1
            """
        ).fetchone()

    if row is None:
        raise RuntimeError("live_state row(id=1) is missing")

    return int(row["win_count"] or 0) + int(row["loss_count"] or 0)


def _read_engine_pid(pid_path: Path = PID_PATH) -> Optional[str]:
    if not pid_path.exists():
        return None
    text = pid_path.read_text(encoding="utf-8").strip()
    return text if text else None


def _count_csv_exits(csv_path: Path, exit_reasons: Set[str] = EXIT_REASONS) -> int:
    if not csv_path.exists():
        return 0

    count = 0
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            reason = str(row.get("reason") or "").strip()
            if reason in exit_reasons:
                count += 1
    return count


def _load_state(state_path: Path) -> Optional[Dict[str, Any]]:
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Failed to read consistency state. It will be reinitialized: %s", exc)
        return None


def _save_state(state_path: Path, state: Dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _make_state(
    check_day: date,
    db_exit_count: int,
    csv_exit_count: int,
    engine_pid: Optional[str],
) -> Dict[str, Any]:
    return {
        "check_date": check_day.isoformat(),
        "db_exit_count": int(db_exit_count),
        "csv_exit_count": int(csv_exit_count),
        "last_engine_pid": engine_pid,
    }


def _build_mismatch_alert(
    check_day: date,
    db_delta: int,
    csv_delta: int,
    diff: int,
    db_exit_count: int,
    csv_exit_count: int,
) -> str:
    return (
        "[ALERT] CSV / live_state.db consistency mismatch\n"
        f"date={check_day.isoformat()}\n"
        f"db_delta={db_delta}\n"
        f"csv_delta={csv_delta}\n"
        f"diff={diff}\n"
        f"db_exit_count={db_exit_count}\n"
        f"csv_exit_count={csv_exit_count}"
    )


def _build_counter_drop_without_restart_alert(
    *,
    check_day: date,
    prev_db: int,
    db_exit_count: int,
    prev_csv: int,
    csv_exit_count: int,
    engine_pid: Optional[str],
) -> str:
    return (
        "[ALERT] CSV / live_state.db consistency: counter decreased without restart evidence\n"
        "再起動の形跡なくカウンターが減少\n"
        f"date={check_day.isoformat()}\n"
        f"prev_db_exit_count={prev_db}\n"
        f"db_exit_count={db_exit_count}\n"
        f"prev_csv_exit_count={prev_csv}\n"
        f"csv_exit_count={csv_exit_count}\n"
        f"engine_pid={engine_pid}"
    )


def run_consistency_check(
    *,
    now: Optional[datetime] = None,
    db_path: Path = LIVE_STATE_DB_PATH,
    log_dir: Path = LOG_DIR,
    state_path: Path = STATE_PATH,
    pid_path: Path = PID_PATH,
    send_message=send_telegram_message,
) -> int:
    """
    Compare DB counter increment vs CSV exit-row increment for the calendar day.

    Returns 0 on success (including baseline resets and notified mismatches),
    1 on hard failures (e.g. DB unreadable).
    """
    check_day = (now or datetime.now()).date()
    try:
        db_exit_count = _read_db_exit_count(db_path)
    except Exception as exc:
        LOGGER.error("Failed to read live_state.db: %s", exc)
        return 1

    csv_path = _csv_path_for_day(check_day, log_dir=log_dir)
    csv_exit_count = _count_csv_exits(csv_path)
    engine_pid = _read_engine_pid(pid_path)
    LOGGER.info(
        "current db_exit_count=%d csv_exit_count=%d date=%s csv_path=%s engine_pid=%s",
        db_exit_count,
        csv_exit_count,
        check_day.isoformat(),
        csv_path,
        engine_pid,
    )

    state = _load_state(state_path)
    if state is None:
        _save_state(
            state_path,
            _make_state(check_day, db_exit_count, csv_exit_count, engine_pid),
        )
        LOGGER.info("Initial run: csv_db_consistency_state.json created; comparison skipped.")
        _record_monitor_heartbeat()
        return 0

    prev_date_raw = state.get("check_date")
    prev_db = int(state.get("db_exit_count", 0))
    prev_csv = int(state.get("csv_exit_count", 0))
    prev_pid = state.get("last_engine_pid")
    if prev_pid is not None:
        prev_pid = str(prev_pid)

    date_changed = str(prev_date_raw) != check_day.isoformat()
    db_reset = db_exit_count < prev_db

    if date_changed:
        LOGGER.info("Baseline refresh (date changed); comparison skipped.")
        _save_state(
            state_path,
            _make_state(check_day, db_exit_count, csv_exit_count, engine_pid),
        )
        _record_monitor_heartbeat()
        return 0

    if db_reset:
        pid_changed = prev_pid != engine_pid
        if pid_changed:
            LOGGER.info(
                "Baseline refresh (db counter reset with restart evidence); "
                "comparison skipped. prev_pid=%s engine_pid=%s",
                prev_pid,
                engine_pid,
            )
        else:
            message = _build_counter_drop_without_restart_alert(
                check_day=check_day,
                prev_db=prev_db,
                db_exit_count=db_exit_count,
                prev_csv=prev_csv,
                csv_exit_count=csv_exit_count,
                engine_pid=engine_pid,
            )
            try:
                send_message(message)
                LOGGER.warning(
                    "Counter decreased without restart evidence; alert sent. "
                    "prev_pid=%s engine_pid=%s",
                    prev_pid,
                    engine_pid,
                )
            except Exception as exc:
                LOGGER.error("Telegram notify failed: %s", exc)
        _save_state(
            state_path,
            _make_state(check_day, db_exit_count, csv_exit_count, engine_pid),
        )
        _record_monitor_heartbeat()
        return 0

    db_delta = db_exit_count - prev_db
    csv_delta = csv_exit_count - prev_csv
    diff = db_delta - csv_delta

    LOGGER.info("db_delta=%d csv_delta=%d diff=%d", db_delta, csv_delta, diff)

    if db_delta != csv_delta:
        message = _build_mismatch_alert(
            check_day=check_day,
            db_delta=db_delta,
            csv_delta=csv_delta,
            diff=diff,
            db_exit_count=db_exit_count,
            csv_exit_count=csv_exit_count,
        )
        try:
            send_message(message)
            LOGGER.warning("Consistency mismatch alert sent.")
        except Exception as exc:
            LOGGER.error("Telegram notify failed: %s", exc)

    _save_state(
        state_path,
        _make_state(check_day, db_exit_count, csv_exit_count, engine_pid),
    )
    _record_monitor_heartbeat()
    return 0


def main() -> int:
    _setup_logging()
    return run_consistency_check()


if __name__ == "__main__":
    raise SystemExit(main())
