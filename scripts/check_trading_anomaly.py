"""Hourly anomaly detector for trading frequency and PnL degradation."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from telegram_notifier import send_telegram_message

ROOT_DIR = Path(__file__).resolve().parent.parent
LIVE_STATE_DB_PATH = ROOT_DIR / "runtime" / "live_state.db"
STATE_PATH = Path(__file__).resolve().parent.parent / "runtime" / "anomaly_state.json"
HEARTBEATS_PATH = ROOT_DIR / "runtime" / "monitor_heartbeats.json"
HEARTBEAT_KEY = "check_trading_anomaly"

# ---- Thresholds (tune later if needed) -------------------------------------
MAX_TRADES_PER_HOUR = 60.0
MAX_LOSS_PER_HOUR = -3000.0
ALERT_COOLDOWN_HOURS = 2.0
# -----------------------------------------------------------------------------

LOGGER = logging.getLogger("anomaly_check")


@dataclass
class LiveSnapshot:
    updated_at: datetime
    win_count: int
    loss_count: int
    cumulative_pnl: float

    @property
    def total_trades(self) -> int:
        return self.win_count + self.loss_count


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [anomaly_check] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _record_monitor_heartbeat() -> None:
    HEARTBEATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {}
    if HEARTBEATS_PATH.exists():
        try:
            loaded = json.loads(HEARTBEATS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception as exc:
            LOGGER.warning("Failed to read heartbeats file; recreating: %s", exc)
    data[HEARTBEAT_KEY] = datetime.now().isoformat(timespec="seconds")
    tmp_path = HEARTBEATS_PATH.with_suffix(HEARTBEATS_PATH.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, HEARTBEATS_PATH)


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _read_live_snapshot() -> LiveSnapshot:
    if not LIVE_STATE_DB_PATH.exists():
        raise FileNotFoundError(f"live_state.db not found: {LIVE_STATE_DB_PATH}")

    with sqlite3.connect(LIVE_STATE_DB_PATH, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT updated_at, win_count, loss_count, cumulative_pnl
            FROM live_state
            WHERE id = 1
            """
        ).fetchone()

    if row is None:
        raise RuntimeError("live_state row(id=1) is missing")

    updated_at = _parse_iso_datetime(row["updated_at"])
    if updated_at is None:
        raise RuntimeError(f"invalid updated_at in live_state: {row['updated_at']!r}")

    return LiveSnapshot(
        updated_at=updated_at,
        win_count=int(row["win_count"] or 0),
        loss_count=int(row["loss_count"] or 0),
        cumulative_pnl=float(row["cumulative_pnl"] or 0.0),
    )


def _default_state(snapshot: LiveSnapshot) -> Dict[str, Any]:
    return {
        "last_checked_at": snapshot.updated_at.isoformat(timespec="seconds"),
        "last_win_count": snapshot.win_count,
        "last_loss_count": snapshot.loss_count,
        "last_cumulative_pnl": snapshot.cumulative_pnl,
        "last_alert_trades_at": None,
        "last_alert_pnl_at": None,
    }


def _load_state() -> Optional[Dict[str, Any]]:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Failed to read anomaly state. It will be reinitialized: %s", exc)
        return None


def _save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _can_send_alert(now_dt: datetime, last_alert_at: Optional[str]) -> bool:
    if not last_alert_at:
        return True
    last_dt = _parse_iso_datetime(last_alert_at)
    if last_dt is None:
        return True
    elapsed = (now_dt - last_dt).total_seconds() / 3600.0
    return elapsed >= ALERT_COOLDOWN_HOURS


def _build_trades_alert(elapsed_hours: float, delta_trades: int, trades_per_hour: float) -> str:
    return (
        "ALERT: BTC auto-trading anomaly detected\n"
        "種別: 取引頻度異常\n"
        f"直近{elapsed_hours:.1f}時間で{delta_trades}件の取引\n"
        f"（時間換算 {trades_per_hour:.1f}件/時、閾値 {MAX_TRADES_PER_HOUR:.1f}件/時）\n"
        "現在のプロセス数を確認してください:\n"
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like \"*trading_engine.py*\" }"
    )


def _build_pnl_alert(elapsed_hours: float, delta_pnl: float, pnl_per_hour: float) -> str:
    return (
        "ALERT: BTC auto-trading anomaly detected\n"
        "種別: 損益異常\n"
        f"直近{elapsed_hours:.1f}時間の損益変化: {delta_pnl:+,.0f}円\n"
        f"（時間換算 {pnl_per_hour:+,.1f}円/時、閾値 {MAX_LOSS_PER_HOUR:,.1f}円/時）"
    )


def _build_db_error_alert(message: str) -> str:
    return (
        "ALERT: BTC auto-trading anomaly detected\n"
        "種別: 監視基盤異常\n"
        f"エンジンが停止しているか、live_state.dbにアクセスできません。\n"
        f"詳細: {message}"
    )


def main() -> int:
    _setup_logging()

    try:
        current = _read_live_snapshot()
    except Exception as exc:
        LOGGER.error("Failed to read live_state.db: %s", exc)
        send_telegram_message(_build_db_error_alert(str(exc)))
        return 1

    state = _load_state()
    if state is None:
        init_state = _default_state(current)
        _save_state(init_state)
        LOGGER.info("Initial run: anomaly_state.json created and alert check skipped.")
        _record_monitor_heartbeat()
        return 0

    prev_checked = _parse_iso_datetime(state.get("last_checked_at"))
    if prev_checked is None:
        LOGGER.warning("Invalid last_checked_at. Reinitializing state baseline.")
        baseline = _default_state(current)
        baseline["last_alert_trades_at"] = state.get("last_alert_trades_at")
        baseline["last_alert_pnl_at"] = state.get("last_alert_pnl_at")
        _save_state(baseline)
        _record_monitor_heartbeat()
        return 0

    prev_total_trades = int(state.get("last_win_count", 0)) + int(state.get("last_loss_count", 0))
    if current.total_trades < prev_total_trades:
        LOGGER.info(
            "live_state reset detected (possible engine restart); skipping alert judgement"
        )
        baseline = _default_state(current)
        _save_state(baseline)
        _record_monitor_heartbeat()
        return 0

    prev_cumulative_pnl = float(state.get("last_cumulative_pnl", 0.0))

    delta_trades = current.total_trades - prev_total_trades
    delta_pnl = current.cumulative_pnl - prev_cumulative_pnl
    elapsed_hours = (current.updated_at - prev_checked).total_seconds() / 3600.0

    next_state = {
        "last_checked_at": current.updated_at.isoformat(timespec="seconds"),
        "last_win_count": current.win_count,
        "last_loss_count": current.loss_count,
        "last_cumulative_pnl": current.cumulative_pnl,
        "last_alert_trades_at": state.get("last_alert_trades_at"),
        "last_alert_pnl_at": state.get("last_alert_pnl_at"),
    }

    if elapsed_hours < 0.1:
        LOGGER.info("Elapsed time %.3f h is too short. Alert judgement skipped.", elapsed_hours)
        _save_state(next_state)
        _record_monitor_heartbeat()
        return 0

    trades_per_hour = delta_trades / elapsed_hours
    pnl_per_hour = delta_pnl / elapsed_hours

    LOGGER.info(
        "delta_trades=%d delta_pnl=%.2f elapsed_hours=%.3f trades_per_hour=%.2f pnl_per_hour=%.2f",
        delta_trades,
        delta_pnl,
        elapsed_hours,
        trades_per_hour,
        pnl_per_hour,
    )

    now_dt = current.updated_at
    if trades_per_hour > MAX_TRADES_PER_HOUR:
        if _can_send_alert(now_dt, state.get("last_alert_trades_at")):
            if send_telegram_message(_build_trades_alert(elapsed_hours, delta_trades, trades_per_hour)):
                next_state["last_alert_trades_at"] = now_dt.isoformat(timespec="seconds")
                LOGGER.warning("Trades anomaly alert sent.")
        else:
            LOGGER.info("Trades anomaly detected but still in cooldown window.")

    if pnl_per_hour < MAX_LOSS_PER_HOUR:
        if _can_send_alert(now_dt, state.get("last_alert_pnl_at")):
            if send_telegram_message(_build_pnl_alert(elapsed_hours, delta_pnl, pnl_per_hour)):
                next_state["last_alert_pnl_at"] = now_dt.isoformat(timespec="seconds")
                LOGGER.warning("PnL anomaly alert sent.")
        else:
            LOGGER.info("PnL anomaly detected but still in cooldown window.")

    _save_state(next_state)
    _record_monitor_heartbeat()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
