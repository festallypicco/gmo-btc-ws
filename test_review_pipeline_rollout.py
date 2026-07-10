"""
test_review_pipeline_rollout.py

review_pipeline.py の外れ値判定・段階適用ヘルパーを検証する。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
_AI_REVIEW_DIR = _ROOT_DIR / "ai_review"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))
if str(_AI_REVIEW_DIR) not in sys.path:
    sys.path.insert(0, str(_AI_REVIEW_DIR))

import review_pipeline as rp  # noqa: E402
from review_pipeline import (  # noqa: E402
    OUTLIER_DIVERGENCE_THRESHOLD,
    OUTLIER_ZSCORE_THRESHOLD,
    ROLLOUT_RATIOS,
    apply_daily_rollouts,
    calc_rolling_stats,
    create_or_replace_pending_rollout,
    judge_outlier,
)

PARAM_PATH = "profiles.full_day.imbalance_entry_threshold"


def _now() -> datetime:
    return datetime.now()


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _update_log_row(
    *,
    days_ago: int,
    value_before: float,
    value_after: float,
    param_path: str = PARAM_PATH,
    now: datetime | None = None,
) -> Dict[str, Any]:
    base = now or _now()
    applied_at = (base - timedelta(days=days_ago)).isoformat(timespec="seconds")
    delta_pct = 0.0 if value_before == 0.0 else abs((value_after - value_before) / value_before)
    return {
        "applied_at": applied_at,
        "param_path": param_path,
        "value_before": value_before,
        "value_after": value_after,
        "applied_delta_pct": delta_pct,
        "source": "ai_review",
    }


def _minimal_config(
    *,
    threshold: float = 0.61,
    pending_rollouts: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "version": "test-rollout",
        "updated_reason": "test",
        "pending_rollouts": pending_rollouts or {},
        "profiles": [
            {
                "name": "full_day",
                "start_time": "00:00",
                "end_time": "24:00",
                "imbalance_entry_threshold": threshold,
                "min_entry_wall_btc": 0.05,
                "min_valid_wall_btc": 0.1,
                "max_spread_pct": 0.0004,
                "max_allowed_spread": 4000.0,
                "imbalance_cancel_threshold": 0.5,
                "take_profit_pct": 0.002,
                "stop_loss_pct": 0.0018,
                "maker_price_offset_jpy": 1.0,
                "max_order_size_btc": 0.1,
                "daily_target_order_size_btc": None,
            }
        ],
    }


@pytest.fixture
def rollout_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    config_path = tmp_path / "config.json"
    update_log_path = tmp_path / "update_log.jsonl"
    monkeypatch.setattr(rp, "CONFIG_PATH", config_path)
    monkeypatch.setattr(rp, "UPDATE_LOG_PATH", update_log_path)
    return config_path, update_log_path


def test_calc_rolling_stats_empty_log(rollout_paths: tuple) -> None:
    _write_jsonl(rollout_paths[1], [])
    stats = calc_rolling_stats(PARAM_PATH)
    assert stats["n"] == 0
    assert stats["mean_change_pct"] == 0.0
    assert stats["stdev_change_pct"] == 0.0
    assert stats["last_direction"] == "none"
    assert stats["divergence_3d_pct"] == 0.0


def test_calc_rolling_stats_ignores_other_param_paths(rollout_paths: tuple) -> None:
    rows = [
        _update_log_row(days_ago=1, value_before=0.60, value_after=0.62),
        _update_log_row(
            days_ago=1,
            value_before=0.60,
            value_after=0.62,
            param_path="profiles.daytime.imbalance_entry_threshold",
        ),
    ]
    _write_jsonl(rollout_paths[1], rows)
    stats = calc_rolling_stats(PARAM_PATH)
    assert stats["n"] == 1


def test_calc_rolling_stats_excludes_entries_older_than_14_days(rollout_paths: tuple) -> None:
    rows = [
        _update_log_row(days_ago=15, value_before=0.60, value_after=0.70),
        _update_log_row(days_ago=1, value_before=0.60, value_after=0.62),
    ]
    _write_jsonl(rollout_paths[1], rows)
    stats = calc_rolling_stats(PARAM_PATH)
    assert stats["n"] == 1
    assert stats["mean_change_pct"] == pytest.approx(0.02 / 0.60)


def test_calc_rolling_stats_computes_mean_stdev_and_last_direction(rollout_paths: tuple) -> None:
    rows = [
        _update_log_row(days_ago=5, value_before=0.60, value_after=0.63),
        _update_log_row(days_ago=4, value_before=0.63, value_after=0.60),
        _update_log_row(days_ago=3, value_before=0.60, value_after=0.66),
    ]
    _write_jsonl(rollout_paths[1], rows)
    stats = calc_rolling_stats(PARAM_PATH)
    assert stats["n"] == 3
    assert stats["last_direction"] == "up"
    assert stats["mean_change_pct"] > 0.0
    assert stats["stdev_change_pct"] > 0.0


def test_calc_rolling_stats_divergence_3d_exactly_at_20_percent(rollout_paths: tuple) -> None:
    rows = [
        _update_log_row(days_ago=2, value_before=0.60, value_after=1.00),
        _update_log_row(days_ago=1, value_before=1.00, value_after=1.20),
    ]
    _write_jsonl(rollout_paths[1], rows)
    stats = calc_rolling_stats(PARAM_PATH)
    assert stats["divergence_3d_pct"] == pytest.approx(0.20)


@patch("review_pipeline.calc_rolling_stats")
def test_judge_outlier_zscore_exactly_2_not_outlier(mock_stats) -> None:
    mock_stats.return_value = {
        "n": 10,
        "mean_change_pct": 0.05,
        "stdev_change_pct": 0.025,
        "last_direction": "up",
        "divergence_3d_pct": 0.0,
    }
    current = 1000.0
    proposed = 1100.0
    result = judge_outlier(PARAM_PATH, current, proposed)
    assert result["zscore"] == pytest.approx(2.0)
    assert abs(result["zscore"]) <= OUTLIER_ZSCORE_THRESHOLD
    assert result["is_outlier"] is False
    assert "zscore=" not in result["reason"]


@patch("review_pipeline.calc_rolling_stats")
def test_judge_outlier_zscore_just_above_2_is_outlier(mock_stats) -> None:
    mock_stats.return_value = {
        "n": 10,
        "mean_change_pct": 0.05,
        "stdev_change_pct": 0.025,
        "last_direction": "up",
        "divergence_3d_pct": 0.0,
    }
    current = 0.60
    proposed = current * (1.0 + 0.05 + 2.01 * 0.025)
    result = judge_outlier(PARAM_PATH, current, proposed)
    assert result["is_outlier"] is True
    assert "zscore=" in result["reason"]


@patch("review_pipeline.calc_rolling_stats")
def test_judge_outlier_divergence_exactly_20_percent_not_outlier(mock_stats) -> None:
    mock_stats.return_value = {
        "n": 10,
        "mean_change_pct": 0.01,
        "stdev_change_pct": 0.01,
        "last_direction": "up",
        "divergence_3d_pct": OUTLIER_DIVERGENCE_THRESHOLD,
    }
    result = judge_outlier(PARAM_PATH, 0.60, 0.606)
    assert result["divergence_3d_pct"] == pytest.approx(0.20)
    assert result["is_outlier"] is False
    assert "divergence_3d=" not in result["reason"]


@patch("review_pipeline.calc_rolling_stats")
def test_judge_outlier_divergence_just_above_20_percent_is_outlier(mock_stats) -> None:
    mock_stats.return_value = {
        "n": 10,
        "mean_change_pct": 0.01,
        "stdev_change_pct": 0.01,
        "last_direction": "up",
        "divergence_3d_pct": OUTLIER_DIVERGENCE_THRESHOLD + 0.001,
    }
    result = judge_outlier(PARAM_PATH, 0.60, 0.606)
    assert result["is_outlier"] is True
    assert "divergence_3d=" in result["reason"]


@patch("review_pipeline.calc_rolling_stats")
def test_judge_outlier_insufficient_data_boundary_n4_vs_n5(mock_stats) -> None:
    mock_stats.return_value = {
        "n": 4,
        "mean_change_pct": 0.01,
        "stdev_change_pct": 0.01,
        "last_direction": "up",
        "divergence_3d_pct": 0.0,
    }
    result_n4 = judge_outlier(PARAM_PATH, 0.60, 0.61)
    assert result_n4["is_outlier"] is True
    assert "insufficient_data n=4" in result_n4["reason"]

    mock_stats.return_value["n"] = 5
    result_n5 = judge_outlier(PARAM_PATH, 0.60, 0.61)
    assert "insufficient_data" not in result_n5["reason"]


@patch("review_pipeline.calc_rolling_stats")
def test_judge_outlier_reverse_direction_is_outlier(mock_stats) -> None:
    mock_stats.return_value = {
        "n": 10,
        "mean_change_pct": 0.01,
        "stdev_change_pct": 0.01,
        "last_direction": "up",
        "divergence_3d_pct": 0.0,
    }
    result = judge_outlier(PARAM_PATH, 0.60, 0.59)
    assert result["is_outlier"] is True
    assert "reverse_direction" in result["reason"]


def test_create_or_replace_pending_rollout_delta_below_min_step_applies_full() -> None:
    current = 0.0015
    target = current + 0.000099
    result = create_or_replace_pending_rollout(
        "profiles.full_day.take_profit_pct",
        current,
        target,
        "test",
    )
    assert result["action"] == "apply_full"
    assert result["skip_reason"] == "delta<0.0001"


def test_create_or_replace_pending_rollout_delta_exactly_min_step_creates_rollout() -> None:
    current = 0.0015
    target = current + 0.0001
    result = create_or_replace_pending_rollout(
        "profiles.full_day.take_profit_pct",
        current,
        target,
        "zscore=2.4",
    )
    assert result["action"] == "rollout"
    entry = result["entry"]
    assert entry["day_index"] == 1
    assert entry["total_days"] == 3
    assert entry["rollout_ratios"] == ROLLOUT_RATIOS
    expected_day1 = current + (target - current) * ROLLOUT_RATIOS[0]
    assert entry["current_applied_value"] == pytest.approx(expected_day1)


def test_create_or_replace_pending_rollout_replaces_existing_entry_fields() -> None:
    result = create_or_replace_pending_rollout(
        PARAM_PATH,
        0.60,
        0.66,
        "insufficient_data n=2",
    )
    assert result["action"] == "rollout"
    assert result["entry"]["start_value"] == pytest.approx(0.60)
    assert result["entry"]["target_value"] == pytest.approx(0.66)
    assert result["entry"]["reason"] == "insufficient_data n=2"


def test_apply_daily_rollouts_no_op_when_pending_empty(rollout_paths: tuple) -> None:
    config_path, _ = rollout_paths
    config_path.write_text(json.dumps(_minimal_config()), encoding="utf-8")
    apply_daily_rollouts()
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["pending_rollouts"] == {}


def test_apply_daily_rollouts_advances_day_index_and_applies_ratio(rollout_paths: tuple) -> None:
    config_path, update_log_path = rollout_paths
    param_path = PARAM_PATH
    pending = {
        param_path: {
            "start_value": 0.60,
            "target_value": 0.70,
            "current_applied_value": 0.63,
            "start_date": "2026-07-09",
            "day_index": 1,
            "total_days": 3,
            "rollout_ratios": list(ROLLOUT_RATIOS),
            "direction": "up",
            "reason": "test",
            "status": "in_progress",
        }
    }
    config_path.write_text(
        json.dumps(_minimal_config(threshold=0.63, pending_rollouts=pending)),
        encoding="utf-8",
    )

    apply_daily_rollouts()

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    expected_value = 0.60 + (0.70 - 0.60) * ROLLOUT_RATIOS[1]
    assert saved["profiles"][0]["imbalance_entry_threshold"] == pytest.approx(expected_value)
    assert param_path in saved["pending_rollouts"]
    assert saved["pending_rollouts"][param_path]["day_index"] == 2

    lines = update_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    logged = json.loads(lines[0])
    assert logged["param_path"] == param_path
    assert logged["value_before"] == pytest.approx(0.63)
    assert logged["value_after"] == pytest.approx(expected_value)


def test_apply_daily_rollouts_removes_entry_on_final_day(rollout_paths: tuple) -> None:
    config_path, _ = rollout_paths
    param_path = PARAM_PATH
    pending = {
        param_path: {
            "start_value": 0.60,
            "target_value": 0.70,
            "current_applied_value": 0.66,
            "start_date": "2026-07-09",
            "day_index": 2,
            "total_days": 3,
            "rollout_ratios": list(ROLLOUT_RATIOS),
            "direction": "up",
            "reason": "test",
            "status": "in_progress",
        }
    }
    config_path.write_text(
        json.dumps(_minimal_config(threshold=0.66, pending_rollouts=pending)),
        encoding="utf-8",
    )

    apply_daily_rollouts()

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["profiles"][0]["imbalance_entry_threshold"] == pytest.approx(0.70)
    assert saved["pending_rollouts"] == {}
