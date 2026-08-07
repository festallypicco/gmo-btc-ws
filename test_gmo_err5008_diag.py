"""
test_gmo_err5008_diag.py

ERR-5008 診断ログ（署名〜送信遅延・RTT）の計測・出力を検証する。
エラーハンドリング自体は変更しない前提。
"""

from __future__ import annotations

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


def _urlopen_json_response(
    payload: dict[str, Any],
    *,
    date_header: str = "Fri, 07 Aug 2026 02:00:00 GMT",
):
    body = json.dumps(payload).encode("utf-8")

    def fake_urlopen(request: Any, timeout: float = 15) -> Any:
        resp = MagicMock()
        resp.headers = {"Date": date_header}
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    return fake_urlopen


def test_gmo_messages_contain_code() -> None:
    assert vt._gmo_messages_contain_code(
        [{"message_code": "ERR-5008", "message_string": "late"}],
        "ERR-5008",
    )
    assert not vt._gmo_messages_contain_code(
        [{"message_code": "ERR-5122"}],
        "ERR-5008",
    )
    assert not vt._gmo_messages_contain_code(None, "ERR-5008")
    assert not vt._gmo_messages_contain_code("bad", "ERR-5008")


def test_format_err5008_diag_line() -> None:
    line = vt._format_err5008_diag_line(
        caller="gmo_cancel_order",
        endpoint="POST /v1/cancelOrder",
        api_timestamp="1700000000123",
        sign_to_send_ms=12.34,
        rtt_ms=456.7,
        response_date="Fri, 07 Aug 2026 02:00:00 GMT",
    )
    assert line.startswith("[ERR5008-DIAG] ")
    assert "caller=gmo_cancel_order" in line
    assert "endpoint=POST /v1/cancelOrder" in line
    assert "api_timestamp=1700000000123" in line
    assert "sign_to_send_ms=12.3" in line
    assert "rtt_ms=456.7" in line
    assert "response_date=Fri, 07 Aug 2026 02:00:00 GMT" in line


def test_err5008_logs_diag_with_timings(
    trade_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # sign -> send = 50ms, send -> end = 200ms
    mono_values = iter([100.0, 100.05, 100.25])
    monkeypatch.setattr(vt.time, "monotonic", lambda: next(mono_values))
    monkeypatch.setattr(vt.time, "time", lambda: 1700000000.123)

    err_payload = {
        "status": 1,
        "messages": [
            {
                "message_code": "ERR-5008",
                "message_string": "Timestamp for this request is too late.",
            }
        ],
    }
    with patch(
        "virtual_trader.urllib.request.urlopen",
        side_effect=_urlopen_json_response(
            err_payload, date_header="Fri, 07 Aug 2026 02:00:01 GMT"
        ),
    ):
        with pytest.raises(vt.GmoApiError) as exc_info:
            vt._gmo_private_request("POST", "/v1/order", {"orderId": 1})

    assert "ERR-5008" in exc_info.value.message_codes
    out = capsys.readouterr().out
    assert "[ERR5008-DIAG]" in out
    assert "endpoint=POST /v1/order" in out
    assert "api_timestamp=1700000000123" in out
    assert "sign_to_send_ms=50.0" in out
    assert "rtt_ms=200.0" in out
    assert "response_date=Fri, 07 Aug 2026 02:00:01 GMT" in out
    assert "caller=" in out


def test_success_does_not_log_err5008_diag(
    trade_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(
        "virtual_trader.urllib.request.urlopen",
        side_effect=_urlopen_json_response({"status": 0, "data": {"ok": 1}}),
    ):
        data = vt._gmo_private_request("GET", "/v1/account/margin")
    assert data == {"ok": 1}
    assert "[ERR5008-DIAG]" not in capsys.readouterr().out


def test_other_api_error_does_not_log_err5008_diag(
    trade_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "status": 1,
        "messages": [{"message_code": "ERR-5122", "message_string": "already"}],
    }
    with patch(
        "virtual_trader.urllib.request.urlopen",
        side_effect=_urlopen_json_response(payload),
    ):
        with pytest.raises(vt.GmoApiError):
            vt._gmo_private_request("POST", "/v1/cancelOrder", {"orderId": 9})
    assert "[ERR5008-DIAG]" not in capsys.readouterr().out


def test_http_error_with_err5008_logs_diag(
    trade_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mono_values = iter([10.0, 10.01, 10.11])
    monkeypatch.setattr(vt.time, "monotonic", lambda: next(mono_values))
    monkeypatch.setattr(vt.time, "time", lambda: 1700000001.0)

    body = json.dumps(
        {
            "status": 1,
            "messages": [{"message_code": "ERR-5008", "message_string": "late"}],
        }
    ).encode("utf-8")

    def fake_urlopen(request: Any, timeout: float = 15) -> Any:
        headers = {"Date": "Fri, 07 Aug 2026 03:00:00 GMT"}
        raise vt.urllib.error.HTTPError(
            url=request.full_url,
            code=400,
            msg="Bad Request",
            hdrs=headers,
            fp=MagicMock(read=MagicMock(return_value=body)),
        )

    with patch("virtual_trader.urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(RuntimeError, match="GMO API HTTP 400"):
            vt._gmo_private_request("POST", "/v1/closeOrder", {"positionId": 1})

    out = capsys.readouterr().out
    assert "[ERR5008-DIAG]" in out
    assert "endpoint=POST /v1/closeOrder" in out
    assert "api_timestamp=1700000001000" in out
    assert "sign_to_send_ms=10.0" in out
    assert "rtt_ms=100.0" in out
    assert "response_date=Fri, 07 Aug 2026 03:00:00 GMT" in out
