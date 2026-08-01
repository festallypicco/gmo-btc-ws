"""Tests for scripts/check_monitor_heartbeats.py SLA coverage."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_monitor_heartbeats as cmh  # noqa: E402


@pytest.fixture()
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    heartbeats = runtime / "monitor_heartbeats.json"
    monkeypatch.setattr(cmh, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(cmh, "HEARTBEATS_PATH", heartbeats)
    return runtime


def test_monitor_slas_includes_check_orphan_orders() -> None:
    assert "check_orphan_orders" in cmh.MONITOR_SLAS
    assert cmh.MONITOR_SLAS["check_orphan_orders"].total_seconds() == 20 * 60


def test_check_orphan_orders_stale_triggers_alert(
    isolated_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeats = isolated_runtime / "monitor_heartbeats.json"
    heartbeats.write_text(
        json.dumps(
            {
                "check_orphan_orders": "2026-07-28T10:00:00",
            }
        ),
        encoding="utf-8",
    )
    alerts: List[str] = []

    def fake_send(message: str) -> bool:
        alerts.append(message)
        return True

    monkeypatch.setattr(cmh, "send_telegram_message", fake_send)

    exit_code = cmh.check_monitor_heartbeats(
        now=datetime(2026, 7, 28, 10, 25, 0),
        heartbeats_path=heartbeats,
    )
    assert exit_code == 0
    assert len(alerts) == 1
    assert "check_orphan_orders" in alerts[0]
    assert "monitor heartbeat stale" in alerts[0]
    flag = isolated_runtime / "heartbeat_stale_notified_check_orphan_orders.flag"
    assert flag.exists()
