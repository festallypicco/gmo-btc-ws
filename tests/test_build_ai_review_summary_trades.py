from __future__ import annotations

import csv
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_ROOT_DIR = Path(__file__).resolve().parent.parent
_AI_REVIEW_DIR = _ROOT_DIR / "ai_review"
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_AI_REVIEW_DIR) not in sys.path:
    sys.path.insert(0, str(_AI_REVIEW_DIR))
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import build_ai_review_summary as bas  # noqa: E402
from prompts import _build_proposer_reduced_summary, build_proposer_prompt  # noqa: E402

PROFILES = [
    {
        "name": "full_day",
        "start_time": "00:00",
        "end_time": "24:00",
    }
]

BASE_FIELDS = [
    "timestamp",
    "best_bid_price",
    "best_bid_size",
    "best_ask_price",
    "best_ask_size",
    "mid_price",
    "imbalance",
    "spread_pct",
]

TRADES_FIELDS = BASE_FIELDS + ["trade_count", "buy_volume", "sell_volume"]


def _write_snapshot(
    log_dir: Path,
    day: date,
    *,
    with_trades: bool,
    trade_count: int = 2,
    buy_volume: float = 0.01,
    sell_volume: float = 0.02,
) -> None:
    path = log_dir / f"market_snapshot_{day.isoformat()}.csv"
    fields = TRADES_FIELDS if with_trades else BASE_FIELDS
    row = {
        "timestamp": f"{day.isoformat()} 10:00:00",
        "best_bid_price": "100",
        "best_bid_size": "1",
        "best_ask_price": "101",
        "best_ask_size": "1",
        "mid_price": "100.5",
        "imbalance": "0.5",
        "spread_pct": "0.001",
    }
    if with_trades:
        row["trade_count"] = str(trade_count)
        row["buy_volume"] = str(buy_volume)
        row["sell_volume"] = str(sell_volume)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerow(row)


@pytest.fixture
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(bas, "LOG_DIR", tmp_path)
    return tmp_path


def test_market_trades_all_days_have_columns(log_dir: Path) -> None:
    target = date(2026, 7, 17)
    for offset in range(3):
        d = target - timedelta(days=offset)
        _write_snapshot(log_dir, d, with_trades=True, trade_count=4, buy_volume=0.1, sell_volume=0.2)

    summary = bas.build_market_trades_summary(target, requested_days=3, profiles=PROFILES)
    overall = summary["overall"]
    assert overall["trades_actual_days"] == 3
    assert overall["buy_volume_total"] == pytest.approx(0.3)
    assert overall["sell_volume_total"] == pytest.approx(0.6)
    assert overall["buy_ratio"] == pytest.approx(0.1 / 0.3)
    assert overall["avg_trade_count_per_snapshot"] == pytest.approx(4.0)
    assert overall["trades_confidence"] == "insufficient"


def test_market_trades_partial_days_exclude_missing_from_totals(log_dir: Path) -> None:
    target = date(2026, 7, 17)
    _write_snapshot(log_dir, target, with_trades=True, trade_count=10, buy_volume=1.0, sell_volume=0.0)
    _write_snapshot(
        log_dir,
        date(2026, 7, 16),
        with_trades=False,
    )
    _write_snapshot(
        log_dir,
        date(2026, 7, 15),
        with_trades=True,
        trade_count=999,
        buy_volume=999.0,
        sell_volume=999.0,
    )

    summary = bas.build_market_trades_summary(target, requested_days=2, profiles=PROFILES)
    overall = summary["overall"]
    assert overall["trades_actual_days"] == 1
    assert overall["buy_volume_total"] == pytest.approx(1.0)
    assert overall["sell_volume_total"] == pytest.approx(0.0)
    assert overall["buy_ratio"] == pytest.approx(1.0)
    assert overall["avg_trade_count_per_snapshot"] == pytest.approx(10.0)
    assert overall["trades_confidence"] == "insufficient"


def test_market_trades_no_columns_anywhere(log_dir: Path) -> None:
    target = date(2026, 7, 17)
    for offset in range(3):
        d = target - timedelta(days=offset)
        _write_snapshot(log_dir, d, with_trades=False)

    summary = bas.build_market_trades_summary(target, requested_days=3, profiles=PROFILES)
    overall = summary["overall"]
    assert overall["trades_actual_days"] == 0
    assert overall["buy_volume_total"] is None
    assert overall["sell_volume_total"] is None
    assert overall["buy_ratio"] is None
    assert overall["avg_trade_count_per_snapshot"] is None
    assert overall["trades_confidence"] == "insufficient"


def test_proposer_prompt_includes_market_trades_aggregates_only(capsys) -> None:
    summary = {
        "target_date": "2026-07-17",
        "current_config": {"profiles": PROFILES},
        "recent_config_changes": [],
        "past_validation_failures": [],
        "recent_change_outcomes": [],
        "windows": {
            "anomaly_check": {"requested_days": 1},
            "rule_review": {
                "requested_days": 14,
                "market_trades": {
                    "overall": {
                        "buy_volume_total": 1.0,
                        "sell_volume_total": 2.0,
                        "buy_ratio": 0.3333,
                        "avg_trade_count_per_snapshot": 3.0,
                        "trades_actual_days": 5,
                        "trades_confidence": "low",
                    },
                    "per_profile": {
                        "full_day": {
                            "buy_volume_total": 1.0,
                            "trades_confidence": "low",
                        }
                    },
                },
            },
            "stability_check": {
                "requested_days": 30,
                "weekly_breakdown": [{"block_index": 0}],
                "market_trades": {
                    "overall": {"trades_confidence": "medium"},
                },
            },
            "regime_reference": {
                "requested_days": 90,
                "blocks": [],
                "summary": {},
                "market_trades": {"overall": {"trades_confidence": "insufficient"}},
            },
        },
    }
    reduced = _build_proposer_reduced_summary(summary, failures_limit=5)
    assert "market_trades" in reduced["windows"]["rule_review"]
    assert "weekly_breakdown" not in reduced["windows"]["stability_check"]
    assert "market_trades" not in reduced["windows"]["stability_check"]
    assert "market_trades" not in reduced["windows"]["regime_reference"]

    _, prompt = build_proposer_prompt(summary)
    assert "market_trades" in prompt
    assert "weekly_breakdown" not in prompt
    out = capsys.readouterr().out
    assert "proposer prompt chars=" in out
    assert "market_trades_delta=+" in out
    assert "depth_delta=+" in out
    assert "volatility_delta=+" in out
