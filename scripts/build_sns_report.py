"""
SNS投稿用テキスト生成・配信（ステップ1: テキスト + AI考察）。

- 公開数値は build_public_report.collect_public_metrics() のみを再利用する
  （CSV/DBへ直接アクセスしない）
- LLM考察は直近平均との比較ラベルのみを入力とする（生数値は渡さない）
- 配信先は TELEGRAM_SNS_*（SNS用Bot）。失敗アラートのみ内部Botへ送る
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

# scripts/ を path 先頭へ（同梱モジュール解決用）。telegram_notifier 本体もここ。
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) in sys.path:
    sys.path.remove(str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR))

from telegram_notifier import (  # noqa: E402
    send_sns_telegram_message,
    send_sns_telegram_video,
    send_telegram_message,
)

import build_public_report as bpr  # noqa: E402
import sns_reel_video as reel  # noqa: E402

ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT_DIR / "log"
RUNTIME_DIR = ROOT_DIR / "runtime"

# QA専用モック（実ログ非参照）。当日損益はマイナスで赤字配色を確認する。
# target_date は HOOK_BTC_EMPHASIS（index 1）が選ばれる日（見た目確認用）。
SAMPLE_TARGET_DATE = "2026-07-23"
SAMPLE_PUBLIC_METRICS: Dict[str, object] = {
    "cumulative_pnl_jpy": 170.0,
    "daily_pnl_jpy": -86.0,
    "trade_count": 57,
    "win_rate_cumulative_pct": 59.9,
    "win_rate_daily_pct": 61.4,
    "circuit_breaker_triggered": False,
    "uptime_days": 4,
    "entry_count": 90,
    "max_drawdown_pct": 0.15,
}


def build_sample_sns_payload() -> Tuple[str, Dict[str, object], str]:
    """
    QA用: モック指標から (target_date, public, threads_text) を返す。
    実ファイル・本番データには一切アクセスしない。
    """
    public = dict(SAMPLE_PUBLIC_METRICS)
    bpr.assert_only_allowed_keys(public)
    base_text = bpr.format_report_text(SAMPLE_TARGET_DATE, public)
    return SAMPLE_TARGET_DATE, public, compose_sns_message(base_text, None)


ENV_CANDIDATES = (
    ROOT_DIR / ".env",
    ROOT_DIR / "scripts" / ".env",
    ROOT_DIR / "ai_review" / ".env",
)

LOGGER = logging.getLogger("sns_report")

# Gemini 3.x は thinking と出力が同一の max_output_tokens 予算を共有するため、
# 短文でも十分な余裕を確保する（尻切れ防止）。
SNS_COMMENTARY_MAX_TOKENS = 2048

SNS_COMMENTARY_SYSTEM = """\
あなたは仮想通貨の自動売買ボットについて、一般向けSNS用の短い所感を書くアシスタントです。

【絶対禁止】
- 具体的な売買ロジック、パラメータ、設定値、閾値、指標名、プロファイル名への言及
- 「設定を見直した」「パラメータ変更の効果」「ロジックを調整した」など、
  内部の変更・チューニングを示唆したり推測したりする表現
- 公開されていない内部情報の推測
- 件数・勝率・損益の生の数値を書くこと
- 与えられたラベルにない独自の多寡・傾向判断をすること

【許可されること】
- 与えられた比較ラベル（決済件数・当日勝率・当日損益）に基づく一般的な所感のみ
- 日本語で1〜2文。見出し・箇条書き・絵文字は使わない

【ラベルの扱い（必須）】
- 与えられたラベルに基づいてのみ多寡・傾向を表現すること。ラベルにない独自の多寡判断をしないこと
- 毎回同じ書き出し・同じ言い回しを使わず、当日のラベルの組み合わせに応じて表現を変えること

【文体・完結性（必須）】
- 必ず文法的に完結した文で終えること
- 体言止め・連用形での中断は不可（例: 「利益を積み」「一日でした」だけで終わるのは不可）
- 文末は必ず句点「。」で終えること
- 途中で切れた文や、締めくくりのない文は出力しないこと

【良い出力例】
- 決済は平常通りで、損益も横ばいの落ち着いた一日でした。
- 決済が多めに動き、損益も改善傾向が見られました。
- 決済は少なめでしたが、勝率は高めに推移しました。
- 決済件数は多く、損益は悪化傾向の慎重な一日でした。
- 勝率は低めでしたが、損益は横ばいを維持しています。
- 決済は少なめ、損益は改善傾向で、勝率は横ばいでした。
"""

# 当日損益符号と考察文のトーン整合性チェック用（LLM再呼び出しなし）
_COMMENTARY_POSITIVE_WORDS: Tuple[str, ...] = (
    "好調",
    "堅調",
    "順調",
    "利益",
    "好調さ",
    "プラス",
    "上昇",
    "積み上げ",
    "伸び",
)
_COMMENTARY_NEGATIVE_WORDS: Tuple[str, ...] = (
    "厳しい",
    "苦戦",
    "損失",
    "低調",
    "不調",
    "マイナス",
    "下落",
    "失速",
    "後退",
)


def is_complete_commentary(text: str) -> bool:
    """
    考察文が句点で終わる完結文かどうかを簡易判定する。
    空・句点なし（尻切れ・連用形中断など）は False。
    """
    note = (text or "").strip()
    if not note:
        return False
    # モデルが付けがちな囲みを外してから判定する
    if (note.startswith("「") and note.endswith("」")) or (
        len(note) >= 2 and note[0] in {'"', "'", "“"} and note[-1] in {'"', "'", "”"}
    ):
        note = note[1:-1].strip()
    return bool(note) and note.endswith("。")


def commentary_tone_matches_daily_pnl(text: str, daily_pnl_jpy: float) -> bool:
    """
    考察文のポジティブ/ネガティブ語と当日損益符号が矛盾していないか。
    矛盾なら False（呼び出し側でフォールバック）。
    """
    note = (text or "").strip()
    if not note:
        return False
    has_pos = any(word in note for word in _COMMENTARY_POSITIVE_WORDS)
    has_neg = any(word in note for word in _COMMENTARY_NEGATIVE_WORDS)
    if daily_pnl_jpy > 0 and has_neg:
        return False
    if daily_pnl_jpy < 0 and has_pos:
        return False
    return True


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [sns_report] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


def _load_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            values[key] = value
    return values


def _merged_env() -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for path in ENV_CANDIDATES:
        merged.update(_load_env_file(path))
    return merged


def _env(name: str, default: str = "") -> str:
    file_env = _merged_env()
    return (os.environ.get(name) or file_env.get(name, default) or default).strip()


def build_base_sns_text(
    now: Optional[datetime] = None,
    collect_metrics: Optional[
        Callable[..., Tuple[str, Dict[str, object]]]
    ] = None,
) -> Tuple[str, Dict[str, object], str]:
    """
    許可リスト済みメトリクスからSNS本文（考察なし）を生成する。
    戻り値: (target_date, public_dict, base_text)
    """
    collect = collect_metrics or bpr.collect_public_metrics
    target_date, public = collect(now=now)
    bpr.assert_only_allowed_keys(public)
    base_text = bpr.format_report_text(target_date, public)
    return target_date, public, base_text


def _call_gemini(
    prompt: str,
    system: str,
    api_key: str,
    max_tokens: int = SNS_COMMENTARY_MAX_TOKENS,
) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    # 短文所感には thinking を抑え、出力側のトークンを確保する。
    thinking_kwargs: dict = {}
    try:
        thinking_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_level="minimal"
        )
    except (TypeError, AttributeError, ValueError):
        # SDK が古い場合は thinking_config なしで続行する。
        thinking_kwargs = {}

    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            temperature=0.4,
            **thinking_kwargs,
        ),
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini response is empty")
    return text


def _call_openai(
    prompt: str,
    system: str,
    api_key: str,
    max_tokens: int = SNS_COMMENTARY_MAX_TOKENS,
) -> str:
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url="https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = json.loads(response.read().decode("utf-8"))
    choices = raw.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI response has no choices")
    content = choices[0].get("message", {}).get("content") or ""
    text = str(content).strip()
    if not text:
        raise RuntimeError("OpenAI response is empty")
    return text


def generate_ai_commentary(
    comparison_labels: Dict[str, str],
    *,
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """
    直近平均との比較ラベルのみを入力に、1〜2文の考察を生成する。
    生の当日数値・平均値は渡さない。失敗時は例外を投げる。
    """
    resolved_provider = (provider or _env("LLM_PROVIDER", "gemini")).strip().lower()
    resolved_key = (api_key or _env("LLM_API_KEY") or "").strip()
    if not resolved_key:
        if resolved_provider == "gemini":
            resolved_key = _env("GEMINI_API_KEY")
        elif resolved_provider == "openai":
            resolved_key = _env("OPENAI_API_KEY")
    if not resolved_key:
        raise RuntimeError(
            f"LLM API key missing for provider={resolved_provider} "
            "(set LLM_API_KEY or provider-specific key)"
        )

    trade_label = str(comparison_labels.get("trade_count") or "")
    win_label = str(comparison_labels.get("win_rate_daily") or "")
    pnl_label = str(comparison_labels.get("daily_pnl") or "")
    user_prompt = (
        "次の比較ラベルだけを事実として使い、一般向けの短い所感を1〜2文で書いてください。\n"
        "ラベルに書かれた多寡・傾向以外の判断や、生の数値の推測はしないでください。\n"
        "必ず文法的に完結した文にし、文末は句点「。」で終えてください。"
        "体言止めや連用形での中断はしないでください。\n\n"
        f"決済件数（直近平均との比較）: {trade_label}\n"
        f"当日勝率（直近平均との比較）: {win_label}\n"
        f"当日損益（直近平均との比較）: {pnl_label}\n"
    )
    if resolved_provider in {"gemini", "google"}:
        return _call_gemini(user_prompt, SNS_COMMENTARY_SYSTEM, resolved_key)
    if resolved_provider == "openai":
        return _call_openai(user_prompt, SNS_COMMENTARY_SYSTEM, resolved_key)
    raise RuntimeError(f"unsupported LLM_PROVIDER: {resolved_provider}")


# Threads本文用の固定免責（LLM生成禁止）。Instagram側とは別文言。
THREADS_TEXT_DISCLAIMER = (
    "※実資金による運用記録です。投資助言ではなく、利益を保証するものではありません。"
)


def compose_sns_message(base_text: str, commentary: Optional[str]) -> str:
    """基本テキストに考察を追記し、末尾にThreads用免責を付ける。"""
    note = (commentary or "").strip()
    if note:
        body = f"{base_text}\n\n{note}"
    else:
        body = base_text
    return f"{body}\n\n{THREADS_TEXT_DISCLAIMER}"


def extract_threads_commentary(base_text: str, threads_text: str) -> Optional[str]:
    """
    Threads全文から考察だけを取り出す。末尾のThreads免責は含めない。
    Instagramへ免責を流用しないための分離処理。
    """
    sep = "\n\n"
    disclaimer_suffix = f"{sep}{THREADS_TEXT_DISCLAIMER}"
    body = threads_text
    if body.endswith(disclaimer_suffix):
        body = body[: -len(disclaimer_suffix)]
    if not body.startswith(base_text):
        return None
    remainder = body[len(base_text) :]
    if remainder.startswith(sep):
        remainder = remainder[len(sep) :]
    note = remainder.strip()
    return note or None


# Instagramキャプション用の固定文言（LLM生成禁止）
INSTAGRAM_CAPTION_HOOKS: Tuple[str, ...] = (
    "今日もAIが自分の判断でBTCを売買しました。",
    "AIが24時間休まずBTCの売買判断を続けています。",
    "今日もAIが自律的にBTCの売買を行いました。",
)
INSTAGRAM_CAPTION_HEADING = "【本日の運用実績】"
INSTAGRAM_CAPTION_DISCLAIMER = (
    "※本アカウントは実資金でAIが自動売買を行った運用実績の記録です。"
    "投資助言・特定の売買を推奨するものではなく、将来の利益を保証するものでもありません。"
    "ご覧いただく際は自己判断でお願いします。"
    "売買ロジックの詳細は非公開としています。"
)
INSTAGRAM_CAPTION_CTA = (
    "毎日この時間に更新しています。フォローして経過を見てもらえたら嬉しいです。"
)
# Instagramハッシュタグ上限5個
INSTAGRAM_CAPTION_HASHTAGS = (
    "#BTC #AI自動売買 #仮想通貨 #トレード記録 #アルゴリズムトレード"
)

# Telegram配信時の識別用プレフィックス（運用者がプラットフォームを取り違えないため）
TELEGRAM_LABEL_INSTAGRAM = "[Instagram用キャプション]"
TELEGRAM_LABEL_THREADS = "[Threads用テキスト]"


def select_instagram_caption_hook(trading_day: str) -> str:
    """取引日で決定的にローテーションする Instagram キャプション冒頭フック。"""
    if not INSTAGRAM_CAPTION_HOOKS:
        raise RuntimeError("INSTAGRAM_CAPTION_HOOKS is empty")
    idx = sum(ord(c) for c in trading_day) % len(INSTAGRAM_CAPTION_HOOKS)
    return INSTAGRAM_CAPTION_HOOKS[idx]


def format_instagram_metrics_body(public: Dict[str, object]) -> str:
    """
    許可リスト dict のみから Instagram 用指標本文を組み立てる。
    新たなデータ取得は行わない。
    """
    bpr.assert_only_allowed_keys(public)
    lines = [
        f"累積損益：{bpr._format_jpy_signed(float(public['cumulative_pnl_jpy']))}",
        f"当日損益：{bpr._format_jpy_signed(float(public['daily_pnl_jpy']))}",
        f"決済件数：{int(public['trade_count'])}件",
        f"当日勝率：{float(public['win_rate_daily_pct']):.1f}%",
        f"累積勝率：{float(public['win_rate_cumulative_pct']):.1f}%",
        f"稼働継続日数：{int(public['uptime_days'])}日",
        f"最大ドローダウン：{float(public['max_drawdown_pct']):.2f}%",
    ]
    return "\n".join(lines)


def build_instagram_caption(
    target_date: str,
    public: Dict[str, object],
    commentary: Optional[str] = None,
) -> str:
    """
    Instagram投稿用キャプションを組み立てる。
    Threads用テキスト生成とは独立。public は collect_public_metrics 出力をそのまま使う。
    """
    bpr.assert_only_allowed_keys(public)
    parts = [
        select_instagram_caption_hook(target_date),
        INSTAGRAM_CAPTION_HEADING,
        format_instagram_metrics_body(public),
    ]
    note = (commentary or "").strip()
    if note:
        parts.append(note)
    parts.extend(
        [
            INSTAGRAM_CAPTION_DISCLAIMER,
            INSTAGRAM_CAPTION_CTA,
            INSTAGRAM_CAPTION_HASHTAGS,
        ]
    )
    return "\n\n".join(parts)


def _resolve_ai_commentary(
    comparison_labels: Dict[str, str],
    *,
    daily_pnl_jpy: float,
    commentary_fn: Optional[Callable[[Dict[str, str]], str]] = None,
) -> Tuple[Optional[str], bool]:
    """
    LLM考察を解決する。成功時 (text, True)、失敗/不完全/トーン矛盾時 (None, False)。
    Threads / Instagram 双方で同じ結果を再利用するための共通処理。
    """
    try:
        fn = commentary_fn or generate_ai_commentary
        commentary = fn(comparison_labels)
        if not is_complete_commentary(commentary or ""):
            LOGGER.warning(
                "AI commentary incomplete (must end with '。'); "
                "posting base text only. raw=%r",
                (commentary or "")[:200],
            )
            return None, False
        if not commentary_tone_matches_daily_pnl(
            commentary or "", daily_pnl_jpy
        ):
            LOGGER.warning(
                "AI commentary tone conflicts with daily_pnl_jpy=%s; "
                "posting base text only. raw=%r",
                daily_pnl_jpy,
                (commentary or "")[:200],
            )
            return None, False
        return commentary, True
    except Exception as exc:
        LOGGER.warning("AI commentary failed; posting base text only: %s", exc)
        return None, False


def build_sns_message(
    now: Optional[datetime] = None,
    *,
    commentary_fn: Optional[Callable[[Dict[str, str]], str]] = None,
) -> Tuple[str, Dict[str, object], str, bool]:
    """
    SNS投稿用全文を返す（Threads用）。
    戻り値: (target_date, public, full_text, commentary_ok)
    commentary_ok=False のとき考察なしフォールバック。
    """
    target_date, public, base_text = build_base_sns_text(now=now)
    recent = bpr.compute_recent_averages(target_date)
    labels = bpr.compare_to_recent_labels(
        trade_count=int(public["trade_count"]),
        win_rate_daily_pct=float(public["win_rate_daily_pct"]),
        daily_pnl_jpy=float(public["daily_pnl_jpy"]),
        recent=recent,
    )
    if labels is None:
        LOGGER.info(
            "AI commentary skipped: no prior trading days for comparison "
            "(target_date=%s)",
            target_date,
        )
        commentary, commentary_ok = None, False
    else:
        commentary, commentary_ok = _resolve_ai_commentary(
            labels,
            daily_pnl_jpy=float(public["daily_pnl_jpy"]),
            commentary_fn=commentary_fn,
        )
    full_text = compose_sns_message(base_text, commentary)
    return target_date, public, full_text, commentary_ok


def label_for_telegram(platform_label: str, body: str) -> str:
    """Telegram配信文面にプラットフォーム識別ラベルを付与する。"""
    return f"{platform_label}\n\n{body}"


def _alert_internal(message: str) -> None:
    """失敗アラートは内部管理Botのみへ送る（SNS Botへは送らない）。"""
    try:
        send_telegram_message(message)
    except Exception as exc:
        LOGGER.warning("internal alert send failed: %s", exc)


def reel_output_path(target_date: str, *, dry_run: bool = False) -> Path:
    """生成動画の出力パス。dry-run は log/、通常は runtime/。"""
    base = LOG_DIR if dry_run else RUNTIME_DIR
    base.mkdir(parents=True, exist_ok=True)
    suffix = "dryrun" if dry_run else "out"
    return base / f"sns_reel_{target_date}_{suffix}.mp4"


def try_generate_reel_video(
    target_date: str,
    public: Dict[str, object],
    output_path: Path,
    *,
    generate_fn: Optional[Callable[..., Path]] = None,
) -> Tuple[Optional[Path], Optional[str]]:
    """
    動画生成を試みる。成功時 (path, None)、失敗時 (None, error_detail)。
    """
    fn = generate_fn or reel.generate_sns_reel_video
    try:
        bpr.assert_only_allowed_keys(public)
        path = fn(target_date, public, output_path)
        return path, None
    except Exception as exc:
        LOGGER.warning("SNS reel video generation failed: %s", exc)
        return None, str(exc)


def deliver_sns_report(
    target_date: str,
    public: Dict[str, object],
    threads_text: str,
    instagram_caption: str,
    *,
    dry_run: bool = False,
    generate_fn: Optional[Callable[..., Path]] = None,
    send_video_fn: Optional[Callable[..., bool]] = None,
    send_text_fn: Optional[Callable[..., bool]] = None,
    alert_fn: Optional[Callable[[str], None]] = None,
) -> int:
    """
    Instagram用: 動画+キャプション配信。失敗時はキャプションをテキスト送信。
    Threads用: テキストを別途配信。
    戻り値はプロセス終了コード。
    """
    alert = alert_fn or _alert_internal
    send_video = send_video_fn or send_sns_telegram_video
    send_text = send_text_fn or send_sns_telegram_message
    out_path = reel_output_path(target_date, dry_run=dry_run)

    ig_payload = label_for_telegram(TELEGRAM_LABEL_INSTAGRAM, instagram_caption)
    threads_payload = label_for_telegram(TELEGRAM_LABEL_THREADS, threads_text)

    video_path, video_err = try_generate_reel_video(
        target_date,
        public,
        out_path,
        generate_fn=generate_fn,
    )

    if dry_run:
        if video_path is not None:
            LOGGER.info(
                "DRY-RUN mode. Captions and video prepared.\n"
                "video_path=%s\n"
                "--- Instagram caption ---\n%s\n"
                "--- Threads text ---\n%s",
                video_path,
                instagram_caption,
                threads_text,
            )
            return 0
        LOGGER.info(
            "DRY-RUN mode. Video failed; Instagram caption-only fallback "
            "would be used for the reel side.\n"
            "video_error=%s\n"
            "--- Instagram caption ---\n%s\n"
            "--- Threads text ---\n%s",
            video_err,
            instagram_caption,
            threads_text,
        )
        return 0

    instagram_ok = False
    if video_path is not None:
        instagram_ok = bool(send_video(video_path, ig_payload))
        if instagram_ok:
            LOGGER.info(
                "SNS Instagram video+caption send succeeded for "
                "trading_day=%s path=%s",
                target_date,
                video_path,
            )
        else:
            LOGGER.error(
                "SNS Instagram video send failed; falling back to caption text"
            )
            alert(
                "[ALERT] SNS reel video send failed; falling back to text\n"
                f"trading_day={target_date}\n"
                f"path={video_path}"
            )
            instagram_ok = bool(send_text(ig_payload))
            if instagram_ok:
                LOGGER.info(
                    "SNS Instagram caption text fallback succeeded for "
                    "trading_day=%s",
                    target_date,
                )
    else:
        alert(
            "[ALERT] SNS reel video generation failed; sending text only\n"
            f"trading_day={target_date}\n"
            f"detail={video_err}"
        )
        instagram_ok = bool(send_text(ig_payload))
        if instagram_ok:
            LOGGER.info(
                "SNS Instagram caption text fallback succeeded for "
                "trading_day=%s",
                target_date,
            )

    threads_ok = bool(send_text(threads_payload))
    if threads_ok:
        LOGGER.info(
            "SNS Threads text send succeeded for trading_day=%s",
            target_date,
        )
    else:
        LOGGER.error("SNS Threads text send failed")
        alert(
            "[ALERT] SNS Threads text delivery failed\n"
            f"trading_day={target_date}"
        )

    if instagram_ok and threads_ok:
        return 0

    LOGGER.error(
        "SNS delivery incomplete (instagram_ok=%s threads_ok=%s)",
        instagram_ok,
        threads_ok,
    )
    alert(
        "[ALERT] SNS report delivery failed (video and text)\n"
        f"trading_day={target_date}\n"
        f"instagram_ok={instagram_ok} threads_ok={threads_ok}"
    )
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="SNS daily report -> Telegram SNS bot (text + Instagram reel video)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build caption and reel mp4 without sending to Telegram",
    )
    parser.add_argument(
        "--sample-data",
        action="store_true",
        help=(
            "QA only: use in-code mock metrics (negative daily pnl) instead of live data. "
            "Implies --dry-run; never sends to Telegram."
        ),
    )
    args = parser.parse_args(argv)
    _setup_logging()

    dry_run = bool(args.dry_run or args.sample_data)

    try:
        if args.sample_data:
            target_date, public, threads_text = build_sample_sns_payload()
            commentary: Optional[str] = None
            commentary_ok = False
            LOGGER.info(
                "SAMPLE-DATA mode: using in-code mock metrics "
                "(daily_pnl_jpy=%s).",
                public.get("daily_pnl_jpy"),
            )
        else:
            target_date, public, threads_text, commentary_ok = build_sns_message()
            # Threads本文から考察を取り出し、同一考察を Instagram にも載せる
            # （LLM再呼び出しはしない。Threads免責は Instagram に流用しない）
            base_text = bpr.format_report_text(target_date, public)
            commentary = extract_threads_commentary(base_text, threads_text)
        bpr.assert_only_allowed_keys(public)
        instagram_caption = build_instagram_caption(
            target_date, public, commentary
        )
    except Exception as exc:
        LOGGER.exception("SNS report build failed: %s", exc)
        _alert_internal(
            "[ALERT] SNS report build failed\n"
            f"error={exc}"
        )
        return 1

    LOGGER.info(
        "SNS report built (commentary_ok=%s):\n"
        "--- Instagram caption ---\n%s\n"
        "--- Threads text ---\n%s",
        commentary_ok,
        instagram_caption,
        threads_text,
    )

    return deliver_sns_report(
        target_date,
        public,
        threads_text,
        instagram_caption,
        dry_run=dry_run,
    )

if __name__ == "__main__":
    raise SystemExit(main())
