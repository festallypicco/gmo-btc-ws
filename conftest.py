"""
pytest 共通設定。

本番取引 CSV がテスト実行中に書き換えられていないことを session 終了時に検証する。
live_state.db も監視対象に含められるが、trading_engine 稼働中は外部更新で
誤検知するため、環境変数 PYTEST_GUARD_LIVE_STATE=1 のときのみ厳密チェックする。
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

ROOT_DIR = Path(__file__).resolve().parent

FileSnapshot = Tuple[int, str]  # (mtime_ns, sha256_hex)


def production_trade_csv_path() -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    return ROOT_DIR / "log" / f"realtime_trading_log_{today}.csv"


def production_live_state_db_path() -> Path:
    return ROOT_DIR / "runtime" / "live_state.db"


def production_guard_paths() -> list[Path]:
    paths = [production_trade_csv_path()]
    if os.environ.get("PYTEST_GUARD_LIVE_STATE", "").strip() == "1":
        paths.append(production_live_state_db_path())
    return paths


def _snapshot_file(path: Path) -> Optional[FileSnapshot]:
    if not path.exists():
        return None
    data = path.read_bytes()
    return path.stat().st_mtime_ns, hashlib.sha256(data).hexdigest()


def _assert_snapshots_unchanged(
    before: Dict[Path, Optional[FileSnapshot]],
    *,
    label: str,
) -> None:
    for path, snap_before in before.items():
        snap_after = _snapshot_file(path)
        if snap_before is None and snap_after is None:
            continue
        if snap_before is None or snap_after is None:
            raise AssertionError(
                f"{label}: production file presence changed during test session: {path}"
            )
        before_mtime, before_hash = snap_before
        after_mtime, after_hash = snap_after
        if before_hash != after_hash:
            raise AssertionError(
                f"{label}: production file content changed during test session: {path} "
                f"(sha256 {before_hash[:12]}.. -> {after_hash[:12]}..)"
            )
        if before_mtime != after_mtime:
            raise AssertionError(
                f"{label}: production file mtime changed during test session: {path}"
            )


@pytest.fixture(scope="session")
def production_runtime_snapshots() -> Dict[Path, Optional[FileSnapshot]]:
    """session 開始時点の本番ファイルスナップショット（trade CSV は常に、DB は任意）。"""
    return {path: _snapshot_file(path) for path in production_guard_paths()}


@pytest.fixture(scope="session", autouse=True)
def guard_production_trade_csv(production_runtime_snapshots: Dict[Path, Optional[FileSnapshot]]) -> None:
    """全テスト終了後、本番取引 CSV が変化していないことを検証する（常時有効）。"""
    csv_path = production_trade_csv_path()
    before = {csv_path: production_runtime_snapshots.get(csv_path)}
    yield
    _assert_snapshots_unchanged(before, label="trade_csv_guard")


@pytest.fixture(scope="session", autouse=True)
def guard_production_live_state_db(production_runtime_snapshots: Dict[Path, Optional[FileSnapshot]]) -> None:
    """
    PYTEST_GUARD_LIVE_STATE=1 のときのみ live_state.db の不変を検証する。
    trading_engine 稼働中は DB が外部更新されるため、通常の pytest では無効。
    """
    if os.environ.get("PYTEST_GUARD_LIVE_STATE", "").strip() != "1":
        yield
        return
    db_path = production_live_state_db_path()
    before = {db_path: production_runtime_snapshots.get(db_path)}
    yield
    _assert_snapshots_unchanged(before, label="live_state_guard")
