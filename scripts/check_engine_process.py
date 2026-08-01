"""Detect native trading_engine.py complete stop and optionally auto-recover.

Windows native deployment (non-Docker). Conditions for abnormal stop:
  - zero trading_engine.py processes
  - runtime/manual_stop.flag is absent

Grace: re-check after GRACE_SEC within the same run to avoid catching
brief gaps during intentional restart.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from telegram_notifier import send_telegram_message

ROOT_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT_DIR / "runtime"
HEARTBEATS_PATH = RUNTIME_DIR / "monitor_heartbeats.json"
STATE_PATH = RUNTIME_DIR / "engine_process_state.json"
MANUAL_STOP_FLAG_PATH = RUNTIME_DIR / "manual_stop.flag"
ENSURE_SCRIPT_PATH = ROOT_DIR / "scripts" / "ensure_engine_running.ps1"
HEARTBEAT_KEY = "check_engine_process"

# Match anomaly-check style: avoid alert spam.
ALERT_COOLDOWN_HOURS = 2.0
# Avoid false positive while engine is briefly restarting.
GRACE_SEC = 90
ENSURE_TIMEOUT_SEC = 120

LOGGER = logging.getLogger("engine_process_check")

FindPidsFn = Callable[[], List[int]]
EnsureFn = Callable[[], Tuple[bool, str]]
SendFn = Callable[[str], bool]


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [engine_process_check] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _record_monitor_heartbeat() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
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


def _load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:
        LOGGER.warning("Failed to read engine_process_state; resetting: %s", exc)
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _can_send_alert(now_dt: datetime, last_alert_at: Optional[str]) -> bool:
    last_dt = _parse_iso(last_alert_at)
    if last_dt is None:
        return True
    elapsed_h = (now_dt - last_dt).total_seconds() / 3600.0
    return elapsed_h >= ALERT_COOLDOWN_HOURS


def find_trading_engine_pids() -> List[int]:
    """
    Enumerate PIDs whose command line contains trading_engine.py
    (same criteria as scripts/engine_process_utils.ps1).
    """
    ps = (
        "$names=@('python.exe','pythonw.exe','py.exe');"
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |"
        "Where-Object {"
        "  $n=(($_.Name)+'').ToLowerInvariant();"
        "  $c=(($_.CommandLine)+'').ToLowerInvariant();"
        "  ($names -contains $n) -and ($c -match 'trading_engine\\.py')"
        "} | ForEach-Object { $_.ProcessId }"
    )
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"process enumeration failed: {err}")
    pids: List[int] = []
    for line in (proc.stdout or "").splitlines():
        text = line.strip()
        if text.isdigit():
            pids.append(int(text))
    return sorted(set(pids))


def run_ensure_engine_running(
    ensure_script: Path = ENSURE_SCRIPT_PATH,
    project_root: Path = ROOT_DIR,
    timeout_sec: int = ENSURE_TIMEOUT_SEC,
) -> Tuple[bool, str]:
    """Call ensure_engine_running.ps1. Returns (ok, detail)."""
    if not ensure_script.exists():
        return False, f"ensure script missing: {ensure_script}"
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ensure_script),
            "-ProjectRoot",
            str(project_root),
        ],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout_sec,
    )
    detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if len(detail) > 1500:
        detail = detail[-1500:]
    ok = proc.returncode == 0
    return ok, detail or f"exit={proc.returncode}"


def _build_alert(
    *,
    process_count_before: int,
    recovery_attempted: bool,
    recovery_ok: Optional[bool],
    process_count_after: Optional[int],
    detail: str,
) -> str:
    if recovery_attempted:
        if recovery_ok:
            recovery_line = (
                f"自動復旧: 成功 (ensure_engine_running.ps1)"
                f" process_count_after={process_count_after}"
            )
        else:
            recovery_line = (
                f"自動復旧: 失敗 (ensure_engine_running.ps1)"
                f" process_count_after={process_count_after}"
            )
    else:
        recovery_line = "自動復旧: 未実施"

    lines = [
        "[ALERT] native engine process down",
        f"process_count_before={process_count_before}",
        "manual_stop.flag=absent",
        recovery_line,
    ]
    if detail:
        lines.append(f"detail={detail}")
    return "\n".join(lines)


def check_engine_process(
    *,
    find_pids: FindPidsFn = find_trading_engine_pids,
    ensure_fn: EnsureFn = run_ensure_engine_running,
    send_fn: SendFn = send_telegram_message,
    manual_stop_path: Path = MANUAL_STOP_FLAG_PATH,
    grace_sec: int = GRACE_SEC,
    sleep_fn: Callable[[float], None] = time.sleep,
    now: Optional[datetime] = None,
    attempt_recovery: bool = True,
) -> Dict[str, Any]:
    """
    Core check. Returns a result dict for tests/logging.
    """
    now_dt = now or datetime.now()
    result: Dict[str, Any] = {
        "status": "ok",
        "process_count": 0,
        "manual_stop": False,
        "grace_skipped": False,
        "recovery_attempted": False,
        "recovery_ok": None,
        "alert_sent": False,
    }

    if manual_stop_path.exists():
        result["status"] = "manual_stop"
        result["manual_stop"] = True
        result["process_count"] = len(find_pids())
        LOGGER.info(
            "manual_stop.flag present; treating as intentional stop "
            "(process_count=%d).",
            result["process_count"],
        )
        return result

    count = len(find_pids())
    result["process_count"] = count
    if count >= 1:
        result["status"] = "running"
        LOGGER.info("engine process healthy: count=%d", count)
        return result

    LOGGER.warning(
        "engine process count=0; waiting %ds grace before confirm.",
        grace_sec,
    )
    if grace_sec > 0:
        sleep_fn(float(grace_sec))

    if manual_stop_path.exists():
        result["status"] = "manual_stop"
        result["manual_stop"] = True
        result["grace_skipped"] = True
        LOGGER.info("manual_stop.flag appeared during grace; skip alert.")
        return result

    count_after_grace = len(find_pids())
    result["process_count"] = count_after_grace
    if count_after_grace >= 1:
        result["status"] = "running"
        result["grace_skipped"] = True
        LOGGER.info(
            "process appeared during grace (count=%d); false positive avoided.",
            count_after_grace,
        )
        return result

    result["status"] = "down"
    recovery_ok: Optional[bool] = None
    recovery_detail = ""
    process_after: Optional[int] = count_after_grace

    if attempt_recovery:
        result["recovery_attempted"] = True
        LOGGER.warning("Attempting auto recovery via ensure_engine_running.ps1")
        try:
            recovery_ok, recovery_detail = ensure_fn()
        except Exception as exc:
            recovery_ok = False
            recovery_detail = f"ensure raised: {exc}"
        result["recovery_ok"] = recovery_ok
        process_after = len(find_pids())
        # Prefer process presence as success signal when ensure exits 0 but
        # also when ensure reports failure yet process is back.
        if process_after >= 1:
            recovery_ok = True
            result["recovery_ok"] = True
        LOGGER.info(
            "recovery_ok=%s process_count_after=%s detail=%s",
            recovery_ok,
            process_after,
            recovery_detail[:200],
        )

    state = _load_state()
    alert_allowed = _can_send_alert(now_dt, state.get("last_alert_at"))
    if alert_allowed:
        message = _build_alert(
            process_count_before=0,
            recovery_attempted=bool(result["recovery_attempted"]),
            recovery_ok=recovery_ok,
            process_count_after=process_after,
            detail=recovery_detail,
        )
        sent = False
        try:
            sent = bool(send_fn(message))
        except Exception as exc:
            LOGGER.error("Telegram send failed: %s", exc)
        result["alert_sent"] = sent
        if sent:
            state["last_alert_at"] = now_dt.isoformat(timespec="seconds")
            LOGGER.warning("Down alert sent.")
        else:
            LOGGER.error("Down alert was not delivered.")
    else:
        LOGGER.info("Down confirmed but still in alert cooldown window.")

    state["last_down_detected_at"] = now_dt.isoformat(timespec="seconds")
    state["last_recovery_attempted"] = bool(result["recovery_attempted"])
    state["last_recovery_ok"] = recovery_ok
    state["last_process_count_after"] = process_after
    _save_state(state)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect native trading_engine complete stop and auto-recover"
    )
    parser.add_argument(
        "--no-recover",
        action="store_true",
        help="detect and alert only; do not call ensure_engine_running.ps1",
    )
    parser.add_argument(
        "--grace-sec",
        type=int,
        default=GRACE_SEC,
        help=f"re-check wait seconds (default {GRACE_SEC})",
    )
    args = parser.parse_args(argv)
    _setup_logging()

    try:
        result = check_engine_process(
            attempt_recovery=not args.no_recover,
            grace_sec=max(0, int(args.grace_sec)),
        )
    except Exception as exc:
        LOGGER.exception("engine process check failed: %s", exc)
        try:
            send_telegram_message(
                "[ALERT] native engine process check failed\n"
                f"error={exc}"
            )
        except Exception:
            pass
        return 1

    try:
        _record_monitor_heartbeat()
    except Exception as exc:
        LOGGER.warning("heartbeat write failed: %s", exc)

    if result["status"] == "down" and result.get("recovery_attempted"):
        if result.get("recovery_ok"):
            return 0
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
