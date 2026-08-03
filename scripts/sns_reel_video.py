"""
Instagramリール向け縦型短尺動画の生成（SNSステップ2）。

- データは build_public_report の許可リスト済み dict のみを使用する
- 冒頭フックは固定テンプレート（LLM非使用）
- 背景テンプレート上に動的テキストのみ描画し、ffmpegで無音 mp4 に組み立てる
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

import build_public_report as bpr

LOGGER = logging.getLogger("sns_reel")

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "config.json"
DEFAULT_FONT_PATH = ROOT_DIR / "assets" / "fonts" / "NotoSansJP-Regular.ttf"
MONO_FONT_PATH = ROOT_DIR / "assets" / "fonts" / "JetBrainsMono-Bold.ttf"

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FPS = 24

# Instagram UI に隠れない上下セーフゾーン
SAFE_TOP = 240
SAFE_BOTTOM = 300

HOOK_DURATION_SEC = 2.0
# 指標カードは2枚に集約（画面切替を減らす）
CARD_DURATION_SEC = 5.0
TEXT_FADE_SEC = 0.55
COUNTUP_SEC = 0.9
ROW_STAGGER_SEC = 0.12
# フック行ごとのフェード開始ずれ（静止感を抑える）
HOOK_LINE_STAGGER_SEC = 0.18

# わずかに青みがかったダークネイビー（純黒ではない）
BG_COLOR = (14, 22, 40)
CARD_FILL = (20, 30, 52)
CARD_BORDER = (70, 100, 140)
HEADER_COLOR = (150, 170, 195)
HEADER_DATE_COLOR = (170, 185, 205)
LABEL_COLOR = (150, 160, 175)
VALUE_NEUTRAL = (235, 240, 250)
VALUE_POS = (80, 200, 140)
# マイナス表示用。背景ネイビーに対して視認しやすい明度・彩度の高い赤
VALUE_NEG = (255, 138, 128)  # #FF8A80
HOOK_TEXT_COLOR = (245, 247, 250)
BADGE_FILL = (32, 48, 72)
BADGE_BORDER = (90, 130, 170)
BADGE_TEXT_COLOR = (200, 215, 235)

VALUE_FONT_SIZE = 46
# 単位(円・%・日・件・回)は数字の約75%（テキストのみの値には使わない）
UNIT_FONT_RATIO = 0.75
UNIT_FONT_SIZE = max(1, int(round(VALUE_FONT_SIZE * UNIT_FONT_RATIO)))
LABEL_FONT_SIZE = 34

SYSTEM_NAME = "BTC/JPY AUTO-TRADING SYSTEM"
# 実資金モード時のみ。断定・勧誘ではなく稼働事実のみ。
REAL_MODE_BADGE_TEXT = "実資金で稼働中"


@dataclass(frozen=True)
class HookLayout:
    """
    フック画面の共通描画ルール（文言と独立）。
    見た目の変更はこの1箇所だけを直す。
    """

    line_count: int
    emphasize_line_index: int  # 0-based
    normal_font_size: int
    emphasize_font_size: int
    line_gap: int
    fake_bold_on_emphasize: bool


# 3パターン共通の描画ルール（行数・強調行・フォント・行間）
HOOK_LAYOUT = HookLayout(
    line_count=3,
    emphasize_line_index=1,
    normal_font_size=64,
    emphasize_font_size=96,
    line_gap=52,
    fake_bold_on_emphasize=True,
)

# 人手レビュー済み。LLMには生成させない。
# 日替わりで文言だけを変える（描画設定は HOOK_LAYOUT のみ）。
HOOK_TEXT_PATTERNS: Tuple[Tuple[str, ...], ...] = (
    ("AIが実資金で自動売買", "BTC", "24時間稼働中"),
    ("AIが実資金で運用中", "BTC", "24時間自動売買"),
    ("実資金でAIが売買", "BTC", "休みなく稼働中"),
)


def _join_hook_lines(lines: Sequence[str]) -> str:
    return "\n".join(lines)


# 後方互換: 改行結合テキスト（既存呼び出し・テスト用）
HOOK_TEMPLATES: Tuple[str, ...] = tuple(
    _join_hook_lines(lines) for lines in HOOK_TEXT_PATTERNS
)
HOOK_BTC_EMPHASIS = HOOK_TEMPLATES[1]

# 後方互換エイリアス（値の正本は HOOK_LAYOUT）
HOOK_FONT_SIZE = HOOK_LAYOUT.normal_font_size
HOOK_BTC_FONT_SIZE = HOOK_LAYOUT.emphasize_font_size
HOOK_BTC_LINE_GAP = HOOK_LAYOUT.line_gap

# 許可リスト9項目を2カードに集約（切替回数を減らす）
METRIC_SLIDE_GROUPS: Tuple[Tuple[str, ...], ...] = (
    (
        "cumulative_pnl_jpy",
        "daily_pnl_jpy",
        "max_drawdown_pct",
        "win_rate_cumulative_pct",
        "win_rate_daily_pct",
    ),
    (
        "uptime_days",
        "trade_count",
        "entry_count",
        "circuit_breaker_triggered",
    ),
)

# 固定ヘッダー（セーフゾーン上端内・全フレーム共通）
HEADER_NAME_Y = SAFE_TOP
HEADER_DATE_Y = SAFE_TOP + 36
HEADER_BADGE_Y = SAFE_TOP + 74

# カード領域（セーフゾーン内・ヘッダー下）
CARD_LEFT = 72
CARD_RIGHT = VIDEO_WIDTH - 72
CARD_TOP = SAFE_TOP + 130
CARD_BOTTOM = VIDEO_HEIGHT - SAFE_BOTTOM - 40
LABEL_X = CARD_LEFT + 48
VALUE_RIGHT_X = CARD_RIGHT - 48
ROW_START_Y = CARD_TOP + 72
ROW_HEIGHT = 118


@dataclass(frozen=True)
class MetricRow:
    """カード1行分の表示データ。"""

    key: str
    label: str
    value_text: str
    target_number: Optional[float]
    signed: bool  # True なら +/- で緑/赤
    suffix: str = ""
    decimals: int = 0
    is_jpy: bool = False


def select_hook_lines(trading_day: str) -> Tuple[str, ...]:
    """取引日で決定的にローテーションする固定フック文言（行テキストのみ）。"""
    if not HOOK_TEXT_PATTERNS:
        raise RuntimeError("HOOK_TEXT_PATTERNS is empty")
    idx = sum(ord(c) for c in trading_day) % len(HOOK_TEXT_PATTERNS)
    return HOOK_TEXT_PATTERNS[idx]


def select_hook_text(trading_day: str) -> str:
    """取引日で決定的にローテーションする固定フック文言（改行結合）。"""
    return _join_hook_lines(select_hook_lines(trading_day))


def _metric_row(key: str, public: Dict[str, object]) -> MetricRow:
    """許可リストキー1件をカード行に変換する。"""
    if key == "cumulative_pnl_jpy":
        v = float(public[key])
        return MetricRow(
            key=key,
            label="累積損益",
            value_text=bpr._format_jpy_signed(v),
            target_number=v,
            signed=True,
            is_jpy=True,
        )
    if key == "daily_pnl_jpy":
        v = float(public[key])
        return MetricRow(
            key=key,
            label="当日損益",
            value_text=bpr._format_jpy_signed(v),
            target_number=v,
            signed=True,
            is_jpy=True,
        )
    if key == "max_drawdown_pct":
        v = float(public[key])
        return MetricRow(
            key=key,
            label="最大ドローダウン",
            value_text=f"{v:.2f}%",
            target_number=v,
            signed=False,
            suffix="%",
            decimals=2,
        )
    if key == "uptime_days":
        v = int(public[key])
        return MetricRow(
            key=key,
            label="稼働継続日数",
            value_text=f"{v}日",
            target_number=float(v),
            signed=False,
            suffix="日",
            decimals=0,
        )
    if key == "trade_count":
        v = int(public[key])
        return MetricRow(
            key=key,
            label="当日決済件数",
            value_text=f"{v}件",
            target_number=float(v),
            signed=False,
            suffix="件",
            decimals=0,
        )
    if key == "entry_count":
        v = int(public[key])
        return MetricRow(
            key=key,
            label="当日エントリー回数",
            value_text=f"{v}回",
            target_number=float(v),
            signed=False,
            suffix="回",
            decimals=0,
        )
    if key == "win_rate_cumulative_pct":
        v = float(public[key])
        return MetricRow(
            key=key,
            label="累積勝率",
            value_text=f"{v:.1f}%",
            target_number=v,
            signed=False,
            suffix="%",
            decimals=1,
        )
    if key == "win_rate_daily_pct":
        v = float(public[key])
        return MetricRow(
            key=key,
            label="当日勝率",
            value_text=f"{v:.1f}%",
            target_number=v,
            signed=False,
            suffix="%",
            decimals=1,
        )
    if key == "circuit_breaker_triggered":
        cb = "発動あり" if public[key] else "発動なし"
        return MetricRow(
            key=key,
            label="サーキットブレーカー",
            value_text=cb,
            target_number=None,
            signed=False,
        )
    raise KeyError(f"unsupported metric key for reel: {key}")


def build_reel_slide_texts(
    target_date: str,
    public: Dict[str, object],
) -> Tuple[str, List[Tuple[str, List[str]]]]:
    """
    互換用: フックと「ラベル / 値」文字列リストを返す。
    戻り値: (hook_text, [(card_title, lines), ...])
    """
    hook, cards = build_reel_cards(target_date, public)
    slides: List[Tuple[str, List[str]]] = []
    for idx, rows in enumerate(cards, start=1):
        lines = [f"{row.label}  {row.value_text}" for row in rows]
        slides.append((f"指標カード{idx}", lines))
    return hook, slides


def build_reel_cards(
    target_date: str,
    public: Dict[str, object],
) -> Tuple[str, List[List[MetricRow]]]:
    """
    動画用カード素材を組み立てる。
    戻り値: (hook_text, [card_rows, ...])
    """
    bpr.assert_only_allowed_keys(public)
    used_keys = {k for group in METRIC_SLIDE_GROUPS for k in group}
    if used_keys != bpr.ALLOWED_KEYS:
        missing = bpr.ALLOWED_KEYS - used_keys
        extra = used_keys - bpr.ALLOWED_KEYS
        raise RuntimeError(
            f"reel metric groups must cover ALLOWED_KEYS exactly; "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )

    hook = select_hook_text(target_date)
    cards = [[_metric_row(key, public) for key in group] for group in METRIC_SLIDE_GROUPS]
    return hook, cards


def _load_font(size: int, font_path: Path) -> ImageFont.FreeTypeFont:
    if not font_path.exists():
        raise FileNotFoundError(
            f"Bundled font missing: {font_path}. "
            "Place required fonts under assets/fonts/."
        )
    return ImageFont.truetype(str(font_path), size=size)


def _signed_color(value: float) -> Tuple[int, int, int]:
    """テキストレポートの符号ロジックに合わせ、>=0 を緑系、<0 を赤系。"""
    rounded = int(round(value))
    return VALUE_POS if rounded >= 0 else VALUE_NEG


def _format_countup_value(row: MetricRow, progress: float) -> Tuple[str, Tuple[int, int, int]]:
    """カウントアップ途中の表示文字列と色を返す。progress は 0..1。"""
    p = max(0.0, min(1.0, progress))
    if row.target_number is None:
        return row.value_text, VALUE_NEUTRAL

    current = row.target_number * p
    if row.is_jpy:
        text = bpr._format_jpy_signed(current if p < 1.0 else row.target_number)
        color = _signed_color(row.target_number)
        return text, color

    if row.decimals > 0:
        text = f"{current:.{row.decimals}f}{row.suffix}"
    else:
        text = f"{int(round(current))}{row.suffix}"
    if p >= 1.0:
        text = row.value_text
    return text, VALUE_NEUTRAL


def _split_mono_and_suffix(row: MetricRow, value_text: str) -> Tuple[str, str]:
    """
    等幅フォントで描く部分と、Noto で描く単位を分離する。
    JetBrains Mono は日本語グリフを持たないため「円」等は Noto 側へ回す。
    """
    if row.target_number is None:
        return "", value_text
    if row.is_jpy and value_text.endswith("円"):
        return value_text[:-1], "円"
    if row.suffix and value_text.endswith(row.suffix):
        return value_text[: -len(row.suffix)], row.suffix
    return value_text, ""


def load_trading_mode(config_path: Path = CONFIG_PATH) -> str:
    """
    config.json の trading_mode を返す。
    ファイル無し・キー無し・不正値は virtual 扱い。
    """
    if not config_path.exists():
        return "virtual"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Failed to read config.json; treat as virtual: %s", exc)
        return "virtual"
    if not isinstance(payload, dict):
        return "virtual"
    mode = str(payload.get("trading_mode", "virtual")).strip().lower()
    if mode not in {"virtual", "real"}:
        return "virtual"
    return mode


def _draw_fixed_header(
    draw: ImageDraw.ImageDraw,
    target_date: str,
    *,
    trading_mode: str,
    label_font_path: Path,
) -> None:
    """全フレーム共通の固定ヘッダー（システム名・日付・任意で実資金バッジ）。"""
    name_font = _load_font(28, label_font_path)
    date_font = _load_font(30, label_font_path)
    badge_font = _load_font(24, label_font_path)

    hb = draw.textbbox((0, 0), SYSTEM_NAME, font=name_font)
    hw = hb[2] - hb[0]
    draw.text(
        ((VIDEO_WIDTH - hw) // 2, HEADER_NAME_Y),
        SYSTEM_NAME,
        font=name_font,
        fill=HEADER_COLOR,
    )

    db = draw.textbbox((0, 0), target_date, font=date_font)
    dw = db[2] - db[0]
    draw.text(
        ((VIDEO_WIDTH - dw) // 2, HEADER_DATE_Y),
        target_date,
        font=date_font,
        fill=HEADER_DATE_COLOR,
    )

    if trading_mode == "real":
        pad_x = 18
        pad_y = 8
        bb = draw.textbbox((0, 0), REAL_MODE_BADGE_TEXT, font=badge_font)
        tw = bb[2] - bb[0]
        th = bb[3] - bb[1]
        box_w = tw + pad_x * 2
        box_h = th + pad_y * 2
        left = (VIDEO_WIDTH - box_w) // 2
        top = HEADER_BADGE_Y
        draw.rounded_rectangle(
            (left, top, left + box_w, top + box_h),
            radius=14,
            fill=BADGE_FILL,
            outline=BADGE_BORDER,
            width=2,
        )
        draw.text(
            (left + pad_x, top + pad_y - 2),
            REAL_MODE_BADGE_TEXT,
            font=badge_font,
            fill=BADGE_TEXT_COLOR,
        )


def build_background_template(
    target_date: str,
    *,
    label_font_path: Path = DEFAULT_FONT_PATH,
    trading_mode: str = "virtual",
    show_card_frame: bool = True,
) -> Image.Image:
    """
    固定ヘッダー込みの背景テンプレートを1枚生成する。
    動的テキストはこの上の指定座標にのみ描画する。
    """
    img = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)
    mode = str(trading_mode or "virtual").strip().lower()
    if mode not in {"virtual", "real"}:
        mode = "virtual"

    _draw_fixed_header(
        draw,
        target_date,
        trading_mode=mode,
        label_font_path=label_font_path,
    )

    if show_card_frame:
        radius = 28
        draw.rounded_rectangle(
            (CARD_LEFT, CARD_TOP, CARD_RIGHT, CARD_BOTTOM),
            radius=radius,
            fill=CARD_FILL,
            outline=CARD_BORDER,
            width=3,
        )
    return img


def _hook_line_alphas(
    frame_index: int,
    fps: int,
    line_count: int,
) -> List[float]:
    """フック各行のフェード進捗（行ごとに開始をずらし静止感を抑える）。"""
    fade_frames = max(1, int(round(TEXT_FADE_SEC * fps)))
    stagger = max(1, int(round(HOOK_LINE_STAGGER_SEC * fps)))
    alphas: List[float] = []
    for i in range(line_count):
        local = frame_index - i * stagger
        alphas.append(min(1.0, max(0.0, (local + 1) / fade_frames)))
    return alphas


def _draw_hook_on_template(
    template: Image.Image,
    hook_text: str,
    *,
    alpha: float = 1.0,
    line_alphas: Optional[Sequence[float]] = None,
    font_path: Path = DEFAULT_FONT_PATH,
    layout: HookLayout = HOOK_LAYOUT,
) -> Image.Image:
    """
    フック文言を共通レイアウトで描画する。
    パターンごとの描画分岐は持たず、見た目ルールは layout のみを参照する。
    """
    base = template.convert("RGBA")
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    lines = hook_text.split("\n")
    if len(lines) != layout.line_count:
        raise ValueError(
            f"hook text must have {layout.line_count} lines "
            f"(layout), got {len(lines)}: {hook_text!r}"
        )
    emp_i = layout.emphasize_line_index
    if not (0 <= emp_i < layout.line_count):
        raise ValueError(
            f"emphasize_line_index out of range: {emp_i} "
            f"(line_count={layout.line_count})"
        )

    if line_alphas is None:
        alphas = [alpha] * layout.line_count
    else:
        if len(line_alphas) != layout.line_count:
            raise ValueError(
                f"line_alphas must have {layout.line_count} entries, "
                f"got {len(line_alphas)}"
            )
        alphas = [float(a) * float(alpha) for a in line_alphas]

    normal_font = _load_font(layout.normal_font_size, font_path)
    emphasize_font = _load_font(layout.emphasize_font_size, font_path)
    line_gap = layout.line_gap

    fonts = [
        emphasize_font if i == emp_i else normal_font for i in range(len(lines))
    ]

    heights = []
    widths = []
    for line, font in zip(lines, fonts):
        bbox = draw.textbbox((0, 0), line, font=font)
        widths.append(bbox[2] - bbox[0])
        heights.append(bbox[3] - bbox[1])
    total_h = sum(heights) + line_gap * (len(lines) - 1)
    center_y = (CARD_TOP + CARD_BOTTOM) // 2
    y = center_y - total_h // 2
    for i, (line, w, h, font, line_a) in enumerate(
        zip(lines, widths, heights, fonts, alphas)
    ):
        a = max(0, min(255, int(round(255 * line_a))))
        if a <= 0:
            y += h + line_gap
            continue
        x = (VIDEO_WIDTH - w) // 2
        fill = (*HOOK_TEXT_COLOR, a)
        if layout.fake_bold_on_emphasize and i == emp_i:
            # 太さ強調（同梱 Regular のみのため疑似ボールド）
            for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
                draw.text((x + dx, y + dy), line, font=font, fill=fill)
        else:
            draw.text((x, y), line, font=font, fill=fill)
        y += h + line_gap
    return Image.alpha_composite(base, overlay).convert("RGB")


def _draw_card_rows_on_template(
    template: Image.Image,
    rows: Sequence[MetricRow],
    *,
    frame_index: int,
    fps: int,
    label_font_path: Path = DEFAULT_FONT_PATH,
    mono_font_path: Path = MONO_FONT_PATH,
) -> Image.Image:
    """
    カード行を描画する。行ごとにフェードイン＋数値カウントアップ。
    背景テンプレート自体は常に不透明（画面全体の暗転なし）。
    """
    base = template.convert("RGBA")
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    label_font = _load_font(LABEL_FONT_SIZE, label_font_path)
    value_font = _load_font(VALUE_FONT_SIZE, mono_font_path)
    unit_font = _load_font(UNIT_FONT_SIZE, label_font_path)

    fade_frames = max(1, int(round(TEXT_FADE_SEC * fps)))
    count_frames = max(1, int(round(COUNTUP_SEC * fps)))
    stagger = max(1, int(round(ROW_STAGGER_SEC * fps)))

    for i, row in enumerate(rows):
        start = i * stagger
        local = frame_index - start
        if local < 0:
            continue
        fade_p = min(1.0, (local + 1) / fade_frames)
        count_p = min(1.0, max(0.0, local / count_frames))
        a = max(0, min(255, int(round(255 * fade_p))))

        y = ROW_START_Y + i * ROW_HEIGHT
        # ラベル（左・小さめ・薄いグレー）
        draw.text(
            (LABEL_X, y + 8),
            row.label,
            font=label_font,
            fill=(*LABEL_COLOR, a),
        )

        value_text, color = _format_countup_value(row, count_p)
        mono_part, suffix_part = _split_mono_and_suffix(row, value_text)

        # 右端揃え: 単位(Noto・数字の70-80%) + 数値(等幅・現状サイズ)。
        cursor_x = VALUE_RIGHT_X
        if suffix_part:
            sb = draw.textbbox((0, 0), suffix_part, font=unit_font)
            sw = sb[2] - sb[0]
            # 数字のベースラインに合わせ、小さめ単位をやや下げる
            unit_y = y + max(0, (VALUE_FONT_SIZE - UNIT_FONT_SIZE) // 2)
            cursor_x -= sw
            draw.text(
                (cursor_x, unit_y),
                suffix_part,
                font=unit_font,
                fill=(*color, a),
            )
            cursor_x -= 6
        if mono_part:
            vb = draw.textbbox((0, 0), mono_part, font=value_font)
            vw = vb[2] - vb[0]
            cursor_x -= vw
            draw.text(
                (cursor_x, y),
                mono_part,
                font=value_font,
                fill=(*color, a),
            )
        elif value_text and not suffix_part:
            # サーキットブレーカー等の純テキスト（単位縮小の対象外）
            vb = draw.textbbox((0, 0), value_text, font=label_font)
            vw = vb[2] - vb[0]
            draw.text(
                (VALUE_RIGHT_X - vw, y + 8),
                value_text,
                font=label_font,
                fill=(*VALUE_NEUTRAL, a),
            )

        # 行区切り
        if i < len(rows) - 1:
            sep_y = y + ROW_HEIGHT - 18
            draw.line(
                (LABEL_X, sep_y, VALUE_RIGHT_X, sep_y),
                fill=(55, 75, 105, max(0, a // 2)),
                width=1,
            )

    return Image.alpha_composite(base, overlay).convert("RGB")


def _resolve_ffmpeg() -> str:
    which = shutil.which("ffmpeg")
    if which:
        return which
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(
            "ffmpeg not found. Install ffmpeg or pip install imageio-ffmpeg."
        ) from exc


def encode_frames_to_mp4(
    frames_dir: Path,
    output_path: Path,
    *,
    fps: int = FPS,
    pattern: str = "frame_%05d.png",
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _resolve_ffmpeg()
    input_pattern = str(frames_dir / pattern)
    cmd = [
        ffmpeg,
        "-y",
        "-framerate",
        str(fps),
        "-i",
        input_pattern,
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            f"ffmpeg failed (exit={proc.returncode}): {detail[-1500:]}"
        )
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError(f"mp4 was not created: {output_path}")
    return output_path


def generate_sns_reel_video(
    target_date: str,
    public: Dict[str, object],
    output_path: Path,
    *,
    font_path: Path = DEFAULT_FONT_PATH,
    mono_font_path: Path = MONO_FONT_PATH,
    fps: int = FPS,
    work_dir: Optional[Path] = None,
    trading_mode: Optional[str] = None,
    config_path: Path = CONFIG_PATH,
) -> Path:
    """
    許可リスト済み public から Instagram リール向け無音 mp4 を生成する。
    trading_mode 未指定時は config.json の値を参照する。
    """
    bpr.assert_only_allowed_keys(public)
    hook, cards = build_reel_cards(target_date, public)
    mode = (
        str(trading_mode).strip().lower()
        if trading_mode is not None
        else load_trading_mode(config_path)
    )
    if mode not in {"virtual", "real"}:
        mode = "virtual"

    # フック中も同じ固定ヘッダーを出し、空のカード枠は出さない
    hook_template = build_background_template(
        target_date,
        label_font_path=font_path,
        trading_mode=mode,
        show_card_frame=False,
    )
    card_template = build_background_template(
        target_date,
        label_font_path=font_path,
        trading_mode=mode,
        show_card_frame=True,
    )

    cleanup_work = False
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="sns_reel_"))
        cleanup_work = True
    else:
        work_dir.mkdir(parents=True, exist_ok=True)

    frame_paths: List[Path] = []
    try:
        # フック: 固定ヘッダー上で行ごとにフェードイン
        hook_n = max(1, int(round(HOOK_DURATION_SEC * fps)))
        for i in range(hook_n):
            frame = _draw_hook_on_template(
                hook_template,
                hook,
                line_alphas=_hook_line_alphas(i, fps, HOOK_LAYOUT.line_count),
                font_path=font_path,
            )
            path = work_dir / f"frame_{len(frame_paths):05d}.png"
            frame.save(path, format="PNG")
            frame_paths.append(path)

        # 指標カード: 行ごとのフェード＋カウントアップ
        card_n = max(1, int(round(CARD_DURATION_SEC * fps)))
        for rows in cards:
            for i in range(card_n):
                frame = _draw_card_rows_on_template(
                    card_template,
                    rows,
                    frame_index=i,
                    fps=fps,
                    label_font_path=font_path,
                    mono_font_path=mono_font_path,
                )
                path = work_dir / f"frame_{len(frame_paths):05d}.png"
                frame.save(path, format="PNG")
                frame_paths.append(path)

        duration_sec = len(frame_paths) / float(fps)
        if duration_sec < 10.0 or duration_sec > 15.5:
            LOGGER.warning(
                "reel duration=%.2fs outside 10-15s target (frames=%d fps=%d)",
                duration_sec,
                len(frame_paths),
                fps,
            )

        return encode_frames_to_mp4(work_dir, output_path, fps=fps)
    finally:
        if cleanup_work:
            shutil.rmtree(work_dir, ignore_errors=True)


def expected_reel_duration_sec() -> float:
    """設計上の想定尺（テスト用）。"""
    return HOOK_DURATION_SEC + CARD_DURATION_SEC * len(METRIC_SLIDE_GROUPS)
