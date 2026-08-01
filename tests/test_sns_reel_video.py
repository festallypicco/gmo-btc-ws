"""
tests/test_sns_reel_video.py

Instagramリール動画生成の許可リスト境界・固定フック・フォールバックを検証する。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Dict

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import build_public_report as bpr  # noqa: E402
import build_sns_report as sns  # noqa: E402
import sns_reel_video as reel  # noqa: E402

# リファクタ前に固定したフック画面ピクセルハッシュ（見た目退行検知用）
_HOOK_FRAME_SHA256 = (
    "d2b196eb1a2ded3451303816c5f0c9ef5c42d39153129e3a23c44a1f5272a186",
    "58f4fc3ab3659ed15b6982d02aea3efc88c9e6120b24aa17f9139a3327147a2b",
    "bfa5196bd7dfe0ae2374f0be23e507c0580dd6f5cf9d1206b1f0565ed0a326e0",
)


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


def test_hook_text_and_layout_are_separated() -> None:
    """文言データと共通レイアウトが分離され、全パターンが同一ルールに従うこと。"""
    layout = reel.HOOK_LAYOUT
    assert layout.line_count == 3
    assert layout.emphasize_line_index == 1
    assert layout.normal_font_size == 64
    assert layout.emphasize_font_size == 96
    assert layout.line_gap == 52
    assert layout.fake_bold_on_emphasize is True

    assert len(reel.HOOK_TEXT_PATTERNS) == 3
    expected = (
        ("AIが24時間", "BTC", "自律的に売買中"),
        ("AI自律運用中", "BTC", "24時間トレード"),
        ("24時間稼働中", "BTC", "AIが自動売買"),
    )
    assert reel.HOOK_TEXT_PATTERNS == expected

    for lines in reel.HOOK_TEXT_PATTERNS:
        assert len(lines) == layout.line_count
        assert lines[layout.emphasize_line_index] == "BTC"

    # 後方互換テキストは文言タプルから導出される
    assert reel.HOOK_TEMPLATES == tuple("\n".join(p) for p in reel.HOOK_TEXT_PATTERNS)
    assert reel.HOOK_TEMPLATES[1] == reel.HOOK_BTC_EMPHASIS
    assert "自律" in reel.HOOK_BTC_EMPHASIS
    assert "自立" not in reel.HOOK_BTC_EMPHASIS

    for day in ("2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23"):
        assert reel.select_hook_lines(day) in reel.HOOK_TEXT_PATTERNS
        assert reel.select_hook_text(day) in reel.HOOK_TEMPLATES
    hooks = {reel.select_hook_text(f"2026-07-{d:02d}") for d in range(1, 28)}
    assert len(hooks) >= 2
    assert hooks.issubset(set(reel.HOOK_TEMPLATES))


def test_hook_frames_match_pre_refactor_pixels() -> None:
    """リファクタ後もフック画面の見た目が完全に同一であること。"""
    template = reel.build_background_template("2026-07-24")
    assert len(reel.HOOK_TEMPLATES) == len(_HOOK_FRAME_SHA256)
    for hook, expected in zip(reel.HOOK_TEMPLATES, _HOOK_FRAME_SHA256):
        img = reel._draw_hook_on_template(template, hook, alpha=1.0)
        digest = hashlib.sha256(img.tobytes()).hexdigest()
        assert digest == expected, f"hook visual changed: {hook!r}"


def test_reel_slides_use_only_allowed_keys() -> None:
    public = _allowed_public()
    hook, slides = reel.build_reel_slide_texts("2026-07-22", public)
    assert hook in reel.HOOK_TEMPLATES
    used_keys = {k for group in reel.METRIC_SLIDE_GROUPS for k in group}
    assert used_keys == bpr.ALLOWED_KEYS
    # スライド本文は許可リスト由来の表示のみ（内部語が出ない）
    blob = "\n".join(line for _title, lines in slides for line in lines)
    for word in ("imbalance", "offset", "config", "profile", "threshold", "LLM"):
        assert word.lower() not in blob.lower()
    assert "累積損益" in blob
    assert "当日勝率" in blob
    # LIVE/RUNNING 等の稼働断定表現を出さない
    for banned in ("LIVE", "RUNNING", "NORMAL"):
        assert banned not in blob
        assert banned not in hook


def test_reel_cards_are_label_value_rows() -> None:
    public = _allowed_public()
    hook, cards = reel.build_reel_cards("2026-07-22", public)
    assert hook in reel.HOOK_TEMPLATES
    assert len(cards) == 2  # 切替を減らした2カード構成
    flat = [row for card in cards for row in card]
    assert {row.key for row in flat} == bpr.ALLOWED_KEYS
    pnl = next(r for r in flat if r.key == "daily_pnl_jpy")
    assert pnl.signed is True
    assert pnl.value_text.startswith("+")


def test_signed_value_colors_match_report_sign_logic() -> None:
    pos = reel._format_countup_value(
        reel.MetricRow(
            key="daily_pnl_jpy",
            label="当日損益",
            value_text="+100円",
            target_number=100.0,
            signed=True,
            is_jpy=True,
        ),
        1.0,
    )
    neg = reel._format_countup_value(
        reel.MetricRow(
            key="daily_pnl_jpy",
            label="当日損益",
            value_text="-50円",
            target_number=-50.0,
            signed=True,
            is_jpy=True,
        ),
        1.0,
    )
    assert pos[1] == reel.VALUE_POS
    assert neg[1] == reel.VALUE_NEG
    assert reel.VALUE_NEG == (255, 138, 128)


def test_unit_font_is_smaller_than_value_font() -> None:
    ratio = reel.UNIT_FONT_SIZE / reel.VALUE_FONT_SIZE
    assert 0.70 <= ratio <= 0.80
    assert reel.UNIT_FONT_SIZE < reel.VALUE_FONT_SIZE


def test_bundled_mono_font_exists() -> None:
    assert reel.MONO_FONT_PATH.exists()
    assert reel.DEFAULT_FONT_PATH.exists()


def test_background_template_has_neutral_chrome_only() -> None:
    img = reel.build_background_template("2026-07-22")
    assert img.size == (reel.VIDEO_WIDTH, reel.VIDEO_HEIGHT)
    # 純黒ではないネイビー系
    assert img.getpixel((10, 10)) == reel.BG_COLOR
    assert "LIVE" not in reel.SYSTEM_NAME
    assert "RUNNING" not in reel.SYSTEM_NAME


def test_reel_rejects_extra_keys() -> None:
    public = _allowed_public()
    public["secret_equity"] = 999999
    with pytest.raises(ValueError):
        reel.build_reel_slide_texts("2026-07-22", public)


def test_video_failure_falls_back_to_text_and_alerts_internal() -> None:
    public = _allowed_public()
    alerts: list[str] = []
    videos: list[tuple] = []
    texts: list[str] = []

    def boom(*_args, **_kwargs):
        raise RuntimeError("ffmpeg missing")

    code = sns.deliver_sns_report(
        "2026-07-22",
        public,
        "THREADS BODY",
        "IG CAPTION",
        dry_run=False,
        generate_fn=boom,
        send_video_fn=lambda *a, **k: videos.append((a, k)) or True,
        send_text_fn=lambda text: texts.append(text) or True,
        alert_fn=lambda msg: alerts.append(msg),
    )
    assert code == 0
    assert videos == []
    assert len(texts) == 2
    assert texts[0].startswith(sns.TELEGRAM_LABEL_INSTAGRAM)
    assert "IG CAPTION" in texts[0]
    assert texts[1].startswith(sns.TELEGRAM_LABEL_THREADS)
    assert "THREADS BODY" in texts[1]
    assert len(alerts) == 1
    assert "video generation failed" in alerts[0]
    assert "text only" in alerts[0]


def test_video_success_sends_instagram_video_and_threads_text() -> None:
    public = _allowed_public()
    alerts: list[str] = []
    videos: list[tuple] = []
    texts: list[str] = []

    def fake_gen(target_date, public, output_path, **_kwargs):
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00\x00fake-mp4")
        return path

    code = sns.deliver_sns_report(
        "2026-07-22",
        public,
        "THREADS BODY",
        "IG CAPTION",
        dry_run=False,
        generate_fn=fake_gen,
        send_video_fn=lambda path, caption="": videos.append((str(path), caption))
        or True,
        send_text_fn=lambda text: texts.append(text) or True,
        alert_fn=lambda msg: alerts.append(msg),
    )
    assert code == 0
    assert len(videos) == 1
    assert videos[0][1].startswith(sns.TELEGRAM_LABEL_INSTAGRAM)
    assert "IG CAPTION" in videos[0][1]
    assert len(texts) == 1
    assert texts[0].startswith(sns.TELEGRAM_LABEL_THREADS)
    assert "THREADS BODY" in texts[0]
    assert alerts == []


def test_generate_reel_video_smoke(tmp_path: Path) -> None:
    """実ffmpegで短い検証用動画を生成できること（環境依存）。"""
    public = _allowed_public()
    out = tmp_path / "reel.mp4"
    original = (
        reel.HOOK_DURATION_SEC,
        reel.CARD_DURATION_SEC,
        reel.TEXT_FADE_SEC,
        reel.COUNTUP_SEC,
        reel.ROW_STAGGER_SEC,
    )
    try:
        reel.HOOK_DURATION_SEC = 0.4
        reel.CARD_DURATION_SEC = 0.6
        reel.TEXT_FADE_SEC = 0.1
        reel.COUNTUP_SEC = 0.2
        reel.ROW_STAGGER_SEC = 0.05
        path = reel.generate_sns_reel_video(
            "2026-07-22",
            public,
            out,
            fps=8,
            work_dir=tmp_path / "frames",
        )
    finally:
        (
            reel.HOOK_DURATION_SEC,
            reel.CARD_DURATION_SEC,
            reel.TEXT_FADE_SEC,
            reel.COUNTUP_SEC,
            reel.ROW_STAGGER_SEC,
        ) = original
    assert path.exists()
    assert path.stat().st_size > 1000
