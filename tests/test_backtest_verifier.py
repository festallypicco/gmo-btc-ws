from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Dict, List

import pytest

_ROOT_DIR = Path(__file__).resolve().parent.parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
_AI_REVIEW_DIR = _ROOT_DIR / "ai_review"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))
if str(_AI_REVIEW_DIR) not in sys.path:
    sys.path.insert(0, str(_AI_REVIEW_DIR))

import backtest_verifier as bv  # noqa: E402
from backtest_verifier import (  # noqa: E402
    build_strategy_config,
    filter_rows_by_time_window,
    run_backtest_check,
    simulate_profile,
)
from strategy_logic import StrategyConfig  # noqa: E402


def _row(ts: str, bid: float, ask: float, bid_size: float = 2.0, ask_size: float = 1.0) -> Dict[str, float | str]:
    return {
        "timestamp": ts,
        "best_bid_price": bid,
        "best_bid_size": bid_size,
        "best_ask_price": ask,
        "best_ask_size": ask_size,
    }


def _base_profile(**overrides: float | str) -> dict:
    profile = {
        "name": "full_day",
        "start_time": "00:00",
        "end_time": "24:00",
        "imbalance_entry_threshold": 0.55,
        "min_entry_wall_btc": 0.05,
        "min_valid_wall_btc": 0.1,
        "max_spread_pct": 0.02,
        "max_allowed_spread": 5000.0,
        "imbalance_cancel_threshold": 0.50,
        "take_profit_pct": 0.01,
        "stop_loss_pct": 0.01,
        "maker_price_offset_jpy": 1.0,
        "max_order_size_btc": 0.1,
    }
    profile.update(overrides)
    return profile


def _write_market_snapshot(path: Path, rows: List[Dict[str, float | str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "best_bid_price",
                "best_bid_size",
                "best_ask_price",
                "best_ask_size",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def test_filter_rows_by_time_window_in_out_and_boundary() -> None:
    rows_by_day = {
        "2026-07-10": [
            _row("2026-07-10 08:59:59", 100.0, 101.0),
            _row("2026-07-10 09:00:00", 101.0, 102.0),
            _row("2026-07-10 16:59:59", 102.0, 103.0),
            _row("2026-07-10 17:00:00", 103.0, 104.0),
        ]
    }
    filtered = filter_rows_by_time_window(rows_by_day, "09:00", "17:00")
    timestamps = [r["timestamp"] for r in filtered["2026-07-10"]]
    assert "2026-07-10 08:59:59" not in timestamps
    assert "2026-07-10 09:00:00" in timestamps
    assert "2026-07-10 16:59:59" in timestamps
    assert "2026-07-10 17:00:00" not in timestamps


def test_build_strategy_config_uses_profile_overrides_and_defaults() -> None:
    profile = {
        "imbalance_entry_threshold": 0.60,
        "take_profit_pct": 0.0025,
    }
    cfg = build_strategy_config(profile, {"take_profit_pct": 0.0030})
    defaults = StrategyConfig()
    assert cfg.imbalance_entry_threshold == pytest.approx(0.60)
    assert cfg.take_profit_pct == pytest.approx(0.0030)
    assert cfg.stop_loss_pct == pytest.approx(defaults.stop_loss_pct)
    assert cfg.max_order_size_btc == pytest.approx(defaults.max_order_size_btc)


def test_simulate_profile_generates_buy_entry_and_take_profit() -> None:
    cfg = StrategyConfig(
        imbalance_entry_threshold=0.55,
        min_entry_wall_btc=0.05,
        max_spread_pct=0.02,
        max_allowed_spread=5000.0,
        imbalance_cancel_threshold=0.50,
        take_profit_pct=0.01,
        stop_loss_pct=0.01,
        maker_price_offset_jpy=1.0,
    )
    rows_by_day = {
        "2026-07-10": [
            _row("2026-07-10 09:00:00", 100.0, 101.0, bid_size=3.0, ask_size=1.0),
            _row("2026-07-10 09:00:10", 101.0, 102.0, bid_size=3.0, ask_size=1.0),
            _row("2026-07-10 09:00:20", 102.1, 103.1, bid_size=3.0, ask_size=1.0),
        ]
    }
    result = simulate_profile(rows_by_day, cfg)
    assert result["trade_count"] == 1
    assert result["win_count"] == 1
    assert result["total_pnl_pct"] == pytest.approx(cfg.take_profit_pct)


def test_simulate_profile_stop_loss_uses_actual_price_based_pnl() -> None:
    cfg = StrategyConfig(
        imbalance_entry_threshold=0.55,
        min_entry_wall_btc=0.05,
        max_spread_pct=0.02,
        max_allowed_spread=5000.0,
        imbalance_cancel_threshold=0.50,
        take_profit_pct=0.02,
        stop_loss_pct=0.01,
        maker_price_offset_jpy=1.0,
    )
    rows_by_day = {
        "2026-07-10": [
            _row("2026-07-10 09:00:00", 100.0, 101.0, bid_size=3.0, ask_size=1.0),
            _row("2026-07-10 09:00:10", 101.0, 102.0, bid_size=3.0, ask_size=1.0),
            _row("2026-07-10 09:00:20", 98.5, 99.5, bid_size=2.0, ask_size=2.0),
        ]
    }
    result = simulate_profile(rows_by_day, cfg)
    expected = (98.5 - 101.0) / 101.0
    assert result["trade_count"] == 1
    assert result["total_pnl_pct"] < 0.0
    assert result["total_pnl_pct"] == pytest.approx(expected)


def test_simulate_profile_pending_cancel_not_counted_as_trade() -> None:
    cfg = StrategyConfig(
        imbalance_entry_threshold=0.55,
        min_entry_wall_btc=0.05,
        max_spread_pct=0.02,
        max_allowed_spread=5000.0,
        imbalance_cancel_threshold=0.50,
        take_profit_pct=0.01,
        stop_loss_pct=0.01,
        maker_price_offset_jpy=1.0,
    )
    rows_by_day = {
        "2026-07-10": [
            _row("2026-07-10 09:00:00", 100.0, 101.0, bid_size=3.0, ask_size=1.0),
            _row("2026-07-10 09:00:10", 100.5, 101.5, bid_size=1.0, ask_size=3.0),
        ]
    }
    result = simulate_profile(rows_by_day, cfg)
    assert result["trade_count"] == 0
    assert result["total_pnl_pct"] == pytest.approx(0.0)
    assert result["win_rate"] is None


def test_simulate_profile_resets_position_at_day_boundary() -> None:
    cfg = StrategyConfig(
        imbalance_entry_threshold=0.55,
        min_entry_wall_btc=0.05,
        max_spread_pct=0.02,
        max_allowed_spread=5000.0,
        imbalance_cancel_threshold=0.50,
        take_profit_pct=0.01,
        stop_loss_pct=0.01,
        maker_price_offset_jpy=1.0,
    )
    rows_by_day = {
        "2026-07-10": [
            _row("2026-07-10 09:00:00", 100.0, 101.0, bid_size=3.0, ask_size=1.0),
            _row("2026-07-10 09:00:10", 101.0, 102.0, bid_size=3.0, ask_size=1.0),
        ],
        "2026-07-11": [
            _row("2026-07-11 09:00:00", 100.0, 101.0, bid_size=3.0, ask_size=1.0),
            _row("2026-07-11 09:00:10", 101.0, 102.0, bid_size=3.0, ask_size=1.0),
            _row("2026-07-11 09:00:20", 102.1, 103.1, bid_size=3.0, ask_size=1.0),
        ],
    }
    result = simulate_profile(rows_by_day, cfg)
    assert result["trade_count"] == 1
    assert result["total_pnl_pct"] == pytest.approx(cfg.take_profit_pct)


def test_run_backtest_check_returns_ran_false_when_no_backtestable_change(tmp_path: Path) -> None:
    current = _base_profile(take_profit_pct=0.01)
    proposed = _base_profile(take_profit_pct=0.01, max_order_size_btc=0.2)
    result = run_backtest_check("full_day", current, proposed, tmp_path, "2026-07-10")
    assert result == {"ran": False}


def test_run_backtest_check_insufficient_data_when_trade_count_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(bv, "BACKTEST_MIN_TRADES", 5)
    monkeypatch.setattr(bv, "load_market_snapshot_rows", lambda **_: {})
    monkeypatch.setattr(bv, "filter_rows_by_time_window", lambda *args, **kwargs: {})
    calls = iter(
        [
            {"trade_count": 4, "total_pnl_pct": 0.02, "win_count": 2, "win_rate": 0.5},
            {"trade_count": 5, "total_pnl_pct": 0.01, "win_count": 2, "win_rate": 0.4},
        ]
    )
    monkeypatch.setattr(bv, "simulate_profile", lambda *_args, **_kwargs: next(calls))
    current = _base_profile(take_profit_pct=0.01)
    proposed = _base_profile(take_profit_pct=0.02)
    result = run_backtest_check("full_day", current, proposed, tmp_path, "2026-07-10")
    assert result["ran"] is True
    assert result["gated"] is False
    assert result["reason"] == "insufficient_data"


def test_run_backtest_check_gates_when_old_positive_and_new_below_90_percent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(bv, "BACKTEST_MIN_TRADES", 1)
    monkeypatch.setattr(bv, "load_market_snapshot_rows", lambda **_: {})
    monkeypatch.setattr(bv, "filter_rows_by_time_window", lambda *args, **kwargs: {})
    calls = iter(
        [
            {"trade_count": 5, "total_pnl_pct": 1.0, "win_count": 3, "win_rate": 0.6},
            {"trade_count": 5, "total_pnl_pct": 0.89, "win_count": 3, "win_rate": 0.6},
        ]
    )
    monkeypatch.setattr(bv, "simulate_profile", lambda *_args, **_kwargs: next(calls))
    current = _base_profile(take_profit_pct=0.01)
    proposed = _base_profile(take_profit_pct=0.02)
    result = run_backtest_check("full_day", current, proposed, tmp_path, "2026-07-10")
    assert result["ran"] is True
    assert result["gated"] is True


def test_run_backtest_check_not_gated_when_old_positive_and_new_keeps_90_percent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(bv, "BACKTEST_MIN_TRADES", 1)
    monkeypatch.setattr(bv, "load_market_snapshot_rows", lambda **_: {})
    monkeypatch.setattr(bv, "filter_rows_by_time_window", lambda *args, **kwargs: {})
    calls = iter(
        [
            {"trade_count": 5, "total_pnl_pct": 1.0, "win_count": 3, "win_rate": 0.6},
            {"trade_count": 5, "total_pnl_pct": 0.90, "win_count": 3, "win_rate": 0.6},
        ]
    )
    monkeypatch.setattr(bv, "simulate_profile", lambda *_args, **_kwargs: next(calls))
    current = _base_profile(take_profit_pct=0.01)
    proposed = _base_profile(take_profit_pct=0.02)
    result = run_backtest_check("full_day", current, proposed, tmp_path, "2026-07-10")
    assert result["ran"] is True
    assert result["gated"] is False


def test_run_backtest_check_gates_when_old_non_positive_and_new_worsens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(bv, "BACKTEST_MIN_TRADES", 1)
    monkeypatch.setattr(bv, "load_market_snapshot_rows", lambda **_: {})
    monkeypatch.setattr(bv, "filter_rows_by_time_window", lambda *args, **kwargs: {})
    calls = iter(
        [
            {"trade_count": 5, "total_pnl_pct": -0.10, "win_count": 1, "win_rate": 0.2},
            {"trade_count": 5, "total_pnl_pct": -0.20, "win_count": 1, "win_rate": 0.2},
        ]
    )
    monkeypatch.setattr(bv, "simulate_profile", lambda *_args, **_kwargs: next(calls))
    current = _base_profile(take_profit_pct=0.01)
    proposed = _base_profile(take_profit_pct=0.02)
    result = run_backtest_check("full_day", current, proposed, tmp_path, "2026-07-10")
    assert result["ran"] is True
    assert result["gated"] is True


def test_run_backtest_check_handles_missing_market_snapshot_days(tmp_path: Path) -> None:
    target_date = "2026-07-10"
    existing_day = "2026-07-09"
    path = tmp_path / f"market_snapshot_{existing_day}.csv"
    _write_market_snapshot(
        path,
        [
            _row(f"{existing_day} 09:00:00", 100.0, 101.0, bid_size=3.0, ask_size=1.0),
            _row(f"{existing_day} 09:00:10", 101.0, 102.0, bid_size=3.0, ask_size=1.0),
            _row(f"{existing_day} 09:00:20", 102.6, 103.6, bid_size=3.0, ask_size=1.0),
            _row(f"{existing_day} 09:01:00", 100.0, 101.0, bid_size=3.0, ask_size=1.0),
            _row(f"{existing_day} 09:01:10", 101.0, 102.0, bid_size=3.0, ask_size=1.0),
            _row(f"{existing_day} 09:01:20", 102.6, 103.6, bid_size=3.0, ask_size=1.0),
            _row(f"{existing_day} 09:02:00", 100.0, 101.0, bid_size=3.0, ask_size=1.0),
            _row(f"{existing_day} 09:02:10", 101.0, 102.0, bid_size=3.0, ask_size=1.0),
            _row(f"{existing_day} 09:02:20", 102.6, 103.6, bid_size=3.0, ask_size=1.0),
        ],
    )
    current = _base_profile(take_profit_pct=0.01, stop_loss_pct=0.02)
    proposed = _base_profile(take_profit_pct=0.015, stop_loss_pct=0.02)
    result = run_backtest_check("full_day", current, proposed, tmp_path, target_date)
    assert result["ran"] is True
    assert "old" in result and "new" in result
    assert result["old"]["trade_count"] > 0
    assert result["new"]["trade_count"] > 0
