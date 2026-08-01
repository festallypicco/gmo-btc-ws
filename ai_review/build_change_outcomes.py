from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AI_REVIEW_DIR = PROJECT_ROOT / "ai_review"
if str(AI_REVIEW_DIR) not in sys.path:
    sys.path.insert(0, str(AI_REVIEW_DIR))

from build_ai_review_summary import classify_confidence  # noqa: E402

LOG_DIR = PROJECT_ROOT / "log"
CONFIG_HISTORY_PATH = LOG_DIR / "config_history.jsonl"
CHANGE_OUTCOMES_PATH = LOG_DIR / "change_outcomes.jsonl"

EXIT_REASONS = {"TAKE_PROFIT", "STOP_LOSS", "FORCE_CLOSE_MAINTENANCE"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track outcomes for past config changes")
    parser.add_argument(
        "--now",
        help="評価基準時刻(ISO8601)。省略時は現在時刻。",
    )
    return parser.parse_args()


def _to_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def _write_jsonl_atomic(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp_path, path)


def _iter_trade_rows() -> Iterable[Dict[str, Any]]:
    for path in sorted(LOG_DIR.glob("realtime_trading_log_*.csv")):
        try:
            with path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts = row.get("timestamp")
                    if not ts:
                        continue
                    try:
                        ts_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    yield {
                        "timestamp": ts_dt,
                        "date": ts_dt.date(),
                        "config_version": str(row.get("config_version", "")).strip(),
                        "profile_name": str(row.get("profile_name", "")).strip(),
                        "reason": str(row.get("reason", "")).strip().upper(),
                        "pnl": float(row.get("pnl") or 0.0),
                    }
        except Exception:
            continue


def _iter_market_rows() -> Iterable[Dict[str, Any]]:
    for path in sorted(LOG_DIR.glob("market_snapshot_*.csv")):
        try:
            with path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts = row.get("timestamp")
                    mid = row.get("mid_price")
                    if not ts or mid is None:
                        continue
                    try:
                        ts_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                        mid_f = float(mid)
                    except ValueError:
                        continue
                    if mid_f <= 0:
                        continue
                    yield {"timestamp": ts_dt, "mid_price": mid_f}
        except Exception:
            continue


def _extract_profile_changes(config_history_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    changes: List[Dict[str, Any]] = []
    for row in config_history_rows:
        timestamp = row.get("timestamp")
        version = row.get("version")
        previous_version = row.get("previous_version")
        if not isinstance(timestamp, str) or not isinstance(version, str):
            continue
        if not isinstance(previous_version, str) or not previous_version:
            continue
        changed_profiles = row.get("changed_profiles", {})
        if isinstance(changed_profiles, dict):
            for profile_name, detail in changed_profiles.items():
                changed_fields = {}
                if isinstance(detail, dict):
                    changed_fields = detail.get("changed_fields", {}) or {}
                changes.append(
                    {
                        "change_id": f"{timestamp}|{profile_name}|{version}",
                        "timestamp": timestamp,
                        "change_ts": _to_dt(timestamp),
                        "profile_name": profile_name,
                        "version": version,
                        "previous_version": previous_version,
                        "change_type": "changed",
                        "changed_fields": changed_fields,
                        "reason": row.get("reason", ""),
                    }
                )
        for profile_name in row.get("added_profiles", []) or []:
            changes.append(
                {
                    "change_id": f"{timestamp}|{profile_name}|{version}",
                    "timestamp": timestamp,
                    "change_ts": _to_dt(timestamp),
                    "profile_name": profile_name,
                    "version": version,
                    "previous_version": previous_version,
                    "change_type": "added",
                    "changed_fields": {},
                    "reason": row.get("reason", ""),
                }
            )
        for profile_name in row.get("removed_profiles", []) or []:
            changes.append(
                {
                    "change_id": f"{timestamp}|{profile_name}|{version}",
                    "timestamp": timestamp,
                    "change_ts": _to_dt(timestamp),
                    "profile_name": profile_name,
                    "version": version,
                    "previous_version": previous_version,
                    "change_type": "removed",
                    "changed_fields": {},
                    "reason": row.get("reason", ""),
                }
            )
    changes.sort(key=lambda x: x["change_ts"])
    return changes


def _next_profile_change_ts(
    all_changes: List[Dict[str, Any]],
    profile_name: str,
    current_ts: datetime,
) -> Optional[datetime]:
    for c in all_changes:
        if c["profile_name"] != profile_name:
            continue
        if c["change_ts"] > current_ts:
            return c["change_ts"]
    return None


def _previous_profile_change_ts(
    all_changes: List[Dict[str, Any]],
    profile_name: str,
    current_ts: datetime,
) -> Optional[datetime]:
    prev: Optional[datetime] = None
    for c in all_changes:
        if c["profile_name"] != profile_name:
            continue
        c_ts = c["change_ts"]
        if c_ts >= current_ts:
            continue
        if prev is None or c_ts > prev:
            prev = c_ts
    return prev


def _btc_change_pct(
    market_rows: List[Dict[str, Any]],
    start_ts: datetime,
    end_ts: datetime,
) -> Optional[float]:
    if end_ts < start_ts:
        return None
    mids = [
        r["mid_price"]
        for r in market_rows
        if start_ts <= r["timestamp"] <= end_ts
    ]
    if len(mids) < 2:
        return None
    first = mids[0]
    last = mids[-1]
    if first <= 0:
        return None
    return (last - first) / first


def _calc_metrics(
    rows: List[Dict[str, Any]],
    market_rows: List[Dict[str, Any]],
    requested_days: int,
) -> Dict[str, Any]:
    exit_rows = [r for r in rows if r["reason"] in EXIT_REASONS]
    trade_count = len(exit_rows)
    win_count = sum(1 for r in exit_rows if r["pnl"] > 0)
    total_pnl = sum(r["pnl"] for r in exit_rows)
    win_rate = (win_count / trade_count) if trade_count > 0 else None
    actual_days = len({r["date"] for r in rows})
    confidence = classify_confidence(
        exit_count=trade_count,
        actual_days=actual_days,
        requested_days=requested_days,
    )

    btc_price_change_pct = None
    if rows:
        start_ts = min(r["timestamp"] for r in rows)
        end_ts = max(r["timestamp"] for r in rows)
        btc_price_change_pct = _btc_change_pct(market_rows, start_ts, end_ts)

    return {
        "trade_count": trade_count,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "btc_price_change_pct": btc_price_change_pct,
        "actual_days": actual_days,
        "requested_days": requested_days,
        "confidence": confidence,
    }


def _select_rows_for_window(
    trade_rows: List[Dict[str, Any]],
    profile_name: str,
    before_version: str,
    after_version: str,
    change_ts: datetime,
    end_ts: datetime,
    requested_days: int,
    profile_effective_start_ts: Optional[datetime],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    before_start = change_ts - timedelta(days=requested_days)
    if profile_effective_start_ts is not None and profile_effective_start_ts > before_start:
        before_start = profile_effective_start_ts
    before_requested_days = max(
        1,
        int((change_ts - before_start).total_seconds() / 86400),
    )
    before_rows = [
        r
        for r in trade_rows
        if r["profile_name"] == profile_name
        and r["config_version"] == before_version
        and before_start <= r["timestamp"] <= change_ts
    ]
    after_rows = [
        r
        for r in trade_rows
        if r["profile_name"] == profile_name
        and r["config_version"] == after_version
        and change_ts <= r["timestamp"] <= end_ts
    ]
    return before_rows, after_rows, before_requested_days


def _build_evaluation(
    trade_rows: List[Dict[str, Any]],
    market_rows: List[Dict[str, Any]],
    profile_name: str,
    previous_version: str,
    version: str,
    change_ts: datetime,
    end_ts: datetime,
    requested_days: int,
    label: str,
    profile_effective_start_ts: Optional[datetime],
) -> Dict[str, Any]:
    before_rows, after_rows, before_requested_days = _select_rows_for_window(
        trade_rows=trade_rows,
        profile_name=profile_name,
        before_version=previous_version,
        after_version=version,
        change_ts=change_ts,
        end_ts=end_ts,
        requested_days=requested_days,
        profile_effective_start_ts=profile_effective_start_ts,
    )
    before = _calc_metrics(before_rows, market_rows, requested_days=before_requested_days)
    after = _calc_metrics(after_rows, market_rows, requested_days=requested_days)
    trade_count_diff_pct = None
    if before["trade_count"] > 0:
        trade_count_diff_pct = (after["trade_count"] - before["trade_count"]) / before["trade_count"]
    win_rate_diff = None
    if before["win_rate"] is not None and after["win_rate"] is not None:
        win_rate_diff = after["win_rate"] - before["win_rate"]
    return {
        "type": label,
        "window_end": end_ts.isoformat(timespec="seconds"),
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
        "before": before,
        "after": after,
        "comparison": {
            "win_rate_diff": win_rate_diff,
            "trade_count_diff_pct": trade_count_diff_pct,
            "total_pnl_diff": after["total_pnl"] - before["total_pnl"],
        },
        "confidence": after["confidence"],
    }


def _merge_outcomes(
    existing_rows: List[Dict[str, Any]],
    profile_changes: List[Dict[str, Any]],
    trade_rows: List[Dict[str, Any]],
    market_rows: List[Dict[str, Any]],
    now_dt: datetime,
) -> List[Dict[str, Any]]:
    existing_map = {
        str(r.get("change_id")): r
        for r in existing_rows
        if isinstance(r, dict) and r.get("change_id")
    }
    result: List[Dict[str, Any]] = []

    for change in profile_changes:
        change_id = change["change_id"]
        row = dict(existing_map.get(change_id, {}))
        row.update(
            {
                "change_id": change_id,
                "timestamp": change["timestamp"],
                "profile_name": change["profile_name"],
                "version": change["version"],
                "previous_version": change["previous_version"],
                "change_type": change["change_type"],
                "changed_fields": change["changed_fields"],
                "reason": change["reason"],
            }
        )

        change_ts = change["change_ts"]
        next_change_ts = _next_profile_change_ts(profile_changes, change["profile_name"], change_ts)
        row["next_change_timestamp"] = (
            next_change_ts.isoformat(timespec="seconds") if next_change_ts else None
        )
        prev_change_ts = _previous_profile_change_ts(profile_changes, change["profile_name"], change_ts)
        row["previous_change_timestamp"] = (
            prev_change_ts.isoformat(timespec="seconds") if prev_change_ts else None
        )

        if row.get("provisional_evaluation") is None and now_dt >= (change_ts + timedelta(days=7)):
            end_ts = min(now_dt, change_ts + timedelta(days=7))
            row["provisional_evaluation"] = _build_evaluation(
                trade_rows=trade_rows,
                market_rows=market_rows,
                profile_name=change["profile_name"],
                previous_version=change["previous_version"],
                version=change["version"],
                change_ts=change_ts,
                end_ts=end_ts,
                requested_days=7,
                label="provisional",
                profile_effective_start_ts=prev_change_ts,
            )

        final_due = False
        final_end_ts = min(now_dt, change_ts + timedelta(days=30))
        if next_change_ts is not None and now_dt >= next_change_ts:
            final_due = True
            final_end_ts = min(now_dt, next_change_ts)
        elif now_dt >= (change_ts + timedelta(days=30)):
            final_due = True

        if row.get("final_evaluation") is None and final_due:
            requested_days = max(1, (final_end_ts.date() - change_ts.date()).days)
            row["final_evaluation"] = _build_evaluation(
                trade_rows=trade_rows,
                market_rows=market_rows,
                profile_name=change["profile_name"],
                previous_version=change["previous_version"],
                version=change["version"],
                change_ts=change_ts,
                end_ts=final_end_ts,
                requested_days=requested_days,
                label="final",
                profile_effective_start_ts=prev_change_ts,
            )

        result.append(row)
    result.sort(key=lambda r: (str(r.get("timestamp", "")), str(r.get("profile_name", ""))))
    return result


def _resolve_now(raw_now: Optional[str]) -> datetime:
    if not raw_now:
        return datetime.now()
    return _to_dt(raw_now)


def main() -> int:
    args = parse_args()
    now_dt = _resolve_now(args.now)
    config_history_rows = _read_jsonl(CONFIG_HISTORY_PATH)
    profile_changes = _extract_profile_changes(config_history_rows)
    if not profile_changes:
        _write_jsonl_atomic(CHANGE_OUTCOMES_PATH, [])
        print("[INFO] no applicable config changes found for outcome tracking")
        return 0

    existing_outcomes = _read_jsonl(CHANGE_OUTCOMES_PATH)
    trade_rows = list(_iter_trade_rows())
    market_rows = list(_iter_market_rows())

    merged_rows = _merge_outcomes(
        existing_rows=existing_outcomes,
        profile_changes=profile_changes,
        trade_rows=trade_rows,
        market_rows=market_rows,
        now_dt=now_dt,
    )
    _write_jsonl_atomic(CHANGE_OUTCOMES_PATH, merged_rows)
    print(f"[INFO] change outcomes updated: {CHANGE_OUTCOMES_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

