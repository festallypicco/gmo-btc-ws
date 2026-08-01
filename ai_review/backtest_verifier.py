from __future__ import annotations

import csv
from dataclasses import fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from strategy_logic import (  # noqa: E402
    OrderbookSnapshot,
    PositionState,
    Signal,
    StrategyConfig,
    evaluate,
)

BACKTEST_LOOKBACK_DAYS = 30
BACKTEST_MIN_TRADES = 5
BACKTEST_DEGRADE_RATIO = 0.9
BACKTESTABLE_KEYS = {"imbalance_entry_threshold", "take_profit_pct", "stop_loss_pct"}


def _parse_ts(raw: str) -> Optional[datetime]:
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


def _hhmm_to_minute(hhmm: str) -> int:
    text = str(hhmm).strip()
    if text == "24:00":
        return 1440
    h, m = text.split(":", 1)
    return int(h) * 60 + int(m)


def load_market_snapshot_rows(log_dir: Path, target_date: str, lookback_days: int) -> Dict[str, List[dict]]:
    rows_by_day: Dict[str, List[dict]] = {}
    base_day = date.fromisoformat(target_date)
    for offset in range(lookback_days):
        d = base_day - timedelta(days=offset)
        day_str = d.isoformat()
        path = log_dir / f"market_snapshot_{day_str}.csv"
        if not path.exists():
            continue
        day_rows: List[dict] = []
        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = str(row.get("timestamp") or "").strip()
                if not ts:
                    continue
                try:
                    bid = float(row.get("best_bid_price") or 0.0)
                    bid_size = float(row.get("best_bid_size") or 0.0)
                    ask = float(row.get("best_ask_price") or 0.0)
                    ask_size = float(row.get("best_ask_size") or 0.0)
                except (TypeError, ValueError):
                    continue
                day_rows.append(
                    {
                        "timestamp": ts,
                        "best_bid_price": bid,
                        "best_bid_size": bid_size,
                        "best_ask_price": ask,
                        "best_ask_size": ask_size,
                    }
                )
        day_rows.sort(key=lambda x: (_parse_ts(str(x.get("timestamp") or "")) or datetime.min))
        rows_by_day[day_str] = day_rows
    return rows_by_day


def filter_rows_by_time_window(rows_by_day: Dict[str, List[dict]], start_time: str, end_time: str) -> Dict[str, List[dict]]:
    start_min = _hhmm_to_minute(start_time)
    end_min = _hhmm_to_minute(end_time)
    out: Dict[str, List[dict]] = {}
    for day, rows in rows_by_day.items():
        kept: List[dict] = []
        for row in rows:
            ts = _parse_ts(str(row.get("timestamp") or ""))
            if ts is None:
                continue
            minute = ts.hour * 60 + ts.minute
            if start_min <= minute < end_min:
                kept.append(row)
        out[day] = kept
    return out


def build_strategy_config(profile_dict: dict, overrides: Dict[str, float]) -> StrategyConfig:
    kwargs: Dict[str, Any] = {}
    defaults = StrategyConfig()
    for f in fields(StrategyConfig):
        key = f.name
        val = profile_dict.get(key, getattr(defaults, key))
        if key in overrides:
            val = overrides[key]
        kwargs[key] = val
    return StrategyConfig(**kwargs)


def _simulate_pending_fill(position: dict, snap: OrderbookSnapshot, cfg: StrategyConfig) -> Tuple[str, Optional[dict]]:
    # NOTE: virtual_trader.py の _check_pending_fill 条件式を複製。
    # 本番側を変更した場合はこちらも要修正。
    side = str(position.get("side"))
    entry_price = float(position.get("entry_price", 0.0))
    filled = (
        (side == "LONG" and snap.best_bid_price >= entry_price)
        or (side == "SHORT" and snap.best_ask_price <= entry_price)
    )
    if filled:
        if side == "LONG":
            tp_price = entry_price * (1 + cfg.take_profit_pct)
        else:
            tp_price = entry_price * (1 - cfg.take_profit_pct)
        next_pos = dict(position)
        next_pos["is_pending"] = False
        next_pos["exit_price_target"] = tp_price
        return "held", next_pos

    imbalance_reversed = (
        (side == "LONG" and snap.imbalance < cfg.imbalance_cancel_threshold)
        or (side == "SHORT" and snap.imbalance > cfg.imbalance_cancel_threshold)
    )
    spread_too_wide = snap.spread >= cfg.max_allowed_spread
    if imbalance_reversed or spread_too_wide:
        return "cancelled", None
    return "held", position


def _simulate_active_position(position: dict, snap: OrderbookSnapshot, cfg: StrategyConfig) -> Tuple[Optional[str], Optional[float]]:
    # NOTE: virtual_trader.py の _check_active_position/_exit_* 条件式を複製。
    # 本番側を変更した場合はこちらも要修正。
    side = str(position.get("side"))
    entry = float(position.get("entry_price", 0.0))
    target = float(position.get("exit_price_target", 0.0))

    if side == "LONG":
        sl_thresh = entry * (1 - cfg.stop_loss_pct)
        if target > 0 and snap.best_bid_price >= target:
            return "TAKE_PROFIT", float(cfg.take_profit_pct)
        if snap.best_bid_price <= sl_thresh:
            pnl_pct = (snap.best_bid_price - entry) / entry if entry > 0 else 0.0
            return "STOP_LOSS", pnl_pct
        return None, None

    if side == "SHORT":
        sl_thresh = entry * (1 + cfg.stop_loss_pct)
        if target > 0 and snap.best_ask_price <= target:
            return "TAKE_PROFIT", float(cfg.take_profit_pct)
        if snap.best_ask_price >= sl_thresh:
            pnl_pct = (entry - snap.best_ask_price) / entry if entry > 0 else 0.0
            return "STOP_LOSS", pnl_pct
        return None, None

    return None, None


def _build_snap(row: dict) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_bid_price=float(row["best_bid_price"]),
        best_bid_size=float(row["best_bid_size"]),
        best_ask_price=float(row["best_ask_price"]),
        best_ask_size=float(row["best_ask_size"]),
    )


def simulate_profile(rows_by_day: Dict[str, List[dict]], cfg: StrategyConfig) -> Dict[str, Any]:
    pnl_list: List[float] = []

    for _day, rows in sorted(rows_by_day.items()):
        position: Optional[dict] = None
        for row in rows:
            snap = _build_snap(row)
            if position is None:
                sig = evaluate(snap, PositionState(), cfg)
                if sig == Signal.BUY_ENTRY:
                    position = {
                        "side": "LONG",
                        "entry_price": snap.best_bid_price + cfg.maker_price_offset_jpy,
                        "is_pending": True,
                        "exit_price_target": 0.0,
                    }
                elif sig == Signal.SELL_ENTRY:
                    position = {
                        "side": "SHORT",
                        "entry_price": snap.best_ask_price - cfg.maker_price_offset_jpy,
                        "is_pending": True,
                        "exit_price_target": 0.0,
                    }
                continue

            if bool(position.get("is_pending")):
                state, next_pos = _simulate_pending_fill(position, snap, cfg)
                if state == "cancelled":
                    position = None
                else:
                    position = next_pos
                continue

            event, pnl_pct = _simulate_active_position(position, snap, cfg)
            if event in {"TAKE_PROFIT", "STOP_LOSS"} and pnl_pct is not None:
                pnl_list.append(float(pnl_pct))
                position = None

    trade_count = len(pnl_list)
    win_count = sum(1 for x in pnl_list if x > 0)
    total_pnl_pct = float(sum(pnl_list))
    win_rate: Optional[float] = (win_count / trade_count) if trade_count > 0 else None

    return {
        "trade_count": trade_count,
        "total_pnl_pct": total_pnl_pct,
        "win_count": win_count,
        "win_rate": win_rate,
    }


def run_backtest_check(
    profile_name: str,
    current_profile: dict,
    proposed_profile: dict,
    log_dir: Path,
    target_date: str,
) -> Dict[str, Any]:
    changed_keys: Set[str] = set()
    for key in BACKTESTABLE_KEYS:
        old = current_profile.get(key)
        new = proposed_profile.get(key)
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            continue
        if float(old) != float(new):
            changed_keys.add(key)

    if not changed_keys:
        return {"ran": False}

    rows = load_market_snapshot_rows(
        log_dir=log_dir,
        target_date=target_date,
        lookback_days=BACKTEST_LOOKBACK_DAYS,
    )
    filtered_rows = filter_rows_by_time_window(
        rows,
        start_time=str(current_profile.get("start_time", "00:00")),
        end_time=str(current_profile.get("end_time", "24:00")),
    )

    old_cfg = build_strategy_config(current_profile, {})
    new_cfg = build_strategy_config(
        current_profile,
        {k: float(proposed_profile[k]) for k in changed_keys},
    )

    old_result = simulate_profile(filtered_rows, old_cfg)
    new_result = simulate_profile(filtered_rows, new_cfg)

    if (
        int(old_result.get("trade_count", 0)) < BACKTEST_MIN_TRADES
        or int(new_result.get("trade_count", 0)) < BACKTEST_MIN_TRADES
    ):
        return {
            "ran": True,
            "gated": False,
            "reason": "insufficient_data",
            "old": old_result,
            "new": new_result,
            "changed_keys": sorted(changed_keys),
        }

    old_total = float(old_result.get("total_pnl_pct", 0.0))
    new_total = float(new_result.get("total_pnl_pct", 0.0))
    if old_total > 0:
        gated = new_total < old_total * BACKTEST_DEGRADE_RATIO
    else:
        gated = new_total < old_total

    return {
        "ran": True,
        "gated": gated,
        "old": old_result,
        "new": new_result,
        "changed_keys": sorted(changed_keys),
        "profile_name": profile_name,
    }
