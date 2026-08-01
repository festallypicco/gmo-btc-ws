"""
tests/test_check_orphan_orders.py

孤児注文チェック（check_orphan_orders.py）の単体テスト。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import check_orphan_orders as coo  # noqa: E402


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    db_path = runtime / "live_state.db"
    heartbeats = runtime / "monitor_heartbeats.json"

    monkeypatch.setattr(coo, "CONFIG_PATH", config_path)
    monkeypatch.setattr(coo, "LIVE_STATE_DB_PATH", db_path)
    monkeypatch.setattr(coo, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(coo, "HEARTBEATS_PATH", heartbeats)
    return {
        "config": config_path,
        "db": db_path,
        "heartbeats": heartbeats,
        "runtime": runtime,
    }


def _write_config(path: Path, trading_mode: str | None) -> None:
    payload: Dict[str, Any] = {"version": "test"}
    if trading_mode is not None:
        payload["trading_mode"] = trading_mode
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
            CREATE TABLE live_state (
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
    )

    assert result["status"] == "ok"
    assert result["orphan_count"] == 0
    assert fetch_calls == [1]
    assert alerts == []


def test_orphan_detected_and_telegram_notified(isolated_paths: Dict[str, Path]) -> None:
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

    result = coo.check_orphan_orders(
        config_path=isolated_paths["config"],
        db_path=isolated_paths["db"],
        fetch_fn=fetch_fn,
        send_fn=lambda text: alerts.append(text) or True,
        ensure_credentials_fn=lambda: None,
    )

    assert result["status"] == "orphan_detected"
    assert result["orphan_count"] == 1
    assert len(alerts) == 1
    assert "9999" in alerts[0]
    assert "BUY" in alerts[0]
    assert "9900000" in alerts[0]
    assert "0.05" in alerts[0]
    assert "orphan GMO active order" in alerts[0]


def test_all_none_known_ids_treats_any_active_as_orphan(
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

    result = coo.check_orphan_orders(
        config_path=isolated_paths["config"],
        db_path=isolated_paths["db"],
        fetch_fn=lambda: [
            {"orderId": 555, "side": "SELL", "price": "10000000", "size": "0.02"}
        ],
        send_fn=lambda text: alerts.append(text) or True,
        ensure_credentials_fn=lambda: None,
    )

    assert result["status"] == "orphan_detected"
    assert result["orphan_count"] == 1
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
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "not_real"
    assert fetch_calls == []
    assert alerts == []


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
        )

    assert result["status"] == "ok"
    assert len(seen) == 1
    assert seen[0]["kwargs"].get("credential_scope") == "readonly"
    assert alerts == []
