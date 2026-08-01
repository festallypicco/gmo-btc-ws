"""
tests/test_csv_db_consistency.py

scripts/check_csv_db_consistency.py の CSV / DB 増分突き合わせを検証する。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from check_csv_db_consistency import (  # noqa: E402
    _count_csv_exits,
    run_consistency_check,
)


def _write_live_state(db_path: Path, win_count: int, loss_count: int) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                updated_at TEXT,
                win_count INTEGER,
                loss_count INTEGER,
                cumulative_pnl REAL
            )
            """
        )
        conn.execute("DELETE FROM live_state WHERE id = 1")
        conn.execute(
            """
            INSERT INTO live_state (id, updated_at, win_count, loss_count, cumulative_pnl)
            VALUES (1, ?, ?, ?, 0.0)
            """,
            (datetime.now().isoformat(timespec="seconds"), win_count, loss_count),
        )
        conn.commit()


def _write_state(
    state_path: Path,
    check_day: date,
    db_exit_count: int,
    csv_exit_count: int,
    last_engine_pid: Optional[str] = None,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "check_date": check_day.isoformat(),
                "db_exit_count": db_exit_count,
                "csv_exit_count": csv_exit_count,
                "last_engine_pid": last_engine_pid,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_pid(pid_path: Path, pid: str) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(pid, encoding="utf-8")


def _write_csv(csv_path: Path, reasons: list[str]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["timestamp,trade_id,side,order_type,reason"]
    for i, reason in enumerate(reasons, start=1):
        lines.append(f"2026-07-14 10:00:0{i},t{i},SELL,MAKER,{reason}")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_state(state_path: Path) -> dict:
    return json.loads(state_path.read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path: Path):
    day = date(2026, 7, 14)
    db_path = tmp_path / "live_state.db"
    log_dir = tmp_path / "log"
    state_path = tmp_path / "csv_db_consistency_state.json"
    csv_path = log_dir / f"realtime_trading_log_{day.isoformat()}.csv"
    pid_path = tmp_path / "trading_engine.pid"
    send_message = MagicMock(return_value=True)
    return {
        "day": day,
        "now": datetime(2026, 7, 14, 12, 0, 0),
        "db_path": db_path,
        "log_dir": log_dir,
        "state_path": state_path,
        "csv_path": csv_path,
        "pid_path": pid_path,
        "send_message": send_message,
    }


def _run(env, **kwargs):
    params = {
        "now": env["now"],
        "db_path": env["db_path"],
        "log_dir": env["log_dir"],
        "state_path": env["state_path"],
        "pid_path": env["pid_path"],
        "send_message": env["send_message"],
    }
    params.update(kwargs)
    return run_consistency_check(**params)


def test_matching_deltas_do_not_notify(env) -> None:
    _write_state(env["state_path"], env["day"], db_exit_count=5, csv_exit_count=5, last_engine_pid="100")
    _write_pid(env["pid_path"], "100")
    _write_live_state(env["db_path"], win_count=6, loss_count=1)  # total 7 (+2)
    _write_csv(
        env["csv_path"],
        [
            "TAKE_PROFIT",
            "TAKE_PROFIT",
            "TAKE_PROFIT",
            "STOP_LOSS",
            "STOP_LOSS",
            "FORCE_CLOSE_MAINTENANCE",
            "FORCE_CLOSE_MAINTENANCE",
            "ENTRY",
        ],
    )  # 7 exits (+2)

    rc = _run(env)
    assert rc == 0
    env["send_message"].assert_not_called()
    state = _load_state(env["state_path"])
    assert state["db_exit_count"] == 7
    assert state["csv_exit_count"] == 7
    assert state["last_engine_pid"] == "100"


def test_mismatching_deltas_send_telegram(env) -> None:
    _write_state(env["state_path"], env["day"], db_exit_count=5, csv_exit_count=5, last_engine_pid="100")
    _write_pid(env["pid_path"], "100")
    _write_live_state(env["db_path"], win_count=8, loss_count=0)  # total 8 (+3)
    _write_csv(
        env["csv_path"],
        ["TAKE_PROFIT", "STOP_LOSS", "TAKE_PROFIT", "STOP_LOSS", "TAKE_PROFIT", "ENTRY"],
    )  # 5 exits (+0)

    rc = _run(env)
    assert rc == 0
    env["send_message"].assert_called_once()
    message = env["send_message"].call_args.args[0]
    assert "db_delta=3" in message
    assert "csv_delta=0" in message
    assert "diff=3" in message
    state = _load_state(env["state_path"])
    assert state["db_exit_count"] == 8
    assert state["csv_exit_count"] == 5
    assert state["last_engine_pid"] == "100"


def test_db_counter_reset_with_pid_change_skips_notify(env) -> None:
    _write_state(
        env["state_path"],
        env["day"],
        db_exit_count=20,
        csv_exit_count=18,
        last_engine_pid="111",
    )
    _write_pid(env["pid_path"], "222")
    _write_live_state(env["db_path"], win_count=1, loss_count=1)  # total 2 (< 20)
    _write_csv(env["csv_path"], ["TAKE_PROFIT", "STOP_LOSS", "ENTRY"])  # 2 exits

    rc = _run(env)
    assert rc == 0
    env["send_message"].assert_not_called()
    state = _load_state(env["state_path"])
    assert state["check_date"] == env["day"].isoformat()
    assert state["db_exit_count"] == 2
    assert state["csv_exit_count"] == 2
    assert state["last_engine_pid"] == "222"


def test_db_counter_reset_with_same_pid_sends_telegram(env) -> None:
    _write_state(
        env["state_path"],
        env["day"],
        db_exit_count=20,
        csv_exit_count=18,
        last_engine_pid="333",
    )
    _write_pid(env["pid_path"], "333")
    _write_live_state(env["db_path"], win_count=1, loss_count=1)  # total 2 (< 20)
    _write_csv(env["csv_path"], ["TAKE_PROFIT", "STOP_LOSS", "ENTRY"])  # 2 exits

    rc = _run(env)
    assert rc == 0
    env["send_message"].assert_called_once()
    message = env["send_message"].call_args.args[0]
    assert "再起動の形跡なくカウンターが減少" in message
    assert "prev_db_exit_count=20" in message
    assert "db_exit_count=2" in message
    assert "engine_pid=333" in message
    state = _load_state(env["state_path"])
    assert state["db_exit_count"] == 2
    assert state["csv_exit_count"] == 2
    assert state["last_engine_pid"] == "333"


def test_db_counter_reset_null_to_pid_treated_as_restart(env) -> None:
    _write_state(
        env["state_path"],
        env["day"],
        db_exit_count=20,
        csv_exit_count=18,
        last_engine_pid=None,
    )
    _write_pid(env["pid_path"], "444")
    _write_live_state(env["db_path"], win_count=1, loss_count=0)  # total 1 (< 20)
    _write_csv(env["csv_path"], ["TAKE_PROFIT"])

    rc = _run(env)
    assert rc == 0
    env["send_message"].assert_not_called()
    state = _load_state(env["state_path"])
    assert state["db_exit_count"] == 1
    assert state["csv_exit_count"] == 1
    assert state["last_engine_pid"] == "444"


def test_date_change_skips_notify_and_refreshes_baseline(env) -> None:
    prev_day = date(2026, 7, 13)
    _write_state(
        env["state_path"],
        prev_day,
        db_exit_count=10,
        csv_exit_count=10,
        last_engine_pid="100",
    )
    _write_pid(env["pid_path"], "100")
    _write_live_state(env["db_path"], win_count=3, loss_count=1)  # total 4
    _write_csv(
        env["csv_path"],
        ["TAKE_PROFIT", "STOP_LOSS", "FORCE_CLOSE_MAINTENANCE", "ENTRY"],
    )  # 3 exits

    rc = _run(env)
    assert rc == 0
    env["send_message"].assert_not_called()
    state = _load_state(env["state_path"])
    assert state["check_date"] == "2026-07-14"
    assert state["db_exit_count"] == 4
    assert state["csv_exit_count"] == 3
    assert state["last_engine_pid"] == "100"


def test_missing_csv_treated_as_zero(env) -> None:
    _write_state(
        env["state_path"],
        env["day"],
        db_exit_count=0,
        csv_exit_count=0,
        last_engine_pid=None,
    )
    _write_live_state(env["db_path"], win_count=0, loss_count=0)
    assert not env["csv_path"].exists()
    assert not env["pid_path"].exists()

    rc = _run(env)
    assert rc == 0
    env["send_message"].assert_not_called()
    assert _count_csv_exits(env["csv_path"]) == 0
    state = _load_state(env["state_path"])
    assert state["csv_exit_count"] == 0
    assert state["last_engine_pid"] is None
