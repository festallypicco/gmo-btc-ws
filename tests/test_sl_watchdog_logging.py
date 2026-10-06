"""
test_sl_watchdog_logging.py

real mode SL発注成功ログ（fill_to_sl_ms / attempt / bid / ask）と
[SL-WATCHDOG] ログ（retry / force_close）の出力確認。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent.parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as virtual_trader_module  # noqa: E402
from strategy_logic import OrderbookSnapshot, PositionState  # noqa: E402
from virtual_trader import (  # noqa: E402
    GmoApiError,
    SL_MISSING_RETRY_INTERVAL_SEC,
    VirtualTrader,
)


@pytest.fixture(autouse=True)
def isolated_trade_csv_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)
    return log_dir


def _err(code: str, text: str = "Invalid request parameter.") -> GmoApiError:
    return GmoApiError(1, [{"message_code": code, "message_string": text}])


def _snap(*, bid: float = 10_000_000.0, ask: float = 10_000_100.0) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_bid_price=bid,
        best_bid_size=0.5,
        best_ask_price=ask,
        best_ask_size=0.5,
    )


def _real_trader(alerts: Optional[List[str]] = None) -> VirtualTrader:
    alert_list = alerts if alerts is not None else []
    return VirtualTrader(
        initial_jpy=50_000.0,
        trading_mode="real",
        on_critical_alert=lambda msg: alert_list.append(msg),
    )


def _held_long_missing_sl(trader: VirtualTrader, *, position_id: int = 55) -> None:
    entry = 10_000_000.0
    trader.position = PositionState(
        side="LONG",
        entry_price=entry,
        size=0.01,
        is_pending=False,
        exit_price_target=entry * (1 + trader.config.take_profit_pct),
        entry_order_id=111,
        tp_order_id=None,
        sl_order_id=None,
        position_id=position_id,
    )
    trader._position_filled_at = datetime.now()
    trader._latest_orderbook_snap = _snap()


def _lines(out: str, needle: str) -> List[str]:
    return [line for line in out.splitlines() if needle in line]


def _open_pos(position_id: int = 55) -> Dict[str, Any]:
    return {
        "positionId": position_id,
        "side": "BUY",
        "price": "10000000.0",
        "size": "0.01",
    }


def _account() -> Dict[str, float]:
    return {
        "jpy_balance": 49_900.0,
        "equity_jpy": 49_900.0,
        "position_size_btc": 0.0,
    }


def test_sl_success_log_first_attempt(capsys: pytest.CaptureFixture[str]) -> None:
    trader = _real_trader()
    _held_long_missing_sl(trader)
    trader._latest_orderbook_snap = _snap(bid=9_999_000.0, ask=10_001_000.0)

    with patch("virtual_trader.gmo_close_order", return_value="9001"):
        trader._place_real_tp_sl_orders()

    ok_lines = _lines(capsys.readouterr().out, "[OK] [REAL-SL]")
    assert len(ok_lines) == 1
    line = ok_lines[0]
    assert "orderId=9001" in line
    assert "positionId=55" in line
    assert "attempt=1" in line
    assert "bid=9999000.0 ask=10001000.0" in line
    fill_ms = line.split("fill_to_sl_ms=")[1].split()[0]
    assert float(fill_ms) >= 0.0
    assert line.isascii()
    assert trader._sl_log_attempt_count == 0


def test_watchdog_retry_log_and_success_attempt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    trader = _real_trader()
    _held_long_missing_sl(trader)
    clock = {"t": 1_000.0}
    calls: List[str] = []

    def fake_close(**kwargs: Any) -> str:
        calls.append(str(kwargs.get("execution_type")))
        if len(calls) == 1:
            raise _err("ERR-5106")
        return "9002"

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close), patch(
        "virtual_trader.time.time", side_effect=lambda: clock["t"]
    ):
        trader._place_real_tp_sl_orders()
        clock["t"] = 1_000.0 + SL_MISSING_RETRY_INTERVAL_SEC
        trader._maybe_protect_missing_sl_unlocked(now_ts=clock["t"])

    out = capsys.readouterr().out
    retry_lines = _lines(out, "[SL-WATCHDOG] retry")
    assert len(retry_lines) == 1
    assert "[SL-WATCHDOG] retry attempt=2 elapsed_sec=2.0 position_id=55" in retry_lines[0]
    assert retry_lines[0].isascii()
    ok_lines = _lines(out, "[OK] [REAL-SL]")
    assert len(ok_lines) == 1
    assert "attempt=2" in ok_lines[0]
    assert "fill_to_sl_ms=" in ok_lines[0]
    assert calls == ["STOP", "STOP"]
    assert trader.position.sl_order_id == 9002
    assert trader._sl_log_attempt_count == 0
    assert _lines(out, "[SL-WATCHDOG] force_close") == []


def test_attempt_restarts_for_new_position(capsys: pytest.CaptureFixture[str]) -> None:
    trader = _real_trader()
    _held_long_missing_sl(trader, position_id=55)

    with patch("virtual_trader.gmo_close_order", side_effect=_err("ERR-5106")):
        trader._place_real_tp_sl_orders()
    _held_long_missing_sl(trader, position_id=66)
    with patch("virtual_trader.gmo_close_order", return_value="9003"):
        trader._place_real_tp_sl_orders()

    ok_lines = _lines(capsys.readouterr().out, "[OK] [REAL-SL]")
    assert len(ok_lines) == 1
    assert "positionId=66" in ok_lines[0]
    assert "attempt=1" in ok_lines[0]


def test_watchdog_force_close_log_after_three_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _held_long_missing_sl(trader)
    clock = {"t": 1_000.0}
    calls: List[str] = []

    def fake_close(**kwargs: Any) -> str:
        calls.append(str(kwargs.get("execution_type")))
        if kwargs.get("execution_type") == "STOP":
            raise _err("ERR-5106")
        return "8001"

    def fake_open() -> List[Dict[str, Any]]:
        return [] if "MARKET" in calls else [_open_pos()]

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_open
    ), patch(
        "virtual_trader.fetch_real_account_state", return_value=_account()
    ), patch("virtual_trader.gmo_fetch_order_execution_fee", return_value=0), patch(
        "virtual_trader.time.sleep"
    ), patch(
        "virtual_trader.time.time", side_effect=lambda: clock["t"]
    ):
        trader._place_real_tp_sl_orders()
        for i in (1, 2):
            clock["t"] = 1_000.0 + SL_MISSING_RETRY_INTERVAL_SEC * i
            trader._maybe_protect_missing_sl_unlocked(now_ts=clock["t"])

    out = capsys.readouterr().out
    retry_lines = _lines(out, "[SL-WATCHDOG] retry")
    assert len(retry_lines) == 2
    assert "attempt=2 elapsed_sec=2.0 position_id=55" in retry_lines[0]
    assert "attempt=3 elapsed_sec=4.0 position_id=55" in retry_lines[1]
    fc_lines = _lines(out, "[SL-WATCHDOG] force_close")
    assert len(fc_lines) == 1
    assert (
        "[SL-WATCHDOG] force_close attempt=3 elapsed_sec=4.0"
        " position_id=55 last_error_code=ERR-5106"
    ) in fc_lines[0]
    assert fc_lines[0].isascii()
    assert calls.count("STOP") == 3
    assert calls.count("MARKET") == 1
    assert any("force closing position" in msg for msg in alerts)


def test_watchdog_force_close_log_on_mark_through_sl(
    capsys: pytest.CaptureFixture[str],
) -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _held_long_missing_sl(trader)
    sl_price = 10_000_000.0 * (1 - trader.config.stop_loss_pct)
    trader._latest_orderbook_snap = _snap(bid=sl_price - 1.0, ask=sl_price)
    calls: List[str] = []

    def fake_close(**kwargs: Any) -> str:
        calls.append(str(kwargs.get("execution_type")))
        return "8001"

    def fake_open() -> List[Dict[str, Any]]:
        return [] if calls else [_open_pos()]

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_open
    ), patch(
        "virtual_trader.fetch_real_account_state", return_value=_account()
    ), patch("virtual_trader.gmo_fetch_order_execution_fee", return_value=0), patch(
        "virtual_trader.time.sleep"
    ):
        trader._place_real_tp_sl_orders()

    out = capsys.readouterr().out
    fc_lines = _lines(out, "[SL-WATCHDOG] force_close")
    assert len(fc_lines) == 1
    assert (
        "[SL-WATCHDOG] force_close attempt=0 elapsed_sec=0.0"
        " position_id=55 last_error_code=none"
    ) in fc_lines[0]
    assert _lines(out, "[SL-WATCHDOG] retry") == []
    assert calls == ["MARKET"]
    assert any("force closing position" in msg for msg in alerts)
