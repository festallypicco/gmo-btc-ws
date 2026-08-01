"""volatility_5min_range_pct のユニット / CSV 書き込みテスト。"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import trading_engine as te  # noqa: E402


class _FakeWs:
    def __init__(self, mid_price: float) -> None:
        self.latest_snapshot = SimpleNamespace(
            best_bid_price=mid_price - 1.0,
            best_bid_size=0.1,
            best_ask_price=mid_price + 1.0,
            best_ask_size=0.1,
            mid_price=mid_price,
            imbalance=0.5,
            spread_pct=0.0001,
        )
        self.latest_depth_stats = {
            "bid_depth5_size": 1.0,
            "ask_depth5_size": 1.0,
            "depth_imbalance": 0.5,
        }

    def consume_trade_window_stats(self) -> dict:
        return {"trade_count": 0, "buy_volume": 0.0, "sell_volume": 0.0}


def test_volatility_buffer_underfilled_returns_none() -> None:
    logger = te.MarketSnapshotLogger(interval_sec=60)
    assert logger._volatility_5min_range_pct(100.0) is None
    assert logger._volatility_5min_range_pct(101.0) is None
    assert logger._volatility_5min_range_pct(102.0) is None
    assert logger._volatility_5min_range_pct(103.0) is None


def test_volatility_buffer_computes_range_pct_at_five() -> None:
    logger = te.MarketSnapshotLogger(interval_sec=60)
    mids = [100.0, 101.0, 99.0, 102.0, 100.0]
    results = [logger._volatility_5min_range_pct(m) for m in mids]
    assert results[0] is None
    assert results[1] is None
    assert results[2] is None
    assert results[3] is None
    # (max 102 - min 99) / 100 = 0.03
    assert results[4] == pytest.approx(0.03)


def test_volatility_zero_mid_returns_none() -> None:
    logger = te.MarketSnapshotLogger(interval_sec=60)
    for _ in range(4):
        logger._volatility_5min_range_pct(1.0)
    assert logger._volatility_5min_range_pct(0.0) is None


def test_market_snapshot_csv_writes_volatility_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(te, "LOG_DIR", tmp_path)
    logger = te.MarketSnapshotLogger(interval_sec=60)
    mids = [100.0, 101.0, 99.0, 102.0, 100.0]
    for mid in mids:
        logger._next_write_ts = 0.0
        logger.maybe_log(_FakeWs(mid))

    files = list(tmp_path.glob("market_snapshot_*.csv"))
    assert len(files) == 1
    with files[0].open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 5
    assert "volatility_5min_range_pct" in rows[0]
    for row in rows[:4]:
        assert row["volatility_5min_range_pct"] == ""
    assert float(rows[4]["volatility_5min_range_pct"]) == pytest.approx(0.03)
    # 既存列も残っていること
    assert "imbalance" in rows[4]
    assert "spread_pct" in rows[4]
    assert "bid_depth5_size" in rows[4]
