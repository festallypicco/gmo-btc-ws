"""
tick_recorder.py
----------------
板（orderbooks）と約定（trades）の tick データを CSV に記録する。

- 記録は専用ライタースレッドが担い、呼び出し側はキューへ積むだけ。
  記録の失敗が取引処理に影響することはない（fail-silent）
- ファイルは log/tick/book_YYYY-MM-DD.csv / trades_YYYY-MM-DD.csv。
  日付は取引日（06:00 境界）で、06:00 のロールオーバー時に前日分を gzip 化する
- 保持は 35 日。空きディスクが 5GB 未満になったら記録を自動停止し、
  アラートコールバックで通知する
"""
from __future__ import annotations

import gzip
import queue
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple

_ROLLOVER_HOUR = 6
_DEFAULT_RETENTION_DAYS = 35
_DEFAULT_MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024
_DEFAULT_FLUSH_INTERVAL_SEC = 5.0
_DEFAULT_FLUSH_MAX_ROWS = 1000
_QUEUE_MAXSIZE = 100_000

_BOOK_HEADER = (
    "timestamp_ms,best_bid_price,best_bid_size,best_ask_price,best_ask_size,"
    "bid_depth5_size,ask_depth5_size,depth_imbalance"
)
_TRADES_HEADER = "timestamp_ms,side,price,size,exchange_timestamp"
_HEADERS = {"book": _BOOK_HEADER, "trades": _TRADES_HEADER}
_PREFIXES = {"book": "book", "trades": "trades"}


def _trading_day_label(now: datetime) -> str:
    """06:00 境界の取引日ラベル（YYYY-MM-DD）。virtual_trader と同じ定義。"""
    current = now
    if current.hour < _ROLLOVER_HOUR:
        current = current - timedelta(days=1)
    return current.date().isoformat()


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return ""
    return repr(float(value))


class TickRecorder:
    """tick データの CSV 記録。record_* は例外を出さない。"""

    def __init__(
        self,
        tick_dir: Path,
        on_alert: Optional[Callable[[str], None]] = None,
        *,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        min_free_bytes: int = _DEFAULT_MIN_FREE_BYTES,
        flush_interval_sec: float = _DEFAULT_FLUSH_INTERVAL_SEC,
        flush_max_rows: int = _DEFAULT_FLUSH_MAX_ROWS,
        now_fn: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._tick_dir = Path(tick_dir)
        self._on_alert = on_alert
        self._retention_days = int(retention_days)
        self._min_free_bytes = int(min_free_bytes)
        self._flush_interval_sec = float(flush_interval_sec)
        self._flush_max_rows = int(flush_max_rows)
        self._now_fn = now_fn
        self._queue: "queue.Queue[Tuple[str, str, str]]" = queue.Queue(
            maxsize=_QUEUE_MAXSIZE
        )
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._disk_stopped = False

    # ------------------------------------------------------------------ #
    #  ライフサイクル                                                       #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """ライタースレッドを開始する（冪等）。"""
        if self._disk_stopped:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="tick-recorder", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None

    @property
    def is_disk_stopped(self) -> bool:
        return self._disk_stopped

    # ------------------------------------------------------------------ #
    #  記録 API（呼び出し側スレッド）                                        #
    # ------------------------------------------------------------------ #

    def record_book(
        self,
        *,
        timestamp_ms: int,
        best_bid_price: float,
        best_bid_size: float,
        best_ask_price: float,
        best_ask_size: float,
        bid_depth5_size: Optional[float],
        ask_depth5_size: Optional[float],
        depth_imbalance: Optional[float],
    ) -> None:
        try:
            line = ",".join(
                [
                    str(int(timestamp_ms)),
                    _fmt(best_bid_price),
                    _fmt(best_bid_size),
                    _fmt(best_ask_price),
                    _fmt(best_ask_size),
                    _fmt(bid_depth5_size),
                    _fmt(ask_depth5_size),
                    _fmt(depth_imbalance),
                ]
            )
            self._enqueue("book", line)
        except Exception:
            pass

    def record_trade(
        self,
        *,
        timestamp_ms: int,
        side: str,
        price: float,
        size: float,
        exchange_timestamp: str = "",
    ) -> None:
        try:
            line = ",".join(
                [
                    str(int(timestamp_ms)),
                    str(side),
                    _fmt(price),
                    _fmt(size),
                    str(exchange_timestamp or ""),
                ]
            )
            self._enqueue("trades", line)
        except Exception:
            pass

    def _enqueue(self, kind: str, line: str) -> None:
        if self._disk_stopped or self._stop_event.is_set():
            return
        day_label = _trading_day_label(self._now_fn())
        try:
            self._queue.put_nowait((kind, day_label, line))
        except queue.Full:
            # 記録側が詰まったら行を捨てる（取引を止めない）
            pass

    # ------------------------------------------------------------------ #
    #  ライタースレッド                                                     #
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        buffers: Dict[str, List[str]] = {"book": [], "trades": []}
        handles: Dict[str, Optional[TextIO]] = {"book": None, "trades": None}
        current_labels: Dict[str, Optional[str]] = {"book": None, "trades": None}
        last_flush = time.monotonic()
        try:
            self._tick_dir.mkdir(parents=True, exist_ok=True)
            self._gzip_stale_files()
            self._prune_old_files()
            if not self._check_disk():
                return
            while not self._stop_event.is_set():
                try:
                    item = self._queue.get(timeout=0.2)
                    pending = [item]
                    while True:
                        try:
                            pending.append(self._queue.get_nowait())
                        except queue.Empty:
                            break
                except queue.Empty:
                    pending = []

                for kind, day_label, line in pending:
                    if current_labels[kind] != day_label:
                        self._flush_kind(kind, buffers, handles)
                        self._close_handle(kind, handles)
                        if current_labels[kind] is not None:
                            self._gzip_file(kind, current_labels[kind])
                            self._prune_old_files()
                        handles[kind] = self._open_file(kind, day_label)
                        current_labels[kind] = day_label
                    buffers[kind].append(line)

                buffered = sum(len(v) for v in buffers.values())
                now_mono = time.monotonic()
                if buffered >= self._flush_max_rows or (
                    buffered > 0
                    and now_mono - last_flush >= self._flush_interval_sec
                ):
                    for kind in buffers:
                        self._flush_kind(kind, buffers, handles)
                    last_flush = now_mono
                    if not self._check_disk():
                        break

            # 終了時: 残りを書き出して閉じる（gzip はロールオーバー時のみ）
            for kind in buffers:
                self._flush_kind(kind, buffers, handles)
        except Exception as exc:
            print(f"[TickRecorder] writer error: {exc}")
        finally:
            for kind in handles:
                self._close_handle(kind, handles)

    def _open_file(self, kind: str, day_label: str) -> Optional[TextIO]:
        path = self._tick_dir / f"{_PREFIXES[kind]}_{day_label}.csv"
        try:
            need_header = not path.exists() or path.stat().st_size == 0
            handle = path.open("a", encoding="utf-8", newline="")
            if need_header:
                handle.write(_HEADERS[kind] + "\n")
                handle.flush()
            return handle
        except Exception as exc:
            print(f"[TickRecorder] open failed: {path}: {exc}")
            return None

    def _flush_kind(
        self,
        kind: str,
        buffers: Dict[str, List[str]],
        handles: Dict[str, Optional[TextIO]],
    ) -> None:
        rows = buffers[kind]
        if not rows:
            return
        buffers[kind] = []
        handle = handles[kind]
        if handle is None:
            return
        try:
            handle.write("\n".join(rows) + "\n")
            handle.flush()
        except Exception as exc:
            print(f"[TickRecorder] write failed ({kind}): {exc}")

    @staticmethod
    def _close_handle(
        kind: str, handles: Dict[str, Optional[TextIO]]
    ) -> None:
        handle = handles[kind]
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
            handles[kind] = None

    def _gzip_file(self, kind: str, day_label: str) -> None:
        src = self._tick_dir / f"{_PREFIXES[kind]}_{day_label}.csv"
        if not src.exists():
            return
        dst = src.with_suffix(".csv.gz")
        try:
            with src.open("rb") as f_in, gzip.open(dst, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            src.unlink()
        except Exception as exc:
            print(f"[TickRecorder] gzip failed: {src}: {exc}")

    def _gzip_stale_files(self) -> None:
        """再起動時: 取引日が過ぎた未圧縮 CSV を gzip 化する。"""
        today_label = _trading_day_label(self._now_fn())
        try:
            candidates = list(self._tick_dir.glob("*.csv"))
        except Exception:
            return
        for path in candidates:
            label = self._label_from_name(path.name)
            if label is not None and label != today_label:
                kind = "book" if path.name.startswith("book_") else "trades"
                self._gzip_file(kind, label)

    def _prune_old_files(self) -> None:
        """保持日数を超えた tick ファイルを削除する。"""
        try:
            today = self._now_fn().date()
            if self._now_fn().hour < _ROLLOVER_HOUR:
                today = today - timedelta(days=1)
            cutoff = today - timedelta(days=self._retention_days)
            for path in self._tick_dir.iterdir():
                label = self._label_from_name(path.name)
                if label is None:
                    continue
                try:
                    file_day = datetime.strptime(label, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if file_day < cutoff:
                    path.unlink()
        except Exception as exc:
            print(f"[TickRecorder] prune failed: {exc}")

    @staticmethod
    def _label_from_name(name: str) -> Optional[str]:
        for prefix in ("book_", "trades_"):
            if name.startswith(prefix):
                rest = name[len(prefix):]
                for suffix in (".csv.gz", ".csv"):
                    if rest.endswith(suffix):
                        return rest[: -len(suffix)]
                return None
        return None

    def _check_disk(self) -> bool:
        """空き容量が下限未満なら記録を停止し、アラートして False を返す。"""
        try:
            free = shutil.disk_usage(self._tick_dir).free
        except Exception:
            return True
        if free >= self._min_free_bytes:
            return True
        self._disk_stopped = True
        message = (
            "[ALERT] tick recorder stopped: low disk space\n"
            f"free_bytes={free}\n"
            f"min_free_bytes={self._min_free_bytes}\n"
            f"tick_dir={self._tick_dir}"
        )
        print(message)
        if self._on_alert is not None:
            try:
                self._on_alert(message)
            except Exception as exc:
                print(f"[TickRecorder] alert failed: {exc}")
        return False
