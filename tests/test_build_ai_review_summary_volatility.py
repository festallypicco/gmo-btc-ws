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

VOLATILITY_FIELDS = BASE_FIELDS + ["volatility_5min_range_pct"]


def _write_snapshot(
    log_dir: Path,
    day: date,
    *,
    with_volatility: bool,
    volatility_5min_range_pct: float | None = 0.01,
) -> None:
    path = log_dir / f"market_snapshot_{day.isoformat()}.csv"
    fields = VOLATILITY_FIELDS if with_volatility else BASE_FIELDS
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
    if with_volatility:
        row["volatility_5min_range_pct"] = (
            "" if volatility_5min_range_pct is None else str(volatility_5min_range_pct)
        )
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerow(row)


@pytest.fixture
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(bas, "LOG_DIR", tmp_path)
    return tmp_path


def test_market_volatility_all_days_have_columns(log_dir: Path) -> None:
    target = date(2026, 7, 17)
    for offset in range(3):
        d = target - timedelta(days=offset)
        _write_snapshot(
            log_dir,
            d,
            with_volatility=True,
            volatility_5min_range_pct=0.02,
        )

    summary = bas.build_market_volatility_summary(
        target, requested_days=3, profiles=PROFILES
    )
    overall = summary["overall"]
    assert overall["volatility_actual_days"] == 3
    assert overall["avg_volatility_5min_range_pct"] == pytest.approx(0.02)
    assert overall["max_volatility_5min_range_pct"] == pytest.approx(0.02)
    assert overall["volatility_confidence"] == "insufficient"


def test_market_volatility_partial_days_exclude_missing(log_dir: Path) -> None:
    target = date(2026, 7, 17)
    _write_snapshot(
        log_dir,
        target,
        with_volatility=True,
        volatility_5min_range_pct=0.05,
    )
    _write_snapshot(log_dir, date(2026, 7, 16), with_volatility=False)
    _write_snapshot(
        log_dir,
        date(2026, 7, 15),
        with_volatility=True,
        volatility_5min_range_pct=0.99,
    )

    summary = bas.build_market_volatility_summary(
        target, requested_days=2, profiles=PROFILES
    )
    overall = summary["overall"]
    assert overall["volatility_actual_days"] == 1
    assert overall["avg_volatility_5min_range_pct"] == pytest.approx(0.05)
    assert overall["max_volatility_5min_range_pct"] == pytest.approx(0.05)
    assert overall["volatility_confidence"] == "insufficient"


def test_market_volatility_no_columns_anywhere(log_dir: Path) -> None:
    target = date(2026, 7, 17)
    for offset in range(3):
        d = target - timedelta(days=offset)
        _write_snapshot(log_dir, d, with_volatility=False)

    summary = bas.build_market_volatility_summary(
        target, requested_days=3, profiles=PROFILES
    )
    overall = summary["overall"]
    assert overall["volatility_actual_days"] == 0
    assert overall["avg_volatility_5min_range_pct"] is None
    assert overall["max_volatility_5min_range_pct"] is None
    assert overall["volatility_confidence"] == "insufficient"


def test_market_volatility_null_rows_excluded_from_avg_and_max(log_dir: Path) -> None:
    target = date(2026, 7, 17)
    path = log_dir / f"market_snapshot_{target.isoformat()}.csv"
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
            "volatility_5min_range_pct": "",
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
            "volatility_5min_range_pct": "0.01",
        },
        {
            "timestamp": f"{target.isoformat()} 10:02:00",
            "best_bid_price": "100",
            "best_bid_size": "1",
            "best_ask_price": "101",
            "best_ask_size": "1",
            "mid_price": "100.5",
            "imbalance": "0.5",
            "spread_pct": "0.001",
            "volatility_5min_range_pct": "0.03",
        },
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=VOLATILITY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    summary = bas.build_market_volatility_summary(
        target, requested_days=1, profiles=PROFILES
    )
    overall = summary["overall"]
    assert overall["volatility_actual_days"] == 1
    assert overall["avg_volatility_5min_range_pct"] == pytest.approx(0.02)
    assert overall["max_volatility_5min_range_pct"] == pytest.approx(0.03)


def test_proposer_prompt_includes_market_volatility_aggregates_only(capsys) -> None:
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
                "market_volatility": {
                    "overall": {
                        "avg_volatility_5min_range_pct": 0.02,
                        "max_volatility_5min_range_pct": 0.05,
                        "volatility_actual_days": 5,
                        "volatility_confidence": "low",
                    },
                    "per_profile": {
                        "full_day": {
                            "avg_volatility_5min_range_pct": 0.02,
                            "volatility_confidence": "low",
                        }
                    },
                },
            },
            "stability_check": {
                "requested_days": 30,
                "weekly_breakdown": [{"block_index": 0}],
                "market_volatility": {
                    "overall": {"volatility_confidence": "medium"},
                },
            },
            "regime_reference": {
                "requested_days": 90,
                "blocks": [],
                "summary": {},
                "market_volatility": {
                    "overall": {"volatility_confidence": "insufficient"}
                },
            },
        },
    }
    reduced = _build_proposer_reduced_summary(summary, failures_limit=5)
    assert "market_volatility" in reduced["windows"]["rule_review"]
    assert "weekly_breakdown" not in reduced["windows"]["stability_check"]
    assert "market_volatility" not in reduced["windows"]["stability_check"]
    assert "market_volatility" not in reduced["windows"]["regime_reference"]

    _, prompt = build_proposer_prompt(summary)
    assert "market_volatility" in prompt
    assert "weekly_breakdown" not in prompt
    assert "avg_volatility_5min_range_pct" in prompt
    assert "max_volatility_5min_range_pct" in prompt
    assert "volatility_confidence" in prompt
    out = capsys.readouterr().out
    assert "proposer prompt chars=" in out
    assert "volatility_delta=+" in out
