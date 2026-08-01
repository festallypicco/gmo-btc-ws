"""
test_force_close_real.py

_real mode 緊急停止 (_force_close_real) の単体テスト。
GMO API 呼び出しはモック化する。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as virtual_trader_module  # noqa: E402
from strategy_logic import OrderbookSnapshot, PositionState  # noqa: E402
from virtual_trader import (  # noqa: E402
    GmoApiError,
    TAKER_FEE_RATE,
    VirtualTrader,
)


@pytest.fixture(autouse=True)
def isolated_trade_csv_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)
    return log_dir


def _snap() -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_bid_price=10_000_000.0,
        best_bid_size=0.5,
        best_ask_price=10_000_100.0,
        best_ask_size=0.5,
    )


def _trader_with_position(
    *,
    tp_order_id: Optional[int] = 1001,
    sl_order_id: Optional[int] = 1002,
) -> VirtualTrader:
    alerts: List[str] = []
    trader = VirtualTrader(
        trading_mode="real",
        on_critical_alert=lambda msg: alerts.append(msg),
    )
    trader._alerts = alerts  # type: ignore[attr-defined]
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=False,
        exit_price_target=10_012_000.0,
        tp_order_id=tp_order_id,
        sl_order_id=sl_order_id,
    )
    return trader


def test_force_close_real_cancel_and_close_success() -> None:
    trader = _trader_with_position()
    cancel_ids: List[int] = []
    close_calls: List[Dict[str, Any]] = []
    open_calls = {"n": 0}

    def fake_cancel(order_id: int) -> None:
        cancel_ids.append(order_id)

    def fake_fetch_open() -> List[Dict[str, Any]]:
        open_calls["n"] += 1
        # 1回目（確認）: 建玉あり / 2回目（決済後確認）: なし
        if open_calls["n"] == 1:
            return [{"positionId": 555, "size": "0.01", "side": "BUY"}]
        return []

    def fake_close(
        *,
        side: str,
        execution_type: str,
        settle_position: Dict[str, Any],
        symbol: str = "BTC_JPY",
    ) -> str:
        close_calls.append(
            {
                "side": side,
                "execution_type": execution_type,
                "settle_position": settle_position,
                "symbol": symbol,
            }
        )
        return "90001"

    with patch("virtual_trader.gmo_cancel_order", side_effect=fake_cancel), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_fetch_open
    ), patch("virtual_trader.gmo_close_order", side_effect=fake_close), patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 50_000.0,
            "equity_jpy": 50_000.0,
            "position_size_btc": 0.0,
        },
    ), patch("virtual_trader.time.sleep"):
        trader._force_close_real(_snap())

    assert cancel_ids == [1001, 1002]
    assert len(close_calls) == 1
    assert close_calls[0]["side"] == "SELL"
    assert close_calls[0]["execution_type"] == "MARKET"
    assert close_calls[0]["settle_position"]["positionId"] == 555
    assert trader.position.side is None
    assert trader._alerts == []  # type: ignore[attr-defined]


def test_force_close_real_benign_cancel_error_continues() -> None:
    trader = _trader_with_position()
    cancel_attempts: List[int] = []
    close_calls: List[Dict[str, Any]] = []
    open_calls = {"n": 0}

    def fake_cancel(order_id: int) -> None:
        cancel_attempts.append(order_id)
        raise GmoApiError(
            status=1,
            messages=[{"message_code": "ERR-5122", "message_string": "already done"}],
        )

    def fake_fetch_open() -> List[Dict[str, Any]]:
        open_calls["n"] += 1
        if open_calls["n"] == 1:
            return [{"positionId": 777, "size": "0.01", "side": "BUY"}]
        return []

    def fake_close(**kwargs: Any) -> str:
        close_calls.append(kwargs)
        return "90002"

    with patch("virtual_trader.gmo_cancel_order", side_effect=fake_cancel), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_fetch_open
    ), patch("virtual_trader.gmo_close_order", side_effect=fake_close), patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 50_000.0,
            "equity_jpy": 50_000.0,
            "position_size_btc": 0.0,
        },
    ), patch("virtual_trader.time.sleep"):
        trader._force_close_real(_snap())

    assert cancel_attempts == [1001, 1002]
    assert len(close_calls) == 1
    assert trader.position.side is None


def test_force_close_real_no_open_positions_skips_close() -> None:
    trader = _trader_with_position()
    close_calls: List[Any] = []

    with patch("virtual_trader.gmo_cancel_order", return_value=None), patch(
        "virtual_trader.fetch_open_positions", return_value=[]
    ), patch(
        "virtual_trader.gmo_close_order",
        side_effect=lambda **kwargs: close_calls.append(kwargs) or "x",
    ), patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 50_000.0,
            "equity_jpy": 50_000.0,
            "position_size_btc": 0.0,
        },
    ), patch("virtual_trader.time.sleep"):
        trader._force_close_real(_snap())

    assert close_calls == []
    assert trader.position.side is None


def test_force_close_real_close_fails_three_times_sends_critical_alert() -> None:
    trader = _trader_with_position()
    sleeps: List[float] = []

    with patch("virtual_trader.gmo_cancel_order", return_value=None), patch(
        "virtual_trader.fetch_open_positions",
        return_value=[{"positionId": 888, "size": "0.01", "side": "BUY"}],
    ), patch(
        "virtual_trader.gmo_close_order",
        side_effect=RuntimeError("close failed"),
    ), patch("virtual_trader.time.sleep", side_effect=lambda s: sleeps.append(s)):
        trader._force_close_real(_snap())

    assert trader.position.side == "LONG"
    assert len(trader._alerts) == 1  # type: ignore[attr-defined]
    assert "[CRITICAL] REAL MODE FORCE CLOSE FAILED" in trader._alerts[0]  # type: ignore[attr-defined]
    assert "manual intervention required immediately" in trader._alerts[0]  # type: ignore[attr-defined]
    assert sleeps == [1.0, 2.0]
    assert trader._force_close_real_cooldown_until > 0


def test_force_close_real_confirm_succeeds_on_second_check() -> None:
    """1回目の確認では残っているが、2回目で消えていれば正常終了。"""
    trader = _trader_with_position()
    open_calls = {"n": 0}
    sleeps: List[float] = []
    close_calls: List[Any] = []

    def fake_fetch_open() -> List[Dict[str, Any]]:
        open_calls["n"] += 1
        # 1: 決済前確認 / 2: close後1回目確認(残) / 3: close後2回目確認(消)
        if open_calls["n"] <= 2:
            return [{"positionId": 555, "size": "0.01", "side": "BUY"}]
        return []

    with patch("virtual_trader.gmo_cancel_order", return_value=None), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_fetch_open
    ), patch(
        "virtual_trader.gmo_close_order",
        side_effect=lambda **kwargs: close_calls.append(kwargs) or "90011",
    ), patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 50_000.0,
            "equity_jpy": 50_000.0,
            "position_size_btc": 0.0,
        },
    ), patch("virtual_trader.time.sleep", side_effect=lambda s: sleeps.append(s)):
        trader._force_close_real(_snap())

    assert len(close_calls) == 1
    assert open_calls["n"] == 3
    assert sleeps == [1.0]
    assert trader.position.side is None
    assert trader._alerts == []  # type: ignore[attr-defined]


def test_force_close_real_confirm_still_present_after_three_checks_fails() -> None:
    """確認3回とも残っている場合は attempt 失敗扱い（外側リトライへ）。"""
    trader = _trader_with_position()
    sleeps: List[float] = []
    close_calls: List[Any] = []
    remaining = [{"positionId": 888, "size": "0.01", "side": "BUY"}]

    with patch("virtual_trader.gmo_cancel_order", return_value=None), patch(
        "virtual_trader.fetch_open_positions",
        return_value=remaining,
    ), patch(
        "virtual_trader.gmo_close_order",
        side_effect=lambda **kwargs: close_calls.append(kwargs) or "90012",
    ), patch("virtual_trader.time.sleep", side_effect=lambda s: sleeps.append(s)):
        trader._force_close_real(_snap())

    # 外側 attempt 3回ぶん closeOrder が走る
    assert len(close_calls) == 3
    assert trader.position.side == "LONG"
    assert len(trader._alerts) == 1  # type: ignore[attr-defined]
    assert "[CRITICAL] REAL MODE FORCE CLOSE FAILED" in trader._alerts[0]  # type: ignore[attr-defined]
    # 各 attempt: confirm待ち 1s x2、attempt間: 1s, 2s
    assert sleeps == [1.0, 1.0, 1.0, 1.0, 1.0, 2.0, 1.0, 1.0]


def test_manual_stop_flag_real_mode_uses_force_close_real(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """起動時に flag がある場合も含め、同一 _update_maintenance_state 分岐を使う。"""
    flag = tmp_path / "manual_stop.flag"
    flag.write_text("stop", encoding="utf-8")
    monkeypatch.setattr("virtual_trader._MANUAL_STOP_FLAG_PATH", flag)

    trader = VirtualTrader(trading_mode="real")
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=False,
    )
    called = {"n": 0}

    def fake_force_close_real(snap: OrderbookSnapshot) -> None:
        called["n"] += 1

    with patch.object(trader, "_force_close_real", side_effect=fake_force_close_real), patch.object(
        trader, "_force_close_maintenance"
    ) as mock_virtual_close:
        from datetime import datetime

        trader._update_maintenance_state(_snap(), datetime.now())

    assert called["n"] == 1
    mock_virtual_close.assert_not_called()
    assert trader.engine_status == "STOPPING"


def test_manual_stop_real_pending_cancels_entry_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pending 緊急停止では entry_order_id の cancel API が呼ばれ、force_close_real は呼ばない。"""
    import virtual_trader as virtual_trader_module
    from datetime import datetime

    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)

    flag = tmp_path / "manual_stop.flag"
    flag.write_text("stop", encoding="utf-8")
    monkeypatch.setattr("virtual_trader._MANUAL_STOP_FLAG_PATH", flag)

    alerts: List[str] = []
    trader = VirtualTrader(
        trading_mode="real",
        on_critical_alert=lambda msg: alerts.append(msg),
    )
    trader.position = PositionState(
        side="SHORT",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=True,
        entry_order_id=555001,
    )
    cancel_ids: List[int] = []

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=lambda oid: cancel_ids.append(int(oid)),
    ), patch.object(trader, "_force_close_real") as mock_force_close:
        trader._update_maintenance_state(_snap(), datetime.now())

    assert cancel_ids == [555001]
    mock_force_close.assert_not_called()
    assert trader.position.side is None
    assert trader.engine_status == "STOPPING"
    assert len(alerts) == 1
    assert "pending entry order cancelled" in alerts[0]
    assert "555001" in alerts[0]


def test_manual_stop_real_pending_benign_cancel_adopts_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ERR-5122 等で cancel 失敗時は建玉採用フォールバックする。"""
    import virtual_trader as virtual_trader_module
    from datetime import datetime

    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)

    flag = tmp_path / "manual_stop.flag"
    flag.write_text("stop", encoding="utf-8")
    monkeypatch.setattr("virtual_trader._MANUAL_STOP_FLAG_PATH", flag)

    alerts: List[str] = []
    trader = VirtualTrader(
        trading_mode="real",
        on_critical_alert=lambda msg: alerts.append(msg),
    )
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=True,
        entry_order_id=555001,
    )

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=GmoApiError(
            status=1,
            messages=[{"message_code": "ERR-5122", "message_string": "done"}],
        ),
    ), patch(
        "virtual_trader.fetch_open_positions",
        return_value=[
            {
                "positionId": 9001,
                "side": "BUY",
                "size": "0.01",
                "price": "10000250",
            }
        ],
    ), patch(
        "virtual_trader.gmo_close_order",
        return_value="7002",
    ), patch.object(trader, "_force_close_real") as mock_force_close:
        trader._update_maintenance_state(_snap(), datetime.now())

    mock_force_close.assert_not_called()
    assert trader.position.is_pending is False
    assert trader.position.side == "LONG"
    assert trader.position.position_id == 9001
    assert trader.position.sl_order_id == 7002
    assert any("adopted open position" in a for a in alerts)
    assert trader.engine_status == "STOPPING"


def test_pre_maintenance_close_real_mode_uses_force_close_real() -> None:
    from datetime import datetime

    trader = VirtualTrader(trading_mode="real", maintenance_pre_action="close")
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=False,
    )
    called = {"n": 0}

    def fake_force_close_real(snap: OrderbookSnapshot) -> None:
        called["n"] += 1

    with patch.object(
        trader, "_is_weekly_pre_maintenance_window", return_value=True
    ), patch.object(
        trader, "_force_close_real", side_effect=fake_force_close_real
    ), patch.object(trader, "_force_close_maintenance") as mock_virtual_close:
        trader._update_maintenance_state(_snap(), datetime.now())

    assert called["n"] == 1
    mock_virtual_close.assert_not_called()


def test_pre_maintenance_close_virtual_mode_uses_force_close_maintenance() -> None:
    from datetime import datetime

    trader = VirtualTrader(trading_mode="virtual", maintenance_pre_action="close")
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=False,
    )
    called = {"n": 0}

    def fake_force_close_maintenance(snap: OrderbookSnapshot) -> None:
        called["n"] += 1

    with patch.object(
        trader, "_is_weekly_pre_maintenance_window", return_value=True
    ), patch.object(trader, "_force_close_real") as mock_real_close, patch.object(
        trader, "_force_close_maintenance", side_effect=fake_force_close_maintenance
    ):
        trader._update_maintenance_state(_snap(), datetime.now())

    assert called["n"] == 1
    mock_real_close.assert_not_called()


def test_force_close_real_success_syncs_jpy_balance_from_equity() -> None:
    trader = _trader_with_position()
    trader.jpy_balance = 40_000.0
    open_calls = {"n": 0}

    def fake_fetch_open() -> List[Dict[str, Any]]:
        open_calls["n"] += 1
        if open_calls["n"] == 1:
            return [{"positionId": 555, "size": "0.01", "side": "BUY"}]
        return []

    with patch("virtual_trader.gmo_cancel_order", return_value=None), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_fetch_open
    ), patch("virtual_trader.gmo_close_order", return_value="90001"), patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 48_000.0,
            "equity_jpy": 49_500.0,
            "position_size_btc": 0.0,
        },
    ), patch("virtual_trader.time.sleep"):
        trader._force_close_real(_snap())

    assert trader.position.side is None
    assert trader.jpy_balance == pytest.approx(49_500.0)
    assert trader._alerts == []  # type: ignore[attr-defined]


def test_force_close_real_equity_sync_failure_keeps_estimated_balance_and_alerts() -> None:
    trader = _trader_with_position()
    trader.jpy_balance = 40_000.0
    entry = trader.position.entry_price
    size = trader.position.size
    open_calls = {"n": 0}

    def fake_fetch_open() -> List[Dict[str, Any]]:
        open_calls["n"] += 1
        if open_calls["n"] == 1:
            return [{"positionId": 555, "size": "0.01", "side": "BUY"}]
        return []

    with patch("virtual_trader.gmo_cancel_order", return_value=None), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_fetch_open
    ), patch("virtual_trader.gmo_close_order", return_value="90001"), patch(
        "virtual_trader.fetch_real_account_state",
        side_effect=RuntimeError("margin api down"),
    ), patch("virtual_trader.time.sleep"):
        trader._force_close_real(_snap())

    assert trader.position.side is None
    # settle で推定 PnL を反映したあと sync 失敗 → 推定残高のまま
    exit_price = 10_000_000.0
    fee = int(exit_price * size * TAKER_FEE_RATE)
    net = (exit_price - entry) * size - fee
    assert trader.jpy_balance == pytest.approx(40_000.0 + net)
    assert len(trader._alerts) == 1  # type: ignore[attr-defined]
    assert "equity sync failed" in trader._alerts[0]  # type: ignore[attr-defined]


def test_force_close_real_success_updates_kpi_and_trade_history() -> None:
    trader = _trader_with_position()
    trader.jpy_balance = 50_000.0
    entry = trader.position.entry_price
    size = trader.position.size
    before_cum = trader._cumulative_pnl
    open_calls = {"n": 0}

    def fake_fetch_open() -> List[Dict[str, Any]]:
        open_calls["n"] += 1
        if open_calls["n"] == 1:
            return [{"positionId": 555, "size": "0.01", "side": "BUY"}]
        return []

    with patch("virtual_trader.gmo_cancel_order", return_value=None), patch(
        "virtual_trader.fetch_open_positions", side_effect=fake_fetch_open
    ), patch("virtual_trader.gmo_close_order", return_value="90001"), patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 49_900.0,
            "equity_jpy": 49_960.0,
            "position_size_btc": 0.0,
        },
    ), patch("virtual_trader.time.sleep"):
        trader._force_close_real(_snap())

    exit_price = 10_000_000.0
    fee = int(exit_price * size * TAKER_FEE_RATE)
    net = (exit_price - entry) * size - fee
    assert trader.position.side is None
    assert trader._cumulative_pnl == pytest.approx(before_cum + net)
    assert trader._loss_count == 1
    assert trader.daily_realized_pnl == pytest.approx(net)
    assert len(trader.trade_history) == 1
    assert trader.trade_history[0].reason == "FORCE_CLOSE_REAL"
    assert trader.jpy_balance == pytest.approx(49_960.0)


def test_force_close_real_failure_does_not_update_kpi() -> None:
    trader = _trader_with_position()
    before_cum = trader._cumulative_pnl
    before_hist = len(trader.trade_history)

    with patch("virtual_trader.gmo_cancel_order", return_value=None), patch(
        "virtual_trader.fetch_open_positions",
        return_value=[{"positionId": 555, "size": "0.01", "side": "BUY"}],
    ), patch(
        "virtual_trader.gmo_close_order",
        side_effect=RuntimeError("close failed"),
    ), patch("virtual_trader.time.sleep"):
        trader._force_close_real(_snap())

    assert trader.position.side == "LONG"
    assert trader._cumulative_pnl == pytest.approx(before_cum)
    assert len(trader.trade_history) == before_hist
    assert len(trader._alerts) == 1  # type: ignore[attr-defined]
    assert "FORCE CLOSE FAILED" in trader._alerts[0]  # type: ignore[attr-defined]
