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
import os
import signal
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

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
    apply_engine_safety_defaults,
    build_profile_definitions,
    default_config_payload,
    load_config_payload,
    payload_to_history_snapshot,
)
from profile_config import validate_profiles  # noqa: E402
from virtual_trader import (  # noqa: E402
    VirtualTrader,
    run_reconciliation_check,
)
from websocket_manager import WebSocketManager  # noqa: E402

SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from telegram_notifier import send_telegram_message  # noqa: E402

PID_PATH = ROOT_DIR / "runtime" / "trading_engine.pid"
LIVE_STATE_DB_PATH = ROOT_DIR / "runtime" / "live_state.db"
LOG_DIR = ROOT_DIR / "log"
CONFIG_DIR = ROOT_DIR / "config"
CONFIG_PATH = CONFIG_DIR / "config.json"
CONFIG_CACHE_PATH = CONFIG_DIR / "last_loaded_config.json"
CONFIG_HISTORY_PATH = LOG_DIR / "config_history.jsonl"
MANUAL_STOP_FLAG_PATH = ROOT_DIR / "runtime" / "manual_stop.flag"

_order_event_timestamps: list[float] = []
_order_rate_lock = threading.Lock()


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


def _trigger_safety_stop(title: str, details: str) -> None:
    if not MANUAL_STOP_FLAG_PATH.exists():
        _create_manual_stop_flag()
    message = f"[ALERT] {title}\n{details}\nmanual_stop.flag を作成しました。"
    print(f"[WARNING] {message}")
    send_telegram_message(message)


def _safety_settings_from_payload(payload: Dict[str, Any]) -> Dict[str, float]:
    normalized = apply_engine_safety_defaults(payload)
    return {
        "order_rate_limit_per_minute": int(normalized["order_rate_limit_per_minute"]),
        "reconciliation_interval_minutes": int(normalized["reconciliation_interval_minutes"]),
        "reconciliation_tolerance_btc": float(normalized["reconciliation_tolerance_btc"]),
        "reconciliation_tolerance_jpy": float(normalized["reconciliation_tolerance_jpy"]),
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
    conn.execute("UPDATE live_state SET engine_status = 'RUNNING' WHERE engine_status IS NULL")


class MarketSnapshotLogger:
    def __init__(self, interval_sec: int = 60) -> None:
        self.interval_sec = interval_sec
        self._next_write_ts = 0.0

    @staticmethod
    def _file_path() -> Path:
        date_str = datetime.now().strftime("%Y-%m-%d")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        return LOG_DIR / f"market_snapshot_{date_str}.csv"

    def maybe_log(self, ws_manager: WebSocketManager) -> None:
        now_ts = time.time()
        if now_ts < self._next_write_ts:
            return
        snap = ws_manager.latest_snapshot
        if snap is None:
            return

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
    win_count INTEGER, loss_count INTEGER,
    total_gross_win REAL, total_gross_loss REAL, cumulative_pnl REAL,
    active_profile_name TEXT,
    engine_status TEXT,
    config_version TEXT,
    ws_connected INTEGER
);
"""


def _write_live_state(trader: VirtualTrader, ws_manager: WebSocketManager) -> None:
    snap = ws_manager.latest_snapshot
    with trader._lock:
        pos = trader.position
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
            "win_count": trader._win_count,
            "loss_count": trader._loss_count,
            "total_gross_win": trader._total_gross_win,
            "total_gross_loss": trader._total_gross_loss,
            "cumulative_pnl": trader._cumulative_pnl,
            "active_profile_name": trader.active_profile_name,
            "engine_status": trader.engine_status,
            "config_version": trader.config_version,
            "ws_connected": 1 if snap is not None else 0,
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
                position_is_pending, position_exit_target,
                win_count, loss_count, total_gross_win, total_gross_loss, cumulative_pnl,
                active_profile_name,
                engine_status,
                config_version, ws_connected
            ) VALUES (
                :id, :updated_at,
                :best_bid_price, :best_bid_size, :best_ask_price, :best_ask_size,
                :jpy_balance,
                :position_side, :position_entry_price, :position_size,
                :position_is_pending, :position_exit_target,
                :win_count, :loss_count, :total_gross_win, :total_gross_loss, :cumulative_pnl,
                :active_profile_name,
                :engine_status,
                :config_version, :ws_connected
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
    reconciliation_pending_mismatch = [False]
    next_reconciliation_ts = time.time() + reconciliation_interval_sec

    def _before_entry_order() -> bool:
        if check_order_rate_limit(order_rate_limit):
            _trigger_safety_stop(
                "Order rate exceeded emergency stop",
                (
                    f"直近60秒の発注回数が上限 {order_rate_limit} 回/分を超えました。"
                    " 新規発注を停止し manual_stop を発動します。"
                ),
            )
            return True
        return False

    def _on_order_placed() -> None:
        record_order_event()

    def _on_reconciliation_mismatch(details: Dict[str, float]) -> None:
        _trigger_safety_stop(
            "Account reconciliation mismatch emergency stop",
            (
                "内部状態とGMO実口座の不一致が2回連続で検知されました。\n"
                f"position_diff={details['position_diff_btc']:.6f} BTC"
                f" (real={details['real_position_size_btc']:.6f}"
                f" internal={details['internal_position_size_btc']:.6f})\n"
                f"balance_diff={details['balance_diff_jpy']:.0f} JPY"
                f" (real={details['real_jpy_balance']:.0f}"
                f" internal={details['internal_jpy_balance']:.0f})"
            ),
        )

    trader = VirtualTrader(
        initial_jpy=50_000.0,
        profiles=profiles,
        maintenance_pre_action=maintenance_pre_action,
        maintenance_prepare_minutes=maintenance_prepare_minutes,
        before_entry_order=_before_entry_order,
        on_order_placed=_on_order_placed,
    )
    trader.engine_status = "RUNNING"
    trader.config_version = str(payload.get("version", DEFAULT_CONFIG_VERSION))
    ws_manager = WebSocketManager(
        on_snapshot_callback=trader.on_orderbook_update,
        on_exchange_status_callback=trader.on_exchange_status,
    )
    snapshot_logger = MarketSnapshotLogger(interval_sec=60)

    _write_pid()
    _print_startup_position(trader)
    ws_manager.start()
    print(
        f"[Engine] started pid={os.getpid()}"
        f" config_version={trader.config_version}"
        f" profiles={len(profiles)}"
        f" maintenance_pre_action={maintenance_pre_action}"
        f" maintenance_prepare_minutes={maintenance_prepare_minutes}"
        f" order_rate_limit_per_minute={order_rate_limit}"
        f" reconciliation_interval_minutes={safety_settings['reconciliation_interval_minutes']}"
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

            if manual_stop_requested:
                with trader._lock:
                    position_cleared = trader.position.side is None
                if position_cleared:
                    trader.engine_status = "STOPPED"
                    try:
                        _write_live_state(trader, ws_manager)
                    except Exception as exc:
                        print(f"[Engine] STOPPED状態の保存エラー: {exc}")
                    print("[Engine] manual stop requested and position is flat. shutting down safely.")
                    shutdown_event.set()
    finally:
        print("[Engine] シャットダウン処理を開始します。")
        ws_manager.stop()
        _remove_pid()
        print("[Engine] stopped")


if __name__ == "__main__":
    main()
