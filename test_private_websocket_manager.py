"""
test_private_websocket_manager.py

PrivateWebSocketManager のトークン延長タイマーのライフサイクルを検証する。
"""

from __future__ import annotations

import sys
import threading
import time
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import websocket_manager as ws_mod  # noqa: E402
from websocket_manager import PrivateWebSocketManager  # noqa: E402


@pytest.fixture(autouse=True)
def _short_extend_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_mod, "_TOKEN_EXTEND_INTERVAL_SEC", 0.05)


def _make_manager() -> PrivateWebSocketManager:
    return PrivateWebSocketManager()


def test_stop_joins_token_renewer_thread() -> None:
    mgr = _make_manager()
    with mgr._token_lock:
        mgr._token = "token-stop"

    started = threading.Event()
    hold = threading.Event()

    def slow_extend(expected_token: str) -> None:
        started.set()
        hold.wait(timeout=2.0)
        assert expected_token == "token-stop"

    with patch.object(mgr, "_extend_token", side_effect=slow_extend):
        mgr._start_token_renewer()
        assert started.wait(timeout=2.0)
        renewer = mgr._renew_thread
        assert renewer is not None
        assert renewer.is_alive()

        hold.set()
        mgr.stop()

        assert renewer.is_alive() is False
        assert mgr._renew_thread is None


def test_reconnect_does_not_accumulate_renewer_threads() -> None:
    mgr = _make_manager()
    alive_after: List[threading.Thread] = []

    for i in range(5):
        with mgr._token_lock:
            mgr._token = f"token-{i}"
        # on_close / reconnect 相当: 旧タイマー停止 -> 新タイマー起動
        mgr._stop_token_renewer()
        mgr._start_token_renewer()
        current = mgr._renew_thread
        assert current is not None
        assert current.is_alive()
        alive_after.append(current)

    # 最後の1本以外は停止済みであること
    for old in alive_after[:-1]:
        assert old.is_alive() is False
    assert alive_after[-1].is_alive()

    mgr._stop_token_renewer()
    assert alive_after[-1].is_alive() is False
    assert mgr._renew_thread is None


def test_old_token_is_not_extended_after_token_replace() -> None:
    mgr = _make_manager()
    put_tokens: List[str] = []
    put_lock = threading.Lock()

    def fake_private_request(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if method == "PUT":
            token = str((body or {}).get("token", ""))
            with put_lock:
                put_tokens.append(token)
            return {"status": 0, "data": None}
        raise AssertionError(f"unexpected request: {method} {path}")

    with mgr._token_lock:
        mgr._token = "old-token"

    with patch.object(mgr, "_private_request", side_effect=fake_private_request):
        mgr._start_token_renewer()
        old_thread = mgr._renew_thread
        assert old_thread is not None

        # 失効再接続: トークン差し替え + 新タイマー
        time.sleep(0.02)
        with mgr._token_lock:
            mgr._token = "new-token"
        mgr._start_token_renewer()

        deadline = time.time() + 2.0
        while old_thread.is_alive() and time.time() < deadline:
            time.sleep(0.01)
        assert old_thread.is_alive() is False

        # 新しいタイマーが延長する時間を少し待つ
        time.sleep(0.12)

        with put_lock:
            observed = list(put_tokens)

    assert "old-token" not in observed
    assert "new-token" in observed

    mgr._stop_token_renewer()


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


def test_private_request_uses_trade_credentials(
    clear_gmo_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMO_API_KEY_TRADE", "trade-key")
    monkeypatch.setenv("GMO_API_SECRET_TRADE", "trade-secret")
    monkeypatch.setenv("GMO_API_KEY", "legacy-key")
    monkeypatch.setenv("GMO_API_SECRET", "legacy-secret")
    monkeypatch.setenv("GMO_API_KEY_READONLY", "ro-key")
    monkeypatch.setenv("GMO_API_SECRET_READONLY", "ro-secret")

    mgr = _make_manager()
    captured: Dict[str, Any] = {}

    class _FakeResponse:
        def read(self) -> bytes:
            return json.dumps({"status": 0, "data": {"token": "t"}}).encode("utf-8")

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def fake_urlopen(request: Any, timeout: float = 10) -> Any:
        captured["api_key"] = request.get_header("Api-key")
        return _FakeResponse()

    with patch("websocket_manager.urllib.request.urlopen", side_effect=fake_urlopen):
        doc = mgr._private_request("POST", "/v1/ws-auth", {"key": "value"})

    assert doc["status"] == 0
    assert captured["api_key"] == "trade-key"
    assert captured["api_key"] != "legacy-key"
    assert captured["api_key"] != "ro-key"


def test_private_request_missing_trade_credentials_propagates_error(
    clear_gmo_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMO_API_KEY", "legacy-key")
    monkeypatch.setenv("GMO_API_SECRET", "legacy-secret")
    monkeypatch.setenv("GMO_API_KEY_READONLY", "ro-key")
    monkeypatch.setenv("GMO_API_SECRET_READONLY", "ro-secret")

    mgr = _make_manager()
    with pytest.raises(RuntimeError, match="GMO_API_KEY_TRADE/GMO_API_SECRET_TRADE"):
        mgr._private_request("POST", "/v1/ws-auth", {})


def test_legacy_env_names_are_ignored_when_trade_present(
    clear_gmo_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧名だけが設定されていても trade 未設定ならエラーになる。"""
    monkeypatch.setenv("GMO_API_KEY", "legacy-key")
    monkeypatch.setenv("GMO_API_SECRET", "legacy-secret")

    mgr = _make_manager()
    with pytest.raises(RuntimeError, match="GMO_API_KEY_TRADE/GMO_API_SECRET_TRADE"):
        mgr._private_request("POST", "/v1/ws-auth", {})
