"""
trading_engine.py
-----------------
売買エンジン本体（Streamlit から独立実行）。

- StrategyConfig を config.json から読み込み（config_manager 経由）
- WebSocketManager + VirtualTrader を起動
- 1秒ごとに live_state.db を更新
- 60秒ごとに log/market_snapshot_YYYY-MM-DD.csv を記録
- SIGINT/SIGTERM で安全停止
"""
from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import csv
import json
import atexit
import os
import platform
import signal
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional, TextIO

ROOT_DIR = Path(__file__).resolve().parent
MODULE_DIR = ROOT_DIR / "btc_trading_tool"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from config_manager import (  # noqa: E402
    ConfigValidationError,
    DEFAULT_CONFIG_VERSION,
    DEFAULT_ORDER_RATE_LIMIT_PER_MINUTE,
    DEFAULT_RECONCILIATION_INTERVAL_MINUTES,
    DEFAULT_RECONCILIATION_TOLERANCE_BTC,
    DEFAULT_RECONCILIATION_TOLERANCE_JPY,
    DEFAULT_TRADING_MODE,
    apply_engine_safety_defaults,
    build_profile_definitions,
    default_config_payload,
    load_config_payload,
    payload_to_history_snapshot,
)
from profile_config import validate_profiles  # noqa: E402
from portfolio_metrics import compute_total_assets  # noqa: E402
from virtual_trader import (  # noqa: E402
    VirtualTrader,
    run_reconciliation_check,
)
from websocket_manager import PrivateWebSocketManager, WebSocketManager  # noqa: E402

SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from telegram_notifier import send_telegram_message  # noqa: E402

PID_PATH = ROOT_DIR / "runtime" / "trading_engine.pid"
LIVE_STATE_DB_PATH = ROOT_DIR / "runtime" / "live_state.db"
GMO_TRADE_KEY_LOCK_PATH = ROOT_DIR / "runtime" / "gmo_trade_key.lock"
LOG_DIR = ROOT_DIR / "log"
CONFIG_DIR = ROOT_DIR / "config"
CONFIG_PATH = CONFIG_DIR / "config.json"
CONFIG_CACHE_PATH = CONFIG_DIR / "last_loaded_config.json"
CONFIG_HISTORY_PATH = LOG_DIR / "config_history.jsonl"
MANUAL_STOP_FLAG_PATH = ROOT_DIR / "runtime" / "manual_stop.flag"
MANUAL_STOP_REASON_PATH = ROOT_DIR / "runtime" / "manual_stop_reason.json"
MANUAL_STOP_PAUSE_POLL_SEC = 5
MANUAL_STOP_STILL_PAUSED_NOTIFY_SEC = 24 * 3600

_order_event_timestamps: list[float] = []
_order_rate_lock = threading.Lock()
_gmo_trade_key_lock_held = False
_gmo_trade_key_lock_fd: Optional[TextIO] = None


def _read_gmo_trade_key_lock(
    lock_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    path = GMO_TRADE_KEY_LOCK_PATH if lock_path is None else lock_path
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _write_gmo_trade_key_lock(
    *,
    pid: int,
    started_at: str,
    lock_path: Optional[Path] = None,
) -> None:
    """診断用メタデータをロックファイルへ書き込む（排他判定には使わない）。"""
    path = GMO_TRADE_KEY_LOCK_PATH if lock_path is None else lock_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": int(pid), "started_at": started_at}
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def _write_gmo_trade_key_lock_fd(
    fd: TextIO,
    *,
    pid: int,
    started_at: str,
) -> None:
    """保持中 FD 経由で診断用メタデータを書き込む（inode を差し替えない）。"""
    payload = {"pid": int(pid), "started_at": started_at}
    fd.seek(0)
    fd.truncate()
    fd.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    fd.flush()


def release_gmo_trade_key_lock(
    lock_path: Optional[Path] = None,
) -> None:
    """自プロセスが保持する trade key ロックのみ解放する。"""
    global _gmo_trade_key_lock_held, _gmo_trade_key_lock_fd
    path = GMO_TRADE_KEY_LOCK_PATH if lock_path is None else lock_path
    fd = _gmo_trade_key_lock_fd
    if fd is None:
        _gmo_trade_key_lock_held = False
        return
    try:
        import fcntl

        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fd.close()
    except OSError as exc:
        print(f"[Engine] gmo_trade_key.lock FD close failed: {exc}")
    _gmo_trade_key_lock_fd = None
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        print(f"[Engine] gmo_trade_key.lock 削除失敗: {exc}")
        _gmo_trade_key_lock_held = False
        return
    _gmo_trade_key_lock_held = False


def acquire_gmo_trade_key_lock(
    lock_path: Optional[Path] = None,
    *,
    on_blocked: Optional[Callable[[int, Dict[str, Any]], None]] = None,
) -> bool:
    """
    real mode 用 GMO TRADE キー排他ロックを取得する。
    成功: True / 他プロセス（または同一プロセスの別 FD）が flock 保持中: False。
    排他は fcntl.flock(LOCK_EX | LOCK_NB)。ファイル内容は診断用のみ。
    """
    global _gmo_trade_key_lock_held, _gmo_trade_key_lock_fd
    import fcntl

    path = GMO_TRADE_KEY_LOCK_PATH if lock_path is None else lock_path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        existing = _read_gmo_trade_key_lock(path) or {}
        try:
            existing_pid = int(existing.get("pid", -1))
        except (TypeError, ValueError):
            existing_pid = -1
        try:
            fd.close()
        except OSError:
            pass
        if on_blocked is not None:
            on_blocked(existing_pid, existing)
        return False
    except OSError as exc:
        try:
            fd.close()
        except OSError:
            pass
        print(f"[Engine] gmo_trade_key.lock flock failed: {exc}")
        if on_blocked is not None:
            on_blocked(-1, {})
        return False

    started_at = datetime.now().isoformat(timespec="seconds")
    try:
        _write_gmo_trade_key_lock_fd(
            fd,
            pid=os.getpid(),
            started_at=started_at,
        )
    except OSError as exc:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fd.close()
        except OSError:
            pass
        print(f"[Engine] gmo_trade_key.lock write failed: {exc}")
        if on_blocked is not None:
            on_blocked(-1, {})
        return False

    _gmo_trade_key_lock_fd = fd
    _gmo_trade_key_lock_held = True
    atexit.register(release_gmo_trade_key_lock)
    return True


def _reject_real_mode_on_windows_or_exit() -> None:
    """Windows ネイティブでの real mode 起動を拒否する。"""
    if platform.system() != "Windows":
        return
    message = "\n".join(
        [
            "[ALERT] real mode start blocked on Windows native",
            "detail=real mode is Docker/Linux only",
            "detail=Windows native start was attempted",
            f"platform={platform.system()}",
        ]
    )
    print(f"[Engine] {message}", file=sys.stderr)
    try:
        send_telegram_message(message)
    except Exception as exc:
        print(f"[Engine] telegram notify failed: {exc}", file=sys.stderr)
    raise SystemExit(1)


def _acquire_gmo_trade_key_lock_or_exit() -> None:
    """real mode 起動時: ロック取得失敗なら Telegram 通知のうえ終了。"""

    def _on_blocked(existing_pid: int, _existing: Dict[str, Any]) -> None:
        message = "\n".join(
            [
                "[ALERT] real mode duplicate start blocked",
                "detail=another process already holds GMO TRADE key lock",
                f"existing_pid={existing_pid}",
                f"lock_path={GMO_TRADE_KEY_LOCK_PATH}",
            ]
        )
        print(f"[Engine] {message}", file=sys.stderr)
        try:
            send_telegram_message(message)
        except Exception as exc:
            print(f"[Engine] telegram notify failed: {exc}", file=sys.stderr)

    if acquire_gmo_trade_key_lock(on_blocked=_on_blocked):
        print(
            f"[Engine] acquired gmo_trade_key.lock"
            f" pid={os.getpid()} path={GMO_TRADE_KEY_LOCK_PATH}"
        )
        return
    raise SystemExit(1)


def record_order_event() -> None:
    now = time.time()
    with _order_rate_lock:
        _order_event_timestamps.append(now)
        cutoff = now - 60.0
        while _order_event_timestamps and _order_event_timestamps[0] < cutoff:
            _order_event_timestamps.pop(0)


def check_order_rate_limit(order_rate_limit_per_minute: int) -> bool:
    """
    直近60秒の発注回数が上限を超えていたら True を返す。
    呼び出し元は True の場合に発注をスキップし、緊急停止を発動する。
    """
    now = time.time()
    cutoff = now - 60.0
    with _order_rate_lock:
        recent_count = sum(1 for ts in _order_event_timestamps if ts >= cutoff)
    return recent_count > order_rate_limit_per_minute


def _create_manual_stop_flag() -> None:
    MANUAL_STOP_FLAG_PATH.write_text(
        datetime.now().isoformat(timespec="seconds"),
        encoding="utf-8",
    )


def _format_safety_stop_message(reason: str, details: Dict[str, Any], triggered_at: str) -> str:
    lines = [
        "[ALERT] circuit breaker safety stop",
        f"reason={reason}",
        f"triggered_at={triggered_at}",
    ]
    for key in sorted(details.keys()):
        lines.append(f"{key}={details[key]}")
    lines.append("manual_stop.flag created")
    return "\n".join(lines)


def _trigger_safety_stop(reason: str, details: Optional[Dict[str, Any]] = None) -> None:
    """
    自動サーキットブレーカー共通経路。
    既に manual_stop.flag がある場合はフラグ作成も通知も行わない。
    通知失敗でもフラグ作成・緊急停止は継続する。
    """
    if MANUAL_STOP_FLAG_PATH.exists():
        return

    detail_map: Dict[str, Any] = dict(details or {})
    triggered_at = datetime.now().isoformat(timespec="seconds")
    _create_manual_stop_flag()
    try:
        MANUAL_STOP_REASON_PATH.write_text(
            json.dumps(
                {
                    "reason": reason,
                    "details": detail_map,
                    "triggered_at": triggered_at,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[WARNING] manual_stop_reason.json write failed (safety stop continues): {exc}")
    message = _format_safety_stop_message(reason, detail_map, triggered_at)
    print(f"[WARNING] {message}")
    try:
        send_telegram_message(message)
    except Exception as exc:
        print(f"[WARNING] Telegram notify failed (safety stop continues): {exc}")


def _notify_still_paused() -> None:
    """PAUSED 待機が継続していることを Telegram へ再通知する。"""
    reason = "unknown"
    detail_map: Dict[str, Any] = {}
    triggered_at = ""
    if MANUAL_STOP_REASON_PATH.exists():
        try:
            doc = json.loads(MANUAL_STOP_REASON_PATH.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                reason = str(doc.get("reason", "unknown"))
                triggered_at = str(doc.get("triggered_at", ""))
                details = doc.get("details")
                if isinstance(details, dict):
                    detail_map = dict(details)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    notified_at = datetime.now().isoformat(timespec="seconds")
    lines = [
        "[ALERT] circuit breaker safety stop",
        "detail=still in emergency stop (PAUSED)",
        f"reason={reason}",
        f"triggered_at={triggered_at}",
        f"notified_at={notified_at}",
    ]
    for key in sorted(detail_map.keys()):
        lines.append(f"{key}={detail_map[key]}")
    message = "\n".join(lines)
    print(f"[WARNING] {message}")
    try:
        send_telegram_message(message)
    except Exception as exc:
        print(f"[WARNING] Telegram notify failed (still paused continues): {exc}")


def _build_ws_managers(
    trader: VirtualTrader,
    trading_mode: str,
) -> tuple[WebSocketManager, Optional[PrivateWebSocketManager]]:
    """起動時 / PAUSED 再開時に使う WebSocket マネージャを生成する。"""
    ws_manager = WebSocketManager(
        on_snapshot_callback=trader.on_orderbook_update,
        on_exchange_status_callback=trader.on_exchange_status,
    )
    private_ws_manager: Optional[PrivateWebSocketManager] = None
    if trading_mode == "real":
        private_ws_manager = PrivateWebSocketManager(
            on_execution_callback=trader.on_execution_event,
            on_order_callback=lambda evt: print(f"[Engine] private order event: {evt}"),
        )
    return ws_manager, private_ws_manager


def _safety_settings_from_payload(payload: Dict[str, Any]) -> Dict[str, float]:
    normalized = apply_engine_safety_defaults(payload)
    return {
        "order_rate_limit_per_minute": int(normalized["order_rate_limit_per_minute"]),
        "reconciliation_interval_minutes": int(normalized["reconciliation_interval_minutes"]),
        "reconciliation_tolerance_btc": float(normalized["reconciliation_tolerance_btc"]),
        "reconciliation_tolerance_jpy": float(normalized["reconciliation_tolerance_jpy"]),
        "daily_loss_limit_pct": float(normalized["daily_loss_limit_pct"]),
        "initial_jpy": float(normalized["initial_jpy"]),
    }


def _profile_map(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    profiles = payload.get("profiles", [])
    out: Dict[str, Dict[str, Any]] = {}
    for p in profiles:
        if isinstance(p, dict) and isinstance(p.get("name"), str):
            out[p["name"]] = p
    return out


def _diff_profile_fields(old_profile: Dict[str, Any], new_profile: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    changed: Dict[str, Dict[str, Any]] = {}
    keys = set(old_profile.keys()) | set(new_profile.keys())
    for key in sorted(keys):
        if key == "name":
            continue
        old_val = old_profile.get(key)
        new_val = new_profile.get(key)
        if old_val != new_val:
            changed[key] = {"old": old_val, "new": new_val}
    return changed


def _diff_profile_payloads(
    old_payload: Dict[str, Any],
    new_payload: Dict[str, Any],
) -> Dict[str, Any]:
    old_map = _profile_map(old_payload)
    new_map = _profile_map(new_payload)

    old_names = set(old_map.keys())
    new_names = set(new_map.keys())

    added_profiles = sorted(list(new_names - old_names))
    removed_profiles = sorted(list(old_names - new_names))

    changed_profiles: Dict[str, Any] = {}
    for name in sorted(old_names & new_names):
        changed_fields = _diff_profile_fields(old_map[name], new_map[name])
        if changed_fields:
            changed_profiles[name] = {"changed_fields": changed_fields}

    return {
        "changed_profiles": changed_profiles,
        "added_profiles": added_profiles,
        "removed_profiles": removed_profiles,
    }


def _log_config_history_if_changed(payload: Dict[str, Any], migration_logged: bool = False) -> None:
    previous_payload: Optional[Dict[str, Any]] = None
    if CONFIG_CACHE_PATH.exists():
        try:
            previous_payload = json.loads(CONFIG_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            previous_payload = None

    base_payload = previous_payload or default_config_payload()
    diffs = _diff_profile_payloads(base_payload, payload)
    changed_profiles = diffs["changed_profiles"]
    added_profiles = diffs["added_profiles"]
    removed_profiles = diffs["removed_profiles"]
    has_change = bool(changed_profiles or added_profiles or removed_profiles)

    previous_version = previous_payload.get("version") if previous_payload else None
    reason = payload.get("updated_reason", "config.json updated")

    # 旧形式 -> 新形式への自動マイグレーションは、値変更がなくても1行残す
    if migration_logged:
        changed_profiles = {}
        added_profiles = []
        removed_profiles = []
        has_change = True
        reason = "config format migrated to multi-profile schema (auto)"

    if has_change:
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "version": payload.get("version", DEFAULT_CONFIG_VERSION),
            "previous_version": previous_version,
            "changed_profiles": changed_profiles,
            "added_profiles": added_profiles,
            "removed_profiles": removed_profiles,
            "reason": reason,
        }
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with CONFIG_HISTORY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_CACHE_PATH.write_text(
        json.dumps(payload_to_history_snapshot(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _ensure_live_state_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_TABLE_SQL)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(live_state)").fetchall()
    }
    if "active_profile_name" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN active_profile_name TEXT")
    if "engine_status" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN engine_status TEXT DEFAULT 'RUNNING'")
    if "trading_day_date" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN trading_day_date TEXT")
    if "daily_start_balance" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN daily_start_balance REAL")
    if "daily_realized_pnl" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN daily_realized_pnl REAL DEFAULT 0")
    if "daily_win_count" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN daily_win_count INTEGER DEFAULT 0")
    if "daily_loss_count" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN daily_loss_count INTEGER DEFAULT 0")
    if "position_filled_at" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN position_filled_at TEXT")
    if "pending_order_placed_at" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN pending_order_placed_at TEXT")
    if "entry_order_id" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN entry_order_id INTEGER")
    if "tp_order_id" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN tp_order_id INTEGER")
    if "sl_order_id" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN sl_order_id INTEGER")
    if "position_id" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN position_id INTEGER")
    if "trading_mode" not in columns:
        conn.execute("ALTER TABLE live_state ADD COLUMN trading_mode TEXT")
    conn.execute("UPDATE live_state SET engine_status = 'RUNNING' WHERE engine_status IS NULL")
    conn.execute("UPDATE live_state SET daily_realized_pnl = 0 WHERE daily_realized_pnl IS NULL")
    conn.execute("UPDATE live_state SET daily_win_count = 0 WHERE daily_win_count IS NULL")
    conn.execute("UPDATE live_state SET daily_loss_count = 0 WHERE daily_loss_count IS NULL")


class MarketSnapshotLogger:
    _VOLATILITY_WINDOW = 5

    def __init__(self, interval_sec: int = 60) -> None:
        self.interval_sec = interval_sec
        self._next_write_ts = 0.0
        self._mid_price_buffer: Deque[float] = deque(maxlen=self._VOLATILITY_WINDOW)

    @staticmethod
    def _file_path() -> Path:
        date_str = datetime.now().strftime("%Y-%m-%d")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        return LOG_DIR / f"market_snapshot_{date_str}.csv"

    def _volatility_5min_range_pct(self, mid_price: float) -> Optional[float]:
        """
        直近5件の mid_price レンジを現在 mid で割った値。
        バッファ不足または mid=0 のときは None（空欄記録）。
        """
        self._mid_price_buffer.append(mid_price)
        if len(self._mid_price_buffer) < self._VOLATILITY_WINDOW:
            return None
        if mid_price == 0:
            return None
        return (max(self._mid_price_buffer) - min(self._mid_price_buffer)) / mid_price

    def maybe_log(self, ws_manager: WebSocketManager) -> None:
        now_ts = time.time()
        if now_ts < self._next_write_ts:
            return
        snap = ws_manager.latest_snapshot
        if snap is None:
            return

        trade_stats = ws_manager.consume_trade_window_stats()
        depth_stats = ws_manager.latest_depth_stats or {}
        depth_imbalance = depth_stats.get("depth_imbalance")
        volatility_5min_range_pct = self._volatility_5min_range_pct(snap.mid_price)
        path = self._file_path()
        write_header = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "timestamp",
                    "best_bid_price",
                    "best_bid_size",
                    "best_ask_price",
                    "best_ask_size",
                    "mid_price",
                    "imbalance",
                    "spread_pct",
                    "trade_count",
                    "buy_volume",
                    "sell_volume",
                    "bid_depth5_size",
                    "ask_depth5_size",
                    "depth_imbalance",
                    "volatility_5min_range_pct",
                ],
            )
            if write_header:
                writer.writeheader()
            writer.writerow(
                {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "best_bid_price": snap.best_bid_price,
                    "best_bid_size": snap.best_bid_size,
                    "best_ask_price": snap.best_ask_price,
                    "best_ask_size": snap.best_ask_size,
                    "mid_price": snap.mid_price,
                    "imbalance": snap.imbalance,
                    "spread_pct": snap.spread_pct,
                    "trade_count": int(trade_stats.get("trade_count", 0)),
                    "buy_volume": float(trade_stats.get("buy_volume", 0.0)),
                    "sell_volume": float(trade_stats.get("sell_volume", 0.0)),
                    "bid_depth5_size": depth_stats.get("bid_depth5_size", ""),
                    "ask_depth5_size": depth_stats.get("ask_depth5_size", ""),
                    "depth_imbalance": (
                        depth_imbalance if depth_imbalance is not None else ""
                    ),
                    "volatility_5min_range_pct": (
                        volatility_5min_range_pct
                        if volatility_5min_range_pct is not None
                        else ""
                    ),
                }
            )
        self._next_write_ts = now_ts + self.interval_sec


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS live_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at TEXT,
    best_bid_price REAL, best_bid_size REAL,
    best_ask_price REAL, best_ask_size REAL,
    jpy_balance REAL,
    position_side TEXT, position_entry_price REAL, position_size REAL,
    position_is_pending INTEGER, position_exit_target REAL,
    position_filled_at TEXT,
    pending_order_placed_at TEXT,
    entry_order_id INTEGER,
    tp_order_id INTEGER,
    sl_order_id INTEGER,
    position_id INTEGER,
    win_count INTEGER, loss_count INTEGER,
    total_gross_win REAL, total_gross_loss REAL, cumulative_pnl REAL,
    active_profile_name TEXT,
    engine_status TEXT,
    config_version TEXT,
    ws_connected INTEGER,
    trading_day_date TEXT,
    daily_start_balance REAL,
    daily_realized_pnl REAL,
    daily_win_count INTEGER,
    daily_loss_count INTEGER,
    trading_mode TEXT
);
"""


def _load_daily_loss_persisted() -> Dict[str, Any]:
    if not LIVE_STATE_DB_PATH.exists():
        return {}
    try:
        with sqlite3.connect(LIVE_STATE_DB_PATH, timeout=5) as conn:
            _ensure_live_state_schema(conn)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT
                    trading_day_date,
                    daily_start_balance,
                    daily_realized_pnl,
                    daily_win_count,
                    daily_loss_count,
                    jpy_balance,
                    win_count,
                    loss_count,
                    total_gross_win,
                    total_gross_loss,
                    cumulative_pnl,
                    position_side,
                    position_entry_price,
                    position_size,
                    position_is_pending,
                    position_exit_target,
                    position_filled_at,
                    pending_order_placed_at,
                    entry_order_id,
                    tp_order_id,
                    sl_order_id,
                    position_id,
                    active_profile_name,
                    best_bid_price,
                    best_ask_price
                FROM live_state
                WHERE id = 1
                """
            ).fetchone()
    except Exception as exc:
        print(f"[Engine] daily loss state load error: {exc}")
        return {}
    if row is None:
        return {}
    return {
        "trading_day_date": row["trading_day_date"],
        "daily_start_balance": row["daily_start_balance"],
        "daily_realized_pnl": row["daily_realized_pnl"],
        "daily_win_count": row["daily_win_count"],
        "daily_loss_count": row["daily_loss_count"],
        "jpy_balance": row["jpy_balance"],
        "win_count": row["win_count"],
        "loss_count": row["loss_count"],
        "total_gross_win": row["total_gross_win"],
        "total_gross_loss": row["total_gross_loss"],
        "cumulative_pnl": row["cumulative_pnl"],
        "position_side": row["position_side"],
        "position_entry_price": row["position_entry_price"],
        "position_size": row["position_size"],
        "position_is_pending": row["position_is_pending"],
        "position_exit_target": row["position_exit_target"],
        "position_filled_at": row["position_filled_at"],
        "pending_order_placed_at": row["pending_order_placed_at"],
        "entry_order_id": row["entry_order_id"],
        "tp_order_id": row["tp_order_id"],
        "sl_order_id": row["sl_order_id"],
        "position_id": row["position_id"],
        "active_profile_name": row["active_profile_name"],
        "best_bid_price": row["best_bid_price"],
        "best_ask_price": row["best_ask_price"],
    }


def _write_live_state(trader: VirtualTrader, ws_manager: WebSocketManager) -> None:
    snap = ws_manager.latest_snapshot
    with trader._lock:
        pos = trader.position
        filled_at = trader._position_filled_at
        pending_placed_at = trader._pending_order_placed_at
        payload = {
            "id": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "best_bid_price": snap.best_bid_price if snap else None,
            "best_bid_size": snap.best_bid_size if snap else None,
            "best_ask_price": snap.best_ask_price if snap else None,
            "best_ask_size": snap.best_ask_size if snap else None,
            "jpy_balance": trader.jpy_balance,
            "position_side": pos.side,
            "position_entry_price": pos.entry_price,
            "position_size": pos.size,
            "position_is_pending": 1 if pos.is_pending else 0,
            "position_exit_target": pos.exit_price_target,
            "position_filled_at": (
                filled_at.isoformat(timespec="seconds") if filled_at is not None else None
            ),
            "pending_order_placed_at": (
                pending_placed_at.isoformat(timespec="seconds")
                if pending_placed_at is not None
                else None
            ),
            "entry_order_id": pos.entry_order_id,
            "tp_order_id": pos.tp_order_id,
            "sl_order_id": pos.sl_order_id,
            "position_id": pos.position_id,
            "win_count": trader._win_count,
            "loss_count": trader._loss_count,
            "total_gross_win": trader._total_gross_win,
            "total_gross_loss": trader._total_gross_loss,
            "cumulative_pnl": trader._cumulative_pnl,
            "active_profile_name": trader.active_profile_name,
            "engine_status": trader.engine_status,
            "config_version": trader.config_version,
            "ws_connected": 1 if snap is not None else 0,
            "trading_day_date": trader.trading_day_date,
            "daily_start_balance": trader.daily_start_balance,
            "daily_realized_pnl": trader.daily_realized_pnl,
            "daily_win_count": trader._daily_win_count,
            "daily_loss_count": trader._daily_loss_count,
            "trading_mode": trader.trading_mode,
        }

    with sqlite3.connect(LIVE_STATE_DB_PATH, timeout=5) as conn:
        _ensure_live_state_schema(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO live_state (
                id, updated_at,
                best_bid_price, best_bid_size, best_ask_price, best_ask_size,
                jpy_balance,
                position_side, position_entry_price, position_size,
                position_is_pending, position_exit_target, position_filled_at,
                pending_order_placed_at,
                entry_order_id, tp_order_id, sl_order_id, position_id,
                win_count, loss_count, total_gross_win, total_gross_loss, cumulative_pnl,
                active_profile_name,
                engine_status,
                config_version, ws_connected,
                trading_day_date, daily_start_balance, daily_realized_pnl,
                daily_win_count, daily_loss_count,
                trading_mode
            ) VALUES (
                :id, :updated_at,
                :best_bid_price, :best_bid_size, :best_ask_price, :best_ask_size,
                :jpy_balance,
                :position_side, :position_entry_price, :position_size,
                :position_is_pending, :position_exit_target, :position_filled_at,
                :pending_order_placed_at,
                :entry_order_id, :tp_order_id, :sl_order_id, :position_id,
                :win_count, :loss_count, :total_gross_win, :total_gross_loss, :cumulative_pnl,
                :active_profile_name,
                :engine_status,
                :config_version, :ws_connected,
                :trading_day_date, :daily_start_balance, :daily_realized_pnl,
                :daily_win_count, :daily_loss_count,
                :trading_mode
            )
            """,
            payload,
        )
        conn.commit()


def _write_pid() -> None:
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def _remove_pid() -> None:
    if PID_PATH.exists():
        PID_PATH.unlink()


def _print_startup_position(trader: VirtualTrader) -> None:
    with trader._lock:
        pos = trader.position
        if pos.side is None:
            print("[Engine] 起動時ポジション: FLAT")
        else:
            print(
                "[Engine] 起動時ポジション: "
                f"{pos.side} size={pos.size:.6f} entry={pos.entry_price:,.0f} pending={pos.is_pending}"
            )


def _maintenance_settings_from_payload(payload: Dict[str, Any]) -> tuple[str, int]:
    raw_action = str(payload.get("maintenance_pre_action", "close")).strip().lower()
    action = raw_action if raw_action in {"wait", "close"} else "close"
    raw_minutes = payload.get("maintenance_prepare_minutes", 5)
    try:
        minutes = max(0, int(raw_minutes))
    except (TypeError, ValueError):
        minutes = 5
    return action, minutes


def main() -> None:
    shutdown_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        print(f"[Engine] シグナル受信: {signum} -> 停止開始")
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    try:
        payload, migrated = load_config_payload(CONFIG_PATH)
        profiles = build_profile_definitions(payload)
    except ConfigValidationError as exc:
        print(f"[Engine] config.json エラー: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    trading_mode = str(payload.get("trading_mode", DEFAULT_TRADING_MODE)).strip().lower()
    if trading_mode == "real":
        _reject_real_mode_on_windows_or_exit()
        _acquire_gmo_trade_key_lock_or_exit()

    validation_error = validate_profiles(profiles)
    if validation_error is not None:
        print(f"[Engine] profile validation エラー: {validation_error}", file=sys.stderr)
        raise SystemExit(1)

    _log_config_history_if_changed(payload, migration_logged=migrated)

    maintenance_pre_action, maintenance_prepare_minutes = _maintenance_settings_from_payload(payload)
    safety_settings = _safety_settings_from_payload(payload)
    order_rate_limit = int(safety_settings["order_rate_limit_per_minute"])
    reconciliation_interval_sec = int(safety_settings["reconciliation_interval_minutes"]) * 60
    reconciliation_tolerance_btc = float(safety_settings["reconciliation_tolerance_btc"])
    reconciliation_tolerance_jpy = float(safety_settings["reconciliation_tolerance_jpy"])
    daily_loss_limit_pct = float(safety_settings["daily_loss_limit_pct"])
    initial_jpy = float(safety_settings["initial_jpy"])
    reconciliation_pending_mismatch = [False]
    next_reconciliation_ts = time.time() + reconciliation_interval_sec

    def _before_entry_order() -> bool:
        if check_order_rate_limit(order_rate_limit):
            now = time.time()
            cutoff = now - 60.0
            with _order_rate_lock:
                recent_count = sum(1 for ts in _order_event_timestamps if ts >= cutoff)
            _trigger_safety_stop(
                "order_rate_limit",
                {
                    "order_rate_limit_per_minute": order_rate_limit,
                    "recent_order_count": recent_count,
                },
            )
            return True
        return False

    def _on_order_placed() -> None:
        record_order_event()

    def _on_reconciliation_mismatch(details: Dict[str, float]) -> None:
        _trigger_safety_stop(
            "reconciliation_mismatch",
            {
                "position_diff_btc": details["position_diff_btc"],
                "real_position_size_btc": details["real_position_size_btc"],
                "internal_position_size_btc": details["internal_position_size_btc"],
                "balance_diff_jpy": details["balance_diff_jpy"],
                "real_jpy_balance": details["real_jpy_balance"],
                "internal_jpy_balance": details["internal_jpy_balance"],
            },
        )

    def _on_daily_loss_limit(details: Dict[str, float]) -> None:
        _trigger_safety_stop(
            "daily_loss_limit",
            {
                "daily_realized_pnl": details["daily_realized_pnl"],
                "daily_start_balance": details["daily_start_balance"],
                "daily_loss_limit_pct": details["daily_loss_limit_pct"],
                "limit_jpy": details["limit_jpy"],
            },
        )

    persisted_daily = _load_daily_loss_persisted()
    trader = VirtualTrader(
        initial_jpy=initial_jpy,
        profiles=profiles,
        maintenance_pre_action=maintenance_pre_action,
        maintenance_prepare_minutes=maintenance_prepare_minutes,
        before_entry_order=_before_entry_order,
        on_order_placed=_on_order_placed,
        daily_loss_limit_pct=daily_loss_limit_pct,
        on_daily_loss_limit=_on_daily_loss_limit,
        trading_mode=trading_mode,
        on_critical_alert=send_telegram_message,
    )
    trader.restore_persisted_account_state(
        jpy_balance=persisted_daily.get("jpy_balance"),
        win_count=persisted_daily.get("win_count"),
        loss_count=persisted_daily.get("loss_count"),
        total_gross_win=persisted_daily.get("total_gross_win"),
        total_gross_loss=persisted_daily.get("total_gross_loss"),
        cumulative_pnl=persisted_daily.get("cumulative_pnl"),
    )
    trader.initialize_daily_loss_state(
        persisted_trading_day_date=persisted_daily.get("trading_day_date"),
        persisted_daily_start_balance=persisted_daily.get("daily_start_balance"),
        persisted_daily_realized_pnl=persisted_daily.get("daily_realized_pnl"),
        persisted_daily_win_count=persisted_daily.get("daily_win_count"),
        persisted_daily_loss_count=persisted_daily.get("daily_loss_count"),
    )
    last_total_assets = None
    bid = persisted_daily.get("best_bid_price")
    ask = persisted_daily.get("best_ask_price")
    if persisted_daily.get("jpy_balance") is not None and (bid is not None or ask is not None):
        try:
            last_total_assets = compute_total_assets(
                jpy_balance=float(persisted_daily["jpy_balance"]),
                position_side=persisted_daily.get("position_side"),
                position_size=float(persisted_daily.get("position_size") or 0.0),
                position_entry_price=float(persisted_daily.get("position_entry_price") or 0.0),
                best_bid=float(bid) if bid is not None else None,
                best_ask=float(ask) if ask is not None else None,
                trading_mode=trading_mode,
                position_is_pending=bool(int(persisted_daily.get("position_is_pending") or 0)),
            )
        except (TypeError, ValueError):
            last_total_assets = None
    trader.restore_persisted_position(
        position_side=persisted_daily.get("position_side"),
        position_entry_price=persisted_daily.get("position_entry_price"),
        position_size=persisted_daily.get("position_size"),
        position_is_pending=persisted_daily.get("position_is_pending"),
        position_exit_target=persisted_daily.get("position_exit_target"),
        position_filled_at=persisted_daily.get("position_filled_at"),
        pending_order_placed_at=persisted_daily.get("pending_order_placed_at"),
        entry_order_id=persisted_daily.get("entry_order_id"),
        tp_order_id=persisted_daily.get("tp_order_id"),
        sl_order_id=persisted_daily.get("sl_order_id"),
        position_id=persisted_daily.get("position_id"),
        locked_profile_name=persisted_daily.get("active_profile_name"),
    )
    if trading_mode == "real":
        trader.reconcile_real_state_on_startup(
            trigger_safety_stop=_trigger_safety_stop,
        )
    mid_for_check = None
    if bid is not None and ask is not None:
        try:
            mid_for_check = (float(bid) + float(ask)) / 2.0
        except (TypeError, ValueError):
            mid_for_check = None
    trader.check_account_integrity(
        mid_price=mid_for_check,
        last_total_assets=last_total_assets,
    )
    trader.engine_status = "RUNNING"
    trader.config_version = str(payload.get("version", DEFAULT_CONFIG_VERSION))
    ws_manager, private_ws_manager = _build_ws_managers(trader, trading_mode)
    snapshot_logger = MarketSnapshotLogger(interval_sec=60)

    _write_pid()
    _print_startup_position(trader)
    ws_manager.start()
    if private_ws_manager is not None:
        private_ws_manager.start()
    print(
        f"[Engine] started pid={os.getpid()}"
        f" config_version={trader.config_version}"
        f" trading_mode={trading_mode}"
        f" profiles={len(profiles)}"
        f" maintenance_pre_action={maintenance_pre_action}"
        f" maintenance_prepare_minutes={maintenance_prepare_minutes}"
        f" order_rate_limit_per_minute={order_rate_limit}"
        f" reconciliation_interval_minutes={safety_settings['reconciliation_interval_minutes']}"
        f" daily_loss_limit_pct={daily_loss_limit_pct}"
        f" trading_day_date={trader.trading_day_date}"
        f" daily_start_balance={trader.daily_start_balance:.0f}"
        f" daily_realized_pnl={trader.daily_realized_pnl:.0f}"
    )

    try:
        while not shutdown_event.is_set():
            time.sleep(1)

            now_ts = time.time()
            if now_ts >= next_reconciliation_ts:
                try:
                    run_reconciliation_check(
                        trader=trader,
                        tolerance_btc=reconciliation_tolerance_btc,
                        tolerance_jpy=reconciliation_tolerance_jpy,
                        pending_mismatch=reconciliation_pending_mismatch,
                        on_confirmed_mismatch=_on_reconciliation_mismatch,
                    )
                except Exception as exc:
                    print(f"[Engine] reconciliation エラー: {exc}")
                next_reconciliation_ts = now_ts + reconciliation_interval_sec

            manual_stop_requested = MANUAL_STOP_FLAG_PATH.exists()
            with trader._lock:
                if manual_stop_requested and trader.position.side is not None:
                    trader.engine_status = "STOPPING"
                elif not manual_stop_requested:
                    trader.engine_status = "RUNNING"

            try:
                _write_live_state(trader, ws_manager)
            except Exception as exc:
                print(f"[Engine] live_state 更新エラー: {exc}")

            try:
                snapshot_logger.maybe_log(ws_manager)
            except Exception as exc:
                print(f"[Engine] market snapshot 記録エラー: {exc}")

            if not manual_stop_requested:
                continue

            with trader._lock:
                position_cleared = trader.position.side is None
            if not position_cleared:
                continue

            trader.engine_status = "PAUSED"
            try:
                _write_live_state(trader, ws_manager)
            except Exception as exc:
                print(f"[Engine] PAUSED状態の保存エラー: {exc}")
            print(
                "[Engine] manual stop requested and position is flat."
                " entering PAUSED wait loop (process stays alive)."
            )

            if private_ws_manager is not None:
                private_ws_manager.stop()
            ws_manager.stop()

            last_still_paused_notify_ts = time.time()
            resumed = False
            while not shutdown_event.is_set():
                time.sleep(MANUAL_STOP_PAUSE_POLL_SEC)
                trader.engine_status = "PAUSED"
                try:
                    _write_live_state(trader, ws_manager)
                except Exception as exc:
                    print(f"[Engine] PAUSED live_state 更新エラー: {exc}")

                now_pause_ts = time.time()
                if (
                    now_pause_ts - last_still_paused_notify_ts
                    >= MANUAL_STOP_STILL_PAUSED_NOTIFY_SEC
                ):
                    _notify_still_paused()
                    last_still_paused_notify_ts = now_pause_ts

                if not MANUAL_STOP_FLAG_PATH.exists():
                    resumed = True
                    break

            if not resumed:
                break

            print("[Engine] manual_stop.flag cleared; resuming from PAUSED")
            trader.engine_status = "RUNNING"
            ws_manager, private_ws_manager = _build_ws_managers(trader, trading_mode)
            ws_manager.start()
            if private_ws_manager is not None:
                private_ws_manager.start()
            try:
                _write_live_state(trader, ws_manager)
            except Exception as exc:
                print(f"[Engine] resume live_state 更新エラー: {exc}")
            next_reconciliation_ts = time.time() + reconciliation_interval_sec
            print("[Engine] resumed to RUNNING")
    finally:
        print("[Engine] シャットダウン処理を開始します。")
        if private_ws_manager is not None:
            private_ws_manager.stop()
        ws_manager.stop()
        _remove_pid()
        if trading_mode == "real":
            release_gmo_trade_key_lock()
        print("[Engine] stopped")


if __name__ == "__main__":
    main()
