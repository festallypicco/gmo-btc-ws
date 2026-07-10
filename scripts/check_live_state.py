import datetime as dt
import os
import sqlite3
import sys


def main() -> int:
    db_path = os.environ.get("LIVE_DB")
    if not db_path:
        return 1

    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        row = conn.execute("SELECT updated_at FROM live_state WHERE id=1").fetchone()
        if not row or not row[0]:
            return 1
        ts = dt.datetime.fromisoformat(row[0])
        return 0 if (dt.datetime.now() - ts).total_seconds() <= 10 else 1
    except Exception:
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
