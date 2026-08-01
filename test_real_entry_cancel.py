"""
test_real_entry_cancel.py

real mode のエントリー指値キャンセル（imbalance反転 / メンテ強制）の単体テスト。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as virtual_trader_module  # noqa: E402
from strategy_logic import OrderbookSnapshot, PositionState  # noqa: E402
from virtual_trader import (  # noqa: E402
    ENTRY_PENDING_DEVIATION_SL_RATIO,
    GmoApiError,
    MAKER_FEE_RATE,
    VirtualTrader,
)

_ENTRY_PRICE = 10_000_000.0
_ENTRY_SIZE = 0.01
_ORDER_ID = 555001
_REAL_ACCOUNT_STATE = {
    "jpy_balance": 50_000.0,
    "equity_jpy": 50_000.0,
    "position_size_btc": 0.0,
}


@pytest.fixture(autouse=True)
def isolated_trade_csv_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)
    return log_dir


def _snap() -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_bid_price=9_999_000.0,
        best_bid_size=0.2,
        best_ask_price=10_001_000.0,
        best_ask_size=0.8,
    )


def _benign_cancel_error() -> GmoApiError:
    return GmoApiError(
        status=1,
        messages=[{"message_code": "ERR-5122", "message_string": "already done"}],
    )


def _pending_real_trader(
    *,
    alerts: Optional[List[str]] = None,
) -> VirtualTrader:
    alert_list = alerts if alerts is not None else []
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        trading_mode="real",
        on_critical_alert=lambda msg: alert_list.append(msg),
    )
    trader._alerts = alert_list  # type: ignore[attr-defined]
    # real LONG エントリー後: fee のみ減算（想定元本は拘束しない）
    fee = int(_ENTRY_PRICE * _ENTRY_SIZE * MAKER_FEE_RATE)
    trader.jpy_balance -= fee
    trader.position = PositionState(
        side="LONG",
        entry_price=_ENTRY_PRICE,
        size=_ENTRY_SIZE,
        is_pending=True,
        entry_order_id=_ORDER_ID,
    )
    return trader


def _pending_virtual_trader() -> VirtualTrader:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    cost = _ENTRY_PRICE * _ENTRY_SIZE
    trader.jpy_balance -= cost
    trader.position = PositionState(
        side="LONG",
        entry_price=_ENTRY_PRICE,
        size=_ENTRY_SIZE,
        is_pending=True,
        entry_order_id=None,
    )
    return trader


@pytest.mark.parametrize(
    "cancel_method",
    ["_cancel_order", "_force_cancel_maintenance"],
)
def test_real_cancel_calls_gmo_cancel_api(cancel_method: str) -> None:
    trader = _pending_real_trader()
    cancel_ids: List[int] = []

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=lambda oid: cancel_ids.append(int(oid)),
    ):
        getattr(trader, cancel_method)(_snap())

    assert cancel_ids == [_ORDER_ID]


@pytest.mark.parametrize(
    "cancel_method",
    ["_cancel_order", "_force_cancel_maintenance"],
)
def test_real_cancel_success_restores_internal_state(cancel_method: str) -> None:
    trader = _pending_real_trader()
    balance_before_entry = 50_000.0

    with patch("virtual_trader.gmo_cancel_order", return_value=None):
        getattr(trader, cancel_method)(_snap())

    assert trader.position.side is None
    assert trader.position.is_pending is False
    assert trader.position.entry_order_id is None
    assert trader.jpy_balance == pytest.approx(balance_before_entry)


@pytest.mark.parametrize(
    "cancel_method",
    ["_cancel_order", "_force_cancel_maintenance"],
)
def test_real_cancel_benign_with_open_position_adopts_fill_and_alerts(
    cancel_method: str,
) -> None:
    alerts: List[str] = []
    trader = _pending_real_trader(alerts=alerts)
    balance_before = trader.jpy_balance

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=_benign_cancel_error(),
    ), patch(
        "virtual_trader.fetch_open_positions",
        return_value=[
            {
                "positionId": 9001,
                "side": "BUY",
                "size": "0.01",
                "price": "10000250",
            }
        ],
    ), patch("virtual_trader.gmo_order") as mock_order, patch(
        "virtual_trader.gmo_close_order",
        return_value="7002",
    ) as mock_close:
        getattr(trader, cancel_method)(_snap())

    assert trader.position.is_pending is False
    assert trader.position.side == "LONG"
    assert trader.position.entry_price == pytest.approx(10_000_250.0)
    assert trader.position.size == pytest.approx(0.01)
    assert trader.position.entry_order_id == _ORDER_ID
    assert trader.position.exit_price_target > trader.position.entry_price
    assert trader.position.tp_order_id is None
    assert trader.position.sl_order_id == 7002
    assert trader.position.position_id == 9001
    assert trader.jpy_balance == pytest.approx(balance_before)  # 返却しない
    assert mock_order.call_count == 0
    assert mock_close.call_count == 1
    assert mock_close.call_args.kwargs["execution_type"] == "STOP"
    assert len(alerts) == 1
    assert "adopted open position" in alerts[0]
    assert str(_ORDER_ID) in alerts[0]


@pytest.mark.parametrize(
    "cancel_method",
    ["_cancel_order", "_force_cancel_maintenance"],
)
def test_real_cancel_benign_without_open_position_clears_state(
    cancel_method: str,
) -> None:
    trader = _pending_real_trader()

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=_benign_cancel_error(),
    ), patch("virtual_trader.fetch_open_positions", return_value=[]):
        getattr(trader, cancel_method)(_snap())

    assert trader.position.side is None
    assert trader.position.is_pending is False
    assert trader.jpy_balance == pytest.approx(50_000.0)
    assert trader._alerts == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "cancel_method",
    ["_cancel_order", "_force_cancel_maintenance"],
)
def test_real_cancel_other_error_keeps_pending_unchanged(cancel_method: str) -> None:
    trader = _pending_real_trader()
    balance_before = trader.jpy_balance
    pos_before = trader.position

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=GmoApiError(
            status=1,
            messages=[{"message_code": "ERR-5003", "message_string": "busy"}],
        ),
    ):
        getattr(trader, cancel_method)(_snap())

    assert trader.position.side == pos_before.side
    assert trader.position.is_pending is True
    assert trader.position.entry_order_id == _ORDER_ID
    assert trader.position.entry_price == pos_before.entry_price
    assert trader.position.size == pos_before.size
    assert trader.jpy_balance == pytest.approx(balance_before)


@pytest.mark.parametrize(
    "cancel_method",
    ["_cancel_order", "_force_cancel_maintenance"],
)
def test_virtual_cancel_does_not_call_gmo_api(cancel_method: str) -> None:
    trader = _pending_virtual_trader()

    with patch("virtual_trader.gmo_cancel_order") as mock_cancel:
        getattr(trader, cancel_method)(_snap())

    assert mock_cancel.call_count == 0
    assert trader.position.side is None
    assert trader.jpy_balance == pytest.approx(50_000.0)


@pytest.mark.parametrize(
    "cancel_method",
    ["_cancel_order", "_force_cancel_maintenance"],
)
def test_real_long_entry_cancel_does_not_add_notional(cancel_method: str) -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    before = trader.jpy_balance

    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value=dict(_REAL_ACCOUNT_STATE),
    ), patch("virtual_trader.gmo_order", return_value=str(_ORDER_ID)), patch(
        "virtual_trader.gmo_cancel_order", return_value=None
    ):
        trader._enter_long(_snap())
        after_entry = trader.jpy_balance
        price = trader.position.entry_price
        size = trader.position.size
        cost = price * size
        fee = int(cost * MAKER_FEE_RATE)
        assert after_entry == pytest.approx(before - fee)

        getattr(trader, cancel_method)(_snap())

    assert trader.position.side is None
    assert trader.jpy_balance == pytest.approx(before)
    assert trader.jpy_balance != pytest.approx(before + cost)


@pytest.mark.parametrize(
    "cancel_method",
    ["_cancel_order", "_force_cancel_maintenance"],
)
def test_real_short_entry_cancel_fee_rollback_unchanged(cancel_method: str) -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    before = trader.jpy_balance

    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value=dict(_REAL_ACCOUNT_STATE),
    ), patch("virtual_trader.gmo_order", return_value=str(_ORDER_ID)), patch(
        "virtual_trader.gmo_cancel_order", return_value=None
    ):
        trader._enter_short(_snap())
        price = trader.position.entry_price
        size = trader.position.size
        fee = int(price * size * MAKER_FEE_RATE)
        assert trader.jpy_balance == pytest.approx(before - fee)

        getattr(trader, cancel_method)(_snap())

    assert trader.position.side is None
    assert trader.jpy_balance == pytest.approx(before)


@pytest.mark.parametrize(
    "cancel_method",
    ["_cancel_order", "_force_cancel_maintenance"],
)
def test_virtual_long_entry_cancel_restores_notional_plus_fee(
    cancel_method: str,
) -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    before = trader.jpy_balance
    trader._enter_long(_snap())
    price = trader.position.entry_price
    size = trader.position.size
    cost = price * size
    fee = int(cost * MAKER_FEE_RATE)
    assert trader.jpy_balance == pytest.approx(before - (cost + fee))

    getattr(trader, cancel_method)(_snap())

    assert trader.position.side is None
    assert trader.jpy_balance == pytest.approx(before)


def test_real_short_board_touch_still_cancels_on_deviation() -> None:
    """
    SHORT: ask が指値以下（filled=True）でも乖離条件でキャンセルされること。
    旧実装では real+filled で early return し、ここへ到達しなかった。
    """
    entry = 10_500_559.0
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    trader.config.stop_loss_pct = 0.0012
    trader.config.imbalance_cancel_threshold = 0.5
    trader.position = PositionState(
        side="SHORT",
        entry_price=entry,
        size=0.001,
        is_pending=True,
        entry_order_id=_ORDER_ID,
    )
    trader._pending_order_placed_at = datetime.now()

    threshold = trader.config.stop_loss_pct * ENTRY_PENDING_DEVIATION_SL_RATIO
    # ask << entry → filled=True。mid も乖離閾値超え。imbalance は低めに保つ。
    far_ask = entry * (1.0 - threshold) - 5_000.0
    far_bid = far_ask - 100.0
    far_snap = OrderbookSnapshot(
        best_bid_price=far_bid,
        best_bid_size=0.01,
        best_ask_price=far_ask,
        best_ask_size=1.0,
    )
    assert far_snap.best_ask_price <= entry
    assert abs(far_snap.mid_price - entry) / entry >= threshold
    assert far_snap.imbalance <= trader.config.imbalance_cancel_threshold

    with patch("virtual_trader.gmo_cancel_order", return_value=None) as mock_cancel:
        trader._check_pending_fill(far_snap)

    assert mock_cancel.call_count == 1
    assert mock_cancel.call_args.args[0] == _ORDER_ID
    assert trader.position.side is None
    assert trader.trade_history[-1].reason == "CANCEL_ORDER"
    cancel = trader._last_cancel_by_side["SELL"]
    assert cancel is not None
    assert cancel[2] == float(virtual_trader_module.ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC)


def test_real_long_board_touch_still_cancels_on_deviation() -> None:
    """
    LONG: bid が指値以上（filled=True）でも乖離条件でキャンセルされること。
    """
    entry = 10_500_559.0
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    trader.config.stop_loss_pct = 0.0012
    trader.config.imbalance_cancel_threshold = 0.5
    trader.position = PositionState(
        side="LONG",
        entry_price=entry,
        size=0.001,
        is_pending=True,
        entry_order_id=_ORDER_ID,
    )
    trader._pending_order_placed_at = datetime.now()

    threshold = trader.config.stop_loss_pct * ENTRY_PENDING_DEVIATION_SL_RATIO
    # bid >> entry → filled=True。mid も乖離閾値超え。imbalance は高め（LONG キャンセル閾値未満にしない）。
    far_bid = entry * (1.0 + threshold) + 5_000.0
    far_ask = far_bid + 100.0
    far_snap = OrderbookSnapshot(
        best_bid_price=far_bid,
        best_bid_size=1.0,
        best_ask_price=far_ask,
        best_ask_size=0.01,
    )
    assert far_snap.best_bid_price >= entry
    assert abs(far_snap.mid_price - entry) / entry >= threshold
    assert far_snap.imbalance >= trader.config.imbalance_cancel_threshold

    with patch("virtual_trader.gmo_cancel_order", return_value=None) as mock_cancel:
        trader._check_pending_fill(far_snap)

    assert mock_cancel.call_count == 1
    assert trader.position.side is None
    assert trader.trade_history[-1].reason == "CANCEL_ORDER"
    cancel = trader._last_cancel_by_side["BUY"]
    assert cancel is not None
    assert cancel[2] == float(virtual_trader_module.ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC)


def test_real_board_touch_cancel_race_adopts_fill_on_err5122() -> None:
    """
    filled=True でキャンセル試行とほぼ同時に GMO 側約定（ERR-5122）した場合、
    FLAT に落とさず adopt-fill で建玉取り込みすること。
    """
    alerts: List[str] = []
    entry = 10_500_559.0
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        trading_mode="real",
        on_critical_alert=lambda msg: alerts.append(msg),
    )
    trader.config.stop_loss_pct = 0.0012
    trader.config.imbalance_cancel_threshold = 0.5
    trader.position = PositionState(
        side="SHORT",
        entry_price=entry,
        size=0.001,
        is_pending=True,
        entry_order_id=_ORDER_ID,
    )
    trader._pending_order_placed_at = datetime.now()

    threshold = trader.config.stop_loss_pct * ENTRY_PENDING_DEVIATION_SL_RATIO
    far_ask = entry * (1.0 - threshold) - 5_000.0
    far_snap = OrderbookSnapshot(
        best_bid_price=far_ask - 100.0,
        best_bid_size=0.01,
        best_ask_price=far_ask,
        best_ask_size=1.0,
    )

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=_benign_cancel_error(),
    ), patch(
        "virtual_trader.fetch_open_positions",
        return_value=[
            {
                "positionId": 9001,
                "side": "SELL",
                "size": "0.001",
                "price": "10500500",
            }
        ],
    ), patch("virtual_trader.gmo_order") as mock_order, patch(
        "virtual_trader.gmo_close_order",
        return_value="7002",
    ):
        trader._check_pending_fill(far_snap)

    assert trader.position.side == "SHORT"
    assert trader.position.is_pending is False
    assert trader.position.position_id == 9001
    assert trader.position.entry_order_id == _ORDER_ID
    assert trader.position.exit_price_target > 0
    assert mock_order.call_count == 0
    assert any("adopted open position" in a for a in alerts)
    # CANCEL_ORDER 行は書かず、FLAT にもしない
    assert all(r.reason != "CANCEL_ORDER" for r in trader.trade_history)


def test_real_board_touch_without_cancel_keeps_pending_for_ws_fill() -> None:
    """
    板タッチだがキャンセル条件未達なら pending 維持し、WS 約定で建玉化できること。
    """
    entry = 10_000_000.0
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    trader.config.stop_loss_pct = 0.0012
    trader.config.imbalance_cancel_threshold = 0.5
    trader.position = PositionState(
        side="SHORT",
        entry_price=entry,
        size=0.001,
        is_pending=True,
        entry_order_id=_ORDER_ID,
    )
    trader._pending_order_placed_at = datetime.now()
    trader._locked_config = trader.config
    trader._locked_profile_name = "early_morning"

    # ask は entry 以下（filled）だが mid 乖離は閾値未満、imbalance も SHORT キャンセル未満
    touch_snap = OrderbookSnapshot(
        best_bid_price=entry - 50.0,
        best_bid_size=0.01,
        best_ask_price=entry - 10.0,
        best_ask_size=1.0,
    )
    threshold = trader.config.stop_loss_pct * ENTRY_PENDING_DEVIATION_SL_RATIO
    assert touch_snap.best_ask_price <= entry
    assert abs(touch_snap.mid_price - entry) / entry < threshold
    assert touch_snap.imbalance <= trader.config.imbalance_cancel_threshold

    with patch("virtual_trader.gmo_cancel_order") as mock_cancel, patch(
        "virtual_trader.gmo_close_order", return_value="8002"
    ), patch("virtual_trader.gmo_order", return_value="8001"):
        trader._check_pending_fill(touch_snap)
        assert mock_cancel.call_count == 0
        assert trader.position.is_pending is True

        trader.on_execution_event(
            {
                "orderId": _ORDER_ID,
                "executionPrice": str(entry),
                "executionSize": "0.001",
                "positionId": 9100,
                "fee": "0",
            }
        )

    assert trader.position.is_pending is False
    assert trader.position.side == "SHORT"
    assert trader.position.position_id == 9100
    assert trader.position.entry_price == pytest.approx(entry)