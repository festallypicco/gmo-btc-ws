"""
dashboard.py
------------
trading_engine.py が書き込む live_state.db と log/ 配下の取引CSVを読み取り、
状態を可視化する表示専用ビューア。
"""
import csv
import sqlite3
import subprocess
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import streamlit as st

REFRESH_INTERVAL_SEC = 1.0
STATE_STALE_SEC = 5
ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT_DIR / "log"
LIVE_STATE_DB_PATH = ROOT_DIR / "runtime" / "live_state.db"
AI_REVIEW_SCRIPT_PATH = ROOT_DIR / "ai_review" / "run_nightly_review.ps1"
AI_REVIEW_LOCK_PATH = ROOT_DIR / "ai_review" / "run_nightly_review.lock"
AI_REVIEW_LOCK_FRESH_SEC = 2 * 60 * 60
AI_RETRY_NOTICE_SEC = 120
MANUAL_STOP_FLAG_PATH = ROOT_DIR / "runtime" / "manual_stop.flag"
ENSURE_ENGINE_SCRIPT_PATH = ROOT_DIR / "scripts" / "ensure_engine_running.ps1"


def _today_trade_log_path() -> Path:
    date_str = datetime.now().strftime("%Y-%m-%d")
    return LOG_DIR / f"realtime_trading_log_{date_str}.csv"


def _load_live_state() -> Optional[Dict[str, object]]:
    if not LIVE_STATE_DB_PATH.exists():
        return None
    try:
        with sqlite3.connect(LIVE_STATE_DB_PATH, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM live_state WHERE id = 1").fetchone()
            if row is None:
                return None
            return dict(row)
    except Exception as exc:
        st.warning(f"live_state.db 読み込みエラー: {exc}")
        return None


def _load_recent_trade_rows(limit: int = 100) -> List[Dict[str, str]]:
    path = _today_trade_log_path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = deque(reader, maxlen=limit)
    except Exception as exc:
        st.warning(f"取引CSV読み込みエラー: {exc}")
        return []
    return list(reversed(rows))


def _is_state_fresh(updated_at: Optional[str]) -> bool:
    if not updated_at:
        return False
    try:
        dt = datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    return (datetime.now() - dt).total_seconds() <= STATE_STALE_SEC


def _nightly_review_running() -> bool:
    if not AI_REVIEW_LOCK_PATH.exists():
        return False
    try:
        lock_mtime = AI_REVIEW_LOCK_PATH.stat().st_mtime
    except Exception:
        # lock metadata read error: treat as stale so manual retry is possible
        return False
    return (time.time() - lock_mtime) <= AI_REVIEW_LOCK_FRESH_SEC


def _launch_nightly_review() -> None:
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [
            "powershell.exe",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(AI_REVIEW_SCRIPT_PATH),
        ],
        creationflags=create_no_window,
    )


def _launch_ensure_engine_running() -> None:
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [
            "powershell.exe",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ENSURE_ENGINE_SCRIPT_PATH),
        ],
        creationflags=create_no_window,
    )


def _engine_status(state_row: Optional[Dict[str, object]]) -> str:
    if state_row is None:
        return "RUNNING"
    raw = str(state_row.get("engine_status") or "RUNNING").strip().upper()
    if raw in {"RUNNING", "STOPPING", "STOPPED"}:
        return raw
    return "RUNNING"


state = _load_live_state()
fresh = _is_state_fresh(state.get("updated_at") if state else None)

st.title("BTC 仮想トレード ダッシュボード")

st.subheader("最新板情報")
if state is None:
    st.info("WebSocket 接続中 / 再接続待ち...")
else:
    ws_connected = bool(state.get("ws_connected", 0))
    if not fresh:
        st.error("エンジン停止中、または状態更新が停止しています。")
    elif not ws_connected:
        st.info("WebSocket 再接続待ち...")

    bid = state.get("best_bid_price")
    ask = state.get("best_ask_price")
    bid_size = state.get("best_bid_size")
    ask_size = state.get("best_ask_size")
    if bid is not None and ask is not None:
        total_size = (bid_size or 0.0) + (ask_size or 0.0)
        imbalance = ((bid_size or 0.0) / total_size) if total_size > 0 else 0.5
        spread_pct = ((ask - bid) / bid) if bid else 0.0
        col1, col2, col3 = st.columns(3)
        col1.metric("Best Ask (円)", f"{ask:,.0f}", f"{(ask_size or 0.0):.4f} BTC")
        col2.metric("Best Bid (円)", f"{bid:,.0f}", f"{(bid_size or 0.0):.4f} BTC")
        col3.metric(
            "買い圧力 (Imbalance)",
            f"{imbalance:.1%}",
            f"スプレッド {spread_pct:.3%}",
        )
    else:
        st.info("板データ未取得")

st.subheader("仮想ポートフォリオ")
if state is not None:
    bid = state.get("best_bid_price")
    ask = state.get("best_ask_price")
    mid = ((bid + ask) / 2.0) if (bid is not None and ask is not None) else 0.0
    jpy_balance = float(state.get("jpy_balance") or 0.0)
    side = state.get("position_side")
    entry_price = float(state.get("position_entry_price") or 0.0)
    size = float(state.get("position_size") or 0.0)

    if side == "LONG" and size > 0:
        unrealized = size * mid
    elif side == "SHORT" and size > 0 and entry_price > 0:
        unrealized = (entry_price - mid) * size
    else:
        unrealized = 0.0

    assets = jpy_balance + unrealized
    cumulative = float(state.get("cumulative_pnl") or 0.0)
    col_a, col_b, col_c = st.columns(3)
    col_a.metric("総資産 (円換算)", f"{assets:,.0f} 円")
    col_b.metric("累積損益", f"{cumulative:+,.0f} 円")
    col_c.metric("決済回数", f"{int(state.get('win_count') or 0) + int(state.get('loss_count') or 0)} 回")
else:
    st.info("エンジン状態を読み込み中です。")

if state is not None and state.get("position_side"):
    side = state["position_side"]
    pending = bool(state.get("position_is_pending", 0))
    entry_price = float(state.get("position_entry_price") or 0.0)
    size = float(state.get("position_size") or 0.0)
    tp = float(state.get("position_exit_target") or 0.0)
    status = "[PENDING] 指値待機中（未約定）" if pending else "[OPEN] ポジション保有中"
    tp_info = f"  TP目標: {tp:,.0f} 円" if (not pending and tp > 0) else ""
    st.warning(
        f"{status}: **{side}**"
        f"  指値/エントリー: {entry_price:,.0f} 円"
        f"  数量: {size:.6f} BTC"
        f"{tp_info}"
    )
else:
    st.success("[FLAT] ポジションなし（待機中）")

st.subheader("パフォーマンス指標（KPI）")
if state is None:
    st.info("KPI はエンジン起動後に表示されます。")
else:
    win = int(state.get("win_count") or 0)
    loss = int(state.get("loss_count") or 0)
    total = win + loss
    gross_win = float(state.get("total_gross_win") or 0.0)
    gross_loss = float(state.get("total_gross_loss") or 0.0)
    cumulative = float(state.get("cumulative_pnl") or 0.0)

    k1, k2, k3 = st.columns(3)
    if total == 0:
        k1.metric("勝率", "- %")
        k2.metric("プロフィットファクター", "-")
        k3.metric("平均損益", "- 円")
    else:
        wr = (win / total) * 100.0
        pf = (gross_win / gross_loss) if gross_loss > 0 else None
        avg = cumulative / total
        k1.metric("勝率", f"{wr:.1f} %", f"{win} 勝  {loss} 敗")
        k2.metric("プロフィットファクター", f"{pf:.2f}" if pf is not None else "-")
        k3.metric("平均損益", f"{avg:+,.1f} 円", f"全 {total} 回決済")

    st.caption(
        f"config_version: {state.get('config_version') or 'unknown'} / "
        f"profile: {state.get('active_profile_name') or 'unknown'}"
    )

st.subheader("仮想取引履歴（直近 100 件）")
rows = _load_recent_trade_rows(limit=100)
if rows:
    table = []
    for r in rows:
        table.append(
            {
                "時刻": r.get("timestamp", ""),
                "方向": r.get("side", ""),
                "種別": r.get("order_type", ""),
                "価格(円)": r.get("price", ""),
                "数量(BTC)": r.get("size", ""),
                "手数料(円)": r.get("fee", ""),
                "損益(円)": r.get("pnl", ""),
                "保有秒": r.get("duration_sec", "0"),
                "累積損益": r.get("cumulative_pnl", "0"),
                "理由": r.get("reason", ""),
                "ConfigVer": r.get("config_version", ""),
                "Profile": r.get("profile_name", ""),
                "Imbalance": r.get("imbalance", ""),
                "Spread": r.get("spread_pct", ""),
                "BidSize": r.get("best_bid_size", ""),
                "AskSize": r.get("best_ask_size", ""),
            }
        )
    st.dataframe(table, use_container_width=True)
else:
    st.info("当日の取引履歴はまだありません。")

st.subheader("システム緊急制御")
engine_status = _engine_status(state)
manual_stop_requested = MANUAL_STOP_FLAG_PATH.exists()

if manual_stop_requested:
    if engine_status == "STOPPING":
        st.warning("手動停止状態: STOPPING（決済処理中）")
    elif engine_status == "STOPPED":
        st.success("手動停止状態: STOPPED（停止完了）")
    else:
        st.info("手動停止状態: 停止要求済み")
else:
    if engine_status == "RUNNING" and fresh:
        st.success("エンジン状態: 稼働中")
    elif engine_status == "RUNNING":
        st.info("エンジン状態: 起動待ち")
    else:
        st.info(f"エンジン状態: {engine_status}")

stop_disabled = (engine_status == "STOPPING")
if st.button("⏸ 緊急停止（安全決済）", disabled=stop_disabled):
    MANUAL_STOP_FLAG_PATH.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    st.session_state["manual_stop_requested_epoch"] = time.time()

resume_disabled = not MANUAL_STOP_FLAG_PATH.exists()
if st.button("▶ 再開", disabled=resume_disabled):
    if MANUAL_STOP_FLAG_PATH.exists():
        MANUAL_STOP_FLAG_PATH.unlink()
    _launch_ensure_engine_running()
    st.session_state["manual_resume_requested_epoch"] = time.time()

st.subheader("AI 夜間レビュー")
nightly_running = _nightly_review_running()
if nightly_running:
    st.warning("状態: 実行中")
else:
    st.success("状態: 実行可能")

retry_disabled = nightly_running
if st.button("AI議論を再実行", disabled=retry_disabled):
    _launch_nightly_review()
    st.session_state["ai_retry_started_epoch"] = time.time()

retry_started_epoch = st.session_state.get("ai_retry_started_epoch")
if isinstance(retry_started_epoch, (int, float)):
    elapsed = time.time() - float(retry_started_epoch)
    if elapsed <= AI_RETRY_NOTICE_SEC:
        retry_started_dt = datetime.fromtimestamp(float(retry_started_epoch))
        st.info(f"{retry_started_dt.strftime('%H:%M')}にリトライを開始しました")
    else:
        st.session_state.pop("ai_retry_started_epoch", None)

time.sleep(REFRESH_INTERVAL_SEC)
st.rerun()
