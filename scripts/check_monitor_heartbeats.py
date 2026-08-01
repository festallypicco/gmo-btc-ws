"""Warn when monitor scripts have not updated their heartbeats within SLA."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from telegram_notifier import send_telegram_message

ROOT_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT_DIR / "runtime"
HEARTBEATS_PATH = RUNTIME_DIR / "monitor_heartbeats.json"

# script key -> max allowed age since last successful run
MONITOR_SLAS = {
    "check_trading_anomaly": timedelta(hours=2),
    "check_engine_crash_loop": timedelta(minutes=20),
    "check_csv_db_consistency": timedelta(hours=2),
    "check_engine_process": timedelta(hours=2),
    # 5分間隔実行。crash_loop と同様に 20 分超で STALE
    "check_orphan_orders": timedelta(minutes=20),
}

LOGGER = logging.getLogger("monitor_heartbeats")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [monitor_heartbeats] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _flag_path(script_key: str) -> Path:
    return RUNTIME_DIR / f"heartbeat_stale_notified_{script_key}.flag"


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _load_heartbeats(path: Path = HEARTBEATS_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Failed to read heartbeats file %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        LOGGER.warning("Heartbeats file is not a JSON object; treating as empty.")
        return {}
    return raw


def _format_elapsed(elapsed: timedelta) -> str:
    total_seconds = max(0, int(elapsed.total_seconds()))
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}h {minutes}m {seconds}s"


def _build_stale_alert(
    script_key: str,
    last_success_at: datetime,
    elapsed: timedelta,
    max_age: timedelta,
) -> str:
    return (
        "[ALERT] monitor heartbeat stale\n"
        f"script={script_key}\n"
        f"last_success_at={last_success_at.isoformat(timespec='seconds')}\n"
        f"elapsed={_format_elapsed(elapsed)}\n"
        f"max_age={_format_elapsed(max_age)}"
    )


def _notify_stale_if_needed(
    script_key: str,
    last_success_at: datetime,
    elapsed: timedelta,
    max_age: timedelta,
) -> None:
    flag = _flag_path(script_key)
    if flag.exists():
        LOGGER.info(
            "Stale flag already present for %s; skip Telegram notification.",
            script_key,
        )
        return

    message = _build_stale_alert(script_key, last_success_at, elapsed, max_age)
    sent = send_telegram_message(message)
    if sent:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        flag.write_text(message + "\n", encoding="utf-8")
        LOGGER.warning(
            "Heartbeat stale notification sent for %s. Flag created: %s",
            script_key,
            flag,
        )
    else:
        LOGGER.error("Failed to send Telegram notification for %s.", script_key)


def _clear_flag_if_present(script_key: str) -> None:
    flag = _flag_path(script_key)
    if not flag.exists():
        return
    flag.unlink()
    LOGGER.info(
        "Heartbeat for %s is within SLA again. Removed flag: %s",
        script_key,
        flag,
    )


def check_monitor_heartbeats(
    *,
    now: Optional[datetime] = None,
    heartbeats_path: Path = HEARTBEATS_PATH,
) -> int:
    now_dt = now or datetime.now()
    heartbeats = _load_heartbeats(heartbeats_path)
    LOGGER.info("Loaded heartbeats keys=%s", sorted(heartbeats.keys()))

    for script_key, max_age in MONITOR_SLAS.items():
        if script_key not in heartbeats:
            LOGGER.info(
                "No heartbeat yet for %s; skip (bootstrap / never succeeded).",
                script_key,
            )
            continue

        last_success_at = _parse_iso_datetime(heartbeats.get(script_key))
        if last_success_at is None:
            LOGGER.warning(
                "Invalid heartbeat timestamp for %s: %r; skip alert.",
                script_key,
                heartbeats.get(script_key),
            )
            continue

        elapsed = now_dt - last_success_at
        if elapsed <= max_age:
            LOGGER.info(
                "%s ok elapsed=%s max_age=%s",
                script_key,
                _format_elapsed(elapsed),
                _format_elapsed(max_age),
            )
            _clear_flag_if_present(script_key)
            continue

        LOGGER.warning(
            "%s stale elapsed=%s max_age=%s last_success_at=%s",
            script_key,
            _format_elapsed(elapsed),
            _format_elapsed(max_age),
            last_success_at.isoformat(timespec="seconds"),
        )
        _notify_stale_if_needed(script_key, last_success_at, elapsed, max_age)

    return 0


def main() -> int:
    _setup_logging()
    return check_monitor_heartbeats()


if __name__ == "__main__":
    raise SystemExit(main())
