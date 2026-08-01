"""
test_live_state_order_ids.py

entry_order_id / tp_order_id / sl_order_id / position_id の live_state 永続化・復元テスト。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import trading_engine as te  # noqa: E402
import virtual_trader as virtual_trader_module  # noqa: E402
from strategy_logic import OrderbookSnapshot, PositionState  # noqa: E402
from virtual_trader import ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC, VirtualTrader  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_trade_csv_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)
    return log_dir


class _FakeWsManager:
    def __init__(self, snap: Optional[OrderbookSnapshot] = None) -> None:
        self.latest_snapshot = snap


def test_write_live_state_persists_order_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "live_state.db"
    monkeypatch.setattr(te, "LIVE_STATE_DB_PATH", db_path)

    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=False,
        exit_price_target=10_015_000.0,
        entry_order_id=1001,
        tp_order_id=2001,
        sl_order_id=2002,
        position_id=90001,
    )
    trader._position_filled_at = datetime(2026, 7, 25, 12, 0, 0)
    ws = _FakeWsManager(
        OrderbookSnapshot(
            best_bid_price=10_000_000.0,
            best_bid_size=0.5,
            best_ask_price=10_000_100.0,
            best_ask_size=0.5,
        )
    )

    te._write_live_state(trader, ws)  # type: ignore[arg-type]

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT entry_order_id, tp_order_id, sl_order_id, position_id, position_side
            FROM live_state WHERE id = 1
            """
        ).fetchone()
    assert row is not None
    assert row[0] == 1001
    assert row[1] == 2001
    assert row[2] == 2002
    assert row[3] == 90001
    assert row[4] == "LONG"


def test_load_persisted_order_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "live_state.db"
    monkeypatch.setattr(te, "LIVE_STATE_DB_PATH", db_path)

    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    trader.position = PositionState(
        side="SHORT",
        entry_price=10_500_000.0,
        size=0.02,
        is_pending=False,
        exit_price_target=10_484_250.0,
        entry_order_id=3001,
        tp_order_id=4001,
        sl_order_id=4002,
        position_id=90002,
    )
    te._write_live_state(trader, _FakeWsManager())  # type: ignore[arg-type]

    loaded = te._load_daily_loss_persisted()
    assert loaded["entry_order_id"] == 3001
    assert loaded["tp_order_id"] == 4001
    assert loaded["sl_order_id"] == 4002
    assert loaded["position_id"] == 90002


def test_restore_persisted_position_applies_order_ids() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0)
    result = trader.restore_persisted_position(
        position_side="LONG",
        position_entry_price=10_800_000.0,
        position_size=0.001,
        position_is_pending=0,
        position_exit_target=10_812_960.0,
        position_filled_at="2026-07-22T12:00:00",
        entry_order_id=111,
        tp_order_id=222,
        sl_order_id=333,
        position_id=444,
    )
    assert result["status"] == "restored"
    assert trader.position.entry_order_id == 111
    assert trader.position.tp_order_id == 222
    assert trader.position.sl_order_id == 333
    assert trader.position.position_id == 444


def test_restore_persisted_position_none_order_ids_ok() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0)
    result = trader.restore_persisted_position(
        position_side="LONG",
        position_entry_price=10_800_000.0,
        position_size=0.001,
        position_is_pending=0,
        position_exit_target=10_812_960.0,
        position_filled_at="2026-07-22T12:00:00",
        entry_order_id=None,
        tp_order_id=None,
        sl_order_id=None,
        position_id=None,
    )
    assert result["status"] == "restored"
    assert trader.position.entry_order_id is None
    assert trader.position.tp_order_id is None
    assert trader.position.sl_order_id is None
    assert trader.position.position_id is None


def test_write_and_restore_pending_order_placed_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """永続化した発注時刻が復元後も時間タイムアウト判定に使える。"""
    db_path = tmp_path / "live_state.db"
    monkeypatch.setattr(te, "LIVE_STATE_DB_PATH", db_path)

    # 現在より十分古い発注時刻（再起動後も経過時間が保持されること）
    placed_at = datetime(2026, 7, 1, 10, 0, 0)
    trader = VirtualTrader(initial_jpy=50_000.0)
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_001.0,
        size=0.001,
        is_pending=True,
        entry_order_id=555001,
    )
    trader._pending_order_placed_at = placed_at
    trader._locked_profile_name = "daytime"
    ws = _FakeWsManager(
        OrderbookSnapshot(
            best_bid_price=10_000_000.0,
            best_bid_size=2.0,
            best_ask_price=10_000_100.0,
            best_ask_size=1.0,
        )
    )
    te._write_live_state(trader, ws)  # type: ignore[arg-type]

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT pending_order_placed_at, position_is_pending FROM live_state WHERE id=1"
        ).fetchone()
    assert row is not None
    assert row[0] == "2026-07-01T10:00:00"
    assert row[1] == 1

    persisted = te._load_daily_loss_persisted()
    restored = VirtualTrader(initial_jpy=50_000.0)
    restored.restore_persisted_position(
        position_side=persisted.get("position_side"),
        position_entry_price=persisted.get("position_entry_price"),
        position_size=persisted.get("position_size"),
        position_is_pending=persisted.get("position_is_pending"),
        position_exit_target=persisted.get("position_exit_target"),
        position_filled_at=persisted.get("position_filled_at"),
        pending_order_placed_at=persisted.get("pending_order_placed_at"),
        entry_order_id=persisted.get("entry_order_id"),
        locked_profile_name="daytime",
    )
    assert restored.position.is_pending is True
    assert restored._pending_order_placed_at == placed_at

    snap = OrderbookSnapshot(
        best_bid_price=9_999_000.0,
        best_bid_size=2.0,
        best_ask_price=9_999_100.0,
        best_ask_size=1.0,
    )
    _, _, time_met, _ = restored._pending_timeout_conditions(snap)
    assert time_met is True
    restored._check_pending_fill(snap)
    assert restored.position.side is None
    cancel = restored._last_cancel_by_side["BUY"]
    assert cancel is not None
    assert cancel[2] == float(ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC)


def test_migration_adds_pending_order_placed_at_column(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_live_state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE live_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                updated_at TEXT,
                jpy_balance REAL,
                position_side TEXT,
                position_entry_price REAL,
                position_size REAL,
                position_is_pending INTEGER,
                position_exit_target REAL,
                position_filled_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO live_state (
                id, updated_at, jpy_balance, position_side,
                position_entry_price, position_size, position_is_pending,
                position_exit_target, position_filled_at
            ) VALUES (1, '2026-07-25T00:00:00', 50000, NULL, 0, 0, 0, 0, NULL)
            """
        )
        conn.commit()
        te._ensure_live_state_schema(conn)
        conn.commit()
        after_cols = {row[1] for row in conn.execute("PRAGMA table_info(live_state)")}
    assert "pending_order_placed_at" in after_cols


def test_migration_adds_order_id_columns_to_legacy_db(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_live_state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE live_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                updated_at TEXT,
                jpy_balance REAL,
                position_side TEXT,
                position_entry_price REAL,
                position_size REAL,
                position_is_pending INTEGER,
                position_exit_target REAL,
                position_filled_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO live_state (
                id, updated_at, jpy_balance, position_side,
                position_entry_price, position_size, position_is_pending,
                position_exit_target, position_filled_at
            ) VALUES (1, '2026-07-25T00:00:00', 50000, NULL, 0, 0, 0, 0, NULL)
            """
        )
        conn.commit()
        before_cols = {row[1] for row in conn.execute("PRAGMA table_info(live_state)")}
        assert "entry_order_id" not in before_cols
        assert "tp_order_id" not in before_cols
        assert "sl_order_id" not in before_cols
        assert "position_id" not in before_cols

        te._ensure_live_state_schema(conn)
        conn.commit()
        after_cols = {row[1] for row in conn.execute("PRAGMA table_info(live_state)")}

    assert "entry_order_id" in after_cols
    assert "tp_order_id" in after_cols
    assert "sl_order_id" in after_cols
    assert "position_id" in after_cols
