"""
test_sl_first_attempt_delay.py

real mode: WS 約定直後の初回 SL 発注の最小待ち（SL_FIRST_ATTEMPT_MIN_DELAY_SEC）と、
SL 発注ログの診断項目（send_delay_ms / rtt_ms / ws_latency_ms / attempt）の検証。
"""

from __future__ import annotations

import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent.parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as virtual_trader_module  # noqa: E402
from strategy_logic import OrderbookSnapshot, PositionState  # noqa: E402
from virtual_trader import (  # noqa: E402
    GmoApiError,
    SL_MISSING_RETRY_INTERVAL_SEC,
    VirtualTrader,
    _parse_gmo_timestamp,
)

MIN_DELAY_SEC = 0.15
ORDER_ID = 111
POSITION_ID = 289105203


@pytest.fixture(autouse=True)
def min_delay_enabled(no_sl_first_attempt_delay: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(virtual_trader_module, "SL_FIRST_ATTEMPT_MIN_DELAY_SEC", MIN_DELAY_SEC)


@pytest.fixture(autouse=True)
def isolated_trade_csv_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)
    return log_dir


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: List[Tuple[float, Callable[[], None]]] = []

    def __call__(self, delay_sec: float, fn: Callable[[], None]) -> None:
        self.calls.append((delay_sec, fn))

    def run_last(self) -> None:
        self.calls[-1][1]()


def _err(code: str, text: str = "Invalid request parameter.") -> GmoApiError:
    return GmoApiError(1, [{"message_code": code, "message_string": text}])


def _snap(*, bid: float = 10_000_000.0, ask: float = 10_000_100.0) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_bid_price=bid,
        best_bid_size=0.5,
        best_ask_price=ask,
        best_ask_size=0.5,
    )


def _trader(
    scheduler: Optional[FakeScheduler] = None,
    alerts: Optional[List[str]] = None,
) -> VirtualTrader:
    alert_list = alerts if alerts is not None else []
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        trading_mode="real",
        on_critical_alert=lambda msg: alert_list.append(msg),
    )
    if scheduler is not None:
        trader._sl_first_attempt_scheduler = scheduler
    trader._latest_orderbook_snap = _snap()
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=True,
        entry_order_id=ORDER_ID,
    )
    return trader


def _fill_event(*, execution_timestamp: Optional[str] = None) -> Dict[str, Any]:
    evt: Dict[str, Any] = {
        "channel": "executionEvents",
        "orderId": ORDER_ID,
        "executionPrice": "10000000",
        "executionSize": "0.01",
        "orderExecutedSize": "0.01",
        "positionId": POSITION_ID,
        "fee": "0",
    }
    if execution_timestamp is not None:
        evt["executionTimestamp"] = execution_timestamp
    return evt


def _age_fill(trader: VirtualTrader, seconds: float = 0.2) -> None:
    assert trader._position_filled_at is not None
    trader._position_filled_at -= timedelta(seconds=seconds)


def _field(line: str, name: str) -> str:
    return line.split(f"{name}=")[1].split()[0]


def _open_pos() -> Dict[str, Any]:
    return {"positionId": POSITION_ID, "side": "BUY", "price": "10000000", "size": "0.01"}


def _account() -> Dict[str, float]:
    return {"jpy_balance": 49_900.0, "equity_jpy": 49_900.0, "position_size_btc": 0.0}


def test_first_sl_not_sent_before_min_delay(capsys: pytest.CaptureFixture[str]) -> None:
    sched = FakeScheduler()
    trader = _trader(sched)
    calls: List[Dict[str, Any]] = []

    def fake_close(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "8002"

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close):
        trader.on_execution_event(_fill_event())
        assert calls == []
        assert trader.position.sl_order_id is None
        assert len(sched.calls) == 1
        assert 0.0 < sched.calls[0][0] <= MIN_DELAY_SEC

        sched.run_last()
        assert calls == []
        assert len(sched.calls) == 2

        _age_fill(trader)
        sched.run_last()

    assert len(calls) == 1
    assert calls[0]["execution_type"] == "STOP"
    assert trader.position.sl_order_id == 8002
    assert trader._sl_first_attempt_due_ts is None
    out = capsys.readouterr().out
    assert "[REAL-SL] first attempt deferred" in out
    ok_line = [ln for ln in out.splitlines() if "[OK] [REAL-SL] LONG" in ln][0]
    assert float(_field(ok_line, "send_delay_ms")) >= MIN_DELAY_SEC * 1000.0
    assert "attempt=1" in ok_line


def test_real_timer_waits_without_holding_lock() -> None:
    trader = _trader()
    sent_at: List[float] = []

    def fake_close(**kwargs: Any) -> str:
        sent_at.append(time.monotonic())
        return "8002"

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close):
        t0 = time.monotonic()
        trader.on_execution_event(_fill_event())
        returned_after = time.monotonic() - t0
        assert sent_at == []
        assert trader._lock.acquire(timeout=0.05)
        trader._lock.release()
        trader.on_orderbook_update(_snap())
        deadline = time.monotonic() + 2.0
        while not sent_at and time.monotonic() < deadline:
            time.sleep(0.01)

    assert returned_after < MIN_DELAY_SEC
    assert len(sent_at) == 1
    assert sent_at[0] - t0 >= MIN_DELAY_SEC
    assert trader.position.sl_order_id == 8002


def test_watchdog_does_not_send_during_deferral_then_no_double_send() -> None:
    sched = FakeScheduler()
    trader = _trader(sched)
    calls: List[str] = []

    def fake_close(**kwargs: Any) -> str:
        calls.append(str(kwargs.get("execution_type")))
        return "8002"

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close):
        trader.on_execution_event(_fill_event())
        trader._maybe_protect_missing_sl_unlocked(now_ts=time.time())
        assert calls == []

        _age_fill(trader)
        trader._maybe_protect_missing_sl_unlocked(now_ts=time.time())
        assert calls == ["STOP"]
        sched.run_last()

    assert calls == ["STOP"]
    assert trader.position.sl_order_id == 8002


def test_mark_through_sl_at_fill_force_closes_without_waiting() -> None:
    sched = FakeScheduler()
    alerts: List[str] = []
    trader = _trader(sched, alerts)
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
    ), patch("virtual_trader.fetch_real_account_state", return_value=_account()), patch(
        "virtual_trader.gmo_fetch_order_execution_fee", return_value=0
    ), patch("virtual_trader.time.sleep"):
        trader.on_execution_event(_fill_event())

    assert sched.calls == []
    assert calls == ["MARKET"]
    assert trader.position.side is None
    assert any("force closing position" in msg for msg in alerts)


def test_mark_through_during_deferral_watchdog_force_closes() -> None:
    sched = FakeScheduler()
    alerts: List[str] = []
    trader = _trader(sched, alerts)
    calls: List[str] = []

    def fake_close(**kwargs: Any) -> str:
        calls.append(str(kwargs.get("execution_type")))
        return "8001"

    def fake_open() -> List[Dict[str, Any]]:
        return [] if "MARKET" in calls else [_open_pos()]

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_open
    ), patch("virtual_trader.fetch_real_account_state", return_value=_account()), patch(
        "virtual_trader.gmo_fetch_order_execution_fee", return_value=0
    ), patch("virtual_trader.time.sleep"):
        trader.on_execution_event(_fill_event())
        sl_price = 10_000_000.0 * (1 - trader.config.stop_loss_pct)
        trader._latest_orderbook_snap = _snap(bid=sl_price - 1.0, ask=sl_price)
        trader._maybe_protect_missing_sl_unlocked(now_ts=time.time())
        sched.run_last()

    assert calls == ["MARKET"]
    assert trader.position.side is None
    assert any("force closing position" in msg for msg in alerts)


def test_deferred_first_fail_then_retry_and_force_close_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sched = FakeScheduler()
    alerts: List[str] = []
    trader = _trader(sched, alerts)
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
    ), patch("virtual_trader.fetch_real_account_state", return_value=_account()), patch(
        "virtual_trader.gmo_fetch_order_execution_fee", return_value=0
    ), patch("virtual_trader.time.sleep"), patch(
        "virtual_trader.time.time", side_effect=lambda: clock["t"]
    ):
        trader.on_execution_event(_fill_event())
        _age_fill(trader)
        sched.run_last()
        assert calls == ["STOP"]
        clock["t"] = 1_000.0 + 1.0
        trader._maybe_protect_missing_sl_unlocked(now_ts=clock["t"])
        assert calls == ["STOP"]
        for i in (1, 2):
            clock["t"] = 1_000.0 + SL_MISSING_RETRY_INTERVAL_SEC * i
            trader._maybe_protect_missing_sl_unlocked(now_ts=clock["t"])

    assert calls.count("STOP") == 3
    assert calls.count("MARKET") == 1
    assert trader.position.side is None
    assert any("force closing position" in msg for msg in alerts)
    out = capsys.readouterr().out
    fail_lines = [ln for ln in out.splitlines() if "[REAL-SL] closeOrder failed" in ln]
    assert [_field(ln, "attempt") for ln in fail_lines] == ["1", "2", "3"]


def test_board_tp_during_deferral_skips_first_sl() -> None:
    sched = FakeScheduler()
    trader = _trader(sched)
    with patch("virtual_trader.gmo_close_order") as mock_close:
        trader.on_execution_event(_fill_event())
        trader.position = PositionState()
        trader._position_filled_at = None
        sched.run_last()

    assert mock_close.call_count == 0
    assert trader._sl_first_attempt_due_ts is None


def test_post_cancel_grace_fill_is_deferred() -> None:
    sched = FakeScheduler()
    trader = _trader(sched)
    with patch("virtual_trader.gmo_cancel_order", return_value=None), patch(
        "virtual_trader.gmo_close_order", return_value="7003"
    ) as mock_close:
        trader._cancel_order(_snap())
        assert trader.position.side is None
        trader.on_execution_event(_fill_event())
        assert mock_close.call_count == 0
        assert len(sched.calls) == 1
        _age_fill(trader)
        sched.run_last()

    assert mock_close.call_count == 1
    assert trader.position.sl_order_id == 7003


def test_err5122_adopt_path_is_not_deferred() -> None:
    sched = FakeScheduler()
    trader = _trader(sched)
    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=_err("ERR-5122", "already executed"),
    ), patch("virtual_trader.fetch_open_positions", return_value=[_open_pos()]), patch(
        "virtual_trader.gmo_close_order", return_value="7004"
    ) as mock_close:
        result = trader._cancel_real_entry_order_or_adopt_fill(context="TEST")

    assert result == "adopted_fill"
    assert sched.calls == []
    assert mock_close.call_count == 1
    assert trader.position.sl_order_id == 7004


def test_startup_reconcile_reorder_is_not_deferred(tmp_path: Path) -> None:
    sched = FakeScheduler()
    trader = _trader(sched)
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=False,
        exit_price_target=10_015_000.0,
        entry_order_id=ORDER_ID,
        tp_order_id=None,
        sl_order_id=2002,
        position_id=POSITION_ID,
    )
    with patch("virtual_trader.fetch_open_positions", return_value=[_open_pos()]), patch(
        "virtual_trader.fetch_active_orders", return_value=[]
    ), patch("virtual_trader.gmo_close_order", return_value="7005") as mock_close:
        result = trader.reconcile_real_state_on_startup(state_path=tmp_path / "r.json")

    assert result["status"] == "reordered"
    assert sched.calls == []
    assert mock_close.call_count == 1
    assert trader.position.sl_order_id == 7005


def test_success_log_fields_with_ws_latency(capsys: pytest.CaptureFixture[str]) -> None:
    sched = FakeScheduler()
    trader = _trader(sched)
    exec_dt = datetime.now(timezone.utc) - timedelta(milliseconds=50)
    exec_ts = exec_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{exec_dt.microsecond // 1000:03d}Z"

    with patch("virtual_trader.gmo_close_order", return_value="8002"):
        trader.on_execution_event(_fill_event(execution_timestamp=exec_ts))
        _age_fill(trader)
        sched.run_last()

    line = [ln for ln in capsys.readouterr().out.splitlines() if "[OK] [REAL-SL] LONG" in ln][0]
    assert re.search(r" fill_to_sl_ms=\d+\.\d ", line)
    assert re.search(r" send_delay_ms=\d+\.\d ", line)
    assert re.search(r" rtt_ms=\d+\.\d ", line)
    assert float(_field(line, "ws_latency_ms")) >= 40.0
    assert "attempt=1" in line
    assert "bid=10000000.0 ask=10000100.0" in line
    assert line.isascii()


def test_failure_log_fields_without_execution_timestamp(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sched = FakeScheduler()
    trader = _trader(sched)
    with patch("virtual_trader.gmo_close_order", side_effect=_err("ERR-5106")):
        trader.on_execution_event(_fill_event())
        _age_fill(trader)
        sched.run_last()

    line = [
        ln for ln in capsys.readouterr().out.splitlines()
        if "[REAL-SL] closeOrder failed" in ln
    ][0]
    assert re.search(r" fill_to_sl_ms=\d+\.\d ", line)
    assert float(_field(line, "send_delay_ms")) >= MIN_DELAY_SEC * 1000.0
    assert re.search(r" rtt_ms=\d+\.\d ", line)
    assert " ws_latency_ms=None " in line
    assert " attempt=1 " in line
    assert "ERR-5106" in line


def test_parse_gmo_timestamp() -> None:
    assert _parse_gmo_timestamp("2019-03-19T02:15:06.081Z") == pytest.approx(
        datetime(2019, 3, 19, 2, 15, 6, 81000, tzinfo=timezone.utc).timestamp()
    )
    assert _parse_gmo_timestamp(None) is None
    assert _parse_gmo_timestamp("bad") is None
    assert _parse_gmo_timestamp("2019-03-19T02:15:06") is None
