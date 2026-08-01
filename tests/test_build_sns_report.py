"""
tests/test_build_sns_report.py

SNS投稿用テキストの許可リスト境界・LLMフォールバック・Bot分離を検証する。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import build_public_report as bpr  # noqa: E402
import build_sns_report as sns  # noqa: E402
import telegram_notifier as tn  # noqa: E402


def _allowed_public() -> Dict[str, object]:
    return {
        "cumulative_pnl_jpy": 88.0,
        "daily_pnl_jpy": 138.0,
        "trade_count": 79,
        "win_rate_cumulative_pct": 59.5,
        "win_rate_daily_pct": 59.5,
        "circuit_breaker_triggered": False,
        "uptime_days": 3,
        "entry_count": 175,
        "max_drawdown_pct": 0.15,
    }


def test_sns_text_uses_only_allowed_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    public = _allowed_public()

    def fake_collect(now=None):
        return "2026-07-22", public

    monkeypatch.setattr(sns.bpr, "collect_public_metrics", fake_collect)
    target, got_public, base = sns.build_base_sns_text()
    assert target == "2026-07-22"
    assert set(got_public.keys()) == bpr.ALLOWED_KEYS
    bpr.assert_only_allowed_keys(got_public)
    assert "累積損益" in base
    assert "当日勝率" in base
    assert "当日増減率" not in base
    # 許可リスト外の内部語が本文に出ないこと
    lowered = base.lower()
    for word in ("imbalance", "offset", "config", "profile", "threshold"):
        assert word not in lowered


def test_commentary_failure_falls_back_to_base_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public = _allowed_public()

    def fake_collect(now=None):
        return "2026-07-22", public

    def boom(_text: str) -> str:
        raise RuntimeError("llm down")

    monkeypatch.setattr(sns.bpr, "collect_public_metrics", fake_collect)
    target, got_public, full, ok = sns.build_sns_message(commentary_fn=boom)
    assert target == "2026-07-22"
    assert ok is False
    assert set(got_public.keys()) == bpr.ALLOWED_KEYS
    assert full == bpr.format_report_text("2026-07-22", public)


def test_commentary_success_appends_note(monkeypatch: pytest.MonkeyPatch) -> None:
    public = _allowed_public()

    def fake_collect(now=None):
        return "2026-07-22", public

    monkeypatch.setattr(sns.bpr, "collect_public_metrics", fake_collect)
    _t, _p, full, ok = sns.build_sns_message(
        commentary_fn=lambda _text: "堅調な推移が見られました。"
    )
    assert ok is True
    assert full.endswith("堅調な推移が見られました。")
    assert "累積損益" in full


def test_incomplete_commentary_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """句点なしの尻切れ考察は破棄し、基本テキストのみにフォールバックする。"""
    public = _allowed_public()

    def fake_collect(now=None):
        return "2026-07-22", public

    monkeypatch.setattr(sns.bpr, "collect_public_metrics", fake_collect)
    _t, _p, full, ok = sns.build_sns_message(
        commentary_fn=lambda _text: "稼働4日目も着実に利益を積み"
    )
    assert ok is False
    assert full == bpr.format_report_text("2026-07-22", public)
    assert "利益を積み" not in full


def test_is_complete_commentary_requires_period() -> None:
    assert sns.is_complete_commentary("本日は堅調な結果となりました。") is True
    assert sns.is_complete_commentary("決済件数が多く、活発な一日でした。") is True
    assert sns.is_complete_commentary("稼働4日目も着実に利益を積み") is False
    assert sns.is_complete_commentary("活発な一日でした") is False
    assert sns.is_complete_commentary("") is False
    assert sns.is_complete_commentary("「本日は堅調な結果となりました。」") is True


def test_bot_credentials_are_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    """内部BotとSNS Botの環境変数が混同されないこと。"""
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_SNS_BOT_TOKEN",
        "TELEGRAM_SNS_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "internal-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "internal-chat")
    monkeypatch.setenv("TELEGRAM_SNS_BOT_TOKEN", "sns-token")
    monkeypatch.setenv("TELEGRAM_SNS_CHAT_ID", "sns-chat")
    monkeypatch.setattr(tn, "_merged_file_env", lambda: {})

    assert tn._get_telegram_config() == ("internal-token", "internal-chat")
    assert tn._get_sns_telegram_config() == ("sns-token", "sns-chat")


def test_main_failure_alerts_internal_bot_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    internal_calls: list[str] = []
    sns_calls: list[str] = []

    monkeypatch.setattr(
        sns,
        "build_sns_message",
        lambda: (_ for _ in ()).throw(RuntimeError("collect failed")),
    )
    monkeypatch.setattr(
        sns,
        "send_telegram_message",
        lambda text: internal_calls.append(text) or True,
    )
    monkeypatch.setattr(
        sns,
        "send_sns_telegram_message",
        lambda text: sns_calls.append(text) or True,
    )

    assert sns.main([]) == 1
    assert len(internal_calls) == 1
    assert "SNS report build failed" in internal_calls[0]
    assert sns_calls == []


def test_main_send_failure_alerts_internal_not_sns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    internal_calls: list[str] = []
    text_calls: list[str] = []
    video_calls: list[tuple] = []

    monkeypatch.setattr(
        sns,
        "build_sns_message",
        lambda: (
            "2026-07-22",
            _allowed_public(),
            "SNS BODY",
            True,
        ),
    )
    monkeypatch.setattr(
        sns,
        "try_generate_reel_video",
        lambda *a, **k: (None, "forced video fail"),
    )
    monkeypatch.setattr(
        sns,
        "send_sns_telegram_message",
        lambda text: text_calls.append(text) or False,
    )
    monkeypatch.setattr(
        sns,
        "send_sns_telegram_video",
        lambda *a, **k: video_calls.append((a, k)) or False,
    )
    monkeypatch.setattr(
        sns,
        "send_telegram_message",
        lambda text: internal_calls.append(text) or True,
    )

    assert sns.main([]) == 1
    assert video_calls == []
    assert len(text_calls) == 2
    assert text_calls[0].startswith(sns.TELEGRAM_LABEL_INSTAGRAM)
    assert text_calls[1].startswith(sns.TELEGRAM_LABEL_THREADS)
    assert "SNS BODY" in text_calls[1]
    assert any("video generation failed" in msg for msg in internal_calls)
    assert any("video and text" in msg for msg in internal_calls)


def test_sample_data_payload_has_negative_daily_pnl() -> None:
    target, public, caption = sns.build_sample_sns_payload()
    assert target == sns.SAMPLE_TARGET_DATE
    assert float(public["daily_pnl_jpy"]) < 0
    assert set(public.keys()) == bpr.ALLOWED_KEYS
    assert "当日損益: -" in caption
    # モックのみ（関数内でファイルI/Oしないことの担保として定数由来であること）
    assert public["daily_pnl_jpy"] == sns.SAMPLE_PUBLIC_METRICS["daily_pnl_jpy"]


def test_instagram_caption_hook_rotates_by_trading_day() -> None:
    assert len(sns.INSTAGRAM_CAPTION_HOOKS) == 3
    hooks = {
        sns.select_instagram_caption_hook(f"2026-07-{d:02d}") for d in range(1, 28)
    }
    assert hooks == set(sns.INSTAGRAM_CAPTION_HOOKS)
    for day in ("2026-07-20", "2026-07-21", "2026-07-22"):
        assert sns.select_instagram_caption_hook(day) in sns.INSTAGRAM_CAPTION_HOOKS


def test_build_instagram_caption_structure_and_metrics_order() -> None:
    public = _allowed_public()
    caption = sns.build_instagram_caption(
        "2026-07-22",
        public,
        commentary="本日は堅調な結果となりました。",
    )
    parts = caption.split("\n\n")
    assert parts[0] == sns.select_instagram_caption_hook("2026-07-22")
    assert parts[1] == sns.INSTAGRAM_CAPTION_HEADING
    metrics = parts[2].split("\n")
    assert metrics == [
        "累積損益：+88円",
        "当日損益：+138円",
        "決済件数：79件",
        "当日勝率：59.5%",
        "累積勝率：59.5%",
        "稼働継続日数：3日",
        "最大ドローダウン：0.15%",
    ]
    assert parts[3] == "本日は堅調な結果となりました。"
    assert parts[4] == sns.INSTAGRAM_CAPTION_DISCLAIMER
    assert parts[5] == sns.INSTAGRAM_CAPTION_CTA
    assert parts[6] == sns.INSTAGRAM_CAPTION_HASHTAGS
    # Threads専用フィールドは載せない
    assert "サーキットブレーカー" not in caption
    assert "エントリー" not in caption
    assert "日次レポート" not in caption


def test_build_instagram_caption_omits_commentary_when_missing() -> None:
    public = _allowed_public()
    caption = sns.build_instagram_caption("2026-07-22", public, commentary=None)
    assert "本日は堅調" not in caption
    parts = caption.split("\n\n")
    assert parts[0] == sns.select_instagram_caption_hook("2026-07-22")
    assert parts[1] == sns.INSTAGRAM_CAPTION_HEADING
    assert parts[2].startswith("累積損益：")
    assert parts[3] == sns.INSTAGRAM_CAPTION_DISCLAIMER
    assert parts[-1] == sns.INSTAGRAM_CAPTION_HASHTAGS


def test_instagram_caption_fixed_strings_are_constants() -> None:
    """冒頭フック・免責・CTA・ハッシュタグは固定定数であること。"""
    assert sns.INSTAGRAM_CAPTION_DISCLAIMER.startswith("※現在は仮想資金")
    assert "シミュレーション運用" in sns.INSTAGRAM_CAPTION_DISCLAIMER
    assert "フォローして経過を見てもらえたら嬉しいです。" in sns.INSTAGRAM_CAPTION_CTA
    assert "#BTC" in sns.INSTAGRAM_CAPTION_HASHTAGS
    assert "#ビットコイン自動売買" in sns.INSTAGRAM_CAPTION_HASHTAGS
    assert sns.INSTAGRAM_CAPTION_HOOKS[0] == (
        "今日もAIが自分の判断でBTCを売買しました。"
    )


def test_threads_text_format_unchanged_by_instagram_caption() -> None:
    """Instagramキャプション追加後も Threads 用本文フォーマットは従来どおり。"""
    public = _allowed_public()
    threads = bpr.format_report_text("2026-07-22", public)
    assert threads.startswith("BTC自動売買 日次レポート (2026-07-22)")
    assert "累積損益: +88円" in threads
    assert "当日決済件数: 79件" in threads
    ig = sns.build_instagram_caption("2026-07-22", public)
    assert "BTC自動売買 日次レポート" not in ig
    assert "決済件数：79件" in ig


def test_sample_data_main_skips_live_collect_and_stays_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def boom_collect():
        calls.append("live")
        raise AssertionError("live collect must not run in sample-data mode")

    monkeypatch.setattr(sns, "build_sns_message", boom_collect)
    monkeypatch.setattr(
        sns,
        "reel_output_path",
        lambda target_date, dry_run=False: tmp_path / f"{target_date}.mp4",
    )
    monkeypatch.setattr(
        sns,
        "try_generate_reel_video",
        lambda target_date, public, output_path, generate_fn=None: (
            Path(output_path).write_bytes(b"x") or Path(output_path),
            None,
        ),
    )
    sent: list[tuple] = []
    monkeypatch.setattr(
        sns,
        "send_sns_telegram_video",
        lambda *a, **k: sent.append(("video", a, k)) or True,
    )
    monkeypatch.setattr(
        sns,
        "send_sns_telegram_message",
        lambda *a, **k: sent.append(("text", a, k)) or True,
    )

    assert sns.main(["--sample-data"]) == 0
    assert calls == []
    assert sent == []  # dry-run: no Telegram send
