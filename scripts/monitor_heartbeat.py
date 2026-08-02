"""Shared atomic writer for runtime/monitor_heartbeats.json.

Multiple systemd timers (crash-loop, orphan-orders, csv-consistency, ...)
may finish around the same minute and update different keys. Use an exclusive
file lock for read-modify-write, and a per-process temp name so os.replace
cannot race on a shared *.tmp path.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional


def _lock_path_for(heartbeats_path: Path) -> Path:
    return heartbeats_path.with_name(heartbeats_path.name + ".lock")


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Binary mode keeps msvcrt.locking happy on Windows; flock ignores content.
    fd = open(lock_path, "a+b")
    locked = False
    try:
        if sys.platform == "win32":
            import msvcrt

            fd.seek(0, os.SEEK_END)
            if fd.tell() == 0:
                fd.write(b"0")
                fd.flush()
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            locked = True
        else:
            import fcntl

            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            fd.close()
        except OSError:
            pass


def _read_heartbeats(path: Path, logger: Optional[logging.Logger]) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded
    except Exception as exc:
        if logger is not None:
            logger.warning("Failed to read heartbeats file; recreating: %s", exc)
    return {}


def record_monitor_heartbeat(
    heartbeats_path: Path,
    heartbeat_key: str,
    *,
    logger: Optional[logging.Logger] = None,
    now: Optional[datetime] = None,
) -> None:
    """Merge ``heartbeat_key`` into heartbeats JSON and replace atomically."""
    heartbeats_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    tmp_path = heartbeats_path.with_name(
        f"{heartbeats_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )

    with _exclusive_lock(_lock_path_for(heartbeats_path)):
        data = _read_heartbeats(heartbeats_path, logger)
        data[str(heartbeat_key)] = stamp
        try:
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp_path, heartbeats_path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
