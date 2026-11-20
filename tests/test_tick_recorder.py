"""
tests/test_tick_recorder.py

tick 記録（板・約定、2026-11-06）の単体テスト。
- 専用スレッドで CSV へ書く。記録の失敗が呼び出し側へ伝播しない
- 06:00 ロールオーバーで前日分を gzip 化し、35 日で破棄する
- 空きディスク 5GB 未満で自動停止してアラートする

レバレッジ市場（BTC_JPY）記録（2026-11-20）のテストも含む。
- file_prefix="_lev" で book_lev_ / trades_lev_ に記録する
- 同じ tick_dir を共有しても gzip / 保持削除は市場間で干渉しない
- BTC_JPY のメッセージは記録専用経路に振り分けられ、現物の
  スナップショット・売買コールバック・約定バッファに混入しない
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
import websocket_manager as websocket_manager_module  # noqa: E402
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


# ---------------------------------------------------------------------- #
#  レバレッジ市場（BTC_JPY）記録                                          #
# ---------------------------------------------------------------------- #


def test_lev_recorder_writes_prefixed_files(tmp_path: Path) -> None:
    now = datetime(2026, 11, 20, 12, 0, 0)
    recorder = TickRecorder(
        tmp_path / "tick",
        file_prefix="_lev",
        flush_interval_sec=0.2,
        now_fn=lambda: now,
    )
    recorder.start()
    try:
        recorder.record_book(**_book_kwargs())
        recorder.record_trade(
            timestamp_ms=1_730_000_000_500,
            side="SELL",
            price=10_000_050.0,
            size=0.02,
            exchange_timestamp="2026-11-20T03:00:00.500Z",
        )
        book_path = tmp_path / "tick" / "book_lev_2026-11-20.csv"
        trades_path = tmp_path / "tick" / "trades_lev_2026-11-20.csv"
        assert _wait_for(
            lambda: book_path.exists()
            and len(book_path.read_text(encoding="utf-8").splitlines()) >= 2
        )
        assert _wait_for(
            lambda: trades_path.exists()
            and len(trades_path.read_text(encoding="utf-8").splitlines()) >= 2
        )
        # 現物側のファイル名では作られない
        assert not (tmp_path / "tick" / "book_2026-11-20.csv").exists()
        assert not (tmp_path / "tick" / "trades_2026-11-20.csv").exists()
    finally:
        recorder.stop()

    book_lines = book_path.read_text(encoding="utf-8").splitlines()
    assert book_lines[0].startswith("timestamp_ms,best_bid_price")
    trade_lines = trades_path.read_text(encoding="utf-8").splitlines()
    assert trade_lines[0] == "timestamp_ms,side,price,size,exchange_timestamp"
    assert trade_lines[1].split(",")[1] == "SELL"


def test_recorders_do_not_touch_each_others_files(tmp_path: Path) -> None:
    tick_dir = tmp_path / "tick"
    tick_dir.mkdir(parents=True)
    # 前取引日の未圧縮ファイル（再起動時に gzip される対象）と保持切れファイル
    spot_stale = tick_dir / "book_2026-11-05.csv"
    spot_stale.write_text("spot stale\n", encoding="utf-8")
    lev_stale = tick_dir / "book_lev_2026-11-05.csv"
    lev_stale.write_text("lev stale\n", encoding="utf-8")
    spot_trades_stale = tick_dir / "trades_2026-11-05.csv"
    spot_trades_stale.write_text("spot trades stale\n", encoding="utf-8")
    lev_trades_stale = tick_dir / "trades_lev_2026-11-05.csv"
    lev_trades_stale.write_text("lev trades stale\n", encoding="utf-8")
    spot_old = tick_dir / "book_2026-09-01.csv.gz"
    spot_old.write_bytes(b"old")
    lev_old = tick_dir / "book_lev_2026-09-01.csv.gz"
    lev_old.write_bytes(b"old")
    spot_trades_old = tick_dir / "trades_2026-09-01.csv.gz"
    spot_trades_old.write_bytes(b"old")
    lev_trades_old = tick_dir / "trades_lev_2026-09-01.csv.gz"
    lev_trades_old.write_bytes(b"old")

    now = datetime(2026, 11, 6, 12, 0, 0)
    spot = TickRecorder(tick_dir, now_fn=lambda: now)
    spot.start()
    try:
        # 現物レコーダーは自分のファイルだけを gzip / 削除する
        assert _wait_for(
            lambda: (tick_dir / "book_2026-11-05.csv.gz").exists()
            and (tick_dir / "trades_2026-11-05.csv.gz").exists()
            and not spot_old.exists()
            and not spot_trades_old.exists()
        )
        assert lev_stale.exists()
        assert lev_trades_stale.exists()
        assert not (tick_dir / "book_lev_2026-11-05.csv.gz").exists()
        assert not (tick_dir / "trades_lev_2026-11-05.csv.gz").exists()
        assert lev_old.exists()
        assert lev_trades_old.exists()
    finally:
        spot.stop()

    lev = TickRecorder(tick_dir, file_prefix="_lev", now_fn=lambda: now)
    lev.start()
    try:
        assert _wait_for(
            lambda: (tick_dir / "book_lev_2026-11-05.csv.gz").exists()
            and (tick_dir / "trades_lev_2026-11-05.csv.gz").exists()
            and not lev_old.exists()
            and not lev_trades_old.exists()
        )
    finally:
        lev.stop()


def test_recorders_run_simultaneously(tmp_path: Path) -> None:
    # 本番と同じく両レコーダーを同時起動しても互いに干渉しないこと
    tick_dir = tmp_path / "tick"
    tick_dir.mkdir(parents=True)
    spot_stale = tick_dir / "book_2026-11-05.csv"
    spot_stale.write_text("spot stale\n", encoding="utf-8")
    lev_stale = tick_dir / "book_lev_2026-11-05.csv"
    lev_stale.write_text("lev stale\n", encoding="utf-8")

    now = datetime(2026, 11, 6, 12, 0, 0)
    spot = TickRecorder(tick_dir, flush_interval_sec=0.2, now_fn=lambda: now)
    lev = TickRecorder(
        tick_dir, file_prefix="_lev", flush_interval_sec=0.2, now_fn=lambda: now
    )
    spot.start()
    lev.start()
    try:
        spot.record_book(**_book_kwargs())
        lev.record_book(**_book_kwargs())
        spot_path = tick_dir / "book_2026-11-06.csv"
        lev_path = tick_dir / "book_lev_2026-11-06.csv"
        assert _wait_for(
            lambda: spot_path.exists()
            and lev_path.exists()
            and len(spot_path.read_text(encoding="utf-8").splitlines()) >= 2
            and len(lev_path.read_text(encoding="utf-8").splitlines()) >= 2
        )
        assert _wait_for(
            lambda: (tick_dir / "book_2026-11-05.csv.gz").exists()
            and (tick_dir / "book_lev_2026-11-05.csv.gz").exists()
        )
    finally:
        spot.stop()
        lev.stop()


def _orderbook_json(symbol: str, base_price: int) -> str:
    bids = [
        {"price": str(base_price - i * 100), "size": "0.3"} for i in range(5)
    ]
    asks = [
        {"price": str(base_price + 100 + i * 100), "size": "0.1"}
        for i in range(5)
    ]
    return json.dumps(
        {
            "channel": "orderbooks",
            "symbol": symbol,
            "bids": bids,
            "asks": asks,
        }
    )


def test_websocket_manager_leverage_routing() -> None:
    spot_stub = _StubRecorder()
    lev_stub = _StubRecorder()
    snapshots: List[Any] = []
    ws = WebSocketManager(
        on_snapshot_callback=lambda snap: snapshots.append(snap),
        tick_recorder=spot_stub,  # type: ignore[arg-type]
        tick_recorder_lev=lev_stub,  # type: ignore[arg-type]
    )

    # レバレッジ板: レバレッジレコーダーだけに届く
    ws._on_message(None, _orderbook_json("BTC_JPY", 11_000_000))  # type: ignore[arg-type]
    assert len(lev_stub.book_calls) == 1
    lev_call = lev_stub.book_calls[0]
    assert lev_call["best_bid_price"] == 11_000_000.0
    assert lev_call["bid_depth5_size"] == pytest.approx(1.5)
    assert lev_call["depth_imbalance"] == pytest.approx(0.75)
    assert spot_stub.book_calls == []
    assert snapshots == []
    assert ws.latest_snapshot is None

    # レバレッジ約定: 現物の約定バッファにも入らない
    lev_trades_msg = json.dumps(
        {
            "channel": "trades",
            "symbol": "BTC_JPY",
            "side": "SELL",
            "price": "11000050",
            "size": "0.02",
            "timestamp": "2026-11-20T03:00:00.000Z",
        }
    )
    ws._on_message(None, lev_trades_msg)  # type: ignore[arg-type]
    assert len(lev_stub.trade_calls) == 1
    assert lev_stub.trade_calls[0]["side"] == "SELL"
    assert spot_stub.trade_calls == []
    assert ws.consume_trade_window_stats()["trade_count"] == 0

    # 現物の板は従来どおり現物経路に届く
    ws._on_message(None, _orderbook_json("BTC", 10_000_000))  # type: ignore[arg-type]
    assert len(spot_stub.book_calls) == 1
    assert len(snapshots) == 1
    assert ws.latest_snapshot is not None


class _FakeWsApp:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send(self, message: str) -> None:
        self.sent.append(message)


def test_websocket_manager_leverage_subscribe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _on_open の subscribe 間隔 sleep を記録に差し替える
    sleeps: List[float] = []
    monkeypatch.setattr(
        websocket_manager_module.time,
        "sleep",
        lambda sec, *_a, **_k: sleeps.append(sec),
    )

    fake = _FakeWsApp()
    ws = WebSocketManager(tick_recorder_lev=_StubRecorder())  # type: ignore[arg-type]
    ws._on_open(fake)  # type: ignore[arg-type]
    sent = [json.loads(m) for m in fake.sent]
    assert [(m["channel"], m["symbol"]) for m in sent] == [
        ("orderbooks", "BTC"),
        ("trades", "BTC"),
        ("orderbooks", "BTC_JPY"),
        ("trades", "BTC_JPY"),
    ]
    # ERR-5003 回避のため、各 subscribe の間に 2 秒空ける前提を固定する
    assert sleeps == [2.0, 2.0, 2.0]

    # レバレッジレコーダーがなければ BTC_JPY は購読しない
    sleeps.clear()
    fake2 = _FakeWsApp()
    ws2 = WebSocketManager()
    ws2._on_open(fake2)  # type: ignore[arg-type]
    sent2 = [json.loads(m) for m in fake2.sent]
    assert [(m["channel"], m["symbol"]) for m in sent2] == [
        ("orderbooks", "BTC"),
        ("trades", "BTC"),
    ]
    assert sleeps == [2.0]


class _ExplodingRecorder:
    """記録のたびに例外を投げるレコーダー。fail-silent の検証用。"""

    def record_book(self, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    def record_trade(self, **kwargs: Any) -> None:
        raise RuntimeError("boom")


def test_websocket_manager_leverage_fail_silent() -> None:
    spot_stub = _StubRecorder()
    snapshots: List[Any] = []
    ws = WebSocketManager(
        on_snapshot_callback=lambda snap: snapshots.append(snap),
        tick_recorder=spot_stub,  # type: ignore[arg-type]
        tick_recorder_lev=_ExplodingRecorder(),  # type: ignore[arg-type]
    )

    # レコーダーの例外が _on_message の呼び出し元へ伝播しないこと
    ws._on_message(None, _orderbook_json("BTC_JPY", 11_000_000))  # type: ignore[arg-type]
    lev_trades_msg = json.dumps(
        {
            "channel": "trades",
            "symbol": "BTC_JPY",
            "side": "BUY",
            "price": "11000050",
            "size": "0.01",
            "timestamp": "2026-11-20T03:00:00.000Z",
        }
    )
    ws._on_message(None, lev_trades_msg)  # type: ignore[arg-type]
    # 壊れたレバレッジ板（数値化できない価格）でも例外にならないこと
    broken_msg = json.dumps(
        {
            "channel": "orderbooks",
            "symbol": "BTC_JPY",
            "bids": [{"price": "not-a-number", "size": "0.3"}],
            "asks": [{"price": "11000100", "size": "0.1"}],
        }
    )
    ws._on_message(None, broken_msg)  # type: ignore[arg-type]

    # 後続の現物メッセージが通常どおり処理されること
    ws._on_message(None, _orderbook_json("BTC", 10_000_000))  # type: ignore[arg-type]
    assert len(spot_stub.book_calls) == 1
    assert len(snapshots) == 1


def test_build_ws_managers_passes_leverage_recorder() -> None:
    if str(_ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(_ROOT_DIR))
    import trading_engine

    class _TraderStub:
        def on_orderbook_update(self, snap: Any) -> None:
            pass

        def on_exchange_status(self, status: str, detail: str) -> None:
            pass

    spot_stub = _StubRecorder()
    lev_stub = _StubRecorder()
    ws_manager, private_ws = trading_engine._build_ws_managers(
        _TraderStub(),  # type: ignore[arg-type]
        "paper",
        tick_recorder=spot_stub,  # type: ignore[arg-type]
        tick_recorder_lev=lev_stub,  # type: ignore[arg-type]
    )
    assert private_ws is None
    assert ws_manager._tick_recorder is spot_stub
    assert ws_manager._tick_recorder_lev is lev_stub
