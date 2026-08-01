"""Tests for scripts/repair_live_state_equity.py and engine position reload."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULE_DIR = ROOT / "btc_trading_tool"
SCRIPTS_DIR = ROOT / "scripts"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import repair_live_state_equity as repair  # noqa: E402
import trading_engine as te  # noqa: E402
import virtual_trader as virtual_trader_module  # noqa: E402
from virtual_trader import VirtualTrader  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_virtual_trader_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)
    return log_dir


def _seed_live_state(db_path: Path, **fields) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(te._CREATE_TABLE_SQL)
        te._ensure_live_state_schema(conn)
        cols = {
            "id": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "jpy_balance": 28_500.0,
            "position_side": "SHORT",
            "position_entry_price": 10_700_000.0,
            "position_size": 0.001,
            "position_is_pending": 0,
            "position_exit_target": 10_687_160.0,
            "position_filled_at": "2026-07-23T10:00:00",
            "win_count": 10,
            "loss_count": 5,
            "total_gross_win": 100.0,
            "total_gross_loss": 20.0,
            "cumulative_pnl": 80.0,
            "active_profile_name": "daytime",
            "engine_status": "RUNNING",
            "config_version": "test",
            "ws_connected": 1,
            "best_bid_price": 10_699_000.0,
            "best_ask_price": 10_701_000.0,
            "trading_day_date": "2026-07-23",
            "daily_start_balance": 28_400.0,
            "daily_realized_pnl": 50.0,
            "daily_win_count": 2,
            "daily_loss_count": 1,
        }
        cols.update(fields)
        placeholders = ", ".join(f":{k}" for k in cols)
        conn.execute(
            f"INSERT OR REPLACE INTO live_state ({', '.join(cols)}) "
            f"VALUES ({placeholders})",
            cols,
        )
        conn.commit()


def test_repair_live_state_equity_dry_run_and_apply(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "live_state.db"
    _seed_live_state(db_path, jpy_balance=28_500.0, cumulative_pnl=80.0, position_side="SHORT")
    monkeypatch.setattr(repair, "LIVE_STATE_DB_PATH", db_path)
    monkeypatch.setattr(repair, "PID_PATH", tmp_path / "missing.pid")
    monkeypatch.setattr(repair, "ROOT_DIR", tmp_path)

    dry = repair.repair(db_path=db_path, apply=False)
    assert dry["applied"] is False
    assert dry["jpy_after"] == pytest.approx(50_080.0)
    assert dry["delta_jpy"] == pytest.approx(21_580.0)

    with sqlite3.connect(db_path) as conn:
        before = conn.execute("SELECT jpy_balance FROM live_state WHERE id=1").fetchone()[0]
    assert before == pytest.approx(28_500.0)

    applied = repair.repair(db_path=db_path, apply=True)
    assert applied["applied"] is True
    with sqlite3.connect(db_path) as conn:
        after = conn.execute("SELECT jpy_balance FROM live_state WHERE id=1").fetchone()[0]
    assert after == pytest.approx(50_080.0)


def test_repair_real_long_does_not_subtract_notional(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "live_state.db"
    _seed_live_state(
        db_path,
        jpy_balance=40_000.0,
        cumulative_pnl=-10.0,
        position_side="LONG",
        position_entry_price=10_000_000.0,
        position_size=0.01,
        trading_mode="real",
    )
    monkeypatch.setattr(repair, "PID_PATH", tmp_path / "missing.pid")
    monkeypatch.setattr(repair, "ROOT_DIR", tmp_path)

    result = repair.repair(db_path=db_path, apply=False)
    # real LONG: initial + cumulative (想定元本を差し引かない)
    assert result["trading_mode"] == "real"
    assert result["jpy_after"] == pytest.approx(50_000.0 - 10.0)
    assert result["delta_jpy"] == pytest.approx(9_990.0)


def test_repair_virtual_long_subtracts_notional(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "live_state.db"
    _seed_live_state(
        db_path,
        jpy_balance=40_000.0,
        cumulative_pnl=-10.0,
        position_side="LONG",
        position_entry_price=10_000_000.0,
        position_size=0.01,
        trading_mode="virtual",
    )
    monkeypatch.setattr(repair, "PID_PATH", tmp_path / "missing.pid")
    monkeypatch.setattr(repair, "ROOT_DIR", tmp_path)

    result = repair.repair(db_path=db_path, apply=False)
    # virtual LONG: initial + cumulative - entry * size
    assert result["trading_mode"] == "virtual"
    assert result["jpy_after"] == pytest.approx(50_000.0 - 10.0 - 100_000.0)
    assert result["delta_jpy"] == pytest.approx(-90_010.0)


def test_repair_real_long_apply_requires_confirm_real(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "live_state.db"
    _seed_live_state(
        db_path,
        jpy_balance=40_000.0,
        cumulative_pnl=-10.0,
        position_side="LONG",
        position_entry_price=10_000_000.0,
        position_size=0.01,
        trading_mode="real",
    )
    monkeypatch.setattr(repair, "PID_PATH", tmp_path / "missing.pid")
    monkeypatch.setattr(repair, "ROOT_DIR", tmp_path)

    with pytest.raises(RuntimeError, match="--confirm-real"):
        repair.repair(db_path=db_path, apply=True, confirm_real=False)

    with sqlite3.connect(db_path) as conn:
        before = conn.execute("SELECT jpy_balance FROM live_state WHERE id=1").fetchone()[0]
    assert before == pytest.approx(40_000.0)

    applied = repair.repair(db_path=db_path, apply=True, confirm_real=True)
    assert applied["applied"] is True
    with sqlite3.connect(db_path) as conn:
        after = conn.execute("SELECT jpy_balance FROM live_state WHERE id=1").fetchone()[0]
    assert after == pytest.approx(49_990.0)


def test_engine_reload_restores_open_long_position(tmp_path: Path, monkeypatch) -> None:
    """ポジション保持中の DB 状態を読み戻す（再起動シナリオの再現）。"""
    db_path = tmp_path / "live_state.db"
    _seed_live_state(
        db_path,
        jpy_balance=39_250.0,
        cumulative_pnl=50.0,
        position_side="LONG",
        position_entry_price=10_800_000.0,
        position_size=0.001,
        position_is_pending=0,
        position_exit_target=10_812_960.0,
        position_filled_at="2026-07-22T12:00:00",
        best_bid_price=10_800_000.0,
        best_ask_price=10_802_000.0,
    )
    monkeypatch.setattr(te, "LIVE_STATE_DB_PATH", db_path)

    persisted = te._load_daily_loss_persisted()
    trader = VirtualTrader(initial_jpy=50_000.0)
    trader.restore_persisted_account_state(
        jpy_balance=persisted.get("jpy_balance"),
        cumulative_pnl=persisted.get("cumulative_pnl"),
        win_count=persisted.get("win_count"),
        loss_count=persisted.get("loss_count"),
        total_gross_win=persisted.get("total_gross_win"),
        total_gross_loss=persisted.get("total_gross_loss"),
    )
    result = trader.restore_persisted_position(
        position_side=persisted.get("position_side"),
        position_entry_price=persisted.get("position_entry_price"),
        position_size=persisted.get("position_size"),
        position_is_pending=persisted.get("position_is_pending"),
        position_exit_target=persisted.get("position_exit_target"),
        position_filled_at=persisted.get("position_filled_at"),
        locked_profile_name=persisted.get("active_profile_name"),
    )
    assert result["status"] == "restored"
    assert trader.position.side == "LONG"
    assert trader.position.entry_price == pytest.approx(10_800_000.0)
    assert trader.position.size == pytest.approx(0.001)
    assert trader._position_filled_at == datetime(2026, 7, 22, 12, 0, 0)
    assert trader.check_account_integrity(mid_price=10_801_000.0)["ok"] is True
