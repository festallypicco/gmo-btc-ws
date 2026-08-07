#!/usr/bin/env python3
"""
Instagram Reels 動画自動投稿（Instagram Graph API）。

- 動画生成・Telegram配信とは独立して呼び出す（承認ゲートなし）
- 認証情報未設定は auth_error として呼び出し側でアラート可能
- 二重投稿防止: runtime/instagram_posted_<trading_day>.flag
- 公開用ファイルは runtime/public_media/<token>.mp4 に一時配置し、
  https://<PUBLIC_MEDIA_DOMAIN>/<token>.mp4 として nginx 経由で一時公開する
  （動画URLでのみ受け付ける Graph API の REELS 投稿仕様に対応するため）。
  投稿成功時はその場でファイルを削除し、失敗・タイムアウト時は削除せず
  既存の2時間自動削除タイマー（フェイルセーフ）に任せる
- .env 読み込みは threads_poster._merged_env() を共用する
"""
from __future__ import annotations

import json
import logging
import secrets
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from threads_poster import _merged_env  # .env読み込みの仕組みを共通化
from sns_reel_video import REELS_THUMB_OFFSET_MS  # フックフェード完了時刻から算出

LOGGER = logging.getLogger("instagram_poster")

ROOT_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT_DIR / "runtime"
PUBLIC_MEDIA_DIR = RUNTIME_DIR / "public_media"

# Instagram API with Instagram Login（Business Login for Instagram経由、IGAAトークン）。
# graph.facebook.com はFacebookログイン経由のEAAトークン専用のため使用しない。
# 公式ドキュメント（Content Publishing）のサンプルもバージョン付きパスを使用しているため、
# graph.instagram.com にもバージョンを付与する（2026-08時点の最新は v26.0）。
INSTAGRAM_API_BASE = "https://graph.instagram.com/v26.0"
HTTP_TIMEOUT_SEC = 30

# コンテナ処理完了待ちポーリング（5秒間隔 x 最大60回 = 約5分）
POLL_INTERVAL_SEC = 5
POLL_MAX_ATTEMPTS = 60

# 一時公開URL（nginxホワイトリスト正規表現 ^[A-Za-z0-9]{32,}\.[A-Za-z0-9]{1,8}$ に合致させる）
PUBLIC_MEDIA_DOMAIN = "https://m7x2kq9.festallypicco.com"
PUBLIC_TOKEN_BYTES = 24  # secrets.token_hex(24) -> 48文字の英数字


@dataclass(frozen=True)
class InstagramCredentials:
    access_token: str
    user_id: str


@dataclass(frozen=True)
class InstagramPostResult:
    """
    status:
      success | dry_run | skipped_already_posted | skipped_no_video |
      auth_error | api_error | file_error
    """

    status: str
    detail: str = ""
    media_id: str = ""
    post_url: str = ""
    dry_run_payload: Optional[Dict[str, Any]] = None

    @property
    def ok(self) -> bool:
        return self.status in (
            "success",
            "dry_run",
            "skipped_already_posted",
            "skipped_no_video",
        )

    @property
    def is_auth_error(self) -> bool:
        return self.status == "auth_error"

    @property
    def is_file_error(self) -> bool:
        return self.status == "file_error"

    @property
    def is_api_error(self) -> bool:
        return self.status == "api_error"

    @property
    def posted(self) -> bool:
        return self.status == "success"


def get_instagram_credentials(
    env: Optional[Dict[str, str]] = None,
) -> Optional[InstagramCredentials]:
    """
    INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_USER_ID を取得する。
    どちらか欠ければ None（呼び出し側で auth_error）。
    """
    src = env if env is not None else _merged_env()
    token = (src.get("INSTAGRAM_ACCESS_TOKEN") or "").strip()
    user_id = (src.get("INSTAGRAM_USER_ID") or "").strip()
    if not token or not user_id:
        return None
    return InstagramCredentials(access_token=token, user_id=user_id)


def instagram_posted_marker_path(
    trading_day: str,
    runtime_dir: Optional[Path] = None,
) -> Path:
    base = RUNTIME_DIR if runtime_dir is None else runtime_dir
    return base / f"instagram_posted_{trading_day}.flag"


def already_posted_instagram(
    trading_day: str,
    runtime_dir: Optional[Path] = None,
) -> bool:
    return instagram_posted_marker_path(trading_day, runtime_dir=runtime_dir).exists()


def mark_instagram_posted(
    trading_day: str,
    runtime_dir: Optional[Path] = None,
    *,
    media_id: str = "",
    post_url: str = "",
) -> Path:
    path = instagram_posted_marker_path(trading_day, runtime_dir=runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "posted_at": datetime.now().isoformat(timespec="seconds"),
        "media_id": media_id,
        "post_url": post_url,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def generate_public_media_token() -> str:
    return secrets.token_hex(PUBLIC_TOKEN_BYTES)


def public_media_url(token: str) -> str:
    return f"{PUBLIC_MEDIA_DOMAIN}/{token}.mp4"


def publish_video_to_public_media(
    video_path: Path,
    *,
    public_media_dir: Optional[Path] = None,
    token: Optional[str] = None,
) -> Tuple[Path, str]:
    """
    動画を runtime/public_media/<token>.mp4 にコピーする。
    戻り値: (配置先パス, 公開URL)
    """
    base_dir = public_media_dir if public_media_dir is not None else PUBLIC_MEDIA_DIR
    base_dir.mkdir(parents=True, exist_ok=True)
    tok = token or generate_public_media_token()
    dest = base_dir / f"{tok}.mp4"
    shutil.copyfile(video_path, dest)
    return dest, public_media_url(tok)


def remove_public_media_file(path: Path) -> None:
    """投稿成功時の後始末。失敗しても例外は上げない（削除できなくても致命的ではない）。"""
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        LOGGER.warning("failed to remove public media file %s: %s", path, exc)


def _http_post_form(
    url: str,
    fields: Dict[str, str],
    *,
    timeout_sec: int = HTTP_TIMEOUT_SEC,
) -> Tuple[int, Dict[str, Any], str]:
    """application/x-www-form-urlencoded POST。戻り値: (http_status, json_dict, raw_body)"""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        status = int(exc.code)
    except urllib.error.URLError as exc:
        return 0, {}, f"URLError: {exc.reason}"

    parsed: Dict[str, Any] = {}
    if raw.strip():
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                parsed = obj
        except json.JSONDecodeError:
            parsed = {}
    return status, parsed, raw


def _http_get_json(
    url: str,
    *,
    timeout_sec: int = HTTP_TIMEOUT_SEC,
) -> Tuple[int, Dict[str, Any], str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        status = int(exc.code)
    except urllib.error.URLError as exc:
        return 0, {}, f"URLError: {exc.reason}"

    parsed: Dict[str, Any] = {}
    if raw.strip():
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                parsed = obj
        except json.JSONDecodeError:
            parsed = {}
    return status, parsed, raw


def _classify_http_error(status: int, body: Dict[str, Any], raw: str) -> str:
    """auth_error / api_error を HTTP 応答から判定する（threads_poster と同等の分類）。"""
    if status in (401, 403):
        return "auth_error"
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = err.get("code")
    message = str(err.get("message") or raw or "")
    if code in (190, 102, 10) or "OAuth" in message or "permission" in message.lower():
        return "auth_error"
    if status == 0:
        return "api_error"
    return "api_error"


def _error_detail(status: int, body: Dict[str, Any], raw: str) -> str:
    err = body.get("error") if isinstance(body.get("error"), dict) else None
    if err:
        return (
            f"http={status} code={err.get('code')!r} "
            f"type={err.get('type')!r} message={err.get('message')!r}"
        )
    snippet = (raw or "").strip().replace("\n", " ")
    if len(snippet) > 300:
        snippet = snippet[:300] + "..."
    return f"http={status} body={snippet!r}"


def fetch_permalink(
    media_id: str,
    access_token: str,
    *,
    http_get_fn: Callable[..., Tuple[int, Dict[str, Any], str]] = _http_get_json,
) -> str:
    if not media_id:
        return ""
    qs = urllib.parse.urlencode(
        {"fields": "permalink", "access_token": access_token}
    )
    url = f"{INSTAGRAM_API_BASE}/{media_id}?{qs}"
    status, body, raw = http_get_fn(url)
    if status == 200 and isinstance(body.get("permalink"), str):
        return str(body["permalink"]).strip()
    LOGGER.info(
        "permalink fetch skipped/failed: %s",
        _error_detail(status, body, raw),
    )
    return ""


def poll_container_status(
    creation_id: str,
    access_token: str,
    *,
    http_get_fn: Callable[..., Tuple[int, Dict[str, Any], str]] = _http_get_json,
    interval_sec: int = POLL_INTERVAL_SEC,
    max_attempts: int = POLL_MAX_ATTEMPTS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Tuple[str, str]:
    """
    GET /{creation_id}?fields=status_code を FINISHED になるまでポーリングする。
    戻り値: (result_kind, detail)
      result_kind: FINISHED | ERROR | TIMEOUT | auth_error | api_error
    """
    for attempt in range(max_attempts):
        qs = urllib.parse.urlencode(
            {"fields": "status_code", "access_token": access_token}
        )
        url = f"{INSTAGRAM_API_BASE}/{creation_id}?{qs}"
        status, body, raw = http_get_fn(url)
        if status != 200:
            kind = _classify_http_error(status, body, raw)
            return kind, _error_detail(status, body, raw)

        code = str(body.get("status_code") or "").upper()
        if code == "FINISHED":
            return "FINISHED", "ok"
        if code == "ERROR":
            return "ERROR", f"container status_code=ERROR body={body}"
        # IN_PROGRESS / PUBLISHED以外はまだ処理中とみなし継続

        if attempt < max_attempts - 1:
            sleep_fn(interval_sec)

    return (
        "TIMEOUT",
        f"polling timed out after {max_attempts} attempts "
        f"({max_attempts * interval_sec}s)",
    )


def _build_dry_run_payload(
    video_path: Path,
    caption: str,
    trading_day: str,
    *,
    creds: Optional[InstagramCredentials],
) -> Dict[str, Any]:
    create_url = (
        f"{INSTAGRAM_API_BASE}/{creds.user_id}/media" if creds else
        f"{INSTAGRAM_API_BASE}/{{INSTAGRAM_USER_ID}}/media"
    )
    publish_url = (
        f"{INSTAGRAM_API_BASE}/{creds.user_id}/media_publish" if creds else
        f"{INSTAGRAM_API_BASE}/{{INSTAGRAM_USER_ID}}/media_publish"
    )
    return {
        "destination": "instagram_reels",
        "trading_day": trading_day,
        "create_url": create_url,
        "publish_url": publish_url,
        "create_fields": {
            "media_type": "REELS",
            "video_url": f"{PUBLIC_MEDIA_DOMAIN}/<token>.mp4",
            "caption": caption,
            # 冒頭フックのフェードイン完了直後（sns_reel_video の定数から算出）
            "thumb_offset": str(int(REELS_THUMB_OFFSET_MS)),
            "access_token": "(redacted)" if creds else "(missing)",
        },
        "publish_fields": {
            "creation_id": "(from create response)",
            "access_token": "(redacted)" if creds else "(missing)",
        },
        "user_id_configured": bool(creds),
        "access_token_configured": bool(creds),
        "caption_length": len(caption),
        "thumb_offset_ms": int(REELS_THUMB_OFFSET_MS),
        "video_path": str(video_path),
    }


def post_instagram_reel(
    video_path: Path,
    caption: str,
    trading_day: str,
    *,
    dry_run: bool = False,
    runtime_dir: Optional[Path] = None,
    public_media_dir: Optional[Path] = None,
    credentials: Optional[InstagramCredentials] = None,
    http_post_fn: Callable[..., Tuple[int, Dict[str, Any], str]] = _http_post_form,
    http_get_fn: Callable[..., Tuple[int, Dict[str, Any], str]] = _http_get_json,
    http_get_permalink_fn: Callable[..., str] = fetch_permalink,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_interval_sec: int = POLL_INTERVAL_SEC,
    poll_max_attempts: int = POLL_MAX_ATTEMPTS,
    token: Optional[str] = None,
) -> InstagramPostResult:
    """
    Instagram Reels へ動画投稿する
    （公開用ファイル配置 -> create container -> poll -> publish -> permalink取得）。

    dry_run=True のときは、ファイルコピー・API呼び出しを一切行わず、
    送信予定内容を result.dry_run_payload に載せて返す。
    """
    if already_posted_instagram(trading_day, runtime_dir=runtime_dir):
        marker = instagram_posted_marker_path(trading_day, runtime_dir=runtime_dir)
        LOGGER.info(
            "Instagram post skipped (already posted): trading_day=%s marker=%s",
            trading_day,
            marker,
        )
        return InstagramPostResult(
            status="skipped_already_posted",
            detail=f"marker exists: {marker.name}",
        )

    creds = credentials if credentials is not None else get_instagram_credentials()

    if dry_run:
        payload = _build_dry_run_payload(video_path, caption, trading_day, creds=creds)
        LOGGER.info(
            "DRY-RUN Instagram Reels post (file copy / API not called):\n%s",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        return InstagramPostResult(
            status="dry_run",
            detail="file copy / API not called (dry-run)",
            dry_run_payload=payload,
        )

    if creds is None:
        return InstagramPostResult(
            status="auth_error",
            detail=(
                "INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_USER_ID are not configured. "
                "Set them in .env (or environment) to enable auto-post."
            ),
        )

    if not Path(video_path).exists():
        return InstagramPostResult(
            status="file_error",
            detail=f"video file not found: {video_path}",
        )

    try:
        public_path, public_url = publish_video_to_public_media(
            video_path, public_media_dir=public_media_dir, token=token
        )
    except OSError as exc:
        return InstagramPostResult(
            status="api_error",
            detail=f"failed to publish media file to public_media: {exc}",
        )

    result = _run_create_poll_publish(
        creds,
        public_url,
        caption,
        http_post_fn=http_post_fn,
        http_get_fn=http_get_fn,
        sleep_fn=sleep_fn,
        poll_interval_sec=poll_interval_sec,
        poll_max_attempts=poll_max_attempts,
    )

    if result.status == "success":
        permalink = ""
        try:
            permalink = http_get_permalink_fn(result.media_id, creds.access_token) or ""
        except Exception as exc:
            LOGGER.warning("permalink fetch raised: %s", exc)
        result = InstagramPostResult(
            status="success",
            detail="published",
            media_id=result.media_id,
            post_url=permalink,
        )
        mark_instagram_posted(
            trading_day,
            runtime_dir=runtime_dir,
            media_id=result.media_id,
            post_url=permalink,
        )
        LOGGER.info(
            "Instagram Reels post succeeded: trading_day=%s media_id=%s url=%s",
            trading_day,
            result.media_id,
            permalink or "(none)",
        )
        # 成功時のみ即削除。失敗時は既存の2時間自動削除タイマーに任せる。
        remove_public_media_file(public_path)
    else:
        LOGGER.info(
            "Instagram post not successful (status=%s); leaving public media "
            "file for retry / auto-cleanup timer: %s",
            result.status,
            public_path,
        )

    return result


def _run_create_poll_publish(
    creds: InstagramCredentials,
    video_url: str,
    caption: str,
    *,
    http_post_fn: Callable[..., Tuple[int, Dict[str, Any], str]],
    http_get_fn: Callable[..., Tuple[int, Dict[str, Any], str]],
    sleep_fn: Callable[[float], None],
    poll_interval_sec: int,
    poll_max_attempts: int,
) -> InstagramPostResult:
    """create container -> poll -> publish の中核部分（permalink取得・後始末は呼び出し側）。"""
    status, resp, raw = http_post_fn(
        f"{INSTAGRAM_API_BASE}/{creds.user_id}/media",
        {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            # プロフィール一覧サムネ: フック文字がはっきり見えるフレーム
            "thumb_offset": str(int(REELS_THUMB_OFFSET_MS)),
            "access_token": creds.access_token,
        },
    )
    if status != 200 or not resp.get("id"):
        kind = _classify_http_error(status, resp, raw)
        detail = _error_detail(status, resp, raw)
        LOGGER.error("Instagram create container failed (%s): %s", kind, detail)
        return InstagramPostResult(status=kind, detail=f"create_container: {detail}")

    creation_id = str(resp["id"])

    poll_status, poll_detail = poll_container_status(
        creation_id,
        creds.access_token,
        http_get_fn=http_get_fn,
        interval_sec=poll_interval_sec,
        max_attempts=poll_max_attempts,
        sleep_fn=sleep_fn,
    )
    if poll_status in ("auth_error", "api_error"):
        LOGGER.error(
            "Instagram container polling failed (%s): %s", poll_status, poll_detail
        )
        return InstagramPostResult(status=poll_status, detail=f"poll: {poll_detail}")
    if poll_status == "ERROR":
        LOGGER.error("Instagram container processing failed: %s", poll_detail)
        return InstagramPostResult(
            status="api_error", detail=f"poll: {poll_detail}"
        )
    if poll_status == "TIMEOUT":
        LOGGER.error("Instagram container polling timed out: %s", poll_detail)
        return InstagramPostResult(status="api_error", detail=f"poll: {poll_detail}")

    # poll_status == "FINISHED"
    status2, resp2, raw2 = http_post_fn(
        f"{INSTAGRAM_API_BASE}/{creds.user_id}/media_publish",
        {
            "creation_id": creation_id,
            "access_token": creds.access_token,
        },
    )
    if status2 != 200 or not resp2.get("id"):
        kind = _classify_http_error(status2, resp2, raw2)
        detail = _error_detail(status2, resp2, raw2)
        LOGGER.error("Instagram publish failed (%s): %s", kind, detail)
        return InstagramPostResult(status=kind, detail=f"publish: {detail}")

    return InstagramPostResult(status="success", media_id=str(resp2["id"]))


def format_dry_run_telegram_message(
    result: InstagramPostResult,
    *,
    trading_day: str,
) -> str:
    """dry-run 時に Telegram へ送る確認文面。"""
    payload = result.dry_run_payload or {}
    caption_preview = ""
    create_fields = payload.get("create_fields") or {}
    if isinstance(create_fields, dict):
        caption_preview = str(create_fields.get("caption") or "")
    lines = [
        "[Instagram Reels自動投稿 DRY-RUN]",
        f"trading_day={trading_day}",
        "ファイルコピー・APIは呼び出していません。送信予定内容:",
        f"create_url={payload.get('create_url')}",
        f"publish_url={payload.get('publish_url')}",
        f"video_url_format={PUBLIC_MEDIA_DOMAIN}/<token>.mp4",
        f"user_id_configured={payload.get('user_id_configured')}",
        f"access_token_configured={payload.get('access_token_configured')}",
        f"caption_length={payload.get('caption_length')}",
        f"thumb_offset_ms={payload.get('thumb_offset_ms')}",
        "",
        "--- caption ---",
        caption_preview,
    ]
    return "\n".join(lines)


def format_failure_alert(
    result: InstagramPostResult,
    *,
    trading_day: str,
) -> str:
    if result.is_auth_error:
        kind = "認証エラー"
    elif result.is_file_error:
        kind = "ファイルエラー"
    else:
        kind = "APIエラー"
    return (
        f"[ALERT] Instagram Reels自動投稿失敗 ({kind})\n"
        f"trading_day={trading_day}\n"
        f"status={result.status}\n"
        f"detail={result.detail}"
    )
