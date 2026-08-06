"""
tests/test_sns_reel_video.py

Instagramリール動画生成の許可リスト境界・固定フック・フォールバックを検証する。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import build_public_report as bpr  # noqa: E402
import build_sns_report as sns  # noqa: E402
import instagram_poster as instagram_api  # noqa: E402
import sns_reel_video as reel  # noqa: E402


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


@dataclass
class _StreamInfo:
    duration_sec: Optional[float]
    has_audio: bool


def _probe_stream_info(path: Path) -> _StreamInfo:
    """
    ffprobeに依存せず、ffmpeg自身の `-i` 出力(stderr)から長さ・音声有無を読み取る
    （imageio-ffmpegはffmpeg本体のみ同梱しffprobeは含まないため）。
    """
    ffmpeg = reel._resolve_ffmpeg()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    stderr = proc.stderr or ""
    has_audio = "Audio:" in stderr
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", stderr)
    duration_sec = None
    if match:
        h, m, s = match.groups()
        duration_sec = int(h) * 3600 + int(m) * 60 + float(s)
    return _StreamInfo(duration_sec=duration_sec, has_audio=has_audio)


def _make_silent_video(path: Path, duration_sec: float, fps: int = 8) -> Path:
    """テスト用の無音動画（単色）をlavfiで生成する。"""
    ffmpeg = reel._resolve_ffmpeg()
    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=64x64:d={duration_sec}:r={fps}",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"failed to create test silent video: {proc.stderr}")
    return path


def _make_sine_bgm(path: Path, duration_sec: float) -> Path:
    """テスト用の合成BGM（サイン波・wav）をlavfiで生成する。"""
    ffmpeg = reel._resolve_ffmpeg()
    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_sec}",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"failed to create test bgm: {proc.stderr}")
    return path


def _header_strip(img) -> bytes:
    """固定ヘッダー領域（システム名・日付・バッジ）のピクセル。"""
    bottom = reel.CARD_TOP - 8
    return img.crop((0, reel.SAFE_TOP, reel.VIDEO_WIDTH, bottom)).tobytes()


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
        ("AIが実資金で自動売買", "BTC", "24時間稼働中"),
        ("AIが実資金で運用中", "BTC", "24時間自動売買"),
        ("実資金でAIが売買", "BTC", "休みなく稼働中"),
    )
    assert reel.HOOK_TEXT_PATTERNS == expected

    for lines in reel.HOOK_TEXT_PATTERNS:
        assert len(lines) == layout.line_count
        assert lines[layout.emphasize_line_index] == "BTC"

    # 後方互換テキストは文言タプルから導出される
    assert reel.HOOK_TEMPLATES == tuple("\n".join(p) for p in reel.HOOK_TEXT_PATTERNS)
    assert reel.HOOK_TEMPLATES[1] == reel.HOOK_BTC_EMPHASIS
    assert "実資金" in reel.HOOK_BTC_EMPHASIS

    for day in ("2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23"):
        assert reel.select_hook_lines(day) in reel.HOOK_TEXT_PATTERNS
        assert reel.select_hook_text(day) in reel.HOOK_TEMPLATES
    hooks = {reel.select_hook_text(f"2026-07-{d:02d}") for d in range(1, 28)}
    assert len(hooks) >= 2
    assert hooks.issubset(set(reel.HOOK_TEMPLATES))


def test_fixed_header_present_on_hook_and_card_templates() -> None:
    """フック／カード双方で同一の固定ヘッダー（システム名＋日付）を持つ。"""
    hook_bg = reel.build_background_template(
        "2026-07-24",
        trading_mode="virtual",
        show_card_frame=False,
    )
    card_bg = reel.build_background_template(
        "2026-07-24",
        trading_mode="virtual",
        show_card_frame=True,
    )
    assert hook_bg.size == (reel.VIDEO_WIDTH, reel.VIDEO_HEIGHT)
    assert _header_strip(hook_bg) == _header_strip(card_bg)
    # ヘッダー領域が背景色だけではない（テキストが描かれている）
    assert _header_strip(hook_bg) != bytes(
        [reel.BG_COLOR[0], reel.BG_COLOR[1], reel.BG_COLOR[2]]
    ) * (len(_header_strip(hook_bg)) // 3)


def test_real_mode_badge_toggles_with_trading_mode() -> None:
    virtual = reel.build_background_template(
        "2026-07-24", trading_mode="virtual", show_card_frame=False
    )
    real = reel.build_background_template(
        "2026-07-24", trading_mode="real", show_card_frame=False
    )
    assert _header_strip(virtual) != _header_strip(real)
    assert "LIVE" not in reel.REAL_MODE_BADGE_TEXT
    assert "RUNNING" not in reel.REAL_MODE_BADGE_TEXT
    assert "実資金" in reel.REAL_MODE_BADGE_TEXT


def test_load_trading_mode_from_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"trading_mode": "real"}), encoding="utf-8")
    assert reel.load_trading_mode(cfg) == "real"
    cfg.write_text(json.dumps({"trading_mode": "virtual"}), encoding="utf-8")
    assert reel.load_trading_mode(cfg) == "virtual"
    assert reel.load_trading_mode(tmp_path / "missing.json") == "virtual"


def test_hook_line_fade_is_animated() -> None:
    """フック中は行フェードが進み、序盤フレームと終盤フレームが異なる。"""
    template = reel.build_background_template(
        "2026-07-24", trading_mode="real", show_card_frame=False
    )
    hook = reel.HOOK_TEMPLATES[0]
    early = reel._draw_hook_on_template(
        template,
        hook,
        line_alphas=reel._hook_line_alphas(0, fps=24, line_count=3),
    )
    late = reel._draw_hook_on_template(
        template,
        hook,
        line_alphas=reel._hook_line_alphas(40, fps=24, line_count=3),
    )
    assert early.tobytes() != late.tobytes()
    # ヘッダーはフェード対象外で同一
    assert _header_strip(early) == _header_strip(late)


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
    # フック／スライド本文に LIVE/RUNNING 断定は出さない（バッジは別レイヤ）
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
    img = reel.build_background_template("2026-07-22", trading_mode="virtual")
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
        instagram_post_fn=lambda *a, **k: instagram_api.InstagramPostResult(
            status="skipped_no_video", detail="video generation failed"
        ),
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
        instagram_post_fn=lambda *a, **k: instagram_api.InstagramPostResult(
            status="success", media_id="ig123", post_url="https://instagram.com/reel/ig123"
        ),
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
    """実ffmpegで短い検証用動画を生成できること（環境依存）。

    BGM合成のffmpegフィルタ対応状況に依存せず既存挙動を維持するため、
    ここでは enable_bgm=False で従来通り無音動画のみを検証する。
    BGM合成自体のテストは別ケース（test_generate_reel_video_with_bgm_smoke 等）で行う。
    """
    public = _allowed_public()
    out = tmp_path / "reel.mp4"
    original = (
        reel.HOOK_DURATION_SEC,
        reel.CARD_DURATION_SEC,
        reel.TEXT_FADE_SEC,
        reel.COUNTUP_SEC,
        reel.ROW_STAGGER_SEC,
        reel.HOOK_LINE_STAGGER_SEC,
    )
    try:
        reel.HOOK_DURATION_SEC = 0.4
        reel.CARD_DURATION_SEC = 0.6
        reel.TEXT_FADE_SEC = 0.1
        reel.COUNTUP_SEC = 0.2
        reel.ROW_STAGGER_SEC = 0.05
        reel.HOOK_LINE_STAGGER_SEC = 0.04
        path = reel.generate_sns_reel_video(
            "2026-07-22",
            public,
            out,
            fps=8,
            work_dir=tmp_path / "frames",
            trading_mode="real",
            enable_bgm=False,
        )
    finally:
        (
            reel.HOOK_DURATION_SEC,
            reel.CARD_DURATION_SEC,
            reel.TEXT_FADE_SEC,
            reel.COUNTUP_SEC,
            reel.ROW_STAGGER_SEC,
            reel.HOOK_LINE_STAGGER_SEC,
        ) = original
    assert path.exists()
    assert path.stat().st_size > 1000
    assert _probe_stream_info(path).has_audio is False


def test_mux_bgm_loops_short_bgm_to_fill_duration(tmp_path: Path) -> None:
    """BGMが動画より短い場合、ループして尺いっぱいまで音声を満たすこと。"""
    target_duration = 5.0
    video = _make_silent_video(tmp_path / "silent.mp4", duration_sec=target_duration)
    bgm = _make_sine_bgm(tmp_path / "short_bgm.wav", duration_sec=2.0)
    out = tmp_path / "out.mp4"

    result = reel.mux_bgm_into_video(
        video, out, duration_sec=target_duration, bgm_path=bgm
    )

    assert result == out
    assert out.exists() and out.stat().st_size > 0
    info = _probe_stream_info(out)
    assert info.has_audio is True
    assert info.duration_sec is not None
    assert abs(info.duration_sec - target_duration) < 0.3


def test_mux_bgm_trims_long_bgm_to_duration(tmp_path: Path) -> None:
    """BGMが動画より長い場合、動画の尺にトリムされること。"""
    target_duration = 4.0
    video = _make_silent_video(tmp_path / "silent.mp4", duration_sec=target_duration)
    bgm = _make_sine_bgm(tmp_path / "long_bgm.wav", duration_sec=10.0)
    out = tmp_path / "out.mp4"

    reel.mux_bgm_into_video(video, out, duration_sec=target_duration, bgm_path=bgm)

    info = _probe_stream_info(out)
    assert info.has_audio is True
    assert info.duration_sec is not None
    assert abs(info.duration_sec - target_duration) < 0.3


def test_mux_bgm_raises_for_missing_file(tmp_path: Path) -> None:
    """BGMファイルが存在しない場合はFileNotFoundErrorを送出する。"""
    video = _make_silent_video(tmp_path / "silent.mp4", duration_sec=2.0)
    with pytest.raises(FileNotFoundError):
        reel.mux_bgm_into_video(
            video,
            tmp_path / "out.mp4",
            duration_sec=2.0,
            bgm_path=tmp_path / "does_not_exist.mp3",
        )


def _generate_reel_with_tiny_durations(
    tmp_path: Path,
    *,
    enable_bgm: bool,
    bgm_path: Optional[Path] = None,
) -> Path:
    """スモークテスト用に極短尺設定でリール動画を生成する共通ヘルパー。"""
    public = _allowed_public()
    out = tmp_path / "reel.mp4"
    original = (
        reel.HOOK_DURATION_SEC,
        reel.CARD_DURATION_SEC,
        reel.TEXT_FADE_SEC,
        reel.COUNTUP_SEC,
        reel.ROW_STAGGER_SEC,
        reel.HOOK_LINE_STAGGER_SEC,
    )
    kwargs = dict(
        fps=8,
        work_dir=tmp_path / "frames",
        trading_mode="real",
        enable_bgm=enable_bgm,
    )
    if bgm_path is not None:
        kwargs["bgm_path"] = bgm_path
    try:
        reel.HOOK_DURATION_SEC = 0.4
        reel.CARD_DURATION_SEC = 0.6
        reel.TEXT_FADE_SEC = 0.1
        reel.COUNTUP_SEC = 0.2
        reel.ROW_STAGGER_SEC = 0.05
        reel.HOOK_LINE_STAGGER_SEC = 0.04
        return reel.generate_sns_reel_video("2026-07-22", public, out, **kwargs)
    finally:
        (
            reel.HOOK_DURATION_SEC,
            reel.CARD_DURATION_SEC,
            reel.TEXT_FADE_SEC,
            reel.COUNTUP_SEC,
            reel.ROW_STAGGER_SEC,
            reel.HOOK_LINE_STAGGER_SEC,
        ) = original


def test_generate_reel_video_with_bgm_smoke(tmp_path: Path) -> None:
    """実ffmpeg・実BGMファイルで、動画に音声トラックが合成されること（環境依存）。"""
    path = _generate_reel_with_tiny_durations(tmp_path, enable_bgm=True)
    assert path.exists()
    assert path.stat().st_size > 1000
    info = _probe_stream_info(path)
    assert info.has_audio is True


def test_generate_reel_video_bgm_missing_falls_back_to_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """BGMファイルが存在しない場合、警告ログを出したうえで無音動画を出力すること。"""
    with caplog.at_level("WARNING", logger="sns_reel"):
        path = _generate_reel_with_tiny_durations(
            tmp_path, enable_bgm=True, bgm_path=tmp_path / "no_such_bgm.mp3"
        )
    assert path.exists()
    assert path.stat().st_size > 1000
    assert _probe_stream_info(path).has_audio is False
    assert any("BGM mux failed" in rec.message for rec in caplog.records)


def test_generate_reel_video_bgm_corrupt_file_falls_back_to_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """BGMファイルの読み込みに失敗する場合も、処理全体は失敗させず無音出力にフォールバックすること。"""
    bad_bgm = tmp_path / "bad_bgm.mp3"
    bad_bgm.write_bytes(b"this is not a valid audio file")
    with caplog.at_level("WARNING", logger="sns_reel"):
        path = _generate_reel_with_tiny_durations(
            tmp_path, enable_bgm=True, bgm_path=bad_bgm
        )
    assert path.exists()
    assert path.stat().st_size > 1000
    assert _probe_stream_info(path).has_audio is False
    assert any("BGM mux failed" in rec.message for rec in caplog.records)
