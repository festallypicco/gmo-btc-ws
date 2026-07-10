"""
process_launcher.py
-------------------
Cross-platform engine process launcher with append-mode log redirection.
Used by restart_engine.ps1 / ensure_engine_running.ps1 on Windows;
same module can be used directly on Linux (Ubuntu VPS) later.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_HELD_LOG_HANDLES: tuple | None = None


def start_engine_process(
    engine_script_path: str | Path,
    log_path: str | Path,
    err_log_path: str | Path,
    working_directory: str | Path | None = None,
) -> int:
    """
    Start trading_engine.py as a background process.

    stdout/stderr are appended to log_path / err_log_path (never truncated).
    Returns the spawned process PID.
    """
    engine_path = Path(engine_script_path).resolve()
    if not engine_path.is_file():
        raise FileNotFoundError(f"engine script not found: {engine_path}")

    workdir = Path(working_directory).resolve() if working_directory else engine_path.parent
    log_file = Path(log_path)
    err_file = Path(err_log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    err_file.parent.mkdir(parents=True, exist_ok=True)

    log_handle = open(log_file, "a", encoding="utf-8", buffering=1)
    err_handle = open(err_file, "a", encoding="utf-8", buffering=1)

    popen_kwargs: dict = {
        "args": [sys.executable, "-u", str(engine_path)],
        "cwd": str(workdir),
        "stdout": log_handle,
        "stderr": err_handle,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    else:
        popen_kwargs["start_new_session"] = True
        popen_kwargs["close_fds"] = True

    proc = subprocess.Popen(**popen_kwargs)
    # Keep handles alive until launcher exits (closing early breaks child I/O on Windows).
    global _HELD_LOG_HANDLES
    _HELD_LOG_HANDLES = (log_handle, err_handle)
    return int(proc.pid)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch trading engine with append log redirection")
    parser.add_argument("--engine-script", required=True, help="Path to trading_engine.py")
    parser.add_argument("--log-path", required=True, help="Append destination for stdout")
    parser.add_argument("--err-log-path", required=True, help="Append destination for stderr")
    parser.add_argument(
        "--working-directory",
        default=None,
        help="Process working directory (default: parent of engine script)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        pid = start_engine_process(
            engine_script_path=args.engine_script,
            log_path=args.log_path,
            err_log_path=args.err_log_path,
            working_directory=args.working_directory,
        )
    except Exception as exc:
        print(f"[ERROR] process_launcher failed: {exc}", file=sys.stderr)
        return 1
    print(pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
