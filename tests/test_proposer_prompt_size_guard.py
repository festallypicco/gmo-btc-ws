from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

_ROOT_DIR = Path(__file__).resolve().parent.parent
_AI_REVIEW_DIR = _ROOT_DIR / "ai_review"
if str(_AI_REVIEW_DIR) not in sys.path:
    sys.path.insert(0, str(_AI_REVIEW_DIR))

import prompts as prompts_mod  # noqa: E402
from prompts import (  # noqa: E402
    _assemble_proposer_prompt,
    _build_proposer_reduced_summary,
    _json,
    _pending_rollouts_summary_for_proposer,
    build_proposer_prompt,
)


def _base_summary() -> dict:
    return {
        "target_date": "2026-07-17",
        "current_config": {
            "version": "test",
            "profiles": [],
            "pending_rollouts": {
                "profiles.early_morning.max_order_size_btc": {
                    "start_value": 0.1,
                    "target_value": 0.09,
                    "current_applied_value": 0.094,
                    "start_date": "2026-07-17",
                    "day_index": 2,
                    "total_days": 3,
                    "rollout_ratios": [0.4, 0.7, 1.0],
                    "direction": "down",
                    "reason": "outlier zscore high enough to trigger staged rollout",
                    "status": "in_progress",
                },
                "profiles.daytime.take_profit_pct": {
                    "start_value": 0.0015,
                    "target_value": 0.0012,
                    "current_applied_value": 0.00132,
                    "start_date": "2026-07-17",
                    "day_index": 2,
                    "total_days": 3,
                    "rollout_ratios": [0.4, 0.7, 1.0],
                    "direction": "down",
                    "reason": "outlier divergence",
                    "status": "in_progress",
                },
                "profiles.daytime.max_order_size_btc": {
                    "start_value": 0.1,
                    "target_value": 0.08,
                    "current_applied_value": 0.088,
                    "start_date": "2026-07-17",
                    "day_index": 1,
                    "total_days": 3,
                    "rollout_ratios": [0.4, 0.7, 1.0],
                    "direction": "down",
                    "reason": "outlier zscore",
                    "status": "in_progress",
                },
            },
        },
        "recent_config_changes": [
            {
                "timestamp": f"2026-07-{d:02d}T06:00:00",
                "reason": ("REASON" * 80) + f"-{d}",
            }
            for d in range(10, 15)
        ],
        "past_validation_failures": [],
        "recent_change_outcomes": [],
        "windows": {
            "anomaly_check": {"requested_days": 1, "overall": {"trade_count": 1}},
            "rule_review": {
                "requested_days": 14,
                "overall": {"trade_count": 10},
                "market_trades": {
                    "overall": {
                        "trades_confidence": "low",
                        "buy_volume_total": 1.0,
                    }
                },
                "market_depth": {
                    "overall": {
                        "depth_confidence": "low",
                        "avg_bid_depth5_size": 1.0,
                    }
                },
                "market_volatility": {
                    "overall": {
                        "volatility_confidence": "low",
                        "avg_volatility_5min_range_pct": 0.01,
                    }
                },
            },
            "stability_check": {
                "requested_days": 30,
                "overall": {"trade_count": 20},
                "weekly_breakdown": [{"week": "2026-W28"}],
                "market_trades": {"overall": {"trades_confidence": "medium"}},
            },
            "regime_reference": {
                "requested_days": 90,
                "actual_days": 10,
                "blocks": [],
                "summary": {"note": "ok"},
                "market_trades": {"overall": {"trades_confidence": "insufficient"}},
            },
        },
    }


def test_pending_rollouts_summary_compresses_three_entries() -> None:
    summary = _base_summary()
    raw = summary["current_config"]["pending_rollouts"]
    raw_chars = len(_json(raw))
    compressed = _pending_rollouts_summary_for_proposer(raw)
    compressed_chars = len(_json(compressed))
    assert compressed["count"] == 3
    assert len(compressed["items"]) == 3
    assert compressed_chars < raw_chars / 2
    assert "start_value" not in _json(compressed)
    assert "rollout_ratios" not in _json(compressed)

    reduced = _build_proposer_reduced_summary(summary, failures_limit=5)
    pending = reduced["current_config"]["pending_rollouts"]
    assert pending["count"] == 3
    assert "start_value" not in _json(pending)


def test_size_guard_does_not_trigger_when_under_safety_line(capsys, monkeypatch) -> None:
    monkeypatch.setattr(prompts_mod, "PROPOSER_COMBINED_SAFETY_CHARS", 100_000)
    summary = _base_summary()
    build_proposer_prompt(summary)
    out = capsys.readouterr().out
    assert "proposer prompt size guard: step_a" not in out
    assert "proposer prompt size guard: step_b" not in out


def test_size_guard_step_a_triggers(capsys, monkeypatch) -> None:
    summary = _base_summary()
    system_len = 401  # approximate; use actual from build
    base_prompt = _assemble_proposer_prompt(
        summary,
        failures_limit=5,
        changes_limit=3,
        reason_max_chars=300,
        include_rule_review_market=True,
    )
    step_a_prompt = _assemble_proposer_prompt(
        summary,
        failures_limit=5,
        changes_limit=2,
        reason_max_chars=200,
        include_rule_review_market=True,
    )
    # safety を base 未満・step_a 以上に置き、step_a のみ発動させる
    system, _ = build_proposer_prompt(copy.deepcopy(summary))
    capsys.readouterr()  # clear prior logs
    safety = len(system) + len(step_a_prompt) + 10
    assert len(system) + len(base_prompt) > safety
    assert len(system) + len(step_a_prompt) <= safety
    monkeypatch.setattr(prompts_mod, "PROPOSER_COMBINED_SAFETY_CHARS", safety)

    _, prompt = build_proposer_prompt(copy.deepcopy(summary))
    out = capsys.readouterr().out
    assert "[WARN] proposer prompt size guard: step_a" in out
    assert "proposer prompt size guard: step_b" not in out
    assert len(system) + len(prompt) <= safety
    # reason が 200+truncation に短縮されていること
    assert "...(truncated)" in prompt


def test_size_guard_step_b_triggers_with_warn(capsys, monkeypatch) -> None:
    summary = _base_summary()
    system, _ = build_proposer_prompt(copy.deepcopy(summary))
    capsys.readouterr()

    step_a_prompt = _assemble_proposer_prompt(
        summary,
        failures_limit=5,
        changes_limit=2,
        reason_max_chars=200,
        include_rule_review_market=True,
    )
    step_b_prompt = _assemble_proposer_prompt(
        summary,
        failures_limit=5,
        changes_limit=2,
        reason_max_chars=200,
        include_rule_review_market=False,
    )
    # step_a 後も超えるが step_b 後は収まるライン
    safety = len(system) + len(step_b_prompt) + 10
    assert len(system) + len(step_a_prompt) > safety
    assert len(system) + len(step_b_prompt) <= safety
    monkeypatch.setattr(prompts_mod, "PROPOSER_COMBINED_SAFETY_CHARS", safety)

    _, prompt = build_proposer_prompt(copy.deepcopy(summary))
    out = capsys.readouterr().out
    assert "[WARN] proposer prompt size guard: step_a" in out
    assert "[WARN] proposer prompt size guard: step_b" in out
    assert "buy_volume_total" not in prompt
    assert "avg_bid_depth5_size" not in prompt
    assert "avg_volatility_5min_range_pct" not in prompt
    assert len(system) + len(prompt) <= safety
