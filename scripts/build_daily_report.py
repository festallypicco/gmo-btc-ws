"""Daily trading summary report for Telegram."""
from __future__ import annotations

import csv
import json
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from telegram_notifier import send_telegram_message

ROOT_DIR = Path(__file__).resolve().parent.parent
BTC_DIR = ROOT_DIR / "btc_trading_tool"
if str(BTC_DIR) not in sys.path:
    sys.path.insert(0, str(BTC_DIR))

from portfolio_metrics import compute_total_assets_from_live_state  # noqa: E402

LIVE_STATE_DB_PATH = ROOT_DIR / "runtime" / "live_state.db"
LOG_DIR = ROOT_DIR / "log"
DAILY_HISTORY_PATH = LOG_DIR / "daily_history.jsonl"
MANUAL_STOP_REASON_PATH = ROOT_DIR / "runtime" / "manual_stop_reason.json"
MONITOR_HEARTBEATS_PATH = ROOT_DIR / "runtime" / "monitor_heartbeats.json"
POSITION_RESTORE_EVENTS_PATH = LOG_DIR / "position_restore_events.jsonl"
CHANGE_OUTCOMES_PATH = LOG_DIR / "change_outcomes.jsonl"

SETTLEMENT_REASONS: Set[str] = {
    "TAKE_PROFIT",
    "STOP_LOSS",
    "FORCE_CLOSE_MAINTENANCE",
}

MONITOR_SLA_HOURS = {
    "check_trading_anomaly": 2.0,
    "check_engine_crash_loop": 1.0 / 3.0,  # 20 minutes
    "check_csv_db_consistency": 2.0,
    "check_engine_process": 2.0,
    "check_orphan_orders": 1.0 / 3.0,  # 20 minutes (5-min cadence)
}

LOGGER = logging.getLogger("daily_report")


def _setup_logging(run_day: Optional[date] = None) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    day = run_day or date.today()
    log_path = LOG_DIR / f"daily_report_{day.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [daily_report] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    return log_path


def get_target_trading_day(
    db_path: Path = LIVE_STATE_DB_PATH,
) -> Tuple[str, float]:
    """
    live_state.db から trading_day_date と daily_realized_pnl を返す。
    想定実行は 05:55 頃（06:00 リセット直前）。
    """
    if not db_path.exists():
        raise FileNotFoundError(f"live_state.db not found: {db_path}")

    with sqlite3.connect(db_path, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT trading_day_date, daily_realized_pnl
            FROM live_state
            WHERE id = 1
            """
        ).fetchone()

    if row is None:
        raise RuntimeError("live_state row(id=1) is missing")

    trading_day = row["trading_day_date"]
    if trading_day is None or str(trading_day).strip() == "":
        raise RuntimeError("trading_day_date is empty")

    pnl = row["daily_realized_pnl"]
    return str(trading_day).strip(), float(pnl if pnl is not None else 0.0)


def _parse_timestamp(raw: str) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _trading_day_label(dt: datetime) -> str:
    d = dt.date()
    if dt.hour < 6:
        d = d - timedelta(days=1)
    return d.isoformat()


def _csv_paths_for_trading_day(target_date: str, log_dir: Path) -> List[Path]:
    day = date.fromisoformat(target_date)
    next_day = day + timedelta(days=1)
    return [
        log_dir / f"realtime_trading_log_{day.isoformat()}.csv",
        log_dir / f"realtime_trading_log_{next_day.isoformat()}.csv",
    ]


def _iter_settlement_rows(
    csv_paths: Sequence[Path],
    window_start: datetime,
    window_end: datetime,
    settlement_reasons: Set[str],
) -> Iterable[dict]:
    for csv_path in csv_paths:
        if not csv_path.exists():
            continue
        with csv_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                reason = str(row.get("reason") or "").strip()
                if reason not in settlement_reasons:
                    continue
                ts = _parse_timestamp(str(row.get("timestamp") or ""))
                if ts is None:
                    continue
                if window_start <= ts < window_end:
                    yield row


def count_settlements(
    target_date: str,
    log_dir: Path = LOG_DIR,
    settlement_reasons: Set[str] = SETTLEMENT_REASONS,
) -> Tuple[int, int]:
    """
    対象期間 [target_date 06:00, next_day 06:00) の決済件数と勝ち件数を返す。
    """
    day = date.fromisoformat(target_date)
    window_start = datetime(day.year, day.month, day.day, 6, 0, 0)
    window_end = window_start + timedelta(days=1)
    csv_paths = _csv_paths_for_trading_day(target_date, log_dir)

    total = 0
    wins = 0
    for row in _iter_settlement_rows(
        csv_paths, window_start, window_end, settlement_reasons
    ):
        total += 1
        try:
            pnl = float(row.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        if pnl > 0:
            wins += 1
    return total, wins


def _format_pnl_jpy(pnl: float) -> str:
    rounded = int(round(pnl))
    sign = "+" if rounded >= 0 else "-"
    return f"{sign}{abs(rounded):,}円"


def _load_manual_stop_reason(
    reason_path: Path = MANUAL_STOP_REASON_PATH,
) -> Optional[Dict[str, Any]]:
    if not reason_path.exists():
        return None
    try:
        obj = json.loads(reason_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def format_circuit_breaker_lines(
    target_date: str,
    reason_path: Path = MANUAL_STOP_REASON_PATH,
) -> List[str]:
    """サーキットブレーカー発動有無と理由・詳細（管理用）。"""
    doc = _load_manual_stop_reason(reason_path=reason_path)
    if doc is None:
        return ["サーキットブレーカー: 発動なし"]

    ts = _parse_timestamp(str(doc.get("triggered_at") or ""))
    if ts is None or _trading_day_label(ts) != target_date:
        return ["サーキットブレーカー: 発動なし"]

    reason = str(doc.get("reason") or "unknown")
    lines = [
        "サーキットブレーカー: 発動あり",
        f"  理由: {reason}",
        f"  発生時刻: {ts.isoformat(timespec='seconds')}",
    ]
    details = doc.get("details")
    if isinstance(details, dict) and details:
        for key in sorted(details.keys()):
            lines.append(f"  {key}={details[key]}")
    return lines


def count_ai_review_activity(
    target_date: str,
    log_dir: Path = LOG_DIR,
    change_outcomes_path: Path = CHANGE_OUTCOMES_PATH,
) -> Tuple[int, int]:
    """
    夜間AI議論の (提案件数, 採用件数)。
    提案: ai_review_decision_{target_date}.json の proposer_output 箇条書き行数
    採用: 同 decision の final_payload.version に一致する change_outcomes の変更フィールド数
    """
    decision_path = log_dir / f"ai_review_decision_{target_date}.json"
    if not decision_path.exists():
        return 0, 0

    try:
        doc = json.loads(decision_path.read_text(encoding="utf-8"))
    except Exception:
        return 0, 0
    if not isinstance(doc, dict):
        return 0, 0

    proposed = 0
    proposer_output = str(doc.get("proposer_output") or "")
    for line in proposer_output.splitlines():
        if line.strip().startswith("-"):
            proposed += 1

    version = ""
    final_payload = doc.get("final_payload")
    if isinstance(final_payload, dict):
        version = str(final_payload.get("version") or "").strip()

    adopted = 0
    if version and change_outcomes_path.exists():
        try:
            for line in change_outcomes_path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                if str(row.get("version") or "") != version:
                    continue
                fields = row.get("changed_fields")
                if isinstance(fields, dict):
                    adopted += len(fields)
        except Exception:
            adopted = 0

    return proposed, adopted


def _classify_ai_review_error_kind_from_text(error_text: str) -> str:
    text = (error_text or "").lower()
    if "429" in text or "resource_exhausted" in text or "quota" in text:
        return "クォータ超過"
    if "503" in text or "unavailable" in text:
        return "混雑"
    if "timed out" in text or "timeout" in text:
        return "タイムアウト"
    return "その他"


def _format_changed_field_summary(
    profile_name: str,
    field_name: str,
    delta: object,
) -> str:
    if isinstance(delta, dict) and ("old" in delta or "new" in delta):
        return (
            f"{profile_name}.{field_name}: "
            f"{delta.get('old')} -> {delta.get('new')}"
        )
    return f"{profile_name}.{field_name}: {delta}"


def collect_ai_review_change_summaries(
    version: str,
    change_outcomes_path: Path = CHANGE_OUTCOMES_PATH,
    *,
    limit: int = 12,
) -> List[str]:
    """change_outcomes から version 一致の変更概要行を返す。"""
    if not version or not change_outcomes_path.exists():
        return []
    lines: List[str] = []
    try:
        for raw in change_outcomes_path.read_text(encoding="utf-8").splitlines():
            text = raw.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if str(row.get("version") or "") != version:
                continue
            profile = str(row.get("profile_name") or "?")
            fields = row.get("changed_fields")
            if not isinstance(fields, dict) or not fields:
                continue
            for field_name in sorted(fields.keys()):
                lines.append(
                    _format_changed_field_summary(
                        profile, str(field_name), fields.get(field_name)
                    )
                )
                if len(lines) >= limit:
                    return lines
    except Exception:
        return lines
    return lines


def format_ai_review_report_lines(
    target_date: str,
    log_dir: Path = LOG_DIR,
    change_outcomes_path: Path = CHANGE_OUTCOMES_PATH,
) -> List[str]:
    """
    管理者日次レポート向け: 前夜AI議論の成否・失敗原因・設定変更概要。
    """
    decision_path = log_dir / f"ai_review_decision_{target_date}.json"
    if not decision_path.exists():
        return ["夜間AI議論: 記録なし"]

    try:
        doc = json.loads(decision_path.read_text(encoding="utf-8"))
    except Exception:
        return ["夜間AI議論: 記録読み取り失敗"]
    if not isinstance(doc, dict):
        return ["夜間AI議論: 記録形式不正"]

    status = str(doc.get("status") or "")
    llm_fail_statuses = {"failed_before_moderator", "failed_moderator_call"}
    if status in llm_fail_statuses:
        kind = str(doc.get("error_kind") or "").strip()
        if not kind:
            kind = _classify_ai_review_error_kind_from_text(str(doc.get("error") or ""))
        return [f"夜間AI議論: 失敗（原因: {kind}）"]

    if status in {
        "missing_summary_file",
        "failed_config_write",
    }:
        kind = str(doc.get("error_kind") or "").strip() or "その他"
        return [f"夜間AI議論: 失敗（原因: {kind}）"]

    proposed, adopted = count_ai_review_activity(
        target_date,
        log_dir=log_dir,
        change_outcomes_path=change_outcomes_path,
    )

    if status == "applied":
        lines = [f"夜間AI議論: 成功（提案{proposed}件 / 採用{adopted}件）"]
        version = ""
        final_payload = doc.get("final_payload")
        if isinstance(final_payload, dict):
            version = str(final_payload.get("version") or "").strip()
        change_lines = collect_ai_review_change_summaries(
            version, change_outcomes_path=change_outcomes_path
        )
        if change_lines:
            lines.append("設定変更:")
            for item in change_lines:
                lines.append(f"  - {item}")
        elif adopted == 0:
            lines.append("設定変更: なし")
        return lines

    # バリデーション見送り等
    return [
        f"夜間AI議論: 完了（設定変更なし / status={status or 'unknown'}）"
    ]


def position_restore_occurred(
    target_date: str,
    events_path: Path = POSITION_RESTORE_EVENTS_PATH,
) -> bool:
    """対象取引日に起動時ポジション復元（restored/fallback）が発生したか。"""
    if not events_path.exists():
        return False
    try:
        for line in events_path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if str(row.get("trading_day") or "") != target_date:
                continue
            if str(row.get("status") or "") in {"restored", "fallback"}:
                return True
    except Exception:
        return False
    return False


def format_heartbeat_lines(
    heartbeats_path: Path = MONITOR_HEARTBEATS_PATH,
    now: Optional[datetime] = None,
) -> List[str]:
    """監視系ハートビートの要約。"""
    now = now or datetime.now()
    data: Dict[str, Any] = {}
    if heartbeats_path.exists():
        try:
            loaded = json.loads(heartbeats_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    lines = ["監視ハートビート:"]
    for key, max_age_hours in MONITOR_SLA_HOURS.items():
        raw = data.get(key)
        ts = _parse_timestamp(str(raw or ""))
        if ts is None:
            lines.append(f"  {key}: missing")
            continue
        age_hours = (now - ts).total_seconds() / 3600.0
        status = "OK" if age_hours <= max_age_hours else "STALE"
        lines.append(
            f"  {key}: {status} (last={ts.isoformat(timespec='seconds')})"
        )
    return lines


def _load_live_state_eod_snapshot(db_path: Path = LIVE_STATE_DB_PATH) -> Optional[dict]:
    """日次レポート実行時点（05:55 頃）の live_state 行を返す。"""
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        # 新カラムが無い古いDBでも落ちないよう、存在する列だけ読む
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(live_state)").fetchall()
        }
        select_cols = [
            "jpy_balance",
            "position_side",
            "position_size",
            "position_entry_price",
            "best_bid_price",
            "best_ask_price",
        ]
        for optional in ("config_version", "active_profile_name", "trading_mode"):
            if optional in columns:
                select_cols.append(optional)
        row = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM live_state WHERE id = 1"
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def _load_jpy_balance(db_path: Path = LIVE_STATE_DB_PATH) -> Optional[float]:
    snapshot = _load_live_state_eod_snapshot(db_path=db_path)
    if snapshot is None:
        return None
    value = snapshot.get("jpy_balance")
    if value is None:
        return None
    return float(value)


def _load_total_assets_eod(db_path: Path = LIVE_STATE_DB_PATH) -> Optional[float]:
    snapshot = _load_live_state_eod_snapshot(db_path=db_path)
    if snapshot is None:
        return None
    return compute_total_assets_from_live_state(snapshot)


def build_report_message(
    db_path: Path = LIVE_STATE_DB_PATH,
    log_dir: Path = LOG_DIR,
    reason_path: Path = MANUAL_STOP_REASON_PATH,
    heartbeats_path: Path = MONITOR_HEARTBEATS_PATH,
    restore_events_path: Path = POSITION_RESTORE_EVENTS_PATH,
    change_outcomes_path: Path = CHANGE_OUTCOMES_PATH,
    now: Optional[datetime] = None,
) -> str:
    target_date, daily_pnl = get_target_trading_day(db_path=db_path)
    total, wins = count_settlements(target_date, log_dir=log_dir)

    lines = [f"[日次レポート] 対象日: {target_date} (06:00-翌06:00)"]
    if total == 0:
        lines.append("決済件数: 0件")
        lines.append("勝率: -")
    else:
        win_rate = (wins / total) * 100.0
        lines.append(f"決済件数: {total}件 (勝率: {win_rate:.1f}%)")
    lines.append(f"実現損益: {_format_pnl_jpy(daily_pnl)}")

    # ---- 管理用追記 ----
    lines.extend(
        format_circuit_breaker_lines(target_date, reason_path=reason_path)
    )

    lines.extend(
        format_ai_review_report_lines(
            target_date,
            log_dir=log_dir,
            change_outcomes_path=change_outcomes_path,
        )
    )

    restore_flag = position_restore_occurred(
        target_date, events_path=restore_events_path
    )
    lines.append(
        "ポジション復元イベント: "
        + ("あり" if restore_flag else "なし")
    )

    lines.extend(format_heartbeat_lines(heartbeats_path=heartbeats_path, now=now))

    snapshot = _load_live_state_eod_snapshot(db_path=db_path) or {}
    config_version = snapshot.get("config_version") or "-"
    active_profile = snapshot.get("active_profile_name") or "-"
    lines.append(f"config_version: {config_version}")
    lines.append(f"active_profile_name: {active_profile}")

    return "\n".join(lines)


def append_daily_history(
    db_path: Path = LIVE_STATE_DB_PATH,
    log_dir: Path = LOG_DIR,
    history_path: Path = DAILY_HISTORY_PATH,
) -> None:
    """
    日次レポートと同じ集計結果を log/daily_history.jsonl へ1行追記する。
    """
    target_date, daily_pnl = get_target_trading_day(db_path=db_path)
    total, wins = count_settlements(target_date, log_dir=log_dir)
    losses = total - wins
    jpy_balance = _load_jpy_balance(db_path=db_path)
    total_assets_eod = _load_total_assets_eod(db_path=db_path)
    record = {
        "trading_day": target_date,
        "settlements": total,
        "wins": wins,
        "losses": losses,
        "realized_pnl": daily_pnl,
        "jpy_balance": jpy_balance,
        "total_assets_eod": total_assets_eod,
    }
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(
    db_path: Path = LIVE_STATE_DB_PATH,
    log_dir: Path = LOG_DIR,
    send_message=send_telegram_message,
) -> int:
    _setup_logging()
    try:
        message = build_report_message(db_path=db_path, log_dir=log_dir)
        LOGGER.info("Report message built:\n%s", message)
        try:
            append_daily_history(db_path=db_path, log_dir=log_dir)
        except Exception as hist_exc:
            print(f"[WARN] daily_history.jsonl append failed: {hist_exc}")
        sent = send_message(message)
        if not sent:
            LOGGER.error("Telegram send failed")
            return 1
        LOGGER.info("Telegram send succeeded")
        return 0
    except Exception as exc:
        LOGGER.exception("daily report failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
