"""
tests/test_check_orphan_orders.py

孤児注文チェック（check_orphan_orders.py）の単体テスト。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
_BTC = _ROOT / "btc_trading_tool"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
if str(_BTC) not in sys.path:
    sys.path.insert(0, str(_BTC))

import check_orphan_orders as coo  # noqa: E402
import virtual_trader as vt  # noqa: E402

# 既存テストが土曜メンテ枠で誤スキップしないよう、平日昼を既定にする
_SAFE_NOW = datetime(2026, 8, 7, 12, 0, 0)  # Friday


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    db_path = runtime / "live_state.db"
    heartbeats = runtime / "monitor_heartbeats.json"
    state_path = runtime / "orphan_orders_state.json"

    monkeypatch.setattr(coo, "CONFIG_PATH", config_path)
    monkeypatch.setattr(coo, "LIVE_STATE_DB_PATH", db_path)
    monkeypatch.setattr(coo, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(coo, "HEARTBEATS_PATH", heartbeats)
    monkeypatch.setattr(coo, "STATE_PATH", state_path)
    return {
        "config": config_path,
        "db": db_path,
        "heartbeats": heartbeats,
        "runtime": runtime,
        "state": state_path,
    }


def _write_config(
    path: Path,
    trading_mode: str | None,
    *,
    maintenance_prepare_minutes: Optional[int] = None,
) -> None:
    payload: Dict[str, Any] = {"version": "test"}
    if trading_mode is not None:
        payload["trading_mode"] = trading_mode
    if maintenance_prepare_minutes is not None:
        payload["maintenance_prepare_minutes"] = maintenance_prepare_minutes
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_live_state(
    db_path: Path,
    *,
    entry_order_id: int | None = None,
    tp_order_id: int | None = None,
    sl_order_id: int | None = None,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_state (
                id INTEGER PRIMARY KEY,
                entry_order_id INTEGER,
                tp_order_id INTEGER,
                sl_order_id INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO live_state (id, entry_order_id, tp_order_id, sl_order_id)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                entry_order_id = excluded.entry_order_id,
                tp_order_id = excluded.tp_order_id,
                sl_order_id = excluded.sl_order_id
            """,
            (entry_order_id, tp_order_id, sl_order_id),
        )
        conn.commit()


def test_no_orphans_when_all_active_match_known_ids(isolated_paths: Dict[str, Path]) -> None:
    _write_config(isolated_paths["config"], "real")
    _write_live_state(
        isolated_paths["db"],
        entry_order_id=111,
        tp_order_id=222,
        sl_order_id=333,
    )
    alerts: List[str] = []
    fetch_calls: List[int] = []

    def fetch_fn() -> List[Dict[str, Any]]:
        fetch_calls.append(1)
        return [
            {"orderId": 111, "side": "BUY", "price": "10000000", "size": "0.01"},
            {"orderId": 222, "side": "SELL", "price": "10015000", "size": "0.01"},
            {"orderId": 333, "side": "SELL", "price": "9985000", "size": "0.01"},
        ]

    result = coo.check_orphan_orders(
        config_path=isolated_paths["config"],
        db_path=isolated_paths["db"],
        fetch_fn=fetch_fn,
        send_fn=lambda text: alerts.append(text) or True,
        ensure_credentials_fn=lambda: None,
        now=_SAFE_NOW,
    )

    assert result["status"] == "ok"
    assert result["orphan_count"] == 0
    assert fetch_calls == [1]
    assert alerts == []


def _run_check(
    isolated_paths: Dict[str, Path],
    *,
    fetch_fn: Any,
    alerts: List[str],
    now: datetime = _SAFE_NOW,
) -> Dict[str, Any]:
    return coo.check_orphan_orders(
        config_path=isolated_paths["config"],
        db_path=isolated_paths["db"],
        state_path=isolated_paths["state"],
        fetch_fn=fetch_fn,
        send_fn=lambda text: alerts.append(text) or True,
        ensure_credentials_fn=lambda: None,
        now=now,
    )


def test_orphan_first_sighting_logs_only_no_telegram(
    isolated_paths: Dict[str, Path],
) -> None:
    _write_config(isolated_paths["config"], "real")
    _write_live_state(
        isolated_paths["db"],
        entry_order_id=111,
        tp_order_id=222,
        sl_order_id=333,
    )
    alerts: List[str] = []

    result = _run_check(
        isolated_paths,
        fetch_fn=lambda: [
            {"orderId": 222, "side": "SELL", "price": "10015000", "size": "0.01"},
            {
                "orderId": 9999,
                "side": "BUY",
                "price": "9900000",
                "size": "0.05",
            },
        ],
        alerts=alerts,
    )

    assert result["status"] == "orphan_pending"
    assert result["pending_count"] == 1
    assert result["confirmed_count"] == 0
    assert alerts == []
    state = json.loads(isolated_paths["state"].read_text(encoding="utf-8"))
    assert state["suspect_order_ids"] == [9999]


def test_orphan_second_consecutive_sighting_sends_telegram(
    isolated_paths: Dict[str, Path],
) -> None:
    _write_config(isolated_paths["config"], "real")
    _write_live_state(
        isolated_paths["db"],
        entry_order_id=111,
        tp_order_id=222,
        sl_order_id=333,
    )
    alerts: List[str] = []

    def fetch_fn() -> List[Dict[str, Any]]:
        return [
            {"orderId": 222, "side": "SELL", "price": "10015000", "size": "0.01"},
            {
                "orderId": 9999,
                "side": "BUY",
                "price": "9900000",
                "size": "0.05",
            },
        ]

    first = _run_check(isolated_paths, fetch_fn=fetch_fn, alerts=alerts)
    assert first["status"] == "orphan_pending"
    assert alerts == []

    second = _run_check(isolated_paths, fetch_fn=fetch_fn, alerts=alerts)
    assert second["status"] == "orphan_detected"
    assert second["orphan_count"] == 1
    assert second["confirmed_count"] == 1
    assert len(alerts) == 1
    assert "9999" in alerts[0]
    assert "BUY" in alerts[0]
    assert "9900000" in alerts[0]
    assert "0.05" in alerts[0]
    assert "orphan GMO active order" in alerts[0]


def test_orphan_race_clears_before_second_check_no_alert(
    isolated_paths: Dict[str, Path],
) -> None:
    """1回目だけ orphan、2回目は known に入った想定（レース解消）。"""
    _write_config(isolated_paths["config"], "real")
    _write_live_state(
        isolated_paths["db"],
        entry_order_id=None,
        tp_order_id=None,
        sl_order_id=None,
    )
    alerts: List[str] = []

    first = _run_check(
        isolated_paths,
        fetch_fn=lambda: [
            {"orderId": 555, "side": "SELL", "price": "10000000", "size": "0.02"}
        ],
        alerts=alerts,
    )
    assert first["status"] == "orphan_pending"
    assert alerts == []

    # live_state に反映された後の2回目
    _write_live_state(
        isolated_paths["db"],
        entry_order_id=555,
        tp_order_id=None,
        sl_order_id=None,
    )

    second = _run_check(
        isolated_paths,
        fetch_fn=lambda: [
            {"orderId": 555, "side": "SELL", "price": "10000000", "size": "0.02"}
        ],
        alerts=alerts,
    )
    assert second["status"] == "ok"
    assert second["orphan_count"] == 0
    assert alerts == []
    state = json.loads(isolated_paths["state"].read_text(encoding="utf-8"))
    assert state["suspect_order_ids"] == []


def test_all_none_known_ids_treats_any_active_as_orphan_after_two_checks(
    isolated_paths: Dict[str, Path],
) -> None:
    _write_config(isolated_paths["config"], "real")
    _write_live_state(
        isolated_paths["db"],
        entry_order_id=None,
        tp_order_id=None,
        sl_order_id=None,
    )
    alerts: List[str] = []

    fetch_fn = lambda: [  # noqa: E731
        {"orderId": 555, "side": "SELL", "price": "10000000", "size": "0.02"}
    ]
    first = _run_check(isolated_paths, fetch_fn=fetch_fn, alerts=alerts)
    assert first["status"] == "orphan_pending"
    assert alerts == []

    second = _run_check(isolated_paths, fetch_fn=fetch_fn, alerts=alerts)
    assert second["status"] == "orphan_detected"
    assert second["orphan_count"] == 1
    assert len(alerts) == 1
    assert "555" in alerts[0]
    assert "0.02" in alerts[0]


def test_non_real_mode_skips_api_and_telegram(isolated_paths: Dict[str, Path]) -> None:
    _write_config(isolated_paths["config"], "virtual")
    _write_live_state(isolated_paths["db"], entry_order_id=111)
    alerts: List[str] = []
    fetch_calls: List[int] = []

    result = coo.check_orphan_orders(
        config_path=isolated_paths["config"],
        db_path=isolated_paths["db"],
        fetch_fn=lambda: fetch_calls.append(1) or [],
        send_fn=lambda text: alerts.append(text) or True,
        ensure_credentials_fn=lambda: None,
        now=_SAFE_NOW,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "not_real"
    assert fetch_calls == []
    assert alerts == []
    heartbeats = json.loads(isolated_paths["heartbeats"].read_text(encoding="utf-8"))
    assert "check_orphan_orders" in heartbeats


def test_missing_trading_mode_skips_without_error(isolated_paths: Dict[str, Path]) -> None:
    _write_config(isolated_paths["config"], None)
    alerts: List[str] = []
    fetch_calls: List[int] = []

    result = coo.check_orphan_orders(
        config_path=isolated_paths["config"],
        db_path=isolated_paths["db"],
        fetch_fn=lambda: fetch_calls.append(1) or [],
        send_fn=lambda text: alerts.append(text) or True,
        ensure_credentials_fn=lambda: None,
        now=_SAFE_NOW,
    )

    assert result["status"] == "skipped"
    assert fetch_calls == []
    assert alerts == []


def test_default_fetch_passes_readonly_credential_scope(
    isolated_paths: Dict[str, Path],
) -> None:
    _write_config(isolated_paths["config"], "real")
    _write_live_state(isolated_paths["db"], entry_order_id=111)
    alerts: List[str] = []
    seen: List[Any] = []

    def fake_fetch_active_orders(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        seen.append({"args": args, "kwargs": kwargs})
        return [{"orderId": 111, "side": "BUY", "price": "1", "size": "0.01"}]

    with patch.object(coo, "fetch_active_orders", side_effect=fake_fetch_active_orders):
        result = coo.check_orphan_orders(
            config_path=isolated_paths["config"],
            db_path=isolated_paths["db"],
            fetch_fn=None,
            send_fn=lambda text: alerts.append(text) or True,
            ensure_credentials_fn=lambda: None,
            now=_SAFE_NOW,
        )

    assert result["status"] == "ok"
    assert len(seen) == 1
    assert seen[0]["kwargs"].get("credential_scope") == "readonly"
    assert alerts == []


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 8, 8, 55, 0),  # Saturday pre-maint start
        datetime(2026, 8, 8, 8, 59, 59),  # Saturday pre-maint end
        datetime(2026, 8, 8, 9, 0, 0),  # Saturday weekly start
        datetime(2026, 8, 8, 10, 30, 0),  # Saturday weekly mid
        datetime(2026, 8, 8, 10, 59, 59),  # Saturday weekly near end
        datetime(2026, 8, 8, 6, 0, 0),  # Saturday daily window
        datetime(2026, 8, 7, 5, 55, 0),  # Friday daily start
    ],
)
def test_maintenance_window_skips_api_and_updates_heartbeat(
    isolated_paths: Dict[str, Path],
    now: datetime,
) -> None:
    _write_config(isolated_paths["config"], "real")
    _write_live_state(isolated_paths["db"], entry_order_id=111)
    alerts: List[str] = []
    fetch_calls: List[int] = []

    result = coo.check_orphan_orders(
        config_path=isolated_paths["config"],
        db_path=isolated_paths["db"],
        fetch_fn=lambda: fetch_calls.append(1) or [],
        send_fn=lambda text: alerts.append(text) or True,
        ensure_credentials_fn=lambda: None,
        now=now,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "maintenance_window"
    assert fetch_calls == []
    assert alerts == []
    heartbeats = json.loads(isolated_paths["heartbeats"].read_text(encoding="utf-8"))
    assert "check_orphan_orders" in heartbeats


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 8, 8, 54, 59),  # just before pre-maint
        datetime(2026, 8, 8, 11, 0, 0),  # weekly end (exclusive)
        datetime(2026, 8, 7, 12, 0, 0),  # weekday midday
        datetime(2026, 8, 7, 6, 30, 0),  # daily end (exclusive)
    ],
)
def test_outside_maintenance_window_still_calls_api(
    isolated_paths: Dict[str, Path],
    now: datetime,
) -> None:
    _write_config(isolated_paths["config"], "real")
    _write_live_state(isolated_paths["db"], entry_order_id=111)
    fetch_calls: List[int] = []

    result = coo.check_orphan_orders(
        config_path=isolated_paths["config"],
        db_path=isolated_paths["db"],
        fetch_fn=lambda: fetch_calls.append(1) or [
            {"orderId": 111, "side": "BUY", "price": "1", "size": "0.01"}
        ],
        send_fn=lambda text: True,
        ensure_credentials_fn=lambda: None,
        now=now,
    )

    assert result["status"] == "ok"
    assert fetch_calls == [1]


def test_pre_maintenance_uses_config_prepare_minutes(
    isolated_paths: Dict[str, Path],
) -> None:
    """prepare_minutes=10 なら 8:50 からスキップする。"""
    _write_config(
        isolated_paths["config"],
        "real",
        maintenance_prepare_minutes=10,
    )
    _write_live_state(isolated_paths["db"], entry_order_id=1)
    fetch_calls: List[int] = []
    now = datetime(2026, 8, 8, 8, 50, 0)

    result = coo.check_orphan_orders(
        config_path=isolated_paths["config"],
        db_path=isolated_paths["db"],
        fetch_fn=lambda: fetch_calls.append(1) or [],
        send_fn=lambda text: True,
        ensure_credentials_fn=lambda: None,
        now=now,
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "maintenance_window"
    assert result["prepare_minutes"] == 10
    assert fetch_calls == []

    # 8:49 はまだ枠外
    result2 = coo.check_orphan_orders(
        config_path=isolated_paths["config"],
        db_path=isolated_paths["db"],
        fetch_fn=lambda: fetch_calls.append(1)
        or [{"orderId": 1, "side": "BUY", "price": "1", "size": "0.01"}],
        send_fn=lambda text: True,
        ensure_credentials_fn=lambda: None,
        now=datetime(2026, 8, 8, 8, 49, 0),
    )
    assert result2["status"] == "ok"
    assert fetch_calls == [1]


def test_shared_maintenance_helpers_match_virtual_trader_instance() -> None:
    """モジュール関数と VirtualTrader メソッドの判定が一致すること。"""
    trader = vt.VirtualTrader(
        initial_jpy=50_000.0,
        maintenance_prepare_minutes=5,
    )
    samples = [
        datetime(2026, 8, 8, 8, 55, 0),
        datetime(2026, 8, 8, 9, 30, 0),
        datetime(2026, 8, 8, 11, 0, 0),
        datetime(2026, 8, 7, 5, 55, 0),
        datetime(2026, 8, 7, 12, 0, 0),
    ]
    for now in samples:
        assert trader._is_weekly_maintenance_window(now) == vt.is_weekly_maintenance_window(
            now
        )
        assert trader._is_daily_maintenance_window(now) == vt.is_daily_maintenance_window(
            now
        )
        assert trader._is_weekly_pre_maintenance_window(
            now
        ) == vt.is_weekly_pre_maintenance_window(now, prepare_minutes=5)
        assert (
            trader._is_weekly_pre_maintenance_window(now)
            or trader._is_weekly_maintenance_window(now)
            or trader._is_daily_maintenance_window(now)
        ) == vt.is_gmo_scheduled_maintenance_window(now, prepare_minutes=5)
