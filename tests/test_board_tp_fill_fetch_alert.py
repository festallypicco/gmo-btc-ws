"""
tests/test_board_tp_fill_fetch_alert.py

板TP（REAL-TP）の実約定価格取得連続失敗時の CRITICAL アラートを検証する。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_BTC_DIR = _ROOT / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as virtual_trader_module  # noqa: E402
from strategy_logic import OrderbookSnapshot, PositionState  # noqa: E402
from virtual_trader import VirtualTrader  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_trade_csv_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)
    return log_dir


def _snap(
    *,
    bid: float = 10_000_000.0,
    ask: float = 10_000_100.0,
) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_bid_price=bid,
        best_bid_size=0.5,
        best_ask_price=ask,
        best_ask_size=0.5,
    )


def _real_trader(alerts: List[str] | None = None) -> VirtualTrader:
    alert_list = alerts if alerts is not None else []
    return VirtualTrader(
        initial_jpy=50_000.0,
        trading_mode="real",
        on_critical_alert=lambda msg: alert_list.append(msg),
    )


def _open_long_with_sl(
    trader: VirtualTrader,
    *,
    entry: float = 10_000_000.0,
    size: float = 0.01,
    position_id: int = 289105203,
    sl_order_id: int = 2002,
) -> None:
    trader.position = PositionState(
        side="LONG",
        entry_price=entry,
        size=size,
        is_pending=False,
        exit_price_target=entry * (1 + trader.config.take_profit_pct),
        entry_order_id=100,
        tp_order_id=None,
        sl_order_id=sl_order_id,
        position_id=position_id,
    )
    trader._position_filled_at = datetime.now()


def _run_board_tp_finalize_with_fill(
    trader: VirtualTrader,
    *,
    fill_return: Any,
    close_oid: int = 90001,
) -> None:
    """板TP settle の実約定取得部分だけを直接呼び出すヘルパ。"""
    snap = _snap()
    with patch(
        "virtual_trader.gmo_fetch_order_execution_fill",
        return_value=fill_return,
    ), patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 50_100.0,
            "equity_jpy": 50_100.0,
            "position_size_btc": 0.0,
        },
    ), patch("virtual_trader.time.sleep"):
        if trader.position.side is None:
            _open_long_with_sl(trader)
        trader._finalize_real_board_tp_settle_unlocked(
            snap=snap,
            position_side="LONG",
            size=float(trader.position.size or 0.01),
            fill_price=float(trader.position.exit_price_target or 10_015_000.0),
            close_oid=close_oid,
        )


def test_board_tp_fill_fetch_alerts_on_third_consecutive_failure() -> None:
    """3回連続で実約定価格取得失敗したら CRITICAL アラートが1回送られる。"""
    alerts: List[str] = []
    trader = _real_trader(alerts)

    for _ in range(3):
        _run_board_tp_finalize_with_fill(
            trader, fill_return=(None, None, "empty_list")
        )

    assert trader._board_tp_fill_fetch_fail_streak == 3
    assert len(alerts) == 1
    assert "[CRITICAL] REAL MODE BOARD TP FILL FETCH FAILED" in alerts[0]
    assert "consecutive_failures=3" in alerts[0]
    assert "空レスポンス" in alerts[0]
    assert "empty_list" in alerts[0]


def test_board_tp_fill_fetch_success_resets_streak_then_alerts_again() -> None:
    """4回目成功でカウンタリセット後、再び3回失敗したら再度アラート。"""
    alerts: List[str] = []
    trader = _real_trader(alerts)

    for _ in range(3):
        _run_board_tp_finalize_with_fill(
            trader, fill_return=(None, None, "price_unavailable")
        )
    assert len(alerts) == 1
    assert trader._board_tp_fill_fetch_fail_streak == 3

    _run_board_tp_finalize_with_fill(
        trader, fill_return=(10_016_000.0, 4, None)
    )
    assert trader._board_tp_fill_fetch_fail_streak == 0
    assert len(alerts) == 1

    for _ in range(3):
        _run_board_tp_finalize_with_fill(
            trader, fill_return=(None, None, "empty_list")
        )
    assert trader._board_tp_fill_fetch_fail_streak == 3
    assert len(alerts) == 2
    assert "consecutive_failures=3" in alerts[1]
    assert "空レスポンス" in alerts[1]


def test_board_tp_fill_fetch_alerts_again_at_6_and_9() -> None:
    """失敗継続時は 6回目・9回目でも追加アラート（サイレンスしない）。"""
    alerts: List[str] = []
    trader = _real_trader(alerts)

    for i in range(9):
        _run_board_tp_finalize_with_fill(
            trader,
            fill_return=(None, None, f"exception: boom-{i}"),
        )

    assert trader._board_tp_fill_fetch_fail_streak == 9
    assert len(alerts) == 3
    assert "consecutive_failures=3" in alerts[0]
    assert "consecutive_failures=6" in alerts[1]
    assert "consecutive_failures=9" in alerts[2]
    assert all("例外" in a for a in alerts)
    assert all(
        "[CRITICAL] REAL MODE BOARD TP FILL FETCH FAILED" in a for a in alerts
    )


def test_board_tp_fill_fetch_fee_only_missing_does_not_count_as_failure() -> None:
    """価格は取れて手数料だけ欠損の場合は理論値フォールバック扱いにせずカウンタを進めない。"""
    alerts: List[str] = []
    trader = _real_trader(alerts)

    for _ in range(3):
        # 実関数と同様: 価格あり・fee欠損時は reason=None
        _run_board_tp_finalize_with_fill(
            trader, fill_return=(10_016_000.0, None, None)
        )

    assert trader._board_tp_fill_fetch_fail_streak == 0
    assert alerts == []
