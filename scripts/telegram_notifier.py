"""Telegram notification helper for anomaly checks."""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict

LOGGER = logging.getLogger(__name__)
MAX_MESSAGE_LENGTH = 4096
TRUNCATED_SUFFIX = "\n(以下省略)"
MAX_RETRIES = 2

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"


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


def _get_telegram_config() -> tuple[str, str]:
    file_env = _load_env_file(ENV_PATH)
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or file_env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or file_env.get("TELEGRAM_CHAT_ID", "")
    return token.strip(), chat_id.strip()


def _truncate_message(text: str) -> str:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    limit = MAX_MESSAGE_LENGTH - len(TRUNCATED_SUFFIX)
    if limit <= 0:
        return TRUNCATED_SUFFIX[:MAX_MESSAGE_LENGTH]
    return text[:limit] + TRUNCATED_SUFFIX


def send_telegram_message(text: str, timeout_sec: int = 15) -> bool:
    """
    Telegram Bot APIでメッセージを送信する。
    成功時True、失敗時（未設定・タイムアウト・APIエラー含む）は
    例外を投げずログにwarningを出してFalseを返す。
    呼び出し元の監視処理全体を止めないことを最優先する。
    """
    token, chat_id = _get_telegram_config()
    if not token or not chat_id:
        LOGGER.warning("Telegram config is missing. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
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
