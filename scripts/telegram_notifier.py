"""Telegram notification helper for anomaly checks and report bots."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

LOGGER = logging.getLogger(__name__)
MAX_MESSAGE_LENGTH = 4096
TRUNCATED_SUFFIX = "\n(以下省略)"
MAX_RETRIES = 2

# このファイルが唯一の実装本体（ai_review/telegram_notifier.py は互換シム）。
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
# 既存の内部レポート/監視用Bot。SNS用Botとは別変数で分離する。
ENV_CANDIDATES = (
    ROOT_DIR / ".env",
    ROOT_DIR / "scripts" / ".env",
    ROOT_DIR / "ai_review" / ".env",
)


def _load_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values

    try:
        raw_text = path.read_text(encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive fallback
        LOGGER.warning("Failed to read env file %s: %s", path, exc)
        return values

    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            values[key] = value
    return values


def _merged_file_env() -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for path in ENV_CANDIDATES:
        merged.update(_load_env_file(path))
    return merged


def _resolve_credential(name: str, file_env: Optional[Dict[str, str]] = None) -> str:
    env_map = file_env if file_env is not None else _merged_file_env()
    return (os.environ.get(name) or env_map.get(name, "")).strip()


def _get_telegram_config() -> Tuple[str, str]:
    """内部管理用Bot（TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID）。"""
    file_env = _merged_file_env()
    token = _resolve_credential("TELEGRAM_BOT_TOKEN", file_env)
    chat_id = _resolve_credential("TELEGRAM_CHAT_ID", file_env)
    return token, chat_id


def _get_sns_telegram_config() -> Tuple[str, str]:
    """SNS投稿用Bot（TELEGRAM_SNS_BOT_TOKEN / TELEGRAM_SNS_CHAT_ID）。内部Botとは別。"""
    file_env = _merged_file_env()
    token = _resolve_credential("TELEGRAM_SNS_BOT_TOKEN", file_env)
    chat_id = _resolve_credential("TELEGRAM_SNS_CHAT_ID", file_env)
    return token, chat_id


def _truncate_message(text: str) -> str:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    limit = MAX_MESSAGE_LENGTH - len(TRUNCATED_SUFFIX)
    if limit <= 0:
        return TRUNCATED_SUFFIX[:MAX_MESSAGE_LENGTH]
    return text[:limit] + TRUNCATED_SUFFIX


def _send_with_credentials(
    text: str,
    token: str,
    chat_id: str,
    *,
    missing_hint: str,
    timeout_sec: int = 15,
) -> bool:
    if not token or not chat_id:
        LOGGER.warning("Telegram config is missing. %s", missing_hint)
        return False

    payload = {
        "chat_id": chat_id,
        "text": _truncate_message(text),
    }
    body = json.dumps(payload).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for attempt in range(MAX_RETRIES + 1):
        try:
            request = urllib.request.Request(
                url=url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                if 200 <= response.status < 300:
                    return True
                LOGGER.warning(
                    "Telegram API returned non-2xx status: %s (attempt %d/%d)",
                    response.status,
                    attempt + 1,
                    MAX_RETRIES + 1,
                )
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            LOGGER.warning(
                "Telegram send failed: %s (attempt %d/%d)",
                exc,
                attempt + 1,
                MAX_RETRIES + 1,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            LOGGER.warning(
                "Unexpected Telegram send error: %s (attempt %d/%d)",
                exc,
                attempt + 1,
                MAX_RETRIES + 1,
            )

    return False


def send_telegram_message(text: str, timeout_sec: int = 15) -> bool:
    """
    内部管理用Botへ送信する。
    成功時True、失敗時は例外を投げずFalseを返す。
    """
    token, chat_id = _get_telegram_config()
    return _send_with_credentials(
        text,
        token,
        chat_id,
        missing_hint="Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.",
        timeout_sec=timeout_sec,
    )


def send_sns_telegram_message(text: str, timeout_sec: int = 15) -> bool:
    """
    SNS投稿用Botへ送信する（内部Botとは別クレデンシャル）。
    成功時True、失敗時は例外を投げずFalseを返す。
    """
    token, chat_id = _get_sns_telegram_config()
    return _send_with_credentials(
        text,
        token,
        chat_id,
        missing_hint="Set TELEGRAM_SNS_BOT_TOKEN and TELEGRAM_SNS_CHAT_ID.",
        timeout_sec=timeout_sec,
    )


def _build_multipart_form(
    fields: Dict[str, str],
    files: Dict[str, Path],
) -> Tuple[bytes, str]:
    boundary = f"----cursorBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for name, path in files.items():
        filename = path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def send_sns_telegram_video(
    video_path: Union[str, Path],
    caption: str = "",
    timeout_sec: int = 120,
) -> bool:
    """
    SNS投稿用Botへ動画(+キャプション)を送信する。
    内部Botへは送らない。成功時True、失敗時False。
    """
    token, chat_id = _get_sns_telegram_config()
    if not token or not chat_id:
        LOGGER.warning(
            "Telegram config is missing. Set TELEGRAM_SNS_BOT_TOKEN and TELEGRAM_SNS_CHAT_ID."
        )
        return False

    path = Path(video_path)
    if not path.exists() or path.stat().st_size <= 0:
        LOGGER.warning("SNS video file missing or empty: %s", path)
        return False

    # Telegram caption limit for media is 1024 chars.
    cap = (caption or "").strip()
    if len(cap) > 1024:
        cap = cap[:1000] + "\n(以下省略)"

    fields = {"chat_id": chat_id, "supports_streaming": "true"}
    if cap:
        fields["caption"] = cap
    body, content_type = _build_multipart_form(fields, {"video": path})
    url = f"https://api.telegram.org/bot{token}/sendVideo"

    for attempt in range(MAX_RETRIES + 1):
        try:
            request = urllib.request.Request(
                url=url,
                data=body,
                headers={"Content-Type": content_type},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                if 200 <= response.status < 300:
                    return True
                LOGGER.warning(
                    "Telegram sendVideo non-2xx: %s (attempt %d/%d)",
                    response.status,
                    attempt + 1,
                    MAX_RETRIES + 1,
                )
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            LOGGER.warning(
                "Telegram sendVideo failed: %s (attempt %d/%d)",
                exc,
                attempt + 1,
                MAX_RETRIES + 1,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            LOGGER.warning(
                "Unexpected Telegram sendVideo error: %s (attempt %d/%d)",
                exc,
                attempt + 1,
                MAX_RETRIES + 1,
            )
    return False
