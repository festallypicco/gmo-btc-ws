"""Detect GMO active orders that are not tracked in live_state.db (orphan orders)."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from monitor_heartbeat import record_monitor_heartbeat
from telegram_notifier import send_telegram_message

ROOT_DIR = Path(__file__).resolve().parent.parent
BTC_DIR = ROOT_DIR / "btc_trading_tool"
if str(BTC_DIR) not in sys.path:
    sys.path.insert(0, str(BTC_DIR))

from virtual_trader import fetch_active_orders  # noqa: E402

CONFIG_PATH = ROOT_DIR / "config" / "config.json"
LIVE_STATE_DB_PATH = ROOT_DIR / "runtime" / "live_state.db"
RUNTIME_DIR = ROOT_DIR / "runtime"
HEARTBEATS_PATH = RUNTIME_DIR / "monitor_heartbeats.json"
# 1回目の orphan 候補を保持し、連続2回で初めてアラートする
# （発注直後の live_state 未反映レース対策。8/3・8/4 誤検知）
STATE_PATH = RUNTIME_DIR / "orphan_orders_state.json"
HEARTBEAT_KEY = "check_orphan_orders"
ENV_CANDIDATES = (
    ROOT_DIR / ".env",
    ROOT_DIR / "scripts" / ".env",
    ROOT_DIR / "ai_review" / ".env",
)

LOGGER = logging.getLogger("orphan_orders_check")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [orphan_orders_check] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _record_monitor_heartbeat() -> None:
    record_monitor_heartbeat(HEARTBEATS_PATH, HEARTBEAT_KEY, logger=LOGGER)


def _load_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    try:
        raw_text = path.read_text(encoding="utf-8")
    except Exception as exc:
        LOGGER.warning("Failed to read env file %s: %s", path, exc)
        return values
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            values[key] = value
    return values


def _ensure_gmo_credentials_from_env_files() -> None:
    """os.environ 未設定時のみ .env から READONLY 認証情報を補完する。"""
    merged: Dict[str, str] = {}
    for path in ENV_CANDIDATES:
        merged.update(_load_env_file(path))
    for name in ("GMO_API_KEY_READONLY", "GMO_API_SECRET_READONLY"):
        if not (os.environ.get(name) or "").strip():
            value = (merged.get(name) or "").strip()
            if value:
                os.environ[name] = value


def load_trading_mode(config_path: Path = CONFIG_PATH) -> str:
    """
    config.json の trading_mode を返す。
    ファイル無し・キー無し・不正値は virtual 扱い（チェック対象外）。
    """
    if not config_path.exists():
        return "virtual"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Failed to read config.json; treat as virtual: %s", exc)
        return "virtual"
    if not isinstance(payload, dict):
        return "virtual"
    mode = str(payload.get("trading_mode", "virtual")).strip().lower()
    if mode not in {"virtual", "real"}:
        return "virtual"
    return mode


def _parse_optional_order_id(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value


def load_known_order_ids(db_path: Path = LIVE_STATE_DB_PATH) -> Set[int]:
    """
    live_state.db の entry/tp/sl_order_id のうち非 None を返す。
    DB が無い場合は空集合（= 追跡中注文なし）。
    """
    if not db_path.exists():
        LOGGER.warning("live_state.db not found: %s (known order ids empty)", db_path)
        return set()

    with sqlite3.connect(db_path, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(live_state)").fetchall()
        }
        select_cols = [
            name
            for name in ("entry_order_id", "tp_order_id", "sl_order_id")
            if name in columns
        ]
        if not select_cols:
            return set()
        row = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM live_state WHERE id = 1"
        ).fetchone()

    if row is None:
        return set()

    known: Set[int] = set()
    for name in select_cols:
        parsed = _parse_optional_order_id(row[name])
        if parsed is not None:
            known.add(parsed)
    return known


def _order_id_from_active(item: Dict[str, Any]) -> Optional[int]:
    return _parse_optional_order_id(item.get("orderId", item.get("order_id")))


def find_orphan_orders(
    active_orders: List[Dict[str, Any]],
    known_order_ids: Set[int],
) -> List[Dict[str, Any]]:
    orphans: List[Dict[str, Any]] = []
    for item in active_orders:
        if not isinstance(item, dict):
            continue
        order_id = _order_id_from_active(item)
        if order_id is None:
            continue
        if order_id not in known_order_ids:
            orphans.append(item)
    return orphans


def load_suspect_order_ids(state_path: Path = STATE_PATH) -> Set[int]:
    """前回チェックで orphan 候補だった orderId 集合を返す。"""
    if not state_path.exists():
        return set()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Failed to read orphan state; treat as empty: %s", exc)
        return set()
    if not isinstance(payload, dict):
        return set()
    raw_ids = payload.get("suspect_order_ids", [])
    if not isinstance(raw_ids, list):
        return set()
    result: Set[int] = set()
    for raw in raw_ids:
        parsed = _parse_optional_order_id(raw)
        if parsed is not None:
            result.add(parsed)
    return result


def save_suspect_order_ids(
    order_ids: Set[int],
    state_path: Path = STATE_PATH,
) -> None:
    """orphan 候補 orderId を atomic write で保存する。"""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "suspect_order_ids": sorted(order_ids),
    }
    tmp_path = state_path.with_name(
        f"{state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, state_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def split_orphans_by_consecutive(
    orphans: List[Dict[str, Any]],
    previous_suspect_ids: Set[int],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    orphan を「連続2回目(=アラート対象)」と「初回(=保留)」に分ける。
    検知条件自体は変えず、発報タイミングだけ遅延させる。
    """
    confirmed: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    for item in orphans:
        order_id = _order_id_from_active(item)
        if order_id is None:
            continue
        if order_id in previous_suspect_ids:
            confirmed.append(item)
        else:
            pending.append(item)
    return confirmed, pending


def build_orphan_alert_message(orphans: List[Dict[str, Any]]) -> str:
    lines = [
        "[ALERT] orphan GMO active order(s) detected",
        f"count={len(orphans)}",
        "detail=orders not tracked by entry/tp/sl_order_id in live_state.db",
        "action=manual review required (no auto-cancel)",
    ]
    for idx, item in enumerate(orphans, start=1):
        order_id = _order_id_from_active(item)
        side = item.get("side", "")
        price = item.get("price", "")
        size = item.get("size", "")
        lines.append(
            f"[{idx}] orderId={order_id} side={side} price={price} size={size}"
        )
    return "\n".join(lines)


def check_orphan_orders(
    *,
    config_path: Path = CONFIG_PATH,
    db_path: Path = LIVE_STATE_DB_PATH,
    state_path: Path = STATE_PATH,
    fetch_fn: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    send_fn: Optional[Callable[[str], bool]] = None,
    ensure_credentials_fn: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """
    real mode のみ孤児注文を検査する。
    virtual / 未設定時は API・Telegram を呼ばずスキップする。

    アラートは同一 orderId が連続2回 orphan と判定された場合のみ送る
    （1回目は状態ファイルへ記録して保留）。
    """
    trading_mode = load_trading_mode(config_path)
    if trading_mode != "real":
        LOGGER.info(
            "trading_mode=%s; orphan order check skipped.",
            trading_mode,
        )
        _record_monitor_heartbeat()
        return {"status": "skipped", "reason": "not_real", "trading_mode": trading_mode}

    if ensure_credentials_fn is not None:
        ensure_credentials_fn()
    else:
        _ensure_gmo_credentials_from_env_files()

    known_ids = load_known_order_ids(db_path)
    LOGGER.info("known order ids from live_state: %s", sorted(known_ids))

    if fetch_fn is not None:
        active_orders = fetch_fn()
    else:
        active_orders = fetch_active_orders(credential_scope="readonly")
    if not isinstance(active_orders, list):
        raise RuntimeError("active orders response is not a list")

    orphans = find_orphan_orders(active_orders, known_ids)
    current_orphan_ids: Set[int] = set()
    for item in orphans:
        order_id = _order_id_from_active(item)
        if order_id is not None:
            current_orphan_ids.add(order_id)

    previous_suspect_ids = load_suspect_order_ids(state_path)
    # 今回の候補を保存（次回連続判定の入力）。orphan 無しなら空でクリア。
    save_suspect_order_ids(current_orphan_ids, state_path)

    if not orphans:
        LOGGER.info(
            "No orphan orders. active=%d known=%d",
            len(active_orders),
            len(known_ids),
        )
        _record_monitor_heartbeat()
        return {
            "status": "ok",
            "orphan_count": 0,
            "active_count": len(active_orders),
            "known_count": len(known_ids),
            "pending_count": 0,
            "confirmed_count": 0,
        }

    confirmed, pending = split_orphans_by_consecutive(
        orphans, previous_suspect_ids
    )
    if pending:
        pending_ids = sorted(
            oid
            for oid in (
                _order_id_from_active(item) for item in pending
            )
            if oid is not None
        )
        LOGGER.info(
            "orphan candidate(s) pending consecutive confirm "
            "(no alert yet): count=%d orderIds=%s",
            len(pending),
            pending_ids,
        )

    if not confirmed:
        _record_monitor_heartbeat()
        return {
            "status": "orphan_pending",
            "orphan_count": len(orphans),
            "pending_count": len(pending),
            "confirmed_count": 0,
            "active_count": len(active_orders),
            "known_count": len(known_ids),
            "telegram_sent": False,
        }

    message = build_orphan_alert_message(confirmed)
    LOGGER.warning(message.replace("\n", " | "))
    sender = send_fn or send_telegram_message
    sent = bool(sender(message))
    if not sent:
        LOGGER.error("Failed to send Telegram notification for orphan orders.")

    _record_monitor_heartbeat()
    return {
        "status": "orphan_detected",
        "orphan_count": len(confirmed),
        "pending_count": len(pending),
        "confirmed_count": len(confirmed),
        "active_count": len(active_orders),
        "known_count": len(known_ids),
        "telegram_sent": sent,
        "message": message,
    }


def main() -> int:
    _setup_logging()
    try:
        result = check_orphan_orders()
        LOGGER.info("orphan order check finished: %s", result.get("status"))
        return 0
    except Exception as exc:
        LOGGER.error("Orphan order check failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
