"""
test_sl_missing_watchdog.py

real mode SL欠落ウォッチドッグの単体テスト。
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

import trading_engine  # noqa: E402
import virtual_trader as virtual_trader_module  # noqa: E402
from strategy_logic import OrderbookSnapshot, PositionState  # noqa: E402
from virtual_trader import (  # noqa: E402
    GmoApiError,
    SL_MISSING_CLOSE_MAX_ATTEMPTS,
    SL_MISSING_CLOSE_RETRY_INTERVAL_SEC,
    SL_MISSING_FORCE_CLOSE_AFTER_SEC,
    SL_MISSING_MAINTENANCE_RETRY_INTERVAL_SEC,
    SL_MISSING_MAX_ATTEMPTS,
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
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        trading_mode="real",
        on_critical_alert=lambda msg: alert_list.append(msg),
    )
    trader._alerts = alert_list  # type: ignore[attr-defined]
    return trader


def _held_long_missing_sl(
    trader: VirtualTrader,
    *,
    entry: float = 10_000_000.0,
    size: float = 0.01,
    position_id: int = 55,
) -> None:
    trader.position = PositionState(
        side="LONG",
        entry_price=entry,
        size=size,
        is_pending=False,
        exit_price_target=entry * (1 + trader.config.take_profit_pct),
        entry_order_id=111,
        tp_order_id=None,
        sl_order_id=None,
        position_id=position_id,
    )
    trader._position_filled_at = datetime.now()
    trader._latest_orderbook_snap = _snap()


def _open_pos(
    *,
    position_id: int = 55,
    side: str = "BUY",
    price: float = 10_000_000.0,
    size: float = 0.01,
) -> Dict[str, Any]:
    return {
        "positionId": position_id,
        "side": side,
        "price": str(price),
        "size": str(size),
    }


def _account() -> Dict[str, float]:
    return {
        "jpy_balance": 49_900.0,
        "equity_jpy": 49_900.0,
        "position_size_btc": 0.0,
    }


def test_retry_succeeds_after_two_seconds() -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _held_long_missing_sl(trader)
    close_calls: List[Dict[str, Any]] = []

    def fake_close(**kwargs: Any) -> str:
        close_calls.append(kwargs)
        if len(close_calls) == 1:
            raise _err("ERR-5106")
        return "9002"

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close), patch(
        "virtual_trader.time.time", return_value=1_000.0
    ):
        trader._place_real_tp_sl_orders()
    with patch("virtual_trader.gmo_close_order", side_effect=fake_close):
        trader._maybe_protect_missing_sl_unlocked(now_ts=1_000.0 + 1.0)
        assert trader.position.sl_order_id is None
        trader._maybe_protect_missing_sl_unlocked(
            now_ts=1_000.0 + SL_MISSING_RETRY_INTERVAL_SEC
        )

    assert [c["execution_type"] for c in close_calls] == ["STOP", "STOP"]
    assert trader.position.sl_order_id == 9002
    assert trader._sl_missing_attempts == 0
    assert any("unprotected position" in msg for msg in alerts)
    assert not any("force closing position" in msg for msg in alerts)


def test_three_failures_force_close() -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _held_long_missing_sl(trader)
    close_calls: List[str] = []
    open_n = {"n": 0}

    def fake_close(**kwargs: Any) -> str:
        close_calls.append(str(kwargs.get("execution_type")))
        if kwargs.get("execution_type") == "STOP":
            raise _err("ERR-5106")
        return "8001"

    def fake_open() -> List[Dict[str, Any]]:
        open_n["n"] += 1
        if close_calls.count("MARKET") == 0:
            return [_open_pos()]
        return []

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_open
    ), patch(
        "virtual_trader.fetch_real_account_state", return_value=_account()
    ), patch("virtual_trader.gmo_fetch_order_execution_fee", return_value=0), patch(
        "virtual_trader.time.sleep"
    ):
        trader._place_real_tp_sl_orders()
        t0 = trader._sl_missing_last_attempt_ts or 0.0
        trader._maybe_protect_missing_sl_unlocked(now_ts=t0 + SL_MISSING_RETRY_INTERVAL_SEC)
        trader._maybe_protect_missing_sl_unlocked(
            now_ts=t0 + SL_MISSING_RETRY_INTERVAL_SEC * 2
        )

    assert close_calls.count("STOP") == SL_MISSING_MAX_ATTEMPTS
    assert close_calls.count("MARKET") == 1
    assert trader.position.side is None
    assert any("force closing position" in msg for msg in alerts)


def test_ten_seconds_force_close_without_third_sl() -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _held_long_missing_sl(trader)
    close_calls: List[str] = []
    market_done = {"n": 0}

    def fake_close(**kwargs: Any) -> str:
        close_calls.append(str(kwargs.get("execution_type")))
        if kwargs.get("execution_type") == "STOP":
            raise _err("ERR-5106")
        market_done["n"] += 1
        return "8001"

    def fake_open() -> List[Dict[str, Any]]:
        if market_done["n"] > 0:
            return []
        return [_open_pos()]

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_open
    ), patch(
        "virtual_trader.fetch_real_account_state", return_value=_account()
    ), patch("virtual_trader.gmo_fetch_order_execution_fee", return_value=0), patch(
        "virtual_trader.time.sleep"
    ):
        trader._place_real_tp_sl_orders()
        t0 = trader._sl_missing_first_fail_ts or 0.0
        trader._maybe_protect_missing_sl_unlocked(now_ts=t0 + SL_MISSING_RETRY_INTERVAL_SEC)
        assert close_calls.count("STOP") == 2
        trader._maybe_protect_missing_sl_unlocked(
            now_ts=t0 + SL_MISSING_FORCE_CLOSE_AFTER_SEC
        )

    assert close_calls.count("STOP") == 2
    assert close_calls.count("MARKET") == 1
    assert trader.position.side is None
    assert any("force closing position" in msg for msg in alerts)


def test_mark_through_sl_force_closes_immediately() -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _held_long_missing_sl(trader, entry=10_000_000.0)
    sl_price = 10_000_000.0 * (1 - trader.config.stop_loss_pct)
    trader._latest_orderbook_snap = _snap(bid=sl_price - 1.0, ask=sl_price)
    close_calls: List[str] = []
    market_done = {"n": 0}

    def fake_close(**kwargs: Any) -> str:
        close_calls.append(str(kwargs.get("execution_type")))
        market_done["n"] += 1
        return "8001"

    def fake_open() -> List[Dict[str, Any]]:
        if market_done["n"] > 0:
            return []
        return [_open_pos()]

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_open
    ), patch(
        "virtual_trader.fetch_real_account_state", return_value=_account()
    ), patch("virtual_trader.gmo_fetch_order_execution_fee", return_value=0), patch(
        "virtual_trader.time.sleep"
    ):
        trader._place_real_tp_sl_orders()

    assert close_calls == ["MARKET"]
    assert trader.position.side is None
    assert any("force closing position" in msg for msg in alerts)
    assert not any("unprotected position" in msg for msg in alerts)


def test_err5201_does_not_count_toward_limits() -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _held_long_missing_sl(trader)
    close_calls: List[str] = []

    def fake_close(**kwargs: Any) -> str:
        close_calls.append(str(kwargs.get("execution_type")))
        raise _err("ERR-5201", "Under maintenance")

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close):
        trader._place_real_tp_sl_orders()
        t0 = trader._sl_missing_last_attempt_ts or 0.0
        trader._maybe_protect_missing_sl_unlocked(
            now_ts=t0 + SL_MISSING_RETRY_INTERVAL_SEC
        )
        assert close_calls == ["STOP"]
        trader._maybe_protect_missing_sl_unlocked(
            now_ts=t0 + SL_MISSING_MAINTENANCE_RETRY_INTERVAL_SEC
        )
        trader._maybe_protect_missing_sl_unlocked(
            now_ts=t0 + SL_MISSING_FORCE_CLOSE_AFTER_SEC + 30.0
        )

    assert close_calls == ["STOP", "STOP", "STOP"]
    assert trader._sl_missing_attempts == 0
    assert trader._sl_missing_first_fail_ts is None
    assert trader.position.side == "LONG"
    assert trader.position.sl_order_id is None
    assert sum(1 for msg in alerts if "unprotected position" in msg) == 1
    assert not any("force closing position" in msg for msg in alerts)


def test_force_close_retries_then_critical() -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _held_long_missing_sl(trader)
    close_n = {"n": 0}

    def fake_close(**kwargs: Any) -> str:
        close_n["n"] += 1
        if kwargs.get("execution_type") == "STOP":
            raise _err("ERR-5106")
        raise RuntimeError("market close failed")

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close), patch(
        "virtual_trader.fetch_open_positions", return_value=[_open_pos()]
    ), patch("virtual_trader.time.sleep"):
        trader._place_real_tp_sl_orders()
        t0 = trader._sl_missing_first_fail_ts or 0.0
        trader._maybe_protect_missing_sl_unlocked(
            now_ts=t0 + SL_MISSING_FORCE_CLOSE_AFTER_SEC
        )
        first_close_ts = trader._sl_missing_last_close_ts
        assert first_close_ts is not None
        assert trader._sl_missing_in_force_close is True
        for i in range(1, SL_MISSING_CLOSE_MAX_ATTEMPTS):
            trader._maybe_protect_missing_sl_unlocked(
                now_ts=first_close_ts + SL_MISSING_CLOSE_RETRY_INTERVAL_SEC * i
            )

    assert trader.position.side == "LONG"
    assert trader._sl_missing_close_attempts == SL_MISSING_CLOSE_MAX_ATTEMPTS
    assert any("force closing position" in msg for msg in alerts)
    criticals = [msg for msg in alerts if msg.startswith("[CRITICAL] SL missing watchdog")]
    assert len(criticals) == 1


def test_watchdog_runs_while_paused() -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _held_long_missing_sl(trader)
    trader.engine_status = "PAUSED"
    close_calls: List[str] = []

    def fake_close(**kwargs: Any) -> str:
        close_calls.append(str(kwargs.get("execution_type")))
        if len(close_calls) == 1:
            raise _err("ERR-5106")
        return "9002"

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close):
        trader._place_real_tp_sl_orders()
        t0 = trader._sl_missing_last_attempt_ts or 0.0
        trading_engine._maybe_run_sl_missing_watchdog(trader)
        assert trader.position.sl_order_id is None
        with patch(
            "virtual_trader.time.time",
            return_value=t0 + SL_MISSING_RETRY_INTERVAL_SEC,
        ):
            trading_engine._maybe_run_sl_missing_watchdog(trader)

    assert trader.engine_status == "PAUSED"
    assert close_calls == ["STOP", "STOP"]
    assert trader.position.sl_order_id == 9002


def test_watchdog_after_startup_reconcile_sl_fail(tmp_path: Path) -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=False,
        exit_price_target=10_015_000.0,
        entry_order_id=111,
        tp_order_id=None,
        sl_order_id=2002,
        position_id=55,
    )
    trader._latest_orderbook_snap = _snap()
    state_path = tmp_path / "reconcile.json"
    close_calls: List[str] = []

    def fake_close(**kwargs: Any) -> str:
        close_calls.append(str(kwargs.get("execution_type")))
        if close_calls.count("STOP") < 2:
            raise _err("ERR-5106")
        return "9002"

    with patch(
        "virtual_trader.fetch_open_positions",
        return_value=[_open_pos(position_id=55)],
    ), patch(
        "virtual_trader.fetch_active_orders", return_value=[]
    ), patch("virtual_trader.gmo_close_order", side_effect=fake_close):
        result = trader.reconcile_real_state_on_startup(state_path=state_path)
        assert result["status"] == "reordered"
        assert trader.position.sl_order_id is None
        t0 = trader._sl_missing_last_attempt_ts or 0.0
        trader._maybe_protect_missing_sl_unlocked(
            now_ts=t0 + SL_MISSING_RETRY_INTERVAL_SEC
        )

    assert close_calls.count("STOP") == 2
    assert trader.position.sl_order_id == 9002
    assert any("unprotected position" in msg for msg in alerts)


def test_no_double_close_after_watchdog_force_close() -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    _held_long_missing_sl(trader)
    close_calls: List[str] = []
    market_done = {"n": 0}

    def fake_close(**kwargs: Any) -> str:
        close_calls.append(str(kwargs.get("execution_type")))
        if kwargs.get("execution_type") == "STOP":
            raise _err("ERR-5106")
        market_done["n"] += 1
        return "8001"

    def fake_open() -> List[Dict[str, Any]]:
        if market_done["n"] > 0:
            return []
        return [_open_pos()]

    with patch("virtual_trader.gmo_close_order", side_effect=fake_close), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_open
    ), patch(
        "virtual_trader.fetch_real_account_state", return_value=_account()
    ), patch("virtual_trader.gmo_fetch_order_execution_fee", return_value=0), patch(
        "virtual_trader.time.sleep"
    ):
        trader._place_real_tp_sl_orders()
        t0 = trader._sl_missing_first_fail_ts or 0.0
        trader._maybe_protect_missing_sl_unlocked(
            now_ts=t0 + SL_MISSING_FORCE_CLOSE_AFTER_SEC
        )
        assert trader.position.side is None
        market_before = close_calls.count("MARKET")
        trader._maybe_protect_missing_sl_unlocked(
            now_ts=t0 + SL_MISSING_FORCE_CLOSE_AFTER_SEC + 3.0
        )
        tp = 10_020_000.0
        trader._check_active_position(_snap(bid=tp, ask=tp + 100.0))

    assert close_calls.count("MARKET") == market_before == 1


def test_board_tp_skipped_while_watchdog_force_closing() -> None:
    trader = _real_trader()
    _held_long_missing_sl(trader)
    trader._sl_missing_in_force_close = True
    close_calls: List[Dict[str, Any]] = []

    with patch(
        "virtual_trader.gmo_close_order",
        side_effect=lambda **kwargs: close_calls.append(kwargs) or "1",
    ):
        tp = trader.position.exit_price_target
        trader._check_active_position(_snap(bid=tp + 100.0, ask=tp + 200.0))

    assert close_calls == []
    assert trader.position.side == "LONG"


def test_sl_fail_log_contains_request_and_book(capsys: pytest.CaptureFixture[str]) -> None:
    trader = _real_trader()
    _held_long_missing_sl(trader, entry=10_000_000.0, size=0.01, position_id=55)
    trader._latest_orderbook_snap = _snap(bid=9_999_000.0, ask=10_001_000.0)
    trader._sl_close_order_ok_count = 4
    trader._sl_close_order_ng_count = 0

    with patch("virtual_trader.gmo_close_order", side_effect=_err("ERR-5106")):
        trader._place_real_tp_sl_orders()

    out = capsys.readouterr().out
    assert "[REAL-SL] closeOrder failed" in out
    assert "'symbol': 'BTC_JPY'" in out
    assert "'executionType': 'STOP'" in out
    assert "'positionId': 55" in out
    assert "'size': '0.01'" in out
    assert "entry_price=10000000.0" in out
    assert "bid=9999000.0" in out
    assert "ask=10001000.0" in out
    assert "ERR-5106" in out
    assert "sl_api_ok=4" in out
    assert "sl_api_ng=1" in out
    assert "fill_to_sl_ms=" in out
