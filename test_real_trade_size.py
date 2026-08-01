"""
test_real_trade_size.py

real mode の発注サイズ計算が GMO 実口座残高を基準にすることの単体テスト。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as virtual_trader_module  # noqa: E402
from strategy_logic import OrderbookSnapshot, StrategyConfig  # noqa: E402
from virtual_trader import VirtualTrader  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_trade_csv_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)
    return log_dir


def _snap(*, bid: float = 10_000_000.0, ask: float = 10_000_100.0) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_bid_price=bid,
        best_bid_size=1.0,
        best_ask_price=ask,
        best_ask_size=1.0,
    )


def _expected_size(jpy_balance: float, price: float, trader: VirtualTrader) -> float:
    raw_size = (jpy_balance * trader.POSITION_RATIO) / price
    floored_size = math.floor(raw_size / trader.LOT_UNIT) * trader.LOT_UNIT
    computed_size = max(floored_size, trader.MIN_TRADE_SIZE)
    limits = [float(trader.config.max_order_size_btc)]
    if trader.config.daily_target_order_size_btc is not None:
        limits.append(float(trader.config.daily_target_order_size_btc))
    return min(computed_size, min(limits))


def test_real_calc_trade_size_uses_fetch_real_account_jpy_balance() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    trader.jpy_balance = 50_000.0  # 内部帳簿は小さく見せる
    trader.config = StrategyConfig(max_order_size_btc=10.0, daily_target_order_size_btc=None)
    price = 500_000.0
    real_jpy = 5_000_000.0  # 実口座の availableAmount。こちらが使われるべき
    equity_jpy = 4_000_000.0  # actualProfitLoss は別値でもサイズ計算には使わない

    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": real_jpy,
            "equity_jpy": equity_jpy,
            "position_size_btc": 0.0,
        },
    ) as fetch_mock:
        size = trader._calc_trade_size(price)

    assert fetch_mock.call_count == 1
    assert size == _expected_size(real_jpy, price, trader)
    assert size != _expected_size(trader.jpy_balance, price, trader)
    assert size != _expected_size(equity_jpy, price, trader)
    assert size == 2.0  # 5_000_000 * 0.2 / 500_000 = 2.0


def test_real_entry_skips_when_fetch_real_account_state_fails() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    before_balance = trader.jpy_balance

    with patch(
        "virtual_trader.fetch_real_account_state",
        side_effect=RuntimeError("api down"),
    ), patch("virtual_trader.gmo_order") as order_mock:
        trader._enter_long(_snap())  # must not raise

    assert order_mock.call_count == 0
    assert trader.position.side is None
    assert trader.jpy_balance == before_balance


def test_virtual_calc_trade_size_uses_internal_balance_without_fetch() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    trader.config = StrategyConfig(max_order_size_btc=1.0, daily_target_order_size_btc=None)
    price = 500_000.0

    with patch("virtual_trader.fetch_real_account_state") as fetch_mock:
        size = trader._calc_trade_size(price)

    assert fetch_mock.call_count == 0
    assert size == _expected_size(50_000.0, price, trader)
    assert size == 0.02


def test_real_calc_trade_size_floor_min_and_clamps() -> None:
    trader = VirtualTrader(initial_jpy=1.0, trading_mode="real")
    # 実口座が小さくても MIN_TRADE_SIZE まで上げる
    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value={"jpy_balance": 100.0, "position_size_btc": 0.0},
    ):
        size_min = trader._calc_trade_size(15_000_000.0)
    assert size_min == trader.MIN_TRADE_SIZE

    # LOT_UNIT 切り捨て: 500_000 * 0.2 / 500_000 = 0.2 → そのまま
    trader.config = StrategyConfig(max_order_size_btc=1.0, daily_target_order_size_btc=None)
    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value={"jpy_balance": 500_000.0, "position_size_btc": 0.0},
    ):
        size_floor = trader._calc_trade_size(500_000.0)
    assert size_floor == 0.2

    # max_order_size_btc クランプ
    trader.config = StrategyConfig(max_order_size_btc=0.05, daily_target_order_size_btc=None)
    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value={"jpy_balance": 5_000_000.0, "position_size_btc": 0.0},
    ):
        size_max = trader._calc_trade_size(500_000.0)
    assert size_max == 0.05

    # daily_target_order_size_btc クランプ
    trader.config = StrategyConfig(
        max_order_size_btc=1.0,
        daily_target_order_size_btc=0.001,
    )
    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value={"jpy_balance": 5_000_000.0, "position_size_btc": 0.0},
    ):
        size_daily = trader._calc_trade_size(500_000.0)
    assert size_daily == 0.001
