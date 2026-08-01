"""
run_engine_with_file_logs.py
----------------------------
Foreground engine entrypoint that appends stdout/stderr to dated log files.

Used by Docker (docker-compose.yml / docker-compose.real-test.yml;
bind-mounted /app/log). Native Windows keeps using process_launcher.py
via restart_engine.ps1.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from process_launcher import attach_engine_file_logs  # noqa: E402


def main() -> int:
    # Import after path setup; trading_engine may reconfigure stdout encoding.
    import trading_engine

    log_path, err_path = attach_engine_file_logs(ROOT_DIR / "log")
    print(f"[Engine] file logging enabled stdout={log_path} stderr={err_path}")
    trading_engine.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
