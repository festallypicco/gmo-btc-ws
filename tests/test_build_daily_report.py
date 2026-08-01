"""
tests/test_build_daily_report.py

scripts/build_daily_report.py の日次レポート集計・文言を検証する。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from build_daily_report import (  # noqa: E402
    append_daily_history,
    build_report_message,
    count_ai_review_activity,
    count_settlements,
    format_ai_review_report_lines,
    format_circuit_breaker_lines,
    format_heartbeat_lines,
    get_target_trading_day,
    position_restore_occurred,
)


def _write_live_state(
    db_path: Path,
    trading_day_date: str,
    daily_realized_pnl: float,
    jpy_balance: float | None = None,
    *,
    position_side: str | None = None,
    position_size: float | None = None,
    position_entry_price: float | None = None,
    best_bid_price: float | None = None,
    best_ask_price: float | None = None,
    config_version: str | None = None,
    active_profile_name: str | None = None,
    trading_mode: str | None = None,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_state (
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
        conn.execute("DELETE FROM live_state WHERE id = 1")
        conn.execute(
            """
            INSERT INTO live_state (
                id, trading_day_date, daily_realized_pnl, jpy_balance,
                position_side, position_size, position_entry_price,
                best_bid_price, best_ask_price,
                config_version, active_profile_name, trading_mode
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trading_day_date,
                daily_realized_pnl,
                jpy_balance,
                position_side,
                position_size,
                position_entry_price,
                best_bid_price,
                best_ask_price,
                config_version,
                active_profile_name,
                trading_mode,
            ),
        )
        conn.commit()


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "timestamp,trade_id,side,order_type,reason,price,size,fee,pnl\n"
    lines = [header]
    for row in rows:
        lines.append(
            "{timestamp},{trade_id},{side},{order_type},{reason},{price},{size},{fee},{pnl}\n".format(
                timestamp=row["timestamp"],
                trade_id=row.get("trade_id", "t1"),
                side=row.get("side", "SELL"),
                order_type=row.get("order_type", "MAKER"),
                reason=row["reason"],
                price=row.get("price", "10000000"),
                size=row.get("size", "0.01"),
                fee=row.get("fee", "0"),
                pnl=row["pnl"],
            )
        )
    path.write_text("".join(lines), encoding="utf-8")


def test_count_settlements_in_window(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    _write_csv(
        log_dir / "realtime_trading_log_2026-07-15.csv",
        [
            {
                "timestamp": "2026-07-15 06:00:00",
                "reason": "TAKE_PROFIT",
                "pnl": "100",
            },
            {
                "timestamp": "2026-07-15 12:00:00",
                "reason": "STOP_LOSS",
                "pnl": "-50",
            },
            {
                "timestamp": "2026-07-15 23:59:59",
                "reason": "FORCE_CLOSE_MAINTENANCE",
                "pnl": "10",
            },
        ],
    )
    total, wins = count_settlements("2026-07-15", log_dir=log_dir)
    assert total == 3
    assert wins == 2


def test_count_settlements_excludes_outside_window(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    _write_csv(
        log_dir / "realtime_trading_log_2026-07-15.csv",
        [
            {
                "timestamp": "2026-07-15 05:59:59",
                "reason": "TAKE_PROFIT",
                "pnl": "100",
            },
            {
                "timestamp": "2026-07-15 06:00:00",
                "reason": "TAKE_PROFIT",
                "pnl": "100",
            },
        ],
    )
    _write_csv(
        log_dir / "realtime_trading_log_2026-07-16.csv",
        [
            {
                "timestamp": "2026-07-16 05:59:59",
                "reason": "STOP_LOSS",
                "pnl": "-10",
            },
            {
                "timestamp": "2026-07-16 06:00:00",
                "reason": "TAKE_PROFIT",
                "pnl": "100",
            },
        ],
    )
    total, wins = count_settlements("2026-07-15", log_dir=log_dir)
    assert total == 2
    assert wins == 1


def test_count_settlements_spans_two_csv_files(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    _write_csv(
        log_dir / "realtime_trading_log_2026-07-15.csv",
        [
            {
                "timestamp": "2026-07-15 22:00:00",
                "reason": "TAKE_PROFIT",
                "pnl": "200",
            },
        ],
    )
    _write_csv(
        log_dir / "realtime_trading_log_2026-07-16.csv",
        [
            {
                "timestamp": "2026-07-16 03:00:00",
                "reason": "STOP_LOSS",
                "pnl": "-30",
            },
        ],
    )
    total, wins = count_settlements("2026-07-15", log_dir=log_dir)
    assert total == 2
    assert wins == 1


def test_count_settlements_missing_csv_is_zero(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    total, wins = count_settlements("2026-07-15", log_dir=log_dir)
    assert total == 0
    assert wins == 0

    _write_csv(
        log_dir / "realtime_trading_log_2026-07-15.csv",
        [
            {
                "timestamp": "2026-07-15 10:00:00",
                "reason": "TAKE_PROFIT",
                "pnl": "1",
            },
        ],
    )
    total2, wins2 = count_settlements("2026-07-15", log_dir=log_dir)
    assert total2 == 1
    assert wins2 == 1


def test_build_report_zero_settlements_no_div_by_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime" / "live_state.db"
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    _write_live_state(db_path, "2026-07-15", 0.0)

    message = build_report_message(db_path=db_path, log_dir=log_dir)
    assert "決済件数: 0件" in message
    assert "勝率: -" in message
    assert "実現損益: +0円" in message


def test_count_settlements_excludes_non_settlement_reasons(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    _write_csv(
        log_dir / "realtime_trading_log_2026-07-15.csv",
        [
            {"timestamp": "2026-07-15 10:00:00", "reason": "ENTRY", "pnl": "-1"},
            {"timestamp": "2026-07-15 10:01:00", "reason": "CANCEL_ORDER", "pnl": "0"},
            {
                "timestamp": "2026-07-15 10:02:00",
                "reason": "FORCE_CANCEL_MAINTENANCE",
                "pnl": "0",
            },
            {"timestamp": "2026-07-15 10:03:00", "reason": "TAKE_PROFIT", "pnl": "50"},
        ],
    )
    total, wins = count_settlements("2026-07-15", log_dir=log_dir)
    assert total == 1
    assert wins == 1


def test_build_report_pnl_positive_and_negative_format(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime" / "live_state.db"
    log_dir = tmp_path / "log"
    _write_csv(
        log_dir / "realtime_trading_log_2026-07-15.csv",
        [
            {
                "timestamp": "2026-07-15 10:00:00",
                "reason": "TAKE_PROFIT",
                "pnl": "100",
            },
            {
                "timestamp": "2026-07-15 11:00:00",
                "reason": "STOP_LOSS",
                "pnl": "-40",
            },
        ],
    )

    _write_live_state(db_path, "2026-07-15", 3240.4)
    msg_pos = build_report_message(db_path=db_path, log_dir=log_dir)
    assert "[日次レポート] 対象日: 2026-07-15 (06:00-翌06:00)" in msg_pos
    assert "決済件数: 2件 (勝率: 50.0%)" in msg_pos
    assert "実現損益: +3,240円" in msg_pos

    _write_live_state(db_path, "2026-07-15", -1500.6)
    msg_neg = build_report_message(db_path=db_path, log_dir=log_dir)
    assert "実現損益: -1,501円" in msg_neg


def test_get_target_trading_day(tmp_path: Path) -> None:
    db_path = tmp_path / "live_state.db"
    _write_live_state(db_path, "2026-07-15", 12.5)
    day, pnl = get_target_trading_day(db_path=db_path)
    assert day == "2026-07-15"
    assert pnl == 12.5


def test_append_daily_history(tmp_path: Path) -> None:
    db_path = tmp_path / "live_state.db"
    log_dir = tmp_path / "log"
    history_path = tmp_path / "log" / "daily_history.jsonl"
    _write_live_state(db_path, "2026-07-15", 1000.0, jpy_balance=51_234.0)
    _write_csv(
        log_dir / "realtime_trading_log_2026-07-15.csv",
        [
            {
                "timestamp": "2026-07-15 10:00:00",
                "reason": "TAKE_PROFIT",
                "pnl": "100",
            },
            {
                "timestamp": "2026-07-15 11:00:00",
                "reason": "STOP_LOSS",
                "pnl": "-50",
            },
        ],
    )

    append_daily_history(
        db_path=db_path, log_dir=log_dir, history_path=history_path
    )

    lines = history_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["trading_day"] == "2026-07-15"
    assert row["settlements"] == 2
    assert row["wins"] == 1
    assert row["losses"] == 1
    assert row["realized_pnl"] == 1000.0
    assert row["jpy_balance"] == 51_234.0
    assert row["total_assets_eod"] == 51_234.0


def test_append_daily_history_records_total_assets_eod_with_open_long(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime" / "live_state.db"
    log_dir = tmp_path / "log"
    history_path = tmp_path / "log" / "daily_history.jsonl"
    _write_live_state(
        db_path,
        "2026-07-15",
        1000.0,
        jpy_balance=39_046.0,
        position_side="LONG",
        position_size=0.001,
        position_entry_price=10_838_554.0,
        best_bid_price=10_838_000.0,
        best_ask_price=10_839_000.0,
    )
    _write_csv(
        log_dir / "realtime_trading_log_2026-07-15.csv",
        [
            {
                "timestamp": "2026-07-15 10:00:00",
                "reason": "TAKE_PROFIT",
                "pnl": "100",
            },
        ],
    )

    append_daily_history(
        db_path=db_path, log_dir=log_dir, history_path=history_path
    )

    row = json.loads(history_path.read_text(encoding="utf-8").strip())
    assert row["jpy_balance"] == 39_046.0
    assert row["total_assets_eod"] == 39_046.0 + 0.001 * 10_838_500.0


def test_append_daily_history_real_long_uses_unrealized_only(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime" / "live_state.db"
    log_dir = tmp_path / "log"
    history_path = tmp_path / "log" / "daily_history.jsonl"
    entry = 10_800_000.0
    mid = 10_838_500.0
    size = 0.001
    jpy = 49_990.0
    _write_live_state(
        db_path,
        "2026-07-15",
        1000.0,
        jpy_balance=jpy,
        position_side="LONG",
        position_size=size,
        position_entry_price=entry,
        best_bid_price=10_838_000.0,
        best_ask_price=10_839_000.0,
        trading_mode="real",
    )
    _write_csv(
        log_dir / "realtime_trading_log_2026-07-15.csv",
        [
            {
                "timestamp": "2026-07-15 10:00:00",
                "reason": "TAKE_PROFIT",
                "pnl": "100",
            },
        ],
    )

    append_daily_history(
        db_path=db_path, log_dir=log_dir, history_path=history_path
    )

    row = json.loads(history_path.read_text(encoding="utf-8").strip())
    assert row["total_assets_eod"] == jpy + (mid - entry) * size
    assert row["total_assets_eod"] != jpy + size * mid


def test_management_sections_appended_to_daily_report(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime" / "live_state.db"
    log_dir = tmp_path / "log"
    reason_path = tmp_path / "runtime" / "manual_stop_reason.json"
    heartbeats_path = tmp_path / "runtime" / "monitor_heartbeats.json"
    restore_path = log_dir / "position_restore_events.jsonl"
    outcomes_path = log_dir / "change_outcomes.jsonl"

    _write_live_state(
        db_path,
        "2026-07-21",
        -73.0,
        jpy_balance=39_000.0,
        config_version="2026-07-21_03-31",
        active_profile_name="daytime",
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    reason_path.parent.mkdir(parents=True, exist_ok=True)
    reason_path.write_text(
        json.dumps(
            {
                "reason": "daily_loss_limit",
                "details": {"limit_jpy": 5000.0, "daily_realized_pnl": -5200.0},
                "triggered_at": "2026-07-21T14:00:00",
            }
        ),
        encoding="utf-8",
    )
    heartbeats_path.write_text(
        json.dumps(
            {
                "check_trading_anomaly": "2026-07-21T14:00:00",
                "check_engine_crash_loop": "2026-07-21T14:05:00",
                "check_csv_db_consistency": "2026-07-21T13:00:00",
            }
        ),
        encoding="utf-8",
    )
    restore_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-21T06:00:15",
                "trading_day": "2026-07-21",
                "status": "restored",
                "side": "LONG",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (log_dir / "ai_review_decision_2026-07-21.json").write_text(
        json.dumps(
            {
                "target_date": "2026-07-21",
                "status": "applied",
                "proposer_output": "- a\n- b\n- c\n",
                "final_payload": {"version": "2026-07-22_03-31"},
            }
        ),
        encoding="utf-8",
    )
    outcomes_path.write_text(
        json.dumps(
            {
                "version": "2026-07-22_03-31",
                "profile_name": "daytime",
                "change_type": "changed",
                "changed_fields": {"max_order_size_btc": {"old": 1, "new": 2}},
            }
        )
        + "\n"
        + json.dumps(
            {
                "version": "2026-07-22_03-31",
                "profile_name": "night",
                "change_type": "changed",
                "changed_fields": {
                    "max_order_size_btc": {"old": 1, "new": 2},
                    "stop_loss_pct": {"old": 0.1, "new": 0.2},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    message = build_report_message(
        db_path=db_path,
        log_dir=log_dir,
        reason_path=reason_path,
        heartbeats_path=heartbeats_path,
        restore_events_path=restore_path,
        change_outcomes_path=outcomes_path,
        now=datetime(2026, 7, 21, 14, 10, 0),
    )
    assert "サーキットブレーカー: 発動あり" in message
    assert "理由: daily_loss_limit" in message
    assert "limit_jpy=5000.0" in message
    assert "夜間AI議論: 成功（提案3件 / 採用3件）" in message
    assert "設定変更:" in message
    assert "daytime.max_order_size_btc: 1 -> 2" in message
    assert "ポジション復元イベント: あり" in message
    assert "監視ハートビート:" in message
    assert "check_trading_anomaly: OK" in message
    assert "config_version: 2026-07-21_03-31" in message
    assert "active_profile_name: daytime" in message


def test_circuit_breaker_other_day_is_inactive(tmp_path: Path) -> None:
    reason_path = tmp_path / "manual_stop_reason.json"
    reason_path.write_text(
        json.dumps(
            {
                "reason": "manual",
                "details": {},
                "triggered_at": "2026-07-20T10:00:00",
            }
        ),
        encoding="utf-8",
    )
    lines = format_circuit_breaker_lines("2026-07-21", reason_path=reason_path)
    assert lines == ["サーキットブレーカー: 発動なし"]


def test_position_restore_and_ai_counts_helpers(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {"trading_day": "2026-07-21", "status": "fallback"}
        )
        + "\n",
        encoding="utf-8",
    )
    assert position_restore_occurred("2026-07-21", events_path=events) is True
    assert position_restore_occurred("2026-07-22", events_path=events) is False

    log_dir = tmp_path / "log"
    log_dir.mkdir()
    (log_dir / "ai_review_decision_2026-07-21.json").write_text(
        json.dumps(
            {
                "proposer_output": "- one\nnot a bullet\n- two\n",
                "final_payload": {"version": "v1"},
            }
        ),
        encoding="utf-8",
    )
    outcomes = tmp_path / "outcomes.jsonl"
    outcomes.write_text(
        json.dumps({"version": "v1", "changed_fields": {"a": 1}}) + "\n",
        encoding="utf-8",
    )
    assert count_ai_review_activity(
        "2026-07-21", log_dir=log_dir, change_outcomes_path=outcomes
    ) == (2, 1)


def test_ai_review_failure_line_in_daily_report(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    (log_dir / "ai_review_decision_2026-07-24.json").write_text(
        json.dumps(
            {
                "status": "failed_before_moderator",
                "error_kind": "クォータ超過",
                "error": "429 RESOURCE_EXHAUSTED",
            }
        ),
        encoding="utf-8",
    )
    lines = format_ai_review_report_lines(
        "2026-07-24",
        log_dir=log_dir,
        change_outcomes_path=tmp_path / "missing.jsonl",
    )
    assert lines == ["夜間AI議論: 失敗（原因: クォータ超過）"]


def test_heartbeat_summary_marks_stale(tmp_path: Path) -> None:
    path = tmp_path / "heartbeats.json"
    path.write_text(
        json.dumps({"check_trading_anomaly": "2026-07-21T10:00:00"}),
        encoding="utf-8",
    )
    lines = format_heartbeat_lines(
        heartbeats_path=path,
        now=datetime(2026, 7, 21, 14, 0, 0),
    )
    assert any("check_trading_anomaly: STALE" in line for line in lines)
    assert any("check_engine_crash_loop: missing" in line for line in lines)
