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


def load_market_rows(dates: List[date]) -> Tuple[List[Dict[str, Any]], Set[date]]:
    rows: List[Dict[str, Any]] = []
    actual_days: Set[date] = set()
    for d in dates:
        path = LOG_DIR / f"market_snapshot_{d.isoformat()}.csv"
        if not path.exists():
            continue
        try:
            with path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(
                        {
                            "date": d,
                            "mid_price": safe_float(row.get("mid_price"), 0.0),
                            "spread_pct": safe_float(row.get("spread_pct"), 0.0),
                            "imbalance": safe_float(row.get("imbalance"), 0.0),
                        }
                    )
            actual_days.add(d)
        except Exception as exc:
            warn(f"相場スナップショットの読み込みに失敗。日次をスキップします: {path.name} ({exc})")
    return rows, actual_days


def build_regime_reference(target_date: date, requested_days: int = 90) -> Dict[str, Any]:
    dates = daterange_desc(target_date, requested_days)
    rows, actual_day_set = load_market_rows(dates)
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
    profile_names = [
        str(p.get("name"))
        for p in (current_config or {}).get("profiles", [])
        if isinstance(p, dict) and p.get("name")
    ]

    anomaly_window = build_trade_window_summary(target_date, requested_days=1, profile_names=profile_names)
    anomaly_window["usage_note"] = "この窓は異常検知専用。単体でルール変更の判断材料にしないこと。"
    anomaly_window.pop("_rows", None)
    anomaly_window.pop("_actual_day_set", None)

    rule_review_window = build_trade_window_summary(target_date, requested_days=14, profile_names=profile_names)
    rule_review_window.pop("_rows", None)
    rule_review_window.pop("_actual_day_set", None)

    stability_window = build_trade_window_summary(target_date, requested_days=30, profile_names=profile_names)
    add_weekly_breakdown(stability_window, target_date=target_date)

    regime_reference_window = build_regime_reference(target_date, requested_days=90)

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
