"""
test_real_entry.py

real mode エントリー発注と Private WS 約定反映の単体テスト。
GMO API 呼び出しはモック化する。
"""

from __future__ import annotations

import csv
import sys
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
from virtual_trader import GmoApiError, MAKER_FEE_RATE, VirtualTrader  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_trade_csv_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """取引 CSV を本番 log/ ではなく tmp へ書く。"""
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
        best_bid_size=1.0,
        best_ask_price=ask,
        best_ask_size=1.0,
    )


def _real_trader() -> VirtualTrader:
    return VirtualTrader(initial_jpy=50_000.0, trading_mode="real")


def test_real_entry_success_sets_pending_with_entry_order_id(
    isolated_trade_csv_log_dir: Path,
) -> None:
    trader = _real_trader()
    placed: List[int] = []
    trader._on_order_placed = lambda: placed.append(1)

    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value={"jpy_balance": 50_000.0, "position_size_btc": 0.0},
    ), patch("virtual_trader.gmo_order", return_value="987654321") as mock_order:
        trader._enter_long(_snap())

    assert mock_order.call_count == 1
    kwargs = mock_order.call_args.kwargs
    assert kwargs["side"] == "BUY"
    assert kwargs["execution_type"] == "LIMIT"
    assert kwargs["time_in_force"] == "SOK"
    assert trader.position.side == "LONG"
    assert trader.position.is_pending is True
    assert trader.position.entry_order_id == 987654321
    assert trader.position.tp_order_id is None
    assert trader.position.sl_order_id is None
    assert placed == [1]
    assert trader.trade_history[-1].reason == "ENTRY_PENDING"
    csv_files = list(isolated_trade_csv_log_dir.glob("realtime_trading_log_*.csv"))
    assert len(csv_files) == 1
    rows = list(csv.DictReader(csv_files[0].open(encoding="utf-8")))
    assert [r["reason"] for r in rows] == ["ENTRY_PENDING"]


def test_real_entry_fill_still_records_entry_after_pending(
    isolated_trade_csv_log_dir: Path,
) -> None:
    trader = _real_trader()
    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value={"jpy_balance": 50_000.0, "position_size_btc": 0.0},
    ), patch("virtual_trader.gmo_order", return_value="111"):
        trader._enter_long(_snap())
    assert trader.trade_history[-1].reason == "ENTRY_PENDING"

    with patch("virtual_trader.gmo_order"), patch(
        "virtual_trader.gmo_close_order", return_value="8002"
    ):
        trader.on_execution_event(
            {
                "channel": "executionEvents",
                "orderId": 111,
                "executionPrice": "10000001",
                "executionSize": "0.001",
                "orderExecutedSize": "0.001",
                "side": "BUY",
                "positionId": 289105203,
                "fee": "0",
            }
        )

    reasons = [r.reason for r in trader.trade_history]
    assert reasons.count("ENTRY_PENDING") == 1
    assert reasons.count("ENTRY") == 1
    csv_files = list(isolated_trade_csv_log_dir.glob("realtime_trading_log_*.csv"))
    rows = list(csv.DictReader(csv_files[0].open(encoding="utf-8")))
    assert [r["reason"] for r in rows].count("ENTRY_PENDING") == 1
    assert [r["reason"] for r in rows].count("ENTRY") == 1


def test_real_entry_api_failure_skips_without_raising() -> None:
    trader = _real_trader()
    before_balance = trader.jpy_balance

    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value={"jpy_balance": 50_000.0, "position_size_btc": 0.0},
    ), patch(
        "virtual_trader.gmo_order",
        side_effect=GmoApiError(status=1, messages=[{"message_code": "ERR-5003"}]),
    ):
        trader._enter_long(_snap())  # must not raise

    assert trader.position.side is None
    assert trader.position.is_pending is False
    assert trader.position.entry_order_id is None
    assert trader.jpy_balance == before_balance


def test_execution_matching_entry_order_id_marks_position_open() -> None:
    trader = _real_trader()
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_001.0,
        size=0.01,
        is_pending=True,
        entry_order_id=111,
    )

    with patch("virtual_trader.gmo_order") as mock_order, patch(
        "virtual_trader.gmo_close_order", return_value="8002"
    ) as mock_close:
        trader.on_execution_event(
            {
                "channel": "executionEvents",
                "orderId": 111,
                "executionPrice": "10000150",
                "executionSize": "0.01",
                "orderExecutedSize": "0.01",
                "side": "BUY",
                "positionId": 289105203,
            }
        )

    assert trader.position.is_pending is False
    assert trader.position.side == "LONG"
    assert trader.position.entry_price == 10_000_150.0
    assert trader.position.size == pytest.approx(0.01)
    assert trader.position.entry_order_id == 111
    assert trader.position.position_id == 289105203
    assert trader.position.tp_order_id is None
    assert trader.position.sl_order_id == 8002
    assert trader.position.exit_price_target > trader.position.entry_price
    assert trader._position_filled_at is not None
    assert mock_order.call_count == 0
    assert mock_close.call_count == 1
    assert mock_close.call_args.kwargs["execution_type"] == "STOP"


def test_execution_without_position_id_leaves_none() -> None:
    trader = _real_trader()
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_001.0,
        size=0.01,
        is_pending=True,
        entry_order_id=111,
    )

    with patch("virtual_trader.gmo_order") as mock_order, patch(
        "virtual_trader.gmo_close_order"
    ) as mock_close:
        trader.on_execution_event(
            {
                "channel": "executionEvents",
                "orderId": 111,
                "executionPrice": "10000150",
                "executionSize": "0.01",
                "orderExecutedSize": "0.01",
                "side": "BUY",
            }
        )

    assert trader.position.is_pending is False
    assert trader.position.position_id is None
    assert trader.position.tp_order_id is None
    assert trader.position.sl_order_id is None
    assert mock_order.call_count == 0
    assert mock_close.call_count == 0


def test_execution_with_other_order_id_is_ignored() -> None:
    trader = _real_trader()
    trader.position = PositionState(
        side="SHORT",
        entry_price=10_000_050.0,
        size=0.02,
        is_pending=True,
        entry_order_id=222,
    )

    trader.on_execution_event(
        {
            "channel": "executionEvents",
            "orderId": 999,
            "executionPrice": "9999999",
            "executionSize": "0.02",
        }
    )

    assert trader.position.is_pending is True
    assert trader.position.entry_price == 10_000_050.0
    assert trader.position.entry_order_id == 222
    assert trader._position_filled_at is None


def test_real_pending_board_touch_does_not_fill_without_execution() -> None:
    """real mode では板タッチだけでは保有中へ遷移しない。"""
    trader = _real_trader()
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=True,
        entry_order_id=333,
    )
    # bid が指値以上 = virtual なら旧ロジックで fill される条件
    trader._check_pending_fill(_snap(bid=10_000_100.0, ask=10_000_200.0))
    assert trader.position.is_pending is True
    assert trader.position.entry_order_id == 333


def test_virtual_entry_unchanged_no_gmo_order_call() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    with patch("virtual_trader.gmo_order") as mock_order:
        trader._enter_long(_snap())
    assert mock_order.call_count == 0
    assert trader.position.side == "LONG"
    assert trader.position.is_pending is True
    assert trader.position.entry_order_id is None
    assert trader.position.position_id is None


def test_real_long_entry_deducts_fee_only_not_notional() -> None:
    trader = _real_trader()
    before = trader.jpy_balance

    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 50_000.0,
            "equity_jpy": 50_000.0,
            "position_size_btc": 0.0,
        },
    ), patch("virtual_trader.gmo_order", return_value="111"):
        trader._enter_long(_snap())

    assert trader.position.side == "LONG"
    assert trader.position.is_pending is True
    price = trader.position.entry_price
    size = trader.position.size
    cost = price * size
    fee = int(cost * MAKER_FEE_RATE)
    assert cost > 0
    assert trader.jpy_balance == pytest.approx(before - fee)
    assert before - trader.jpy_balance != pytest.approx(cost + fee)


def test_real_short_entry_still_deducts_fee_only() -> None:
    trader = _real_trader()
    before = trader.jpy_balance

    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 50_000.0,
            "equity_jpy": 50_000.0,
            "position_size_btc": 0.0,
        },
    ), patch("virtual_trader.gmo_order", return_value="222"):
        trader._enter_short(_snap())

    assert trader.position.side == "SHORT"
    assert trader.position.is_pending is True
    price = trader.position.entry_price
    size = trader.position.size
    fee = int(price * size * MAKER_FEE_RATE)
    assert trader.jpy_balance == pytest.approx(before - fee)


def test_virtual_long_entry_still_locks_notional_plus_fee() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    before = trader.jpy_balance
    trader._enter_long(_snap())
    assert trader.position.side == "LONG"
    price = trader.position.entry_price
    size = trader.position.size
    cost = price * size
    fee = int(cost * MAKER_FEE_RATE)
    total = cost + fee
    assert trader.jpy_balance == pytest.approx(before - total)
    assert before - trader.jpy_balance == pytest.approx(total)
