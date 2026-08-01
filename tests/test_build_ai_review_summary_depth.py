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

DEPTH_FIELDS = BASE_FIELDS + [
    "bid_depth5_size",
    "ask_depth5_size",
    "depth_imbalance",
]


def _write_snapshot(
    log_dir: Path,
    day: date,
    *,
    with_depth: bool,
    bid_depth5_size: float = 1.0,
    ask_depth5_size: float = 1.0,
    depth_imbalance: float | None = 0.5,
) -> None:
    path = log_dir / f"market_snapshot_{day.isoformat()}.csv"
    fields = DEPTH_FIELDS if with_depth else BASE_FIELDS
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
    if with_depth:
        row["bid_depth5_size"] = str(bid_depth5_size)
        row["ask_depth5_size"] = str(ask_depth5_size)
        row["depth_imbalance"] = (
            "" if depth_imbalance is None else str(depth_imbalance)
        )
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerow(row)


@pytest.fixture
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(bas, "LOG_DIR", tmp_path)
    return tmp_path


def test_market_depth_all_days_have_columns(log_dir: Path) -> None:
    target = date(2026, 7, 17)
    for offset in range(3):
        d = target - timedelta(days=offset)
        _write_snapshot(
            log_dir,
            d,
            with_depth=True,
            bid_depth5_size=2.0,
            ask_depth5_size=1.0,
            depth_imbalance=0.6,
        )

    summary = bas.build_market_depth_summary(target, requested_days=3, profiles=PROFILES)
    overall = summary["overall"]
    assert overall["depth_actual_days"] == 3
    assert overall["avg_bid_depth5_size"] == pytest.approx(2.0)
    assert overall["avg_ask_depth5_size"] == pytest.approx(1.0)
    assert overall["avg_depth_imbalance"] == pytest.approx(0.6)
    assert overall["depth_confidence"] == "insufficient"


def test_market_depth_partial_days_exclude_missing_from_averages(log_dir: Path) -> None:
    target = date(2026, 7, 17)
    _write_snapshot(
        log_dir,
        target,
        with_depth=True,
        bid_depth5_size=4.0,
        ask_depth5_size=1.0,
        depth_imbalance=0.8,
    )
    _write_snapshot(log_dir, date(2026, 7, 16), with_depth=False)
    _write_snapshot(
        log_dir,
        date(2026, 7, 15),
        with_depth=True,
        bid_depth5_size=999.0,
        ask_depth5_size=999.0,
        depth_imbalance=0.99,
    )

    summary = bas.build_market_depth_summary(target, requested_days=2, profiles=PROFILES)
    overall = summary["overall"]
    assert overall["depth_actual_days"] == 1
    assert overall["avg_bid_depth5_size"] == pytest.approx(4.0)
    assert overall["avg_ask_depth5_size"] == pytest.approx(1.0)
    assert overall["avg_depth_imbalance"] == pytest.approx(0.8)
    assert overall["depth_confidence"] == "insufficient"


def test_market_depth_no_columns_anywhere(log_dir: Path) -> None:
    target = date(2026, 7, 17)
    for offset in range(3):
        d = target - timedelta(days=offset)
        _write_snapshot(log_dir, d, with_depth=False)

    summary = bas.build_market_depth_summary(target, requested_days=3, profiles=PROFILES)
    overall = summary["overall"]
    assert overall["depth_actual_days"] == 0
    assert overall["avg_bid_depth5_size"] is None
    assert overall["avg_ask_depth5_size"] is None
    assert overall["avg_depth_imbalance"] is None
    assert overall["depth_confidence"] == "insufficient"


def test_market_depth_null_imbalance_excluded_from_average(log_dir: Path) -> None:
    target = date(2026, 7, 17)
    path = log_dir / f"market_snapshot_{target.isoformat()}.csv"
    fields = DEPTH_FIELDS
    rows = [
        {
            "timestamp": f"{target.isoformat()} 10:00:00",
            "best_bid_price": "100",
            "best_bid_size": "1",
            "best_ask_price": "101",
            "best_ask_size": "1",
            "mid_price": "100.5",
            "imbalance": "0.5",
            "spread_pct": "0.001",
            "bid_depth5_size": "2.0",
            "ask_depth5_size": "1.0",
            "depth_imbalance": "0.8",
        },
        {
            "timestamp": f"{target.isoformat()} 10:01:00",
            "best_bid_price": "100",
            "best_bid_size": "1",
            "best_ask_price": "101",
            "best_ask_size": "1",
            "mid_price": "100.5",
            "imbalance": "0.5",
            "spread_pct": "0.001",
            "bid_depth5_size": "4.0",
            "ask_depth5_size": "3.0",
            "depth_imbalance": "",
        },
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    summary = bas.build_market_depth_summary(target, requested_days=1, profiles=PROFILES)
    overall = summary["overall"]
    assert overall["depth_actual_days"] == 1
    assert overall["avg_bid_depth5_size"] == pytest.approx(3.0)
    assert overall["avg_ask_depth5_size"] == pytest.approx(2.0)
    assert overall["avg_depth_imbalance"] == pytest.approx(0.8)


def test_proposer_prompt_includes_market_depth_aggregates_only(capsys) -> None:
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
                "market_depth": {
                    "overall": {
                        "avg_bid_depth5_size": 1.5,
                        "avg_ask_depth5_size": 1.0,
                        "avg_depth_imbalance": 0.6,
                        "depth_actual_days": 5,
                        "depth_confidence": "low",
                    },
                    "per_profile": {
                        "full_day": {
                            "avg_bid_depth5_size": 1.5,
                            "depth_confidence": "low",
                        }
                    },
                },
            },
            "stability_check": {
                "requested_days": 30,
                "weekly_breakdown": [{"block_index": 0}],
                "market_depth": {
                    "overall": {"depth_confidence": "medium"},
                },
            },
            "regime_reference": {
                "requested_days": 90,
                "blocks": [],
                "summary": {},
                "market_depth": {"overall": {"depth_confidence": "insufficient"}},
            },
        },
    }
    reduced = _build_proposer_reduced_summary(summary, failures_limit=5)
    assert "market_depth" in reduced["windows"]["rule_review"]
    assert "weekly_breakdown" not in reduced["windows"]["stability_check"]
    assert "market_depth" not in reduced["windows"]["stability_check"]
    assert "market_depth" not in reduced["windows"]["regime_reference"]

    _, prompt = build_proposer_prompt(summary)
    assert "market_depth" in prompt
    assert "weekly_breakdown" not in prompt
    assert "avg_bid_depth5_size" in prompt
    assert "depth_confidence" in prompt
    out = capsys.readouterr().out
    assert "proposer prompt chars=" in out
    assert "depth_delta=+" in out
    assert "volatility_delta=+" in out
