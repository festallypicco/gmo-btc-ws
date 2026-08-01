"""
test_gmo_credential_scope.py

GMO Private API の trade / readonly 認証情報分離の単体テスト。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as vt  # noqa: E402


@pytest.fixture
def clear_gmo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GMO_API_KEY",
        "GMO_API_SECRET",
        "GMO_API_KEY_TRADE",
        "GMO_API_SECRET_TRADE",
        "GMO_API_KEY_READONLY",
        "GMO_API_SECRET_READONLY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_credential_scope_uses_trade_env(
    clear_gmo_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMO_API_KEY_TRADE", "trade-key")
    monkeypatch.setenv("GMO_API_SECRET_TRADE", "trade-secret")
    monkeypatch.setenv("GMO_API_KEY_READONLY", "ro-key")
    monkeypatch.setenv("GMO_API_SECRET_READONLY", "ro-secret")

    key, secret = vt._resolve_gmo_api_credentials()
    assert key == "trade-key"
    assert secret == "trade-secret"

    captured: Dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float = 15) -> Any:
        captured["api_key"] = request.get_header("Api-key")
        body = json.dumps({"status": 0, "data": {"ok": True}}).encode("utf-8")
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    with patch("virtual_trader.urllib.request.urlopen", side_effect=fake_urlopen):
        vt._gmo_private_request("GET", "/v1/account/margin")

    assert captured["api_key"] == "trade-key"


def test_readonly_credential_scope_uses_readonly_env(
    clear_gmo_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMO_API_KEY_TRADE", "trade-key")
    monkeypatch.setenv("GMO_API_SECRET_TRADE", "trade-secret")
    monkeypatch.setenv("GMO_API_KEY_READONLY", "ro-key")
    monkeypatch.setenv("GMO_API_SECRET_READONLY", "ro-secret")

    key, secret = vt._resolve_gmo_api_credentials("readonly")
    assert key == "ro-key"
    assert secret == "ro-secret"

    with patch(
        "virtual_trader._gmo_private_get",
        return_value={"list": []},
    ) as mock_get:
        vt.fetch_active_orders(credential_scope="readonly")

    assert mock_get.call_args.kwargs["credential_scope"] == "readonly"


def test_missing_trade_credentials_error_mentions_trade_vars(
    clear_gmo_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMO_API_KEY_READONLY", "ro-key")
    monkeypatch.setenv("GMO_API_SECRET_READONLY", "ro-secret")
    with pytest.raises(RuntimeError, match="GMO_API_KEY_TRADE/GMO_API_SECRET_TRADE"):
        vt._resolve_gmo_api_credentials("trade")


def test_missing_readonly_credentials_error_mentions_readonly_vars(
    clear_gmo_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMO_API_KEY_TRADE", "trade-key")
    monkeypatch.setenv("GMO_API_SECRET_TRADE", "trade-secret")
    with pytest.raises(
        RuntimeError,
        match="GMO_API_KEY_READONLY/GMO_API_SECRET_READONLY",
    ):
        vt._resolve_gmo_api_credentials("readonly")


def test_order_cancel_close_and_fetch_default_to_trade(
    clear_gmo_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMO_API_KEY_TRADE", "trade-key")
    monkeypatch.setenv("GMO_API_SECRET_TRADE", "trade-secret")
    # READONLY が無くても trade 既定の呼び出しは成功経路に入れる
    scopes: List[str] = []

    def capture_request(
        method: str,
        path: str,
        body: Any = None,
        *,
        credential_scope: str = "trade",
    ) -> Any:
        scopes.append(credential_scope)
        if method.upper() == "GET":
            if "openPositions" in path or "activeOrders" in path:
                return {"list": []}
            if "margin" in path:
                return {"availableAmount": "1000"}
            return {}
        return "12345"

    with patch("virtual_trader._gmo_private_request", side_effect=capture_request):
        vt.gmo_order(
            side="BUY",
            execution_type="LIMIT",
            price=10_000_000.0,
            size=0.01,
        )
        vt.gmo_cancel_order(12345)
        vt.gmo_close_order(
            side="SELL",
            execution_type="MARKET",
            settle_position={"positionId": 1, "size": "0.01"},
        )
        vt.fetch_open_positions()
        vt.fetch_active_orders()
        vt.fetch_real_account_state()

    assert scopes
    assert all(scope == "trade" for scope in scopes)
