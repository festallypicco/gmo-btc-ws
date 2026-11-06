"""
tests/test_tick_recorder.py

tick 記録（板・約定、2026-11-06）の単体テスト。
- 専用スレッドで CSV へ書く。記録の失敗が呼び出し側へ伝播しない
- 06:00 ロールオーバーで前日分を gzip 化し、35 日で破棄する
- 空きディスク 5GB 未満で自動停止してアラートする
"""

from __future__ import annotations

import gzip
import json
import sys
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

_ROOT_DIR = Path(__file__).resolve().parent.parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import tick_recorder as tick_recorder_module  # noqa: E402
from tick_recorder import TickRecorder  # noqa: E402
from websocket_manager import WebSocketManager  # noqa: E402


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _book_kwargs() -> Dict[str, Any]:
    return dict(
        timestamp_ms=1_730_000_000_000,
        best_bid_price=10_000_000.0,
        best_bid_size=0.3,
        best_ask_price=10_000_100.0,
        best_ask_size=0.2,
        bid_depth5_size=1.5,
        ask_depth5_size=0.5,
        depth_imbalance=0.75,
    )


def test_book_and_trade_rows_written(tmp_path: Path) -> None:
    now = datetime(2026, 11, 6, 12, 0, 0)
    recorder = TickRecorder(
        tmp_path / "tick",
        flush_interval_sec=0.2,
        now_fn=lambda: now,
    )
    recorder.start()
    try:
        recorder.record_book(**_book_kwargs())
        recorder.record_trade(
            timestamp_ms=1_730_000_000_500,
            side="BUY",
            price=10_000_050.0,
            size=0.01,
            exchange_timestamp="2026-11-06T03:00:00.500Z",
        )
        book_path = tmp_path / "tick" / "book_2026-11-06.csv"
        trades_path = tmp_path / "tick" / "trades_2026-11-06.csv"
        assert _wait_for(
            lambda: book_path.exists()
            and len(book_path.read_text(encoding="utf-8").splitlines()) >= 2
        )
        assert _wait_for(
            lambda: trades_path.exists()
            and len(trades_path.read_text(encoding="utf-8").splitlines()) >= 2
        )
    finally:
        recorder.stop()

    book_lines = book_path.read_text(encoding="utf-8").splitlines()
    assert book_lines[0].startswith("timestamp_ms,best_bid_price")
    fields = book_lines[1].split(",")
    assert fields[0] == "1730000000000"
    assert float(fields[1]) == 10_000_000.0
    assert float(fields[5]) == 1.5
    assert float(fields[7]) == 0.75

    trade_lines = trades_path.read_text(encoding="utf-8").splitlines()
    tfields = trade_lines[1].split(",")
    assert tfields[1] == "BUY"
    assert float(tfields[2]) == 10_000_050.0
    assert tfields[4] == "2026-11-06T03:00:00.500Z"


def test_rollover_gzips_previous_day(tmp_path: Path) -> None:
    holder = {"now": datetime(2026, 11, 6, 12, 0, 0)}
    recorder = TickRecorder(
        tmp_path / "tick",
        flush_interval_sec=0.2,
        now_fn=lambda: holder["now"],
    )
    recorder.start()
    try:
        recorder.record_book(**_book_kwargs())
        day1 = tmp_path / "tick" / "book_2026-11-06.csv"
        assert _wait_for(
            lambda: day1.exists()
            and len(day1.read_text(encoding="utf-8").splitlines()) >= 2
        )

        holder["now"] = datetime(2026, 11, 7, 7, 0, 0)
        recorder.record_book(**_book_kwargs())
        gz_path = tmp_path / "tick" / "book_2026-11-06.csv.gz"
        day2 = tmp_path / "tick" / "book_2026-11-07.csv"
        assert _wait_for(lambda: gz_path.exists() and day2.exists())
        assert not day1.exists()
    finally:
        recorder.stop()

    with gzip.open(gz_path, "rt", encoding="utf-8") as f:
        content = f.read()
    assert "timestamp_ms,best_bid_price" in content
    assert "1730000000000" in content


def test_retention_prunes_old_files(tmp_path: Path) -> None:
    tick_dir = tmp_path / "tick"
    tick_dir.mkdir(parents=True)
    old = tick_dir / "book_2026-09-20.csv.gz"
    old.write_bytes(b"old")
    recent = tick_dir / "trades_2026-10-20.csv.gz"
    recent.write_bytes(b"recent")

    now = datetime(2026, 11, 6, 12, 0, 0)
    recorder = TickRecorder(tick_dir, now_fn=lambda: now)
    recorder.start()
    try:
        assert _wait_for(lambda: not old.exists())
        assert recent.exists()
    finally:
        recorder.stop()


def test_low_disk_stops_recording_and_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = namedtuple("usage", ["total", "used", "free"])
    monkeypatch.setattr(
        tick_recorder_module.shutil,
        "disk_usage",
        lambda _path: usage(10**12, 10**12 - 1024, 1024),
    )
    alerts: List[str] = []
    recorder = TickRecorder(
        tmp_path / "tick",
        on_alert=lambda msg: alerts.append(msg),
        flush_interval_sec=0.2,
    )
    recorder.start()
    try:
        assert _wait_for(lambda: recorder.is_disk_stopped)
        assert len(alerts) == 1
        assert "tick recorder stopped" in alerts[0]
        recorder.record_book(**_book_kwargs())  # 例外にならない
    finally:
        recorder.stop()


def test_unwritable_dir_is_fail_silent(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    recorder = TickRecorder(blocker / "tick", flush_interval_sec=0.2)
    recorder.start()
    recorder.record_book(**_book_kwargs())
    recorder.record_trade(
        timestamp_ms=1, side="SELL", price=1.0, size=0.001
    )
    recorder.stop()


class _StubRecorder:
    def __init__(self) -> None:
        self.book_calls: List[Dict[str, Any]] = []
        self.trade_calls: List[Dict[str, Any]] = []

    def record_book(self, **kwargs: Any) -> None:
        self.book_calls.append(kwargs)

    def record_trade(self, **kwargs: Any) -> None:
        self.trade_calls.append(kwargs)


def test_websocket_manager_hooks() -> None:
    stub = _StubRecorder()
    ws = WebSocketManager(tick_recorder=stub)  # type: ignore[arg-type]

    orderbook_msg = json.dumps(
        {
            "channel": "orderbooks",
            "symbol": "BTC",
            "bids": [
                {"price": "10000000", "size": "0.3"},
                {"price": "9999900", "size": "0.3"},
                {"price": "9999800", "size": "0.3"},
                {"price": "9999700", "size": "0.3"},
                {"price": "9999600", "size": "0.3"},
            ],
            "asks": [
                {"price": "10000100", "size": "0.1"},
                {"price": "10000200", "size": "0.1"},
                {"price": "10000300", "size": "0.1"},
                {"price": "10000400", "size": "0.1"},
                {"price": "10000500", "size": "0.1"},
            ],
        }
    )
    ws._on_message(None, orderbook_msg)  # type: ignore[arg-type]
    assert len(stub.book_calls) == 1
    call = stub.book_calls[0]
    assert call["best_bid_price"] == 10_000_000.0
    assert call["best_ask_price"] == 10_000_100.0
    assert call["bid_depth5_size"] == pytest.approx(1.5)
    assert call["ask_depth5_size"] == pytest.approx(0.5)
    assert call["depth_imbalance"] == pytest.approx(0.75)

    trades_msg = json.dumps(
        {
            "channel": "trades",
            "symbol": "BTC",
            "side": "BUY",
            "price": "10000050",
            "size": "0.01",
            "timestamp": "2026-11-06T03:00:00.000Z",
        }
    )
    ws._on_message(None, trades_msg)  # type: ignore[arg-type]
    assert len(stub.trade_calls) == 1
    assert stub.trade_calls[0]["side"] == "BUY"
    assert stub.trade_calls[0]["price"] == 10_000_050.0
