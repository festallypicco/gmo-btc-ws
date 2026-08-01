from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODULE_DIR = PROJECT_ROOT / "btc_trading_tool"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

AI_REVIEW_DIR = PROJECT_ROOT / "ai_review"
if str(AI_REVIEW_DIR) not in sys.path:
    sys.path.insert(0, str(AI_REVIEW_DIR))

from telegram_notifier import send_telegram_message

from profile_config import parse_hhmm_to_minute  # noqa: E402

LOG_DIR = PROJECT_ROOT / "log"
CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"
CONFIG_HISTORY_PATH = LOG_DIR / "config_history.jsonl"
VALIDATION_FAILURES_PATH = LOG_DIR / "ai_validation_failures.jsonl"
CHANGE_OUTCOMES_PATH = LOG_DIR / "change_outcomes.jsonl"


def warn(message: str) -> None:
    print(f"[WARNING] {message}", file=sys.stderr)


def classify_confidence(exit_count: int, actual_days: int, requested_days: int) -> str:
    if requested_days <= 0:
        return "insufficient"
    if (actual_days / requested_days) < 0.5:
        return "insufficient"
    if exit_count < 30:
        return "insufficient"
    if exit_count < 100:
        return "low"
    if exit_count < 300:
        return "medium"
    return "high"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def mean_or_none(values: Iterable[float]) -> Optional[float]:
    values = list(values)
    if not values:
        return None
    return statistics.mean(values)


def max_or_none(values: Iterable[float]) -> Optional[float]:
    values = list(values)
    if not values:
        return None
    return max(values)


def stdev_or_none(values: Iterable[float]) -> Optional[float]:
    values = list(values)
    if not values:
        return None
    if len(values) == 1:
        return 0.0
    return statistics.pstdev(values)


def daterange_desc(target_date: date, requested_days: int) -> List[date]:
    return [target_date - timedelta(days=offset) for offset in range(requested_days)]


def read_json_file(path: Path, required: bool) -> Optional[Dict[str, Any]]:
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        if required:
            raise
        warn(f"JSON 読み込みに失敗したためスキップします: {path} ({exc})")
        return None


def read_jsonl_tail(path: Path, limit: int = 5) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    tail: Deque[Dict[str, Any]] = deque(maxlen=limit)
    try:
        with path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    warn(f"{path.name}:{lineno} の JSONL 行をスキップしました: {exc}")
                    continue
                if isinstance(obj, dict):
                    tail.append(obj)
    except Exception as exc:
        warn(f"JSONL の読み込みに失敗したため空扱いにします: {path} ({exc})")
        return []
    return list(tail)


def load_trade_rows(dates: List[date]) -> Tuple[List[Dict[str, Any]], Set[date]]:
    rows: List[Dict[str, Any]] = []
    actual_days: Set[date] = set()
    for d in dates:
        path = LOG_DIR / f"realtime_trading_log_{d.isoformat()}.csv"
        if not path.exists():
            continue
        try:
            with path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    reason = str(row.get("reason", "")).strip().upper()
                    profile_name = str(row.get("profile_name", "")).strip() or "unknown"
                    parsed = {
                        "date": d,
                        "reason": reason,
                        "profile_name": profile_name,
                        "pnl": safe_float(row.get("pnl"), 0.0),
                        "duration_sec": safe_int(row.get("duration_sec"), 0),
                    }
                    rows.append(parsed)
            actual_days.add(d)
        except Exception as exc:
            warn(f"取引ログの読み込みに失敗。日次をスキップします: {path.name} ({exc})")
    return rows, actual_days


def summarize_trade_group(
    rows: List[Dict[str, Any]],
    requested_days: int,
    actual_days: int,
) -> Dict[str, Any]:
    trade_count = sum(1 for r in rows if r["reason"] == "ENTRY")
    exit_rows = [r for r in rows if r["reason"] in {"TAKE_PROFIT", "STOP_LOSS"}]
    forced_rows = [r for r in rows if r["reason"] == "FORCE_CLOSE_MAINTENANCE"]
    exit_count = len(exit_rows)

    win_count = sum(1 for r in exit_rows if r["pnl"] > 0)
    loss_count = sum(1 for r in exit_rows if r["pnl"] < 0)
    gross_profit = sum(r["pnl"] for r in exit_rows if r["pnl"] > 0)
    gross_loss_abs = sum(-r["pnl"] for r in exit_rows if r["pnl"] < 0)
    total_pnl = sum(r["pnl"] for r in exit_rows)
    forced_close_total_pnl = sum(r["pnl"] for r in forced_rows)
    grand_total_pnl = total_pnl + forced_close_total_pnl
    avg_pnl = (total_pnl / exit_count) if exit_count > 0 else None

    durations = [r["duration_sec"] for r in exit_rows]
    avg_duration_sec = (sum(durations) / len(durations)) if durations else None

    win_rate = (win_count / exit_count) if exit_count > 0 else None
    profit_factor = (gross_profit / gross_loss_abs) if gross_loss_abs > 0 else None
    confidence = classify_confidence(exit_count, actual_days, requested_days)

    return {
        "requested_days": requested_days,
        "actual_days": actual_days,
        "confidence": confidence,
        "trade_count": trade_count,
        "exit_count": exit_count,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_pnl": avg_pnl,
        "total_pnl": total_pnl,
        "forced_close_count": len(forced_rows),
        "forced_close_total_pnl": forced_close_total_pnl,
        "grand_total_pnl": grand_total_pnl,
        "avg_duration_sec": avg_duration_sec,
    }


def build_trade_window_summary(
    target_date: date,
    requested_days: int,
    profile_names: List[str],
) -> Dict[str, Any]:
    dates = daterange_desc(target_date, requested_days)
    rows, actual_day_set = load_trade_rows(dates)

    overall = summarize_trade_group(
        rows=rows,
        requested_days=requested_days,
        actual_days=len(actual_day_set),
    )

    per_profile: Dict[str, Any] = {}
    observed_profiles = {r["profile_name"] for r in rows}
    all_profiles = sorted(set(profile_names) | observed_profiles)

    for profile in all_profiles:
        p_rows = [r for r in rows if r["profile_name"] == profile]
        per_profile[profile] = summarize_trade_group(
            rows=p_rows,
            requested_days=requested_days,
            actual_days=len(actual_day_set),
        )

    return {
        "requested_days": requested_days,
        "actual_days": len(actual_day_set),
        "overall": overall,
        "per_profile": per_profile,
        "_rows": rows,
        "_actual_day_set": actual_day_set,
    }


def add_weekly_breakdown(window_summary: Dict[str, Any], target_date: date) -> None:
    rows: List[Dict[str, Any]] = window_summary.pop("_rows", [])
    actual_day_set: Set[date] = window_summary.pop("_actual_day_set", set())
    requested_days = int(window_summary["requested_days"])
    window_start = target_date - timedelta(days=requested_days - 1)

    weekly_breakdown: List[Dict[str, Any]] = []
    block_index = 0
    block_end = target_date

    while block_end >= window_start:
        block_start = max(window_start, block_end - timedelta(days=6))
        block_rows = [
            r for r in rows
            if block_start <= r["date"] <= block_end
        ]
        block_actual_days = sum(1 for d in actual_day_set if block_start <= d <= block_end)
        block_requested_days = (block_end - block_start).days + 1

        metrics = summarize_trade_group(
            rows=block_rows,
            requested_days=block_requested_days,
            actual_days=block_actual_days,
        )
        weekly_breakdown.append(
            {
                "block_index": block_index,
                "start_date": block_start.isoformat(),
                "end_date": block_end.isoformat(),
                "requested_days": block_requested_days,
                "actual_days": block_actual_days,
                "exit_count": metrics["exit_count"],
                "win_rate": metrics["win_rate"],
                "profit_factor": metrics["profit_factor"],
                "confidence": metrics["confidence"],
            }
        )
        block_index += 1
        block_end = block_start - timedelta(days=1)

    window_summary["weekly_breakdown"] = weekly_breakdown


def classify_trades_confidence(
    total_trade_count: int,
    trades_actual_days: int,
    requested_days: int,
) -> str:
    """market_snapshot の trades 列に基づく信頼度（既存 confidence とは独立）。"""
    return classify_confidence(total_trade_count, trades_actual_days, requested_days)


def classify_depth_confidence(
    snapshot_count: int,
    depth_actual_days: int,
    requested_days: int,
) -> str:
    """market_snapshot の depth5 列に基づく信頼度（既存 confidence / trades_confidence とは独立）。"""
    return classify_confidence(snapshot_count, depth_actual_days, requested_days)


def classify_volatility_confidence(
    snapshot_count: int,
    volatility_actual_days: int,
    requested_days: int,
) -> str:
    """market_snapshot の volatility 列に基づく信頼度（既存 confidence 群とは独立）。"""
    return classify_confidence(snapshot_count, volatility_actual_days, requested_days)


def safe_optional_float(value: Any) -> Optional[float]:
    text = str(value if value is not None else "").strip()
    if text == "" or text.lower() in {"null", "none", "nan"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_snapshot_timestamp(raw: Any) -> Optional[datetime]:
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


def _row_in_profile_window(row: Dict[str, Any], start_minute: int, end_minute: int) -> bool:
    ts = _parse_snapshot_timestamp(row.get("timestamp"))
    if ts is None:
        return False
    minute = ts.hour * 60 + ts.minute
    return start_minute <= minute < end_minute


def load_market_rows(
    dates: List[date],
) -> Tuple[List[Dict[str, Any]], Set[date], Set[date], Set[date], Set[date]]:
    """
    market_snapshot CSV を読み込む。

    Returns:
        rows: スナップショット行（trades/depth/volatility 列がある日のみ該当フィールドを付与）
        actual_days: CSV ファイルが存在した日
        trades_days: trade_count/buy_volume/sell_volume 列が存在した日
        depth_days: bid_depth5_size/ask_depth5_size/depth_imbalance 列が存在した日
        volatility_days: volatility_5min_range_pct 列が存在した日
    """
    rows: List[Dict[str, Any]] = []
    actual_days: Set[date] = set()
    trades_days: Set[date] = set()
    depth_days: Set[date] = set()
    volatility_days: Set[date] = set()
    trades_columns = ("trade_count", "buy_volume", "sell_volume")
    depth_columns = ("bid_depth5_size", "ask_depth5_size", "depth_imbalance")
    volatility_column = "volatility_5min_range_pct"

    for d in dates:
        path = LOG_DIR / f"market_snapshot_{d.isoformat()}.csv"
        if not path.exists():
            continue
        try:
            with path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames or []
                has_trades_cols = all(col in fieldnames for col in trades_columns)
                has_depth_cols = all(col in fieldnames for col in depth_columns)
                has_volatility_col = volatility_column in fieldnames
                for row in reader:
                    parsed: Dict[str, Any] = {
                        "date": d,
                        "timestamp": str(row.get("timestamp") or "").strip(),
                        "mid_price": safe_float(row.get("mid_price"), 0.0),
                        "spread_pct": safe_float(row.get("spread_pct"), 0.0),
                        "imbalance": safe_float(row.get("imbalance"), 0.0),
                        "has_trades_data": has_trades_cols,
                        "has_depth_data": has_depth_cols,
                        "has_volatility_data": has_volatility_col,
                    }
                    if has_trades_cols:
                        parsed["trade_count"] = safe_int(row.get("trade_count"), 0)
                        parsed["buy_volume"] = safe_float(row.get("buy_volume"), 0.0)
                        parsed["sell_volume"] = safe_float(row.get("sell_volume"), 0.0)
                    if has_depth_cols:
                        parsed["bid_depth5_size"] = safe_float(
                            row.get("bid_depth5_size"), 0.0
                        )
                        parsed["ask_depth5_size"] = safe_float(
                            row.get("ask_depth5_size"), 0.0
                        )
                        parsed["depth_imbalance"] = safe_optional_float(
                            row.get("depth_imbalance")
                        )
                    if has_volatility_col:
                        parsed["volatility_5min_range_pct"] = safe_optional_float(
                            row.get(volatility_column)
                        )
                    rows.append(parsed)
            actual_days.add(d)
            if has_trades_cols:
                trades_days.add(d)
            if has_depth_cols:
                depth_days.add(d)
            if has_volatility_col:
                volatility_days.add(d)
        except Exception as exc:
            warn(f"相場スナップショットの読み込みに失敗。日次をスキップします: {path.name} ({exc})")
    return rows, actual_days, trades_days, depth_days, volatility_days


def summarize_market_trades_group(
    rows: List[Dict[str, Any]],
    trades_days: Set[date],
    requested_days: int,
    window_dates: Set[date],
) -> Dict[str, Any]:
    """
  trades 列が存在した日のスナップショットのみ集計する。
  列が無い日は「データ無し」として合計・比率から除外する。
    """
    eligible_dates = trades_days & window_dates
    trades_actual_days = len(eligible_dates)
    eligible_rows = [
        r for r in rows
        if r.get("has_trades_data") and r.get("date") in eligible_dates
    ]

    if not eligible_rows:
        return {
            "requested_days": requested_days,
            "trades_actual_days": trades_actual_days,
            "trades_confidence": classify_trades_confidence(
                0, trades_actual_days, requested_days
            ),
            "buy_volume_total": None,
            "sell_volume_total": None,
            "buy_ratio": None,
            "avg_trade_count_per_snapshot": None,
        }

    buy_total = sum(float(r.get("buy_volume") or 0.0) for r in eligible_rows)
    sell_total = sum(float(r.get("sell_volume") or 0.0) for r in eligible_rows)
    trade_count_total = sum(int(r.get("trade_count") or 0) for r in eligible_rows)
    volume_sum = buy_total + sell_total
    buy_ratio = (buy_total / volume_sum) if volume_sum > 0 else None
    avg_trade_count = trade_count_total / len(eligible_rows)

    return {
        "requested_days": requested_days,
        "trades_actual_days": trades_actual_days,
        "trades_confidence": classify_trades_confidence(
            trade_count_total, trades_actual_days, requested_days
        ),
        "buy_volume_total": buy_total,
        "sell_volume_total": sell_total,
        "buy_ratio": buy_ratio,
        "avg_trade_count_per_snapshot": avg_trade_count,
    }


def build_market_trades_summary(
    target_date: date,
    requested_days: int,
    profiles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    dates = daterange_desc(target_date, requested_days)
    window_dates = set(dates)
    rows, _actual_days, trades_days, _depth_days, _volatility_days = load_market_rows(dates)

    overall = summarize_market_trades_group(
        rows=rows,
        trades_days=trades_days,
        requested_days=requested_days,
        window_dates=window_dates,
    )

    per_profile: Dict[str, Any] = {}
    profile_names = [
        str(p.get("name"))
        for p in profiles
        if isinstance(p, dict) and p.get("name")
    ]
    for profile in profiles:
        if not isinstance(profile, dict) or not profile.get("name"):
            continue
        name = str(profile["name"])
        try:
            start_min = parse_hhmm_to_minute(str(profile.get("start_time", "00:00")))
            end_min = parse_hhmm_to_minute(str(profile.get("end_time", "24:00")))
        except ValueError as exc:
            warn(f"profile '{name}' の時刻範囲が不正のため trades 集計をスキップ: {exc}")
            continue
        profile_rows = [
            r for r in rows
            if r.get("has_trades_data") and _row_in_profile_window(r, start_min, end_min)
        ]
        per_profile[name] = summarize_market_trades_group(
            rows=profile_rows,
            trades_days=trades_days,
            requested_days=requested_days,
            window_dates=window_dates,
        )

    for name in sorted(set(profile_names) - set(per_profile.keys())):
        per_profile[name] = summarize_market_trades_group(
            rows=[],
            trades_days=trades_days,
            requested_days=requested_days,
            window_dates=window_dates,
        )

    return {
        "requested_days": requested_days,
        "overall": overall,
        "per_profile": per_profile,
    }


def summarize_market_depth_group(
    rows: List[Dict[str, Any]],
    depth_days: Set[date],
    requested_days: int,
    window_dates: Set[date],
) -> Dict[str, Any]:
    """
    depth5 列が存在した日のスナップショットのみ集計する。
    列が無い日は「データ無し」として平均から除外する。
    depth_imbalance が null の行は imbalance 平均のみから除外する。
    """
    eligible_dates = depth_days & window_dates
    depth_actual_days = len(eligible_dates)
    eligible_rows = [
        r for r in rows
        if r.get("has_depth_data") and r.get("date") in eligible_dates
    ]

    if not eligible_rows:
        return {
            "requested_days": requested_days,
            "depth_actual_days": depth_actual_days,
            "depth_confidence": classify_depth_confidence(
                0, depth_actual_days, requested_days
            ),
            "avg_bid_depth5_size": None,
            "avg_ask_depth5_size": None,
            "avg_depth_imbalance": None,
        }

    bid_values = [float(r.get("bid_depth5_size") or 0.0) for r in eligible_rows]
    ask_values = [float(r.get("ask_depth5_size") or 0.0) for r in eligible_rows]
    imbalance_values = [
        float(v)
        for r in eligible_rows
        for v in [r.get("depth_imbalance")]
        if v is not None
    ]

    return {
        "requested_days": requested_days,
        "depth_actual_days": depth_actual_days,
        "depth_confidence": classify_depth_confidence(
            len(eligible_rows), depth_actual_days, requested_days
        ),
        "avg_bid_depth5_size": mean_or_none(bid_values),
        "avg_ask_depth5_size": mean_or_none(ask_values),
        "avg_depth_imbalance": mean_or_none(imbalance_values),
    }


def build_market_depth_summary(
    target_date: date,
    requested_days: int,
    profiles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    dates = daterange_desc(target_date, requested_days)
    window_dates = set(dates)
    rows, _actual_days, _trades_days, depth_days, _volatility_days = load_market_rows(dates)

    overall = summarize_market_depth_group(
        rows=rows,
        depth_days=depth_days,
        requested_days=requested_days,
        window_dates=window_dates,
    )

    per_profile: Dict[str, Any] = {}
    profile_names = [
        str(p.get("name"))
        for p in profiles
        if isinstance(p, dict) and p.get("name")
    ]
    for profile in profiles:
        if not isinstance(profile, dict) or not profile.get("name"):
            continue
        name = str(profile["name"])
        try:
            start_min = parse_hhmm_to_minute(str(profile.get("start_time", "00:00")))
            end_min = parse_hhmm_to_minute(str(profile.get("end_time", "24:00")))
        except ValueError as exc:
            warn(f"profile '{name}' の時刻範囲が不正のため depth 集計をスキップ: {exc}")
            continue
        profile_rows = [
            r for r in rows
            if r.get("has_depth_data") and _row_in_profile_window(r, start_min, end_min)
        ]
        per_profile[name] = summarize_market_depth_group(
            rows=profile_rows,
            depth_days=depth_days,
            requested_days=requested_days,
            window_dates=window_dates,
        )

    for name in sorted(set(profile_names) - set(per_profile.keys())):
        per_profile[name] = summarize_market_depth_group(
            rows=[],
            depth_days=depth_days,
            requested_days=requested_days,
            window_dates=window_dates,
        )

    return {
        "requested_days": requested_days,
        "overall": overall,
        "per_profile": per_profile,
    }


def summarize_market_volatility_group(
    rows: List[Dict[str, Any]],
    volatility_days: Set[date],
    requested_days: int,
    window_dates: Set[date],
) -> Dict[str, Any]:
    """
    volatility_5min_range_pct 列が存在した日のスナップショットのみ集計する。
    列が無い日は「データ無し」として除外する。
    列はあるが値が null の行は平均・最大の計算から除外する。
    """
    eligible_dates = volatility_days & window_dates
    volatility_actual_days = len(eligible_dates)
    eligible_rows = [
        r for r in rows
        if r.get("has_volatility_data") and r.get("date") in eligible_dates
    ]

    values = [
        float(v)
        for r in eligible_rows
        for v in [r.get("volatility_5min_range_pct")]
        if v is not None
    ]

    if not eligible_rows:
        return {
            "requested_days": requested_days,
            "volatility_actual_days": volatility_actual_days,
            "volatility_confidence": classify_volatility_confidence(
                0, volatility_actual_days, requested_days
            ),
            "avg_volatility_5min_range_pct": None,
            "max_volatility_5min_range_pct": None,
        }

    return {
        "requested_days": requested_days,
        "volatility_actual_days": volatility_actual_days,
        "volatility_confidence": classify_volatility_confidence(
            len(values), volatility_actual_days, requested_days
        ),
        "avg_volatility_5min_range_pct": mean_or_none(values),
        "max_volatility_5min_range_pct": max_or_none(values),
    }


def build_market_volatility_summary(
    target_date: date,
    requested_days: int,
    profiles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    dates = daterange_desc(target_date, requested_days)
    window_dates = set(dates)
    rows, _actual_days, _trades_days, _depth_days, volatility_days = load_market_rows(dates)

    overall = summarize_market_volatility_group(
        rows=rows,
        volatility_days=volatility_days,
        requested_days=requested_days,
        window_dates=window_dates,
    )

    per_profile: Dict[str, Any] = {}
    profile_names = [
        str(p.get("name"))
        for p in profiles
        if isinstance(p, dict) and p.get("name")
    ]
    for profile in profiles:
        if not isinstance(profile, dict) or not profile.get("name"):
            continue
        name = str(profile["name"])
        try:
            start_min = parse_hhmm_to_minute(str(profile.get("start_time", "00:00")))
            end_min = parse_hhmm_to_minute(str(profile.get("end_time", "24:00")))
        except ValueError as exc:
            warn(f"profile '{name}' の時刻範囲が不正のため volatility 集計をスキップ: {exc}")
            continue
        profile_rows = [
            r for r in rows
            if r.get("has_volatility_data") and _row_in_profile_window(r, start_min, end_min)
        ]
        per_profile[name] = summarize_market_volatility_group(
            rows=profile_rows,
            volatility_days=volatility_days,
            requested_days=requested_days,
            window_dates=window_dates,
        )

    for name in sorted(set(profile_names) - set(per_profile.keys())):
        per_profile[name] = summarize_market_volatility_group(
            rows=[],
            volatility_days=volatility_days,
            requested_days=requested_days,
            window_dates=window_dates,
        )

    return {
        "requested_days": requested_days,
        "overall": overall,
        "per_profile": per_profile,
    }


def build_regime_reference(target_date: date, requested_days: int = 90) -> Dict[str, Any]:
    dates = daterange_desc(target_date, requested_days)
    rows, actual_day_set, _trades_days, _depth_days, _volatility_days = load_market_rows(dates)
    trade_rows, _ = load_trade_rows(dates)

    by_day: Dict[date, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_day[row["date"]].append(row)

    daily: List[Dict[str, Any]] = []
    daily_change_rates: List[float] = []
    all_spreads: List[float] = []
    all_imbalances: List[float] = []
    daily_metrics_by_date: Dict[date, Dict[str, Optional[float]]] = {}

    for d in sorted(by_day.keys()):
        day_rows = by_day[d]
        mid_prices = [r["mid_price"] for r in day_rows if r["mid_price"] > 0]
        spreads = [r["spread_pct"] for r in day_rows]
        imbalances = [r["imbalance"] for r in day_rows]
        if not mid_prices:
            continue

        max_mid = max(mid_prices)
        min_mid = min(mid_prices)
        change_rate = ((max_mid - min_mid) / min_mid) if min_mid > 0 else None
        if change_rate is not None:
            daily_change_rates.append(change_rate)
        all_spreads.extend(spreads)
        all_imbalances.extend(imbalances)
        spread_avg = mean_or_none(spreads)
        imbalance_avg = mean_or_none(imbalances)
        daily_metrics_by_date[d] = {
            "volatility_pct": change_rate,
            "spread_pct_avg": spread_avg,
            "imbalance_avg": imbalance_avg,
        }

        daily.append(
            {
                "date": d.isoformat(),
                "mid_price_max": max_mid,
                "mid_price_min": min_mid,
                "mid_price_change_rate": change_rate,
                "spread_pct_avg": spread_avg,
                "spread_pct_std": stdev_or_none(spreads),
                "imbalance_avg": imbalance_avg,
            }
        )

    summary = {
        "actual_days": len(actual_day_set),
        "mid_price_change_rate_avg": mean_or_none(daily_change_rates),
        "mid_price_change_rate_std": stdev_or_none(daily_change_rates),
        "spread_pct_avg": mean_or_none(all_spreads),
        "spread_pct_std": stdev_or_none(all_spreads),
        "imbalance_avg": mean_or_none(all_imbalances),
        "imbalance_std": stdev_or_none(all_imbalances),
    }

    trade_count_by_date: Dict[date, int] = defaultdict(int)
    for row in trade_rows:
        if row["reason"] == "ENTRY":
            trade_count_by_date[row["date"]] += 1

    def _block(label: str, start_offset: int, end_offset: int) -> Dict[str, Any]:
        block_dates = {
            target_date - timedelta(days=offset)
            for offset in range(start_offset, end_offset + 1)
        }
        block_day_metrics = [
            daily_metrics_by_date[d]
            for d in sorted(block_dates)
            if d in daily_metrics_by_date
        ]
        vol_values = [m["volatility_pct"] for m in block_day_metrics if m["volatility_pct"] is not None]
        spread_values = [m["spread_pct_avg"] for m in block_day_metrics if m["spread_pct_avg"] is not None]
        imbalance_values = [m["imbalance_avg"] for m in block_day_metrics if m["imbalance_avg"] is not None]
        trade_count = sum(trade_count_by_date.get(d, 0) for d in block_dates)
        return {
            "label": label,
            "days_offset": f"{start_offset}-{end_offset}",
            "requested_days": 30,
            "actual_days": len(block_day_metrics),
            "avg_volatility_pct": mean_or_none(vol_values),
            "avg_spread_pct": mean_or_none(spread_values),
            "avg_imbalance": mean_or_none(imbalance_values),
            "trade_count": trade_count,
        }

    blocks = [
        _block("recent_30d", 0, 29),
        _block("mid_30d", 30, 59),
        _block("older_30d", 60, 89),
    ]

    return {
        "requested_days": requested_days,
        "actual_days": len(actual_day_set),
        "daily": daily,
        "blocks": blocks,
        "summary": summary,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI議論用サマリーJSONを生成します。")
    parser.add_argument(
        "--target-date",
        help="対象日 (YYYY-MM-DD)。省略時は前日。",
    )
    return parser.parse_args()


def resolve_target_date(raw_value: Optional[str]) -> date:
    if not raw_value:
        return datetime.now().date() - timedelta(days=1)
    try:
        return datetime.strptime(raw_value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"--target-date の形式が不正です: {raw_value}") from exc


def main() -> int:
    args = parse_args()
    target_date = resolve_target_date(args.target_date)

    try:
        current_config = read_json_file(CONFIG_PATH, required=True)
    except Exception as exc:
        try:
            send_telegram_message(
                "\n".join(
                    [
                        "[BTC AI議論] エラー",
                        f"{target_date.isoformat()}: config/config.jsonの読み込みに失敗し、",
                        "集計処理を中断しました。",
                        f"エラー内容: {exc}",
                    ]
                )
            )
        except Exception as notify_exc:  # pragma: no cover - defensive fallback
            warn(f"Telegram通知の送信に失敗しました: {notify_exc}")
        print(
            f"[ERROR] config/config.json の読み込みに失敗したため終了します: {exc}",
            file=sys.stderr,
        )
        return 1

    recent_config_changes = read_jsonl_tail(CONFIG_HISTORY_PATH, limit=5)
    past_validation_failures = read_jsonl_tail(VALIDATION_FAILURES_PATH, limit=5)
    recent_change_outcomes = read_jsonl_tail(CHANGE_OUTCOMES_PATH, limit=5)
    profile_defs = [
        p for p in (current_config or {}).get("profiles", [])
        if isinstance(p, dict) and p.get("name")
    ]
    profile_names = [str(p.get("name")) for p in profile_defs]

    anomaly_window = build_trade_window_summary(target_date, requested_days=1, profile_names=profile_names)
    anomaly_window["usage_note"] = "この窓は異常検知専用。単体でルール変更の判断材料にしないこと。"
    anomaly_window.pop("_rows", None)
    anomaly_window.pop("_actual_day_set", None)

    rule_review_window = build_trade_window_summary(target_date, requested_days=14, profile_names=profile_names)
    rule_review_window.pop("_rows", None)
    rule_review_window.pop("_actual_day_set", None)
    rule_review_window["market_trades"] = build_market_trades_summary(
        target_date, requested_days=14, profiles=profile_defs
    )
    rule_review_window["market_depth"] = build_market_depth_summary(
        target_date, requested_days=14, profiles=profile_defs
    )
    rule_review_window["market_volatility"] = build_market_volatility_summary(
        target_date, requested_days=14, profiles=profile_defs
    )

    stability_window = build_trade_window_summary(target_date, requested_days=30, profile_names=profile_names)
    add_weekly_breakdown(stability_window, target_date=target_date)
    stability_window["market_trades"] = build_market_trades_summary(
        target_date, requested_days=30, profiles=profile_defs
    )
    stability_window["market_depth"] = build_market_depth_summary(
        target_date, requested_days=30, profiles=profile_defs
    )
    stability_window["market_volatility"] = build_market_volatility_summary(
        target_date, requested_days=30, profiles=profile_defs
    )

    regime_reference_window = build_regime_reference(target_date, requested_days=90)
    regime_reference_window["market_trades"] = build_market_trades_summary(
        target_date, requested_days=90, profiles=profile_defs
    )
    regime_reference_window["market_depth"] = build_market_depth_summary(
        target_date, requested_days=90, profiles=profile_defs
    )
    regime_reference_window["market_volatility"] = build_market_volatility_summary(
        target_date, requested_days=90, profiles=profile_defs
    )

    output = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "target_date": target_date.isoformat(),
        "current_config": current_config,
        "recent_config_changes": recent_config_changes,
        "past_validation_failures": past_validation_failures,
        "recent_change_outcomes": recent_change_outcomes,
        "windows": {
            "anomaly_check": anomaly_window,
            "rule_review": rule_review_window,
            "stability_check": stability_window,
            "regime_reference": regime_reference_window,
        },
    }

    output_path = LOG_DIR / f"ai_review_summary_{target_date.isoformat()}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] AI review summary generated: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
