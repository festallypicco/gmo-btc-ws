"""
tests/test_production_data_integrity.py

本番 runtime ファイルが pytest 実行中に書き換えられないことを保証する回帰テスト。
before/after の比較本体はルート conftest.py の session autouse fixture が
全テスト終了後に実行する。

- 本番取引 CSV: 常時ガード (guard_production_trade_csv)
- live_state.db: PYTEST_GUARD_LIVE_STATE=1 かつ engine 停止時のみ厳密ガード

単体実行例:
  pytest tests/test_production_data_integrity.py
  PYTEST_GUARD_LIVE_STATE=1 pytest   # engine 停止後に live_state も監視
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def _production_trade_csv_path() -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    return ROOT_DIR / "log" / f"realtime_trading_log_{today}.csv"


def test_production_guard_paths_are_configured() -> None:
    csv_path = _production_trade_csv_path()
    db_path = ROOT_DIR / "runtime" / "live_state.db"
    assert csv_path.name.startswith("realtime_trading_log_")
    assert db_path.name == "live_state.db"


def test_production_runtime_snapshots_fixture(production_runtime_snapshots) -> None:
    """session 開始時に本番取引 CSV のスナップショットが取得できること。"""
    csv_path = _production_trade_csv_path()
    assert csv_path in production_runtime_snapshots
