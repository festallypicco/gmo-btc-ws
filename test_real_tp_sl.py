"""
test_real_tp_sl.py

real mode: SL のみ closeOrder(STOP) 常設 + 板監視 TP の単体テスト。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as virtual_trader_module  # noqa: E402
from strategy_logic import OrderbookSnapshot, PositionState  # noqa: E402
from virtual_trader import GmoApiError, TAKER_FEE_RATE, VirtualTrader  # noqa: E402


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
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        trading_mode="real",
        on_critical_alert=lambda msg: alert_list.append(msg),
    )
    trader._alerts = alert_list  # type: ignore[attr-defined]
    return trader


def _pending_long(
    trader: VirtualTrader,
    order_id: int = 111,
    *,
    position_id: int | None = None,
) -> None:
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=True,
        entry_order_id=order_id,
        position_id=position_id,
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


def test_ws_fill_places_sl_close_order_only() -> None:
    trader = _real_trader()
    _pending_long(trader)
    close_calls: List[Dict[str, Any]] = []

    def fake_close(**kwargs: Any) -> str:
        close_calls.append(kwargs)
        return "8002"

    with patch("virtual_trader.gmo_order") as mock_order, patch(
        "virtual_trader.gmo_close_order", side_effect=fake_close
    ):
        trader.on_execution_event(
            {
                "orderId": 111,
                "executionPrice": "10000000",
                "executionSize": "0.01",
                "orderExecutedSize": "0.01",
                "positionId": 289105203,
            }
        )

    assert trader.position.is_pending is False
    assert trader.position.tp_order_id is None
    assert trader.position.sl_order_id == 8002
    assert trader.position.position_id == 289105203
    assert mock_order.call_count == 0
    assert len(close_calls) == 1
    assert close_calls[0]["execution_type"] == "STOP"
    assert close_calls[0]["side"] == "SELL"
    assert close_calls[0]["price"] == pytest.approx(10_000_000.0 * 0.9985)
    assert close_calls[0]["time_in_force"] is None
    assert close_calls[0]["settle_position"]["positionId"] == 289105203
    assert close_calls[0]["settle_position"]["size"] == "0.01"
    assert trader._alerts == []  # type: ignore[attr-defined]


def test_ws_fill_sl_fail_alerts_unprotected() -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _pending_long(trader)

    with patch("virtual_trader.gmo_order") as mock_order, patch(
        "virtual_trader.gmo_close_order",
        side_effect=RuntimeError("api down"),
    ):
        trader.on_execution_event(
            {
                "orderId": 111,
                "executionPrice": "10000000",
                "orderExecutedSize": "0.01",
                "positionId": 77,
            }
        )

    assert mock_order.call_count == 0
    assert trader.position.is_pending is False
    assert trader.position.tp_order_id is None
    assert trader.position.sl_order_id is None
    assert len(alerts) == 1
    assert "SL placement failed" in alerts[0]


def test_board_tp_cancels_sl_then_market_closes() -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _open_long_with_sl(trader)
    before = trader.jpy_balance
    cancel_ids: List[int] = []
    close_calls: List[Dict[str, Any]] = []
    open_calls = {"n": 0}
    entry = trader.position.entry_price
    size = trader.position.size
    actual_fill = 10_016_500.0  # slippage vs target TP
    actual_fee = 4

    def fake_fetch_open() -> List[Dict[str, Any]]:
        open_calls["n"] += 1
        # confirm loop: first still present? settle confirms gone on first empty
        if open_calls["n"] == 1:
            return []
        return []

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=lambda oid: cancel_ids.append(int(oid)),
    ), patch(
        "virtual_trader.gmo_close_order",
        side_effect=lambda **kwargs: close_calls.append(kwargs) or "90001",
    ), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_fetch_open
    ), patch(
        "virtual_trader.gmo_fetch_order_execution_fill",
        return_value=(actual_fill, actual_fee),
    ), patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 50_100.0,
            "equity_jpy": 50_150.0,
            "position_size_btc": 0.0,
        },
    ), patch("virtual_trader.time.sleep"):
        tp = trader.position.exit_price_target
        trader._check_active_position(_snap(bid=tp + 100.0, ask=tp + 200.0))

    assert cancel_ids == [2002]
    assert len(close_calls) == 1
    assert close_calls[0]["execution_type"] == "MARKET"
    assert close_calls[0]["settle_position"]["positionId"] == 289105203
    assert trader.position.side is None
    assert trader.jpy_balance == pytest.approx(50_150.0)
    assert trader.jpy_balance != pytest.approx(before)
    assert len(trader.trade_history) == 1
    rec = trader.trade_history[0]
    assert rec.reason == "TAKE_PROFIT"
    assert rec.price == pytest.approx(actual_fill)
    assert rec.price != pytest.approx(tp)
    expected_pnl = (actual_fill - entry) * size - actual_fee
    assert rec.pnl == pytest.approx(expected_pnl)
    assert rec.fee == actual_fee
    assert alerts == []


def test_board_tp_falls_back_to_target_price_when_fill_unavailable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _open_long_with_sl(trader)
    entry = trader.position.entry_price
    size = trader.position.size
    tp = float(trader.position.exit_price_target)

    with patch("virtual_trader.gmo_cancel_order", return_value=None), patch(
        "virtual_trader.gmo_close_order", return_value="90002"
    ), patch("virtual_trader.fetch_open_positions", return_value=[]), patch(
        "virtual_trader.gmo_fetch_order_execution_fill",
        return_value=(None, None),
    ), patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 50_100.0,
            "equity_jpy": 50_100.0,
            "position_size_btc": 0.0,
        },
    ), patch("virtual_trader.time.sleep"):
        trader._check_active_position(_snap(bid=tp + 100.0, ask=tp + 200.0))

    assert trader.position.side is None
    rec = trader.trade_history[0]
    assert rec.reason == "TAKE_PROFIT"
    assert rec.price == pytest.approx(tp)
    # theoretical maker fee on fallback (MAKER_FEE_RATE is typically 0)
    theoretical_fee = int(tp * size * virtual_trader_module.MAKER_FEE_RATE)
    assert rec.fee == theoretical_fee
    assert rec.pnl == pytest.approx((tp - entry) * size - theoretical_fee)
    out = capsys.readouterr().out
    assert "actual fill price unavailable" in out
    assert alerts == []


def test_gmo_fetch_order_execution_fill_vwap_and_fee_sum() -> None:
    with patch(
        "virtual_trader._gmo_private_get",
        return_value={
            "list": [
                {"price": "10000000", "size": "0.001", "fee": "2"},
                {"price": "10000200", "size": "0.003", "fee": "6"},
            ]
        },
    ):
        avg, fee = virtual_trader_module.gmo_fetch_order_execution_fill(123)
    # (10000000*0.001 + 10000200*0.003) / 0.004 = 10000150
    assert avg == pytest.approx(10_000_150.0)
    assert fee == 8


def test_gmo_fetch_order_execution_fill_empty_or_error_returns_none() -> None:
    with patch("virtual_trader._gmo_private_get", return_value={"list": []}):
        assert virtual_trader_module.gmo_fetch_order_execution_fill(1) == (None, None)
    with patch(
        "virtual_trader._gmo_private_get",
        side_effect=RuntimeError("network"),
    ):
        assert virtual_trader_module.gmo_fetch_order_execution_fill(1) == (None, None)
    with patch(
        "virtual_trader._gmo_private_get",
        return_value={"list": [{"fee": "3"}]},  # price/size missing
    ):
        avg, fee = virtual_trader_module.gmo_fetch_order_execution_fill(1)
    assert avg is None
    assert fee == 3


def test_board_tp_benign_sl_cancel_skips_market_close() -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _open_long_with_sl(trader)
    before_bal = trader.jpy_balance
    before_side = trader.position.side

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=GmoApiError(
            status=1,
            messages=[{"message_code": "ERR-5122", "message_string": "done"}],
        ),
    ), patch("virtual_trader.gmo_close_order") as mock_close:
        tp = trader.position.exit_price_target
        trader._check_active_position(_snap(bid=tp + 100.0, ask=tp + 200.0))

    assert mock_close.call_count == 0
    assert trader.position.side == before_side
    assert trader.jpy_balance == pytest.approx(before_bal)
    assert len(trader.trade_history) == 0
    assert alerts == []


def test_board_tp_sl_cancel_other_error_alerts() -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _open_long_with_sl(trader)

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=GmoApiError(
            status=1,
            messages=[{"message_code": "ERR-5003", "message_string": "busy"}],
        ),
    ), patch("virtual_trader.gmo_close_order") as mock_close:
        tp = trader.position.exit_price_target
        trader._check_active_position(_snap(bid=tp + 100.0, ask=tp + 200.0))

    assert mock_close.call_count == 0
    assert trader.position.side == "LONG"
    assert len(alerts) == 1
    assert "BOARD TP SL CANCEL FAILED" in alerts[0]


def test_board_tp_market_close_fail_after_sl_cancel_alerts() -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _open_long_with_sl(trader)

    with patch("virtual_trader.gmo_cancel_order", return_value=None), patch(
        "virtual_trader.gmo_close_order",
        side_effect=RuntimeError("close failed"),
    ):
        tp = trader.position.exit_price_target
        trader._check_active_position(_snap(bid=tp + 100.0, ask=tp + 200.0))

    assert trader.position.side == "LONG"
    assert len(alerts) == 1
    assert "BOARD TP MARKET CLOSE FAILED" in alerts[0]


def test_board_sl_level_does_not_trigger_board_exit() -> None:
    trader = _real_trader()
    _open_long_with_sl(trader)
    before = trader.position.side

    with patch("virtual_trader.gmo_cancel_order") as mock_cancel, patch(
        "virtual_trader.gmo_close_order"
    ) as mock_close:
        trader._check_active_position(_snap(bid=9_900_000.0, ask=9_900_100.0))

    assert mock_cancel.call_count == 0
    assert mock_close.call_count == 0
    assert trader.position.side == before
    assert len(trader.trade_history) == 0


def test_sl_execution_settles_without_canceling_missing_tp() -> None:
    trader = _real_trader()
    _open_long_with_sl(trader)
    before = trader.jpy_balance
    entry = trader.position.entry_price
    size = trader.position.size
    fill = 9_985_000.0

    with patch("virtual_trader.gmo_cancel_order") as mock_cancel, patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 49_900.0,
            "equity_jpy": 49_850.0,
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

    assert mock_cancel.call_count == 0
    assert trader.position.side is None
    assert trader.trade_history[0].reason == "STOP_LOSS"
    # equity 同期後の残高（推定 net 加算値ではなく equity_jpy）
    fee = int(fill * size * TAKER_FEE_RATE)
    estimated = before + (fill - entry) * size - fee
    assert trader.jpy_balance == pytest.approx(49_850.0)
    assert trader.jpy_balance != pytest.approx(estimated)


def test_sl_execution_syncs_jpy_balance_from_equity() -> None:
    """板 TP と同様、SL の WS 決済後に equity 同期する。"""
    trader = _real_trader()
    _open_long_with_sl(trader)
    sync_calls: List[str] = []

    def fake_sync(*, context: str) -> None:
        sync_calls.append(context)
        trader.jpy_balance = 49_870.0

    with patch.object(
        trader, "_sync_jpy_balance_from_equity_unlocked", side_effect=fake_sync
    ), patch("virtual_trader.gmo_cancel_order"):
        trader.on_execution_event(
            {
                "orderId": 2002,
                "executionPrice": "9985000",
                "orderExecutedSize": "0.01",
            }
        )

    assert sync_calls == ["REAL-SL"]
    assert trader.position.side is None
    assert trader.jpy_balance == pytest.approx(49_870.0)
    assert trader.trade_history[0].reason == "STOP_LOSS"


def test_short_sl_close_order_params() -> None:
    trader = _real_trader()
    trader.position = PositionState(
        side="SHORT",
        entry_price=10_000_000.0,
        size=0.02,
        is_pending=True,
        entry_order_id=222,
    )
    close_calls: List[Dict[str, Any]] = []

    with patch("virtual_trader.gmo_order") as mock_order, patch(
        "virtual_trader.gmo_close_order",
        side_effect=lambda **kwargs: close_calls.append(kwargs) or "9102",
    ):
        trader.on_execution_event(
            {
                "orderId": 222,
                "executionPrice": "10000000",
                "orderExecutedSize": "0.02",
                "positionId": 55,
            }
        )

    assert mock_order.call_count == 0
    assert len(close_calls) == 1
    assert close_calls[0]["side"] == "BUY"
    assert close_calls[0]["execution_type"] == "STOP"
    assert close_calls[0]["price"] == pytest.approx(10_000_000.0 * 1.0015)
    assert trader.position.tp_order_id is None
    assert trader.position.sl_order_id == 9102


def test_virtual_fill_does_not_call_gmo_close_for_tp_sl() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=True,
    )
    with patch("virtual_trader.gmo_order") as mock_order, patch(
        "virtual_trader.gmo_close_order"
    ) as mock_close:
        trader._check_pending_fill(_snap(bid=10_000_100.0, ask=10_000_200.0))
    assert mock_order.call_count == 0
    assert mock_close.call_count == 0
    assert trader.position.is_pending is False
    assert trader.position.tp_order_id is None
    assert trader.position.sl_order_id is None


def test_virtual_board_tp_and_sl_still_work() -> None:
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

    trader._check_active_position(_snap(bid=tp + 100.0, ask=tp + 200.0))
    assert trader.position.side is None
    assert trader.trade_history[0].reason == "TAKE_PROFIT"

    trader2 = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    trader2.jpy_balance -= entry * size
    trader2.position = PositionState(
        side="LONG",
        entry_price=entry,
        size=size,
        is_pending=False,
        exit_price_target=tp,
    )
    trader2._position_filled_at = datetime.now()
    trader2._check_active_position(_snap(bid=9_900_000.0, ask=9_900_100.0))
    assert trader2.position.side is None
    assert trader2.trade_history[0].reason == "STOP_LOSS"
