#!/usr/bin/env python3
"""
Threads テキスト自動投稿（Meta Threads Graph API）。

- キャプション生成・Telegram配信とは独立して呼び出す
- 認証情報未設定は auth_error として呼び出し側でアラート可能
- 二重投稿防止: runtime/threads_posted_<trading_day>.flag
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

LOGGER = logging.getLogger("threads_poster")

ROOT_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT_DIR / "runtime"
ENV_CANDIDATES = (
    ROOT_DIR / ".env",
    ROOT_DIR / "scripts" / ".env",
    ROOT_DIR / "ai_review" / ".env",
)

THREADS_API_BASE = "https://graph.threads.net/v1.0"
THREADS_TEXT_MAX_CHARS = 500
HTTP_TIMEOUT_SEC = 30


@dataclass(frozen=True)
class ThreadsCredentials:
    access_token: str
    user_id: str
    username: str = ""


@dataclass(frozen=True)
class ThreadsPostResult:
    """
    status:
      success | dry_run | skipped_already_posted | auth_error | api_error
    """

    status: str
    detail: str = ""
    post_id: str = ""
    post_url: str = ""
    dry_run_payload: Optional[Dict[str, Any]] = None

    @property
    def ok(self) -> bool:
        return self.status in ("success", "dry_run", "skipped_already_posted")

    @property
    def is_auth_error(self) -> bool:
        return self.status == "auth_error"

    @property
    def is_api_error(self) -> bool:
        return self.status == "api_error"

    @property
    def posted(self) -> bool:
        return self.status == "success"


def _load_env_file(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    out: Dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in out:
                out[key] = value
    except OSError:
        return {}
    return out


def _merged_env() -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for path in ENV_CANDIDATES:
        for key, value in _load_env_file(path).items():
            merged.setdefault(key, value)
    for key, value in os.environ.items():
        if value is not None and str(value).strip():
            merged[key] = str(value).strip()
    return merged


def get_threads_credentials(
    env: Optional[Dict[str, str]] = None,
) -> Optional[ThreadsCredentials]:
    """
    THREADS_ACCESS_TOKEN / THREADS_USER_ID を取得する。
    どちらか欠ければ None（呼び出し側で auth_error）。
    THREADS_USERNAME は投稿URL組み立て用の任意項目。
    """
    src = env if env is not None else _merged_env()
    token = (src.get("THREADS_ACCESS_TOKEN") or "").strip()
    user_id = (src.get("THREADS_USER_ID") or "").strip()
    username = (src.get("THREADS_USERNAME") or "").strip().lstrip("@")
    if not token or not user_id:
        return None
    return ThreadsCredentials(
        access_token=token,
        user_id=user_id,
        username=username,
    )


def threads_posted_marker_path(
    trading_day: str,
    runtime_dir: Optional[Path] = None,
) -> Path:
    base = RUNTIME_DIR if runtime_dir is None else runtime_dir
    return base / f"threads_posted_{trading_day}.flag"


def already_posted_threads(
    trading_day: str,
    runtime_dir: Optional[Path] = None,
) -> bool:
    return threads_posted_marker_path(trading_day, runtime_dir=runtime_dir).exists()


def mark_threads_posted(
    trading_day: str,
    runtime_dir: Optional[Path] = None,
    *,
    post_id: str = "",
    post_url: str = "",
) -> Path:
    path = threads_posted_marker_path(trading_day, runtime_dir=runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "posted_at": datetime.now().isoformat(timespec="seconds"),
        "post_id": post_id,
        "post_url": post_url,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def build_post_url(
    post_id: str,
    *,
    username: str = "",
    permalink: str = "",
) -> str:
    if permalink:
        return permalink.strip()
    if username and post_id:
        return f"https://www.threads.net/@{username}/post/{post_id}"
    if post_id:
        return f"https://www.threads.net/t/{post_id}"
    return ""


def _http_post_form(
    url: str,
    fields: Dict[str, str],
    *,
    timeout_sec: int = HTTP_TIMEOUT_SEC,
) -> Tuple[int, Dict[str, Any], str]:
    """
    application/x-www-form-urlencoded POST。
    戻り値: (http_status, json_dict_or_empty, raw_body)
    """
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
    """auth_error / api_error を HTTP 応答から判定する。"""
    if status in (401, 403):
        return "auth_error"
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = err.get("code")
    message = str(err.get("message") or raw or "")
    # Meta OAuth / permission 系
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
) -> str:
    if not media_id:
        return ""
    qs = urllib.parse.urlencode(
        {"fields": "permalink", "access_token": access_token}
    )
    url = f"{THREADS_API_BASE}/{media_id}?{qs}"
    status, body, raw = _http_get_json(url)
    if status == 200 and isinstance(body.get("permalink"), str):
        return str(body["permalink"]).strip()
    LOGGER.info(
        "permalink fetch skipped/failed: %s",
        _error_detail(status, body, raw),
    )
    return ""


def post_threads_text(
    text: str,
    trading_day: str,
    *,
    dry_run: bool = False,
    runtime_dir: Optional[Path] = None,
    credentials: Optional[ThreadsCredentials] = None,
    http_post_fn=_http_post_form,
    http_get_permalink_fn=fetch_permalink,
) -> ThreadsPostResult:
    """
    Threads へテキスト投稿する（create container -> publish）。

    dry_run=True のときは API を呼ばず、送信予定内容を result.dry_run_payload に載せる。
    """
    body = str(text or "")
    if already_posted_threads(trading_day, runtime_dir=runtime_dir):
        marker = threads_posted_marker_path(trading_day, runtime_dir=runtime_dir)
        LOGGER.info(
            "Threads post skipped (already posted): trading_day=%s marker=%s",
            trading_day,
            marker,
        )
        return ThreadsPostResult(
            status="skipped_already_posted",
            detail=f"marker exists: {marker.name}",
        )

    creds = credentials if credentials is not None else get_threads_credentials()
    create_url = (
        f"{THREADS_API_BASE}/{creds.user_id}/threads" if creds else
        f"{THREADS_API_BASE}/{{THREADS_USER_ID}}/threads"
    )
    publish_url = (
        f"{THREADS_API_BASE}/{creds.user_id}/threads_publish" if creds else
        f"{THREADS_API_BASE}/{{THREADS_USER_ID}}/threads_publish"
    )

    if dry_run:
        payload = {
            "destination": "threads",
            "trading_day": trading_day,
            "create_url": create_url,
            "publish_url": publish_url,
            "create_fields": {
                "media_type": "TEXT",
                "text": body,
                "access_token": "(redacted)" if creds else "(missing)",
            },
            "publish_fields": {
                "creation_id": "(from create response)",
                "access_token": "(redacted)" if creds else "(missing)",
            },
            "user_id_configured": bool(creds),
            "access_token_configured": bool(creds),
            "text_length": len(body),
            "text_max": THREADS_TEXT_MAX_CHARS,
        }
        LOGGER.info(
            "DRY-RUN Threads post (API not called):\n%s",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        return ThreadsPostResult(
            status="dry_run",
            detail="API not called (dry-run)",
            dry_run_payload=payload,
        )

    if creds is None:
        return ThreadsPostResult(
            status="auth_error",
            detail=(
                "THREADS_ACCESS_TOKEN / THREADS_USER_ID are not configured. "
                "Set them in .env (or environment) to enable auto-post."
            ),
        )

    if len(body) > THREADS_TEXT_MAX_CHARS:
        return ThreadsPostResult(
            status="api_error",
            detail=(
                f"text length {len(body)} exceeds Threads limit "
                f"{THREADS_TEXT_MAX_CHARS}"
            ),
        )

    # Step 1: create container
    status, resp, raw = http_post_fn(
        f"{THREADS_API_BASE}/{creds.user_id}/threads",
        {
            "media_type": "TEXT",
            "text": body,
            "access_token": creds.access_token,
        },
    )
    if status != 200 or not resp.get("id"):
        kind = _classify_http_error(status, resp, raw)
        detail = _error_detail(status, resp, raw)
        LOGGER.error("Threads create container failed (%s): %s", kind, detail)
        return ThreadsPostResult(status=kind, detail=f"create_container: {detail}")

    creation_id = str(resp["id"])

    # Step 2: publish
    status2, resp2, raw2 = http_post_fn(
        f"{THREADS_API_BASE}/{creds.user_id}/threads_publish",
        {
            "creation_id": creation_id,
            "access_token": creds.access_token,
        },
    )
    if status2 != 200 or not resp2.get("id"):
        kind = _classify_http_error(status2, resp2, raw2)
        detail = _error_detail(status2, resp2, raw2)
        LOGGER.error("Threads publish failed (%s): %s", kind, detail)
        return ThreadsPostResult(status=kind, detail=f"publish: {detail}")

    post_id = str(resp2["id"])
    permalink = ""
    try:
        permalink = http_get_permalink_fn(post_id, creds.access_token) or ""
    except Exception as exc:
        LOGGER.warning("permalink fetch raised: %s", exc)
    post_url = build_post_url(
        post_id, username=creds.username, permalink=permalink
    )

    mark_threads_posted(
        trading_day,
        runtime_dir=runtime_dir,
        post_id=post_id,
        post_url=post_url,
    )
    LOGGER.info(
        "Threads post succeeded: trading_day=%s post_id=%s url=%s",
        trading_day,
        post_id,
        post_url or "(none)",
    )
    return ThreadsPostResult(
        status="success",
        detail="published",
        post_id=post_id,
        post_url=post_url,
    )


def format_dry_run_telegram_message(
    result: ThreadsPostResult,
    *,
    trading_day: str,
) -> str:
    """dry-run 時に Telegram へ送る確認文面。"""
    payload = result.dry_run_payload or {}
    text_preview = ""
    create_fields = payload.get("create_fields") or {}
    if isinstance(create_fields, dict):
        text_preview = str(create_fields.get("text") or "")
    lines = [
        "[Threads自動投稿 DRY-RUN]",
        f"trading_day={trading_day}",
        "APIは呼び出していません。送信予定内容:",
        f"create_url={payload.get('create_url')}",
        f"publish_url={payload.get('publish_url')}",
        f"user_id_configured={payload.get('user_id_configured')}",
        f"access_token_configured={payload.get('access_token_configured')}",
        f"text_length={payload.get('text_length')}/{payload.get('text_max')}",
        "",
        "--- body ---",
        text_preview,
    ]
    return "\n".join(lines)


def format_failure_alert(
    result: ThreadsPostResult,
    *,
    trading_day: str,
) -> str:
    kind = "認証エラー" if result.is_auth_error else "APIエラー"
    return (
        f"[ALERT] Threads自動投稿失敗 ({kind})\n"
        f"trading_day={trading_day}\n"
        f"status={result.status}\n"
        f"detail={result.detail}"
    )
