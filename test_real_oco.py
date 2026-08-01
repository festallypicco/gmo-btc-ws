"""
test_real_oco.py

real mode 合成 OCO（TP/SL 片方約定でもう片方をキャンセル）の単体テスト。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as virtual_trader_module  # noqa: E402
from strategy_logic import OrderbookSnapshot, PositionState  # noqa: E402
from virtual_trader import (  # noqa: E402
    GmoApiError,
    MAKER_FEE_RATE,
    TAKER_FEE_RATE,
    VirtualTrader,
)


@pytest.fixture(autouse=True)
def isolated_trade_csv_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)
    return log_dir


def _snap(
    *,
    bid: float = 10_010_000.0,
    ask: float = 10_010_100.0,
) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_bid_price=bid,
        best_bid_size=0.4,
        best_ask_price=ask,
        best_ask_size=0.6,
    )


def _open_long_trader(alerts: List[str] | None = None) -> VirtualTrader:
    alert_list = alerts if alerts is not None else []
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        trading_mode="real",
        on_critical_alert=lambda msg: alert_list.append(msg),
    )
    trader._alerts = alert_list  # type: ignore[attr-defined]
    # real 保有中（TP/SL 発注済み）: エントリー時は fee のみ減算
    entry = 10_000_000.0
    size = 0.01
    entry_fee = int(entry * size * MAKER_FEE_RATE)
    trader.jpy_balance -= entry_fee
    trader.position = PositionState(
        side="LONG",
        entry_price=entry,
        size=size,
        is_pending=False,
        exit_price_target=entry * 1.0015,
        entry_order_id=100,
        tp_order_id=2001,
        sl_order_id=2002,
    )
    trader._position_filled_at = datetime.now()
    return trader


def _open_short_trader() -> VirtualTrader:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    entry = 10_000_000.0
    size = 0.01
    entry_fee = int(entry * size * MAKER_FEE_RATE)
    trader.jpy_balance -= entry_fee
    trader.position = PositionState(
        side="SHORT",
        entry_price=entry,
        size=size,
        is_pending=False,
        exit_price_target=entry * (1 - 0.0015),
        entry_order_id=100,
        tp_order_id=3001,
        sl_order_id=3002,
    )
    trader._position_filled_at = datetime.now()
    return trader


def test_tp_fill_settles_and_cancels_sl() -> None:
    trader = _open_long_trader()
    # 板監視のTP/SLを発火させず、ログ用スナップショットのみ保持する
    trader._latest_orderbook_snap = _snap()
    balance_before = trader.jpy_balance
    entry = trader.position.entry_price
    size = trader.position.size
    fill_price = 10_015_000.0
    fee = int(fill_price * size * MAKER_FEE_RATE)
    net_pnl = (fill_price - entry) * size - fee
    cancel_ids: List[int] = []

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=lambda oid: cancel_ids.append(int(oid)),
    ):
        trader.on_execution_event(
            {
                "orderId": 2001,
                "executionPrice": "10015000",
                "executionSize": "0.01",
                "orderExecutedSize": "0.01",
            }
        )

    assert cancel_ids == [2002]
    assert trader.position.side is None
    assert trader.jpy_balance == pytest.approx(balance_before + net_pnl)
    assert trader.jpy_balance != pytest.approx(balance_before + fill_price * size - fee)
    assert trader._cumulative_pnl > 0
    assert trader._win_count == 1
    assert len(trader.trade_history) == 1
    assert trader.trade_history[0].reason == "TAKE_PROFIT"
    assert trader.trade_history[0].price == pytest.approx(10_015_000.0)
    assert trader._alerts == []  # type: ignore[attr-defined]


def test_sl_fill_settles_and_skips_cancel_when_tp_none() -> None:
    trader = _open_long_trader()
    trader.position = PositionState(
        side=trader.position.side,
        entry_price=trader.position.entry_price,
        size=trader.position.size,
        is_pending=False,
        exit_price_target=trader.position.exit_price_target,
        entry_order_id=trader.position.entry_order_id,
        tp_order_id=None,
        sl_order_id=2002,
        position_id=trader.position.position_id,
    )
    trader._latest_orderbook_snap = _snap(bid=10_000_000.0, ask=10_000_100.0)
    balance_before = trader.jpy_balance
    entry = trader.position.entry_price
    size = trader.position.size
    fill_price = 9_985_000.0
    fee = int(fill_price * size * TAKER_FEE_RATE)
    net_pnl = (fill_price - entry) * size - fee
    cancel_ids: List[int] = []

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=lambda oid: cancel_ids.append(int(oid)),
    ), patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": balance_before + net_pnl,
            "equity_jpy": balance_before + net_pnl,
            "position_size_btc": 0.0,
        },
    ):
        trader.on_execution_event(
            {
                "orderId": 2002,
                "executionPrice": "9985000",
                "orderExecutedSize": "0.01",
            }
        )

    assert cancel_ids == []
    assert trader.position.side is None
    assert trader.jpy_balance == pytest.approx(balance_before + net_pnl)
    assert trader._cumulative_pnl < 0
    assert trader._loss_count == 1
    assert trader.trade_history[0].reason == "STOP_LOSS"
    assert trader._alerts == []  # type: ignore[attr-defined]


def test_short_tp_fill_still_adds_net_pnl_only() -> None:
    trader = _open_short_trader()
    trader._latest_orderbook_snap = _snap()
    balance_before = trader.jpy_balance
    entry = trader.position.entry_price
    size = trader.position.size
    fill_price = 9_985_000.0  # SHORT TP: 安く買い戻し
    fee = int(fill_price * size * MAKER_FEE_RATE)
    net_pnl = (entry - fill_price) * size - fee

    with patch("virtual_trader.gmo_cancel_order", return_value=None):
        trader.on_execution_event(
            {
                "orderId": 3001,
                "executionPrice": "9985000",
                "orderExecutedSize": "0.01",
            }
        )

    assert trader.position.side is None
    assert trader.jpy_balance == pytest.approx(balance_before + net_pnl)
    assert trader.trade_history[0].reason == "TAKE_PROFIT"


def test_real_long_entry_fill_then_board_tp_syncs_equity() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    before = trader.jpy_balance
    account = {
        "jpy_balance": 50_000.0,
        "equity_jpy": 50_000.0,
        "position_size_btc": 0.0,
    }

    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value=dict(account),
    ), patch(
        "virtual_trader.gmo_order",
        return_value="1001",
    ), patch(
        "virtual_trader.gmo_close_order",
        return_value="2002",
    ):
        trader._enter_long(_snap(bid=10_000_000.0, ask=10_000_100.0))
        assert trader.position.is_pending is True
        entry_price = trader.position.entry_price
        size = trader.position.size
        entry_fee = int(entry_price * size * MAKER_FEE_RATE)
        assert trader.jpy_balance == pytest.approx(before - entry_fee)

        trader.on_execution_event(
            {
                "orderId": 1001,
                "executionPrice": str(int(entry_price)),
                "executionSize": str(size),
                "orderExecutedSize": str(size),
                "positionId": 55,
            }
        )
        assert trader.position.is_pending is False
        assert trader.position.tp_order_id is None
        assert trader.position.sl_order_id == 2002

    with patch("virtual_trader.gmo_cancel_order", return_value=None), patch(
        "virtual_trader.gmo_close_order", return_value="90001"
    ), patch("virtual_trader.fetch_open_positions", return_value=[]), patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 50_100.0,
            "equity_jpy": 50_120.0,
            "position_size_btc": 0.0,
        },
    ), patch("virtual_trader.time.sleep"):
        tp = trader.position.exit_price_target
        trader._check_active_position(_snap(bid=tp + 100.0, ask=tp + 200.0))

    assert trader.position.side is None
    assert trader.jpy_balance == pytest.approx(50_120.0)
    assert trader.trade_history[-1].reason == "TAKE_PROFIT"


def test_oco_cancel_success_no_alert() -> None:
    trader = _open_long_trader()
    with patch("virtual_trader.gmo_cancel_order", return_value=None):
        trader.on_execution_event(
            {
                "orderId": 2001,
                "executionPrice": "10015000",
                "orderExecutedSize": "0.01",
            }
        )
    assert trader.position.side is None
    assert trader._alerts == []  # type: ignore[attr-defined]


def test_oco_cancel_benign_error_alerts_double_fill() -> None:
    alerts: List[str] = []
    trader = _open_long_trader(alerts)

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=GmoApiError(
            status=1,
            messages=[{"message_code": "ERR-5122", "message_string": "done"}],
        ),
    ):
        trader.on_execution_event(
            {
                "orderId": 2001,
                "executionPrice": "10015000",
                "orderExecutedSize": "0.01",
            }
        )

    assert trader.position.side is None  # 決済自体は完了
    assert len(alerts) == 1
    assert "double fill" in alerts[0]
    assert "2002" in alerts[0]


def test_oco_cancel_other_error_alerts_but_settlement_done() -> None:
    alerts: List[str] = []
    trader = _open_long_trader(alerts)
    balance_before = trader.jpy_balance

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=GmoApiError(
            status=1,
            messages=[{"message_code": "ERR-5003", "message_string": "busy"}],
        ),
    ):
        trader.on_execution_event(
            {
                "orderId": 2001,
                "executionPrice": "10015000",
                "orderExecutedSize": "0.01",
            }
        )

    assert trader.position.side is None
    assert trader.jpy_balance > balance_before
    assert len(trader.trade_history) == 1
    assert len(alerts) == 1
    assert "opposite cancel failed" in alerts[0]


def test_unrelated_execution_and_flat_position_are_ignored() -> None:
    trader = _open_long_trader()
    with patch("virtual_trader.gmo_cancel_order") as mock_cancel:
        trader.on_execution_event(
            {
                "orderId": 9999,
                "executionPrice": "10015000",
                "orderExecutedSize": "0.01",
            }
        )
    assert mock_cancel.call_count == 0
    assert trader.position.tp_order_id == 2001

    trader.position = PositionState()
    with patch("virtual_trader.gmo_cancel_order") as mock_cancel2:
        trader.on_execution_event(
            {
                "orderId": 2001,
                "executionPrice": "10015000",
                "orderExecutedSize": "0.01",
            }
        )
    assert mock_cancel2.call_count == 0


def test_tp_fill_without_orderbook_update_does_not_raise() -> None:
    trader = _open_long_trader()
    assert trader._latest_orderbook_snap is None

    with patch("virtual_trader.gmo_cancel_order", return_value=None):
        trader.on_execution_event(
            {
                "orderId": 2001,
                "executionPrice": "10015000",
                "orderExecutedSize": "0.01",
            }
        )

    assert trader.position.side is None
    assert len(trader.trade_history) == 1
    assert trader.trade_history[0].best_bid_size == 0.0


def test_virtual_board_tp_sl_unaffected() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    entry = 10_000_000.0
    size = 0.01
    trader.jpy_balance -= entry * size
    trader.position = PositionState(
        side="LONG",
        entry_price=entry,
        size=size,
        is_pending=False,
        exit_price_target=entry * (1 + trader.config.take_profit_pct),
    )
    trader._position_filled_at = datetime.now()

    with patch("virtual_trader.gmo_cancel_order") as mock_cancel:
        trader._exit_take_profit(
            _snap(bid=10_020_000.0, ask=10_020_100.0),
            fill_price=trader.position.exit_price_target,
        )

    assert mock_cancel.call_count == 0
    assert trader.position.side is None
    assert trader.trade_history[0].reason == "TAKE_PROFIT"


def test_real_active_position_skips_board_based_sl_only() -> None:
    """real mode では板の SL 水準では決済せず、TP 水準は板監視で決済する。"""
    trader = _open_long_trader()
    trader.position = PositionState(
        side=trader.position.side,
        entry_price=trader.position.entry_price,
        size=trader.position.size,
        is_pending=False,
        exit_price_target=trader.position.exit_price_target,
        entry_order_id=trader.position.entry_order_id,
        tp_order_id=None,
        sl_order_id=2002,
        position_id=289105203,
    )
    balance_before = trader.jpy_balance

    # SL 水準を明確に割る板では決済しない
    sl_touch = _snap(bid=9_900_000.0, ask=9_900_100.0)
    with patch("virtual_trader.gmo_cancel_order") as mock_cancel, patch(
        "virtual_trader.gmo_close_order"
    ) as mock_close:
        trader._check_active_position(sl_touch)
    assert mock_cancel.call_count == 0
    assert mock_close.call_count == 0
    assert trader.position.side == "LONG"
    assert trader.jpy_balance == pytest.approx(balance_before)
    assert len(trader.trade_history) == 0

    # TP 水準到達時は SL cancel -> MARKET
    with patch("virtual_trader.gmo_cancel_order", return_value=None) as mock_cancel, patch(
        "virtual_trader.gmo_close_order", return_value="90001"
    ) as mock_close, patch(
        "virtual_trader.fetch_open_positions", return_value=[]
    ), patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 50_100.0,
            "equity_jpy": 50_150.0,
            "position_size_btc": 0.0,
        },
    ), patch("virtual_trader.time.sleep"):
        tp_touch = _snap(
            bid=trader.position.exit_price_target + 1_000.0,
            ask=trader.position.exit_price_target + 1_100.0,
        )
        trader._check_active_position(tp_touch)

    assert mock_cancel.call_count == 1
    assert mock_close.call_count == 1
    assert trader.position.side is None
    assert trader.trade_history[0].reason == "TAKE_PROFIT"


def test_virtual_active_position_still_exits_on_board_tp() -> None:
    """virtual mode では従来通り板ベースで利確する（回帰）。"""
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    entry = 10_000_000.0
    size = 0.01
    trader.jpy_balance -= entry * size
    tp = entry * (1 + trader.config.take_profit_pct)
    trader.position = PositionState(
        side="LONG",
        entry_price=entry,
        size=size,
        is_pending=False,
        exit_price_target=tp,
    )
    trader._position_filled_at = datetime.now()
    balance_before = trader.jpy_balance

    trader._check_active_position(_snap(bid=tp + 100.0, ask=tp + 200.0))

    assert trader.position.side is None
    assert trader.jpy_balance > balance_before
    assert len(trader.trade_history) == 1
    assert trader.trade_history[0].reason == "TAKE_PROFIT"
