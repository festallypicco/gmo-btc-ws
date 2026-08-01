"""
test_gmo_private_request_signing.py

GMO Private API 署名文字列（特にクエリ付き GET）の単体テスト。
"""

from __future__ import annotations

import hmac
import json
import sys
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as vt  # noqa: E402


@pytest.fixture
def trade_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMO_API_KEY_TRADE", "trade-key")
    monkeypatch.setenv("GMO_API_SECRET_TRADE", "trade-secret")


def _fake_urlopen_factory(captured: dict[str, Any]):
    def fake_urlopen(request: Any, timeout: float = 15) -> Any:
        captured["full_url"] = request.full_url
        captured["method"] = request.get_method()
        body = json.dumps({"status": 0, "data": {"list": []}}).encode("utf-8")
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    return fake_urlopen


def test_get_with_query_excludes_query_from_signature_but_keeps_url_query(
    trade_env: None,
) -> None:
    captured: dict[str, Any] = {}
    signed_msgs: List[bytes] = []
    real_hmac_new = hmac.new

    def capture_hmac(key: bytes, msg: bytes, digestmod: Any = None):
        signed_msgs.append(msg)
        return real_hmac_new(key, msg, digestmod)

    path_with_query = "/v1/openPositions?symbol=BTC&page=1&count=100"
    with patch("virtual_trader.hmac.new", side_effect=capture_hmac), patch(
        "virtual_trader.urllib.request.urlopen",
        side_effect=_fake_urlopen_factory(captured),
    ):
        vt._gmo_private_get(path_with_query)

    assert len(signed_msgs) == 1
    signed_text = signed_msgs[0].decode("utf-8")
    assert "?" not in signed_text
    assert signed_text.endswith("GET/v1/openPositions")
    assert "symbol=BTC" not in signed_text

    assert "symbol=BTC" in captured["full_url"]
    assert "page=1" in captured["full_url"]
    assert "count=100" in captured["full_url"]
    assert captured["full_url"].endswith(path_with_query) or path_with_query in captured[
        "full_url"
    ]
    assert captured["method"] == "GET"


def test_post_signature_still_includes_path_and_body(trade_env: None) -> None:
    captured: dict[str, Any] = {}
    signed_msgs: List[bytes] = []
    real_hmac_new = hmac.new

    def capture_hmac(key: bytes, msg: bytes, digestmod: Any = None):
        signed_msgs.append(msg)
        return real_hmac_new(key, msg, digestmod)

    body = {"symbol": "BTC_JPY", "side": "BUY", "executionType": "LIMIT"}
    payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    with patch("virtual_trader.hmac.new", side_effect=capture_hmac), patch(
        "virtual_trader.urllib.request.urlopen",
        side_effect=_fake_urlopen_factory(captured),
    ):
        vt._gmo_private_request("POST", "/v1/order", body)

    assert len(signed_msgs) == 1
    signed_text = signed_msgs[0].decode("utf-8")
    assert "POST/v1/order" in signed_text
    assert signed_text.endswith(payload)
    assert "?" not in signed_text
    assert captured["full_url"].endswith("/v1/order")
    assert captured["method"] == "POST"
