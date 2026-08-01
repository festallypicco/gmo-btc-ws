"""
test_gmo_leverage_symbol_and_margin.py

BTC_JPY 定数・margin 余力・ロット単位の単体テスト。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as vt  # noqa: E402
from virtual_trader import VirtualTrader  # noqa: E402


@pytest.fixture
def trade_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMO_API_KEY_TRADE", "trade-key")
    monkeypatch.setenv("GMO_API_SECRET_TRADE", "trade-secret")


def test_leverage_symbol_constant_is_btc_jpy() -> None:
    assert vt._GMO_LEVERAGE_SYMBOL == "BTC_JPY"


def test_min_trade_size_and_lot_unit_match_btc_jpy_rules() -> None:
    assert VirtualTrader.MIN_TRADE_SIZE == 0.001
    assert VirtualTrader.LOT_UNIT == 0.001


def test_gmo_order_default_symbol_is_btc_jpy(trade_env: None) -> None:
    captured: List[Dict[str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        body: Any = None,
        *,
        credential_scope: str = "trade",
    ) -> Any:
        captured.append({"method": method, "path": path, "body": body})
        return "12345"

    with patch("virtual_trader._gmo_private_request", side_effect=fake_request):
        vt.gmo_order(
            side="BUY",
            execution_type="LIMIT",
            price=10_000_000.0,
            size=0.001,
        )

    assert captured[0]["body"]["symbol"] == "BTC_JPY"


def test_gmo_close_order_default_symbol_is_btc_jpy(trade_env: None) -> None:
    captured: List[Dict[str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        body: Any = None,
        *,
        credential_scope: str = "trade",
    ) -> Any:
        captured.append({"method": method, "path": path, "body": body})
        return "12345"

    with patch("virtual_trader._gmo_private_request", side_effect=fake_request):
        vt.gmo_close_order(
            side="SELL",
            execution_type="MARKET",
            settle_position={"positionId": 1, "size": "0.001"},
        )

    assert captured[0]["body"]["symbol"] == "BTC_JPY"


def test_fetch_open_positions_and_active_orders_default_symbol(
    trade_env: None,
) -> None:
    paths: List[str] = []

    def fake_get(path: str, *, credential_scope: str = "trade") -> Dict[str, Any]:
        paths.append(path)
        return {"list": []}

    with patch("virtual_trader._gmo_private_get", side_effect=fake_get):
        vt.fetch_open_positions()
        vt.fetch_active_orders()

    assert paths[0].startswith("/v1/openPositions?symbol=BTC_JPY&")
    assert paths[1].startswith("/v1/activeOrders?symbol=BTC_JPY&")


def test_fetch_real_account_state_uses_margin_available_amount(
    trade_env: None,
) -> None:
    calls: List[str] = []

    def fake_get(path: str, *, credential_scope: str = "trade") -> Dict[str, Any]:
        calls.append(path)
        if path.startswith("/v1/account/margin"):
            return {
                "availableAmount": "123456.7",
                "actualProfitLoss": "130000.5",
            }
        if path.startswith("/v1/openPositions"):
            return {
                "list": [
                    {"side": "BUY", "size": "0.002"},
                    {"side": "SELL", "size": "0.001"},
                ]
            }
        raise AssertionError(f"unexpected path: {path}")

    with patch("virtual_trader._gmo_private_get", side_effect=fake_get):
        state = vt.fetch_real_account_state()

    assert any(p.startswith("/v1/account/margin") for p in calls)
    assert not any("account/assets" in p for p in calls)
    assert state["jpy_balance"] == pytest.approx(123456.7)
    assert state["equity_jpy"] == pytest.approx(130000.5)
    assert state["position_size_btc"] == pytest.approx(0.001)
