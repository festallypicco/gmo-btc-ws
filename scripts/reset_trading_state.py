"""Reset live_state.db and archive today's trading log for a clean restart."""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "runtime" / "live_state.db"
LOG_DIR = ROOT / "log"
INITIAL_JPY = 50_000.0


def main() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = LOG_DIR / f"realtime_trading_log_{today}.csv"
    backup_dir = LOG_DIR / f"reset_backup_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    if log_path.exists():
        shutil.copy2(log_path, backup_dir / log_path.name)
        print(f"[reset] copied log to backup: {backup_dir / log_path.name}")

    if DB_PATH.exists():
        shutil.copy2(DB_PATH, backup_dir / "live_state.db.before_reset")
        print(f"[reset] db backup: {backup_dir / 'live_state.db.before_reset'}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            updated_at TEXT,
            best_bid_price REAL, best_bid_size REAL,
            best_ask_price REAL, best_ask_size REAL,
            jpy_balance REAL,
            position_side TEXT, position_entry_price REAL, position_size REAL,
            position_is_pending INTEGER, position_exit_target REAL,
            win_count INTEGER, loss_count INTEGER,
            total_gross_win REAL, total_gross_loss REAL, cumulative_pnl REAL,
            active_profile_name TEXT,
            engine_status TEXT,
            config_version TEXT,
            ws_connected INTEGER,
            trading_day_date TEXT,
            daily_start_balance REAL,
            daily_realized_pnl REAL
        )
        """
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO live_state (
            id, updated_at,
            best_bid_price, best_bid_size, best_ask_price, best_ask_size,
            jpy_balance,
            position_side, position_entry_price, position_size,
            position_is_pending, position_exit_target,
            win_count, loss_count, total_gross_win, total_gross_loss, cumulative_pnl,
            active_profile_name, engine_status, config_version, ws_connected,
            trading_day_date, daily_start_balance, daily_realized_pnl
        ) VALUES (
            1, ?, NULL, NULL, NULL, NULL, ?,
            NULL, NULL, NULL, 0, NULL,
            0, 0, 0.0, 0.0, 0.0,
            NULL, NULL, 'reset', 0,
            NULL, NULL, NULL
        )
        """,
        (datetime.now().isoformat(timespec="seconds"), INITIAL_JPY),
    )
    conn.commit()
    conn.close()
    print(f"[reset] live_state reset: jpy_balance={INITIAL_JPY:.0f}, position=FLAT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
