"""
test_real_startup_reconcile.py

real mode 起動時の建玉・有効注文突き合わせテスト。
"""

from __future__ import annotations

import json
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
from strategy_logic import PositionState  # noqa: E402
from virtual_trader import VirtualTrader  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_trade_csv_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)
    return log_dir


def _real_trader(alerts: Optional[List[str]] = None) -> VirtualTrader:
    alert_list = alerts if alerts is not None else []
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        trading_mode="real",
        on_critical_alert=lambda msg: alert_list.append(msg),
    )
    trader._alerts = alert_list  # type: ignore[attr-defined]
    return trader


def _active(order_id: int) -> Dict[str, Any]:
    return {"orderId": order_id, "status": "ORDERED"}


def _open_pos(
    *,
    position_id: int = 9001,
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


def test_pending_entry_still_active_no_change(tmp_path: Path) -> None:
    trader = _real_trader()
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=True,
        entry_order_id=111,
    )
    state_path = tmp_path / "reconcile.json"
    before = trader.position

    with patch(
        "virtual_trader.fetch_open_positions", return_value=[]
    ), patch(
        "virtual_trader.fetch_active_orders", return_value=[_active(111)]
    ), patch("virtual_trader.gmo_order") as order_mock:
        result = trader.reconcile_real_state_on_startup(state_path=state_path)

    assert result["status"] == "ok"
    assert result["case"] == "pending_order_live"
    assert trader.position.side == before.side
    assert trader.position.is_pending is True
    assert trader.position.entry_order_id == 111
    assert order_mock.call_count == 0
    assert not state_path.exists()


def test_pending_filled_on_exchange_adopts_and_places_sl(tmp_path: Path) -> None:
    trader = _real_trader()
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=True,
        entry_order_id=111,
    )
    state_path = tmp_path / "reconcile.json"
    calls: List[Dict[str, Any]] = []

    def fake_close(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "8002"

    with patch(
        "virtual_trader.fetch_open_positions",
        return_value=[_open_pos(position_id=77, price=10_001_000.0, size=0.01)],
    ), patch(
        "virtual_trader.fetch_active_orders", return_value=[]
    ), patch("virtual_trader.gmo_order") as order_mock, patch(
        "virtual_trader.gmo_close_order", side_effect=fake_close
    ):
        result = trader.reconcile_real_state_on_startup(state_path=state_path)

    assert result["status"] == "adopted_fill"
    assert trader.position.is_pending is False
    assert trader.position.side == "LONG"
    assert trader.position.entry_price == 10_001_000.0
    assert trader.position.position_id == 77
    assert trader.position.tp_order_id is None
    assert trader.position.sl_order_id == 8002
    assert order_mock.call_count == 0
    assert len(calls) == 1
    assert calls[0]["execution_type"] == "STOP"
    assert not state_path.exists()


def test_pending_gone_releases_locked_capital_and_clears(tmp_path: Path) -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    # real は想定元本を拘束しないため、pending 消失時も残高は据え置き
    trader.jpy_balance = 40_000.0
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.001,
        is_pending=True,
        entry_order_id=111,
    )
    state_path = tmp_path / "reconcile.json"

    with patch(
        "virtual_trader.fetch_open_positions", return_value=[]
    ), patch(
        "virtual_trader.fetch_active_orders", return_value=[]
    ), patch("virtual_trader.gmo_order") as order_mock:
        result = trader.reconcile_real_state_on_startup(state_path=state_path)

    assert result["status"] == "cleared"
    assert trader.position.side is None
    assert trader.position.is_pending is False
    assert trader.jpy_balance == pytest.approx(40_000.0)
    assert order_mock.call_count == 0
    assert any("startup_reconcile_pending_gone" in a for a in alerts)
    assert any("estimated_loss_jpy=" in a for a in alerts)


def test_held_missing_on_exchange_clears_and_alerts(tmp_path: Path) -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=False,
        exit_price_target=10_015_000.0,
        entry_order_id=111,
        tp_order_id=2001,
        sl_order_id=2002,
    )
    state_path = tmp_path / "reconcile.json"

    with patch(
        "virtual_trader.fetch_open_positions", return_value=[]
    ), patch(
        "virtual_trader.fetch_active_orders",
        return_value=[_active(2001), _active(2002)],
    ), patch("virtual_trader.gmo_order") as order_mock:
        result = trader.reconcile_real_state_on_startup(state_path=state_path)

    assert result["status"] == "cleared"
    assert result["case"] == "held_missing_on_exchange"
    assert trader.position.side is None
    assert order_mock.call_count == 0
    assert any("held position missing on GMO" in a for a in alerts)
    assert not state_path.exists()


def test_held_sl_present_with_tp_none_is_ok_no_reorder(tmp_path: Path) -> None:
    """新設計: tp_order_id=None は正常。SL が active なら再発注しない。"""
    trader = _real_trader()
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
    state_path = tmp_path / "reconcile.json"
    state_path.write_text(
        json.dumps({"fingerprint": "positionId=55", "reordered": True}),
        encoding="utf-8",
    )

    with patch(
        "virtual_trader.fetch_open_positions",
        return_value=[_open_pos(position_id=55, size=0.01)],
    ), patch(
        "virtual_trader.fetch_active_orders",
        return_value=[_active(2002)],
    ), patch("virtual_trader.gmo_order") as order_mock, patch(
        "virtual_trader.gmo_close_order"
    ) as close_mock:
        result = trader.reconcile_real_state_on_startup(state_path=state_path)

    assert result["status"] == "ok"
    assert result["case"] == "held_orders_live"
    assert order_mock.call_count == 0
    assert close_mock.call_count == 0
    assert trader.position.tp_order_id is None
    assert trader.position.sl_order_id == 2002
    assert not state_path.exists()


def test_held_missing_sl_reorders_sl_only(tmp_path: Path) -> None:
    trader = _real_trader()
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
    state_path = tmp_path / "reconcile.json"
    calls: List[Dict[str, Any]] = []

    def fake_close(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "9002"

    with patch(
        "virtual_trader.fetch_open_positions",
        return_value=[_open_pos(position_id=55, price=10_000_000.0, size=0.01)],
    ), patch(
        "virtual_trader.fetch_active_orders",
        return_value=[],  # SL missing
    ), patch("virtual_trader.gmo_order") as order_mock, patch(
        "virtual_trader.gmo_close_order", side_effect=fake_close
    ):
        result = trader.reconcile_real_state_on_startup(state_path=state_path)

    assert result["status"] == "reordered"
    assert result["missing"] == ["sl"]
    assert trader.position.tp_order_id is None
    assert trader.position.sl_order_id == 9002
    assert order_mock.call_count == 0
    assert len(calls) == 1
    assert calls[0]["execution_type"] == "STOP"
    assert calls[0]["settle_position"]["positionId"] == 55
    assert state_path.exists()
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["fingerprint"] == "positionId=55"
    assert saved["reordered"] is True
    assert saved["missing"] == ["sl"]


def test_held_persistent_sl_mismatch_triggers_safety_stop(tmp_path: Path) -> None:
    trader = _real_trader()
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=False,
        exit_price_target=10_015_000.0,
        tp_order_id=None,
        sl_order_id=2002,
        position_id=55,
    )
    state_path = tmp_path / "reconcile.json"
    state_path.write_text(
        json.dumps(
            {
                "fingerprint": "positionId=55",
                "reordered": True,
                "missing": ["sl"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    safety_calls: List[Any] = []

    def trigger(reason: str, details: Optional[Dict[str, Any]] = None) -> None:
        safety_calls.append((reason, details))

    with patch(
        "virtual_trader.fetch_open_positions",
        return_value=[_open_pos(position_id=55, price=10_000_000.0, size=0.01)],
    ), patch(
        "virtual_trader.fetch_active_orders",
        return_value=[],  # still missing SL
    ), patch("virtual_trader.gmo_order") as order_mock, patch(
        "virtual_trader.gmo_close_order"
    ) as close_mock:
        result = trader.reconcile_real_state_on_startup(
            trigger_safety_stop=trigger,
            state_path=state_path,
        )

    assert result["status"] == "safety_stop"
    assert result["missing"] == ["sl"]
    assert order_mock.call_count == 0
    assert close_mock.call_count == 0
    assert len(safety_calls) == 1
    assert safety_calls[0][0] == "startup_reconcile_persistent_mismatch"
    assert safety_calls[0][1] is not None
    assert safety_calls[0][1]["missing"] == ["sl"]
    assert trader.position.tp_order_id is None
    assert trader.position.sl_order_id == 2002


def test_held_tp_none_does_not_false_trigger_safety_or_tp_reorder(
    tmp_path: Path,
) -> None:
    """tp_order_id=None だけでは安全停止・再発注しない（今回の主目的）。"""
    trader = _real_trader()
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=False,
        exit_price_target=10_015_000.0,
        tp_order_id=None,
        sl_order_id=2002,
        position_id=55,
    )
    state_path = tmp_path / "reconcile.json"
    # 前回 SL 欠落として reordered 済みでも、今回 SL が生きていれば ok
    state_path.write_text(
        json.dumps(
            {
                "fingerprint": "positionId=55",
                "reordered": True,
                "missing": ["sl"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    safety_calls: List[Any] = []

    def trigger(reason: str, details: Optional[Dict[str, Any]] = None) -> None:
        safety_calls.append((reason, details))

    with patch(
        "virtual_trader.fetch_open_positions",
        return_value=[_open_pos(position_id=55, size=0.01)],
    ), patch(
        "virtual_trader.fetch_active_orders",
        return_value=[_active(2002)],
    ), patch("virtual_trader.gmo_order") as order_mock, patch(
        "virtual_trader.gmo_close_order"
    ) as close_mock:
        result = trader.reconcile_real_state_on_startup(
            trigger_safety_stop=trigger,
            state_path=state_path,
        )

    assert result["status"] == "ok"
    assert result["case"] == "held_orders_live"
    assert safety_calls == []
    assert order_mock.call_count == 0
    assert close_mock.call_count == 0
    assert trader.position.tp_order_id is None
    assert not state_path.exists()


def test_held_orders_match_clears_reconcile_state(tmp_path: Path) -> None:
    trader = _real_trader()
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=False,
        exit_price_target=10_015_000.0,
        tp_order_id=None,
        sl_order_id=2002,
        position_id=55,
    )
    state_path = tmp_path / "reconcile.json"
    state_path.write_text(
        json.dumps({"fingerprint": "positionId=55", "reordered": True}),
        encoding="utf-8",
    )

    with patch(
        "virtual_trader.fetch_open_positions",
        return_value=[_open_pos(position_id=55, size=0.01)],
    ), patch(
        "virtual_trader.fetch_active_orders",
        return_value=[_active(2002)],
    ), patch("virtual_trader.gmo_order") as order_mock, patch(
        "virtual_trader.gmo_close_order"
    ) as close_mock:
        result = trader.reconcile_real_state_on_startup(state_path=state_path)

    assert result["status"] == "ok"
    assert result["case"] == "held_orders_live"
    assert order_mock.call_count == 0
    assert close_mock.call_count == 0
    assert not state_path.exists()


def test_virtual_mode_skips_reconcile_without_gmo_calls() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=True,
        entry_order_id=111,
    )
    with patch("virtual_trader.fetch_open_positions") as pos_mock, patch(
        "virtual_trader.fetch_active_orders"
    ) as ord_mock:
        result = trader.reconcile_real_state_on_startup()

    assert result == {"status": "skipped", "reason": "not_real"}
    assert pos_mock.call_count == 0
    assert ord_mock.call_count == 0
    assert trader.position.is_pending is True
    assert trader.position.entry_order_id == 111
