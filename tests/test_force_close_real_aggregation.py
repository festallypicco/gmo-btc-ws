"""
tests/test_force_close_real_aggregation.py

FORCE_CLOSE_REAL の件数系集計漏れ修正（2026-11-06）の単体テスト。
日次レポート / AI サマリ / change_outcomes / 整合性チェッカーの
決済理由集合に FORCE_CLOSE_REAL が含まれることを検証する（前方のみ）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
_AI_REVIEW = _ROOT / "ai_review"
for _d in (_SCRIPTS, _AI_REVIEW):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import build_ai_review_summary as bas  # noqa: E402
import build_change_outcomes as bco  # noqa: E402
import check_csv_db_consistency as checker  # noqa: E402
from build_daily_report import (  # noqa: E402
    SETTLEMENT_REASONS,
    build_report_message,
    count_settlements,
    format_equity_guard_lines,
)


def _write_trade_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "timestamp,trade_id,side,order_type,reason,price,size,fee,pnl\n"
    lines = [header]
    for row in rows:
        lines.append(
            "{timestamp},t1,SELL,TAKER,{reason},10000000,0.001,0,{pnl}\n".format(
                timestamp=row["timestamp"],
                reason=row["reason"],
                pnl=row["pnl"],
            )
        )
    path.write_text("".join(lines), encoding="utf-8")


def test_reason_sets_include_force_close_real() -> None:
    assert "FORCE_CLOSE_REAL" in SETTLEMENT_REASONS
    assert "FORCE_CLOSE_REAL" in checker.EXIT_REASONS
    assert "FORCE_CLOSE_REAL" in bco.EXIT_REASONS


def test_daily_report_counts_force_close_real(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    _write_trade_csv(
        log_dir / "realtime_trading_log_2026-07-15.csv",
        [
            {"timestamp": "2026-07-15 10:00:00", "reason": "TAKE_PROFIT", "pnl": "120"},
            {"timestamp": "2026-07-15 12:00:00", "reason": "FORCE_CLOSE_REAL", "pnl": "-243"},
        ],
    )
    total, wins = count_settlements("2026-07-15", log_dir=log_dir)
    assert total == 2
    assert wins == 1


def test_ai_summary_counts_force_close_real_as_forced() -> None:
    rows = [
        {
            "reason": "TAKE_PROFIT",
            "pnl": 120.0,
            "duration_sec": 60,
            "profile_name": "full_day",
        },
        {
            "reason": "FORCE_CLOSE_REAL",
            "pnl": -243.0,
            "duration_sec": 30,
            "profile_name": "full_day",
        },
    ]
    summary = bas.summarize_trade_group(rows, requested_days=7, actual_days=7)
    assert summary["exit_count"] == 1
    assert summary["forced_close_count"] == 1
    assert summary["forced_close_total_pnl"] == -243.0
    assert summary["grand_total_pnl"] == -123.0


def test_consistency_checker_counts_force_close_real(tmp_path: Path) -> None:
    csv_path = tmp_path / "realtime_trading_log_2026-07-15.csv"
    _write_trade_csv(
        csv_path,
        [
            {"timestamp": "2026-07-15 10:00:00", "reason": "FORCE_CLOSE_REAL", "pnl": "-100"},
            {"timestamp": "2026-07-15 11:00:00", "reason": "FORCE_CLOSE_REAL", "pnl": "50"},
            {"timestamp": "2026-07-15 12:00:00", "reason": "ENTRY", "pnl": "0"},
        ],
    )
    assert checker._count_csv_exits(csv_path) == 2


# --------------------------------------------------------------------- #
#  日次レポートの equity ガード表示                                       #
# --------------------------------------------------------------------- #


def test_format_equity_guard_lines_not_introduced(tmp_path: Path) -> None:
    lines = format_equity_guard_lines(guard_path=tmp_path / "missing.json")
    assert lines == ["equityガード: 未導入"]


def test_format_equity_guard_lines_stopped(tmp_path: Path) -> None:
    guard_path = tmp_path / "equity_guard.json"
    guard_path.write_text(
        '{"state": "stopped", "baseline_equity": 43887.0, '
        '"stop_equity": 38887.0, "last_equity": 38500.0, '
        '"stopped_at": "2026-11-06T05:00:00"}',
        encoding="utf-8",
    )
    lines = format_equity_guard_lines(guard_path=guard_path)
    assert lines[0] == "equityガード: 停止中"
    assert "43,887円" in lines[1]
    assert "38,887円" in lines[1]
    assert "38,500円" in lines[1]
    assert "2026-11-06T05:00:00" in lines[2]


def _write_live_state(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE live_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                trading_day_date TEXT,
                daily_realized_pnl REAL,
                jpy_balance REAL,
                position_side TEXT,
                position_size REAL,
                position_entry_price REAL,
                best_bid_price REAL,
                best_ask_price REAL,
                config_version TEXT,
                active_profile_name TEXT,
                trading_mode TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO live_state (
                id, trading_day_date, daily_realized_pnl, jpy_balance,
                position_side, position_size, position_entry_price,
                best_bid_price, best_ask_price,
                config_version, active_profile_name, trading_mode
            ) VALUES (1, '2026-07-15', 0.0, 50000.0,
                      NULL, NULL, NULL, NULL, NULL, 'v1', 'full_day', 'real')
            """
        )


def test_report_message_includes_equity_guard_status(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime" / "live_state.db"
    _write_live_state(db_path)
    message = build_report_message(
        db_path=db_path,
        log_dir=tmp_path / "log",
        reason_path=tmp_path / "manual_stop_reason.json",
        heartbeats_path=tmp_path / "monitor_heartbeats.json",
        restore_events_path=tmp_path / "position_restore_events.jsonl",
        change_outcomes_path=tmp_path / "change_outcomes.jsonl",
        equity_guard_path=tmp_path / "equity_guard.json",
    )
    assert "equityガード: 未導入" in message
