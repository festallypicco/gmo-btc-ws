"""
test_review_pipeline_telegram.py

status=applied 時の Telegram 本文組み立てを検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT_DIR = Path(__file__).resolve().parent.parent
_AI_REVIEW_DIR = _ROOT_DIR / "ai_review"
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
for path in (_AI_REVIEW_DIR, _BTC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from review_pipeline import (  # noqa: E402
    build_applied_telegram_message,
    collect_profile_field_changes,
    _localize_profile_field_token,
    _localize_profile_field_tokens_in_text,
    _status_label_ja,
)


def test_localize_profile_field_token() -> None:
    assert (
        _localize_profile_field_token("profiles.full_day.take_profit_pct")
        == "full_day / 利確幅（％）"
    )
    assert (
        _localize_profile_field_token("full_day.max_spread_pct")
        == "full_day / スプレッド上限（％）"
    )


def test_localize_tokens_in_rejected_reason() -> None:
    text = "full_day.daily_target_order_size_btc=1.5 は範囲外 (0.001-0.05) のため据え置き"
    out = _localize_profile_field_tokens_in_text(text)
    assert "1日あたりの発注サイズ上限" in out
    assert "daily_target_order_size_btc" not in out


def test_collect_profile_field_changes_skips_identity_keys() -> None:
    before = [
        {
            "name": "full_day",
            "start_time": "00:00",
            "end_time": "24:00",
            "imbalance_entry_threshold": 0.55,
            "take_profit_pct": 0.0015,
        }
    ]
    after = [
        {
            "name": "full_day",
            "start_time": "01:00",
            "end_time": "23:00",
            "imbalance_entry_threshold": 0.56,
            "take_profit_pct": 0.0015,
        }
    ]
    changes = collect_profile_field_changes(before, after)
    assert changes == [("full_day", "imbalance_entry_threshold", 0.55, 0.56)]


def test_build_applied_telegram_includes_reason_and_changes() -> None:
    message = build_applied_telegram_message(
        target_date="2026-08-08",
        updated_reason="Proposerの新規提案を却下し、ロールアウトを継続しました。",
        profile_changes=[
            ("full_day", "imbalance_entry_threshold", 0.556, 0.563),
            ("full_day", "max_spread_pct", 0.00028185, 0.0002787),
        ],
        outlier_marks={},
        reverted_fields=[],
        clamped_to_bounds_fields=[],
        rejected_daily_target_reasons=[],
        backtest_results={},
    )
    assert "[BTC AI議論] 本日の変更内容をお知らせします" in message
    assert "date=2026-08-08" in message
    assert "判断理由:" in message
    assert "Proposerの新規提案を却下し、ロールアウトを継続しました。" in message
    assert "数値変更:" in message
    assert "full_day / エントリー用インバランス閾値: 0.556 -> 0.563" in message
    assert "full_day / スプレッド上限（％）: 0.00028185 -> 0.0002787" in message
    assert "imbalance_entry_threshold" not in message
    assert "max_spread_pct" not in message


def test_build_applied_telegram_empty_reason_and_no_changes() -> None:
    message = build_applied_telegram_message(
        target_date="2026-08-01",
        updated_reason="  ",
        profile_changes=[],
    )
    assert "理由の記録なし" in message
    assert "数値変更:\n- なし" in message


def test_build_applied_telegram_localizes_special_sections() -> None:
    message = build_applied_telegram_message(
        target_date="2026-08-07",
        updated_reason="段階適用を開始",
        profile_changes=[("full_day", "imbalance_entry_threshold", 0.55, 0.556)],
        outlier_marks={
            "profiles.full_day.imbalance_entry_threshold": {
                "reason": "insufficient_data n=0",
                "current_applied_value": 0.556,
                "target_value": 0.57,
                "day_index": 1,
                "total_days": 3,
            }
        },
        reverted_fields=["full_day.take_profit_pct"],
        clamped_to_bounds_fields=["full_day.daily_target_order_size_btc"],
        rejected_daily_target_reasons=[
            "full_day.daily_target_order_size_btc=1.5 は範囲外 (0.001-0.05) のため据え置き"
        ],
        backtest_results={
            "full_day": {
                "gated": True,
                "changed_keys": ["imbalance_entry_threshold", "take_profit_pct"],
                "old": {"total_pnl_pct": 0.01},
                "new": {"total_pnl_pct": -0.02},
            }
        },
    )
    assert "段階適用（外れ値）:" in message
    assert "full_day / エントリー用インバランス閾値 day 1/3" in message
    assert "変更幅上限で据え置き: full_day / 利確幅（％）" in message
    assert "絶対範囲で補正: full_day / 1日あたりの発注サイズ上限" in message
    assert "日次発注サイズ上限の拒否:" in message
    assert "1日あたりの発注サイズ上限=1.5" in message
    assert "バックテストにより据え置き:" in message
    assert "reverted=エントリー用インバランス閾値, 利確幅（％）" in message
    assert "profiles.full_day.imbalance_entry_threshold" not in message


def test_status_label_ja() -> None:
    assert _status_label_ja("held_profile_name_mismatch") == (
        "プロファイル名不一致のため保留"
    )
