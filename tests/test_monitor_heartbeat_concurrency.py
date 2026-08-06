"""Concurrent writes to monitor_heartbeats.json must not race or drop keys."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_csv_db_consistency as ccd  # noqa: E402
import check_engine_crash_loop as cec  # noqa: E402
import check_engine_process as cep  # noqa: E402
import check_orphan_orders as coo  # noqa: E402
import check_trading_anomaly as cta  # noqa: E402
from monitor_heartbeat import record_monitor_heartbeat  # noqa: E402

MONITOR_KEYS = (
    "check_engine_crash_loop",
    "check_orphan_orders",
    "check_csv_db_consistency",
    "check_trading_anomaly",
    "check_engine_process",
)


def _worker_record(heartbeats_path_str: str, key: str, repeats: int) -> str:
    path = Path(heartbeats_path_str)
    for _ in range(repeats):
        record_monitor_heartbeat(path, key)
    return key


def test_record_monitor_heartbeat_concurrent_processes_keep_all_keys(
    tmp_path: Path,
) -> None:
    heartbeats = tmp_path / "runtime" / "monitor_heartbeats.json"
    heartbeats.parent.mkdir(parents=True, exist_ok=True)
    repeats = 40

    errors: list[BaseException] = []
    with ProcessPoolExecutor(max_workers=len(MONITOR_KEYS)) as pool:
        futures = [
            pool.submit(_worker_record, str(heartbeats), key, repeats)
            for key in MONITOR_KEYS
        ]
        for fut in as_completed(futures):
            try:
                fut.result()
            except BaseException as exc:  # noqa: BLE001 - collect all worker failures
                errors.append(exc)

    assert errors == [], f"concurrent writers failed: {errors!r}"
    assert heartbeats.exists()
    payload = json.loads(heartbeats.read_text(encoding="utf-8"))
    assert set(payload) == set(MONITOR_KEYS)
    leftovers = list(heartbeats.parent.glob("monitor_heartbeats.json.*.tmp"))
    assert leftovers == []


def test_monitor_scripts_heartbeat_wrappers_concurrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate crash-loop / orphan / csv / anomaly / engine-process finishing together."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    heartbeats = runtime / "monitor_heartbeats.json"

    modules = (cec, coo, ccd, cta, cep)
    for mod in modules:
        monkeypatch.setattr(mod, "HEARTBEATS_PATH", heartbeats)
        if hasattr(mod, "RUNTIME_DIR"):
            monkeypatch.setattr(mod, "RUNTIME_DIR", runtime)

    # Thread pool is enough here: wrappers share one interpreter but still
    # exercise the lock + unique-tmp path used by systemd oneshots.
    from concurrent.futures import ThreadPoolExecutor

    def _hammer(mod: object, n: int = 30) -> None:
        for _ in range(n):
            mod._record_monitor_heartbeat()  # type: ignore[attr-defined]

    with ThreadPoolExecutor(max_workers=len(modules)) as pool:
        list(pool.map(_hammer, modules))

    payload = json.loads(heartbeats.read_text(encoding="utf-8"))
    expected = {mod.HEARTBEAT_KEY for mod in modules}
    assert set(payload) == expected
