"""Detect Docker Compose restart exhaustion and notify via Telegram."""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram_notifier import send_telegram_message

ROOT_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT_DIR / "runtime"
HEARTBEATS_PATH = RUNTIME_DIR / "monitor_heartbeats.json"
HEARTBEAT_KEY = "check_engine_crash_loop"
TARGET_SERVICES = ("engine", "dashboard")
RESTART_LIMIT = 5

LOGGER = logging.getLogger("crash_loop_check")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [crash_loop_check] %(message)s",
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


def _flag_path(service: str) -> Path:
    return RUNTIME_DIR / f"crash_loop_notified_{service}.flag"


def _run_command(args: List[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _parse_compose_ps_json(raw: str) -> List[Dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return []

    # Prefer NDJSON (one object per line). Fallback: single JSON array/object.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1 or (lines and lines[0].startswith("{")):
        items: List[Dict[str, Any]] = []
        for line in lines:
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                items.append(obj)
        if items:
            return items

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return []


def _fetch_compose_ps(compose_file: Path, project_dir: Path) -> List[Dict[str, Any]]:
    proc = _run_command(
        ["docker", "compose", "-f", str(compose_file), "ps", "-a", "--format", "json"],
        cwd=project_dir,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"docker compose ps failed (exit={proc.returncode}): {err}")
    return _parse_compose_ps_json(proc.stdout)


def _fetch_restart_count(container_name: str, project_dir: Path) -> int:
    proc = _run_command(
        ["docker", "inspect", "-f", "{{.RestartCount}}", container_name],
        cwd=project_dir,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"docker inspect failed for {container_name}: {err}")
    text = (proc.stdout or "").strip()
    try:
        return int(text)
    except ValueError as exc:
        raise RuntimeError(f"invalid RestartCount for {container_name}: {text!r}") from exc


def _service_name(entry: Dict[str, Any]) -> str:
    value = entry.get("Service") or entry.get("service") or ""
    return str(value).strip()


def _container_name(entry: Dict[str, Any]) -> str:
    value = entry.get("Name") or entry.get("name") or ""
    return str(value).strip()


def _container_state(entry: Dict[str, Any]) -> str:
    value = entry.get("State") or entry.get("state") or ""
    return str(value).strip().lower()


def _notify_flag_if_needed(service: str, restart_count: int) -> None:
    flag = _flag_path(service)
    if flag.exists():
        LOGGER.info(
            "Crash-loop flag already present for %s; skip Telegram notification.",
            service,
        )
        return

    message = (
        f"[{service}] がクラッシュループ後に停止しました。"
        f"手動確認が必要です。RestartCount={restart_count}"
    )
    sent = send_telegram_message(message)
    if sent:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        flag.write_text(message + "\n", encoding="utf-8")
        LOGGER.warning("Crash-loop notification sent for %s. Flag created: %s", service, flag)
    else:
        LOGGER.error("Failed to send Telegram notification for %s.", service)


def _clear_flag_if_present(service: str) -> None:
    flag = _flag_path(service)
    if not flag.exists():
        return
    flag.unlink()
    LOGGER.info("Service %s is running again. Removed flag: %s", service, flag)


def check_services(compose_file: Path) -> int:
    project_dir = compose_file.resolve().parent
    entries = _fetch_compose_ps(compose_file, project_dir)
    by_service: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        service = _service_name(entry)
        if service in TARGET_SERVICES:
            by_service[service] = entry

    for service in TARGET_SERVICES:
        entry: Optional[Dict[str, Any]] = by_service.get(service)
        if entry is None:
            LOGGER.info("Service %s not found in compose ps output; skipped.", service)
            continue

        container = _container_name(entry)
        state = _container_state(entry)
        if not container:
            LOGGER.warning("Service %s has empty container name; skipped.", service)
            continue

        if state == "running":
            # engine_status=PAUSED もコンテナは running のままのため、
            # ここを通過し crash-loop とは判定されない。
            _clear_flag_if_present(service)
            continue

        if state != "exited":
            LOGGER.info(
                "Service %s state=%s; crash-loop judgement skipped.",
                service,
                state,
            )
            continue

        restart_count = _fetch_restart_count(container, project_dir)
        LOGGER.info(
            "Service %s state=exited RestartCount=%d container=%s",
            service,
            restart_count,
            container,
        )
        if restart_count >= RESTART_LIMIT:
            _notify_flag_if_needed(service, restart_count)
        else:
            LOGGER.info(
                "Service %s exited but RestartCount=%d < %d; not a exhausted crash loop.",
                service,
                restart_count,
                RESTART_LIMIT,
            )

    _record_monitor_heartbeat()
    return 0


def main() -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(
        description="Detect Docker Compose on-failure restart exhaustion and notify via Telegram."
    )
    parser.add_argument(
        "--compose-file",
        "-f",
        default=str(ROOT_DIR / "docker-compose.yml"),
        help="Path to docker-compose file (production or test).",
    )
    args = parser.parse_args()
    compose_file = Path(args.compose_file)
    if not compose_file.is_absolute():
        compose_file = (Path.cwd() / compose_file).resolve()
    else:
        compose_file = compose_file.resolve()

    if not compose_file.exists():
        LOGGER.error("compose file not found: %s", compose_file)
        return 1

    try:
        return check_services(compose_file)
    except Exception as exc:
        LOGGER.error("Crash-loop check failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
