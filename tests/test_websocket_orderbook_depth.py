"""板厚み5階層集計のユニットテスト。"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
MODULE_DIR = ROOT_DIR / "btc_trading_tool"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from websocket_manager import (  # noqa: E402
    WebSocketManager,
    _compute_depth_stats,
    _sum_orderbook_sizes,
)


def test_sum_orderbook_sizes_fewer_than_five_levels() -> None:
    levels = [{"size": "0.1"}, {"size": "0.2"}]
    assert _sum_orderbook_sizes(levels, 5) == pytest.approx(0.3)


def test_sum_orderbook_sizes_ignores_invalid_size() -> None:
    levels = [{"size": "0.1"}, {"size": "bad"}, {"size": None}]
    assert _sum_orderbook_sizes(levels, 5) == pytest.approx(0.1)


def test_compute_depth_stats_ratio() -> None:
    bids = [{"size": "0.3"} for _ in range(5)]
    asks = [{"size": "0.1"} for _ in range(5)]
    stats = _compute_depth_stats(bids, asks, 5)
    assert stats["bid_depth5_size"] == pytest.approx(1.5)
    assert stats["ask_depth5_size"] == pytest.approx(0.5)
    assert stats["depth_imbalance"] == pytest.approx(0.75)


def test_compute_depth_stats_zero_denominator_is_null() -> None:
    stats = _compute_depth_stats([], [], 5)
    assert stats["bid_depth5_size"] == pytest.approx(0.0)
    assert stats["ask_depth5_size"] == pytest.approx(0.0)
    assert stats["depth_imbalance"] is None


def test_live_market_snapshot_writes_depth_columns(tmp_path: Path) -> None:
    """Live WS から1行書き込み、新列が含まれることを確認する。"""
    import trading_engine as te

    te.LOG_DIR = tmp_path
    ws = WebSocketManager()
    ws.start()
    try:
        deadline = time.time() + 25
        while time.time() < deadline:
            if ws.latest_snapshot is not None and ws.latest_depth_stats is not None:
                break
            time.sleep(0.5)
        assert ws.latest_snapshot is not None, "orderbook snapshot not received"
        assert ws.latest_depth_stats is not None, "depth stats not computed"

        logger = te.MarketSnapshotLogger(interval_sec=60)
        logger._next_write_ts = 0.0
        logger.maybe_log(ws)

        files = list(tmp_path.glob("market_snapshot_*.csv"))
        assert len(files) == 1
        with files[0].open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        row = rows[0]
        assert "bid_depth5_size" in row
        assert "ask_depth5_size" in row
        assert "depth_imbalance" in row
        assert float(row["bid_depth5_size"]) > 0
        assert float(row["ask_depth5_size"]) > 0
        assert row["depth_imbalance"] != ""
        assert float(row["depth_imbalance"]) > 0
    finally:
        ws.stop()
