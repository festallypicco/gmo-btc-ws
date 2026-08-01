"""
tests/test_check_engine_process.py

ネイティブ運用向けエンジン完全停止検知・自動復旧の単体テスト。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import check_engine_process as cep  # noqa: E402


@pytest.fixture
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cep, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(cep, "HEARTBEATS_PATH", runtime / "monitor_heartbeats.json")
    monkeypatch.setattr(cep, "STATE_PATH", runtime / "engine_process_state.json")
    monkeypatch.setattr(cep, "MANUAL_STOP_FLAG_PATH", runtime / "manual_stop.flag")
    return runtime


def test_manual_stop_skips_alert_and_recovery(isolated_runtime: Path) -> None:
    (isolated_runtime / "manual_stop.flag").write_text("stop", encoding="utf-8")
    ensure_calls: list[int] = []
    alerts: list[str] = []

    result = cep.check_engine_process(
        find_pids=lambda: [],
        ensure_fn=lambda: ensure_calls.append(1) or (True, "ok"),
        send_fn=lambda text: alerts.append(text) or True,
        manual_stop_path=isolated_runtime / "manual_stop.flag",
        grace_sec=0,
        sleep_fn=lambda _s: None,
    )
    assert result["status"] == "manual_stop"
    assert result["manual_stop"] is True
    assert ensure_calls == []
    assert alerts == []


def test_down_without_manual_stop_alerts_and_calls_ensure(
    isolated_runtime: Path,
) -> None:
    ensure_calls: list[int] = []
    alerts: list[str] = []
    after_ensure_pids = {"n": 0}

    def find_pids_with_recovery() -> list[int]:
        # first two grace samples empty; subsequent calls see process
        after_ensure_pids["n"] += 1
        if after_ensure_pids["n"] <= 2:
            return []
        return [12345]

    def ensure_fn():
        ensure_calls.append(1)
        return True, "started"

    result = cep.check_engine_process(
        find_pids=find_pids_with_recovery,
        ensure_fn=ensure_fn,
        send_fn=lambda text: alerts.append(text) or True,
        manual_stop_path=isolated_runtime / "manual_stop.flag",
        grace_sec=5,
        sleep_fn=lambda _s: None,
        now=datetime(2026, 7, 23, 16, 0, 0),
        attempt_recovery=True,
    )
    assert result["status"] == "down"
    assert result["recovery_attempted"] is True
    assert result["recovery_ok"] is True
    assert ensure_calls == [1]
    assert len(alerts) == 1
    assert "native engine process down" in alerts[0]
    assert "自動復旧: 成功" in alerts[0]


def test_grace_period_avoids_false_positive_when_process_returns(
    isolated_runtime: Path,
) -> None:
    ensure_calls: list[int] = []
    alerts: list[str] = []
    samples = [[], [999]]  # empty then recovered during grace

    def find_pids() -> list[int]:
        return samples.pop(0)

    result = cep.check_engine_process(
        find_pids=find_pids,
        ensure_fn=lambda: ensure_calls.append(1) or (True, "ok"),
        send_fn=lambda text: alerts.append(text) or True,
        manual_stop_path=isolated_runtime / "manual_stop.flag",
        grace_sec=30,
        sleep_fn=lambda _s: None,
    )
    assert result["status"] == "running"
    assert result["grace_skipped"] is True
    assert ensure_calls == []
    assert alerts == []


def test_running_process_is_ok(isolated_runtime: Path) -> None:
    ensure_calls: list[int] = []
    alerts: list[str] = []
    result = cep.check_engine_process(
        find_pids=lambda: [4242],
        ensure_fn=lambda: ensure_calls.append(1) or (True, "ok"),
        send_fn=lambda text: alerts.append(text) or True,
        grace_sec=0,
        sleep_fn=lambda _s: None,
    )
    assert result["status"] == "running"
    assert result["process_count"] == 1
    assert ensure_calls == []
    assert alerts == []


def test_recovery_failure_reported_in_alert(isolated_runtime: Path) -> None:
    alerts: list[str] = []
    result = cep.check_engine_process(
        find_pids=lambda: [],
        ensure_fn=lambda: (False, "startup timeout"),
        send_fn=lambda text: alerts.append(text) or True,
        manual_stop_path=isolated_runtime / "manual_stop.flag",
        grace_sec=0,
        sleep_fn=lambda _s: None,
        now=datetime(2026, 7, 23, 16, 0, 0),
    )
    assert result["status"] == "down"
    assert result["recovery_ok"] is False
    assert "自動復旧: 失敗" in alerts[0]
    assert "startup timeout" in alerts[0]
