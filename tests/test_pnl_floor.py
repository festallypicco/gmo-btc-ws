"""
tests/test_pnl_floor.py

PnL 切り捨て揃え（2026-11-06）の単体テスト。
決済 PnL は GMO の整数円切り捨てに合わせ、KPI 累積と CSV の pnl 列へ
floor した値を流す。jpy_balance の更新には使わない。
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT_DIR = Path(__file__).resolve().parent.parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as virtual_trader_module  # noqa: E402
from strategy_logic import OrderbookSnapshot, PositionState  # noqa: E402
from virtual_trader import VirtualTrader  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)
    monkeypatch.setattr(
        virtual_trader_module,
        "_MANUAL_STOP_FLAG_PATH",
        tmp_path / "manual_stop.flag",
    )
    monkeypatch.setattr(
        virtual_trader_module,
        "_EQUITY_GUARD_STATE_PATH",
        tmp_path / "equity_guard.json",
    )
    return log_dir


def _snap(bid: float = 10_000_000.0, ask: float = 10_000_100.0) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_bid_price=bid,
        best_bid_size=0.5,
        best_ask_price=ask,
        best_ask_size=0.5,
    )


def _trader_with_long(
    *,
    trading_mode: str = "virtual",
    entry_price: float = 10_000_000.0,
    size: float = 0.001,
) -> VirtualTrader:
    trader = VirtualTrader(trading_mode=trading_mode)
    trader.position = PositionState(
        side="LONG",
        entry_price=entry_price,
        size=size,
        is_pending=False,
        exit_price_target=entry_price * 1.0012,
    )
    return trader


def test_floored_settlement_pnl_values() -> None:
    floor = VirtualTrader._floored_settlement_pnl
    assert floor(0.65) == 0.0
    assert floor(-0.4) == -1.0
    assert floor(23.6) == 23.0
    assert floor(-20.0) == -20.0
    assert floor(0.0) == 0.0


def test_board_take_profit_fractional_profit_floored_to_zero() -> None:
    trader = _trader_with_long()
    # gross = (10,000,650 - 10,000,000) * 0.001 = +0.65 -> floor 0
    trader._exit_take_profit(_snap(), fill_price=10_000_650.0)

    record = trader.trade_history[-1]
    assert record.reason == "TAKE_PROFIT"
    assert record.pnl == 0.0
    assert record.cumulative_pnl == 0.0
    assert trader.daily_realized_pnl == 0.0
    assert trader._cumulative_pnl == 0.0
    # floor 後の 0 は勝ち/負けどちらにも数えない（GMO の計上どおり）
    assert trader._win_count == 0
    assert trader._loss_count == 0


def test_board_stop_loss_negative_fraction_floored_down() -> None:
    trader = _trader_with_long()
    # gross = (9,999,600 - 10,000,000) * 0.001 = -0.4 -> floor -1
    trader._exit_stop_loss(_snap(bid=9_999_600.0))

    record = trader.trade_history[-1]
    assert record.reason == "STOP_LOSS"
    assert record.pnl == -1.0
    assert trader.daily_realized_pnl == -1.0
    assert trader._cumulative_pnl == -1.0
    assert trader._loss_count == 1
    assert trader._total_gross_loss == 1.0


def test_real_exit_from_execution_floored() -> None:
    trader = _trader_with_long(trading_mode="real")
    trader._settle_real_exit_from_execution_unlocked(
        fill_price=10_000_650.0,
        fill_size=0.001,
        reason="TAKE_PROFIT",
        is_take_profit=True,
        actual_fee=0,
    )
    record = trader.trade_history[-1]
    assert record.pnl == 0.0
    assert trader._cumulative_pnl == 0.0


def test_force_close_maintenance_floored() -> None:
    trader = _trader_with_long()
    trader._force_close_maintenance(_snap(bid=9_999_600.0))
    record = trader.trade_history[-1]
    assert record.reason == "FORCE_CLOSE_MAINTENANCE"
    assert record.pnl == -1.0
    assert trader.daily_realized_pnl == -1.0


def test_csv_pnl_column_uses_floored_value(_isolated_paths: Path) -> None:
    trader = _trader_with_long()
    trader._exit_stop_loss(_snap(bid=9_999_600.0))

    csv_path = _isolated_paths / (
        f"realtime_trading_log_{datetime.now().strftime('%Y-%m-%d')}.csv"
    )
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    stop_rows = [r for r in rows if r["reason"] == "STOP_LOSS"]
    assert len(stop_rows) == 1
    assert float(stop_rows[0]["pnl"]) == -1.0
    assert float(stop_rows[0]["cumulative_pnl"]) == -1.0


def test_cutover_marker_created_once(_isolated_paths: Path) -> None:
    VirtualTrader()
    marker = _isolated_paths / "pnl_cutover.json"
    assert marker.exists()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["floored_from"]

    marker.write_text("sentinel", encoding="utf-8")
    VirtualTrader()
    assert marker.read_text(encoding="utf-8") == "sentinel"
