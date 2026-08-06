#!/usr/bin/env python3
"""
Threads / Instagram の長期アクセストークンを自動更新する。

- Threads: GET https://graph.threads.net/v1.0/refresh_access_token
    ?grant_type=th_refresh_token&access_token=<現在のTHREADS_ACCESS_TOKEN>
- Instagram: GET https://graph.instagram.com/refresh_access_token
    ?grant_type=ig_refresh_token&access_token=<現在のINSTAGRAM_ACCESS_TOKEN>

- 成功時は .env（ROOT/.env, scripts/.env, ai_review/.env のうち該当キーが
  存在するファイルすべて）を os.replace() によるアトミック書き込みで更新する。
  対象キー以外の行は一切変更しない。
- Threads/Instagramどちらか一方が失敗しても、もう一方の処理は独立して継続する。
- 失敗時（HTTPエラー・レスポンス不正等）は .env の値を変更せず維持し、
  内部管理用Bot（telegram_notifier.send_telegram_message）へアラートする。
- --check-only 指定時はリフレッシュAPIを呼ばず、現在のトークンの有効性/有効期限を
  Meta debug_token（自己検証）で確認し、ログ出力・Telegram通知するだけにする。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# scripts/ を path 先頭へ（同梱モジュール解決用）。telegram_notifier 本体もここ。
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) in sys.path:
    sys.path.remove(str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR))

from telegram_notifier import send_telegram_message  # noqa: E402
from threads_poster import ENV_CANDIDATES, THREADS_API_BASE, _merged_env  # noqa: E402

LOGGER = logging.getLogger("refresh_sns_tokens")

ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT_DIR / "log"

HTTP_TIMEOUT_SEC = 30

THREADS_REFRESH_URL = f"{THREADS_API_BASE}/refresh_access_token"
# Instagram Login (graph.instagram.com) の refresh_access_token は
# 公式ドキュメント上バージョンパスを付けない（content publishing 系とは異なる）。
INSTAGRAM_REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
# 有効期限確認（--check-only）用。app access token が無くても自己検証（
# input_token=access_token=対象トークン自身）で試行し、取れなければ有効性のみ報告する。
DEBUG_TOKEN_URL = "https://graph.facebook.com/debug_token"

PLATFORM_ENV_KEYS: Dict[str, str] = {
    "threads": "THREADS_ACCESS_TOKEN",
    "instagram": "INSTAGRAM_ACCESS_TOKEN",
}
PLATFORM_LABELS: Dict[str, str] = {
    "threads": "Threads",
    "instagram": "Instagram",
}


@dataclass(frozen=True)
class RefreshResult:
    """
    status:
      success | error
    """

    platform: str
    status: str
    detail: str = ""
    new_access_token: str = ""
    expires_in_sec: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status == "success"


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


def _error_detail(status: int, body: Dict[str, Any], raw: str) -> str:
    err = body.get("error") if isinstance(body.get("error"), dict) else None
    if err:
        return (
            f"http={status} code={err.get('code')!r} message={err.get('message')!r}"
        )
    snippet = (raw or "").strip().replace("\n", " ")
    if len(snippet) > 300:
        snippet = snippet[:300] + "..."
    return f"http={status} body={snippet!r}"


def _refresh_token(
    platform: str,
    url: str,
    grant_type: str,
    current_token: str,
    *,
    http_get_fn: Callable[..., Tuple[int, Dict[str, Any], str]] = _http_get_json,
) -> RefreshResult:
    if not current_token:
        return RefreshResult(
            platform=platform,
            status="error",
            detail=(
                f"{PLATFORM_ENV_KEYS[platform]} is not configured; "
                "nothing to refresh."
            ),
        )
    qs = urllib.parse.urlencode(
        {"grant_type": grant_type, "access_token": current_token}
    )
    status, body, raw = http_get_fn(f"{url}?{qs}")
    new_token = body.get("access_token") if isinstance(body, dict) else None
    if status != 200 or not isinstance(new_token, str) or not new_token:
        return RefreshResult(
            platform=platform,
            status="error",
            detail=_error_detail(status, body, raw),
        )
    expires_in = body.get("expires_in")
    return RefreshResult(
        platform=platform,
        status="success",
        new_access_token=new_token,
        expires_in_sec=(
            int(expires_in) if isinstance(expires_in, (int, float)) else None
        ),
    )


def refresh_threads_token(
    current_token: str,
    *,
    http_get_fn: Callable[..., Tuple[int, Dict[str, Any], str]] = _http_get_json,
) -> RefreshResult:
    return _refresh_token(
        "threads",
        THREADS_REFRESH_URL,
        "th_refresh_token",
        current_token,
        http_get_fn=http_get_fn,
    )


def refresh_instagram_token(
    current_token: str,
    *,
    http_get_fn: Callable[..., Tuple[int, Dict[str, Any], str]] = _http_get_json,
) -> RefreshResult:
    return _refresh_token(
        "instagram",
        INSTAGRAM_REFRESH_URL,
        "ig_refresh_token",
        current_token,
        http_get_fn=http_get_fn,
    )


def _update_env_value_in_file(path: Path, key: str, new_value: str) -> bool:
    """
    path 内の "<key>=..." 行だけを new_value に書き換える。他の行は変更しない。
    該当行が無い/ファイルが存在しない場合は何もせず False を返す。
    書き込みは tmp ファイル + os.replace() によるアトミック置換。
    """
    if not path.exists():
        return False
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("failed to read %s: %s", path, exc)
        return False

    prefix = f"{key}="
    lines = original.splitlines(keepends=True)
    changed = False
    new_lines: List[str] = []
    for line in lines:
        body = line.rstrip("\r\n")
        if not changed and body.startswith(prefix):
            eol = line[len(body):]
            new_lines.append(f"{prefix}{new_value}{eol}")
            changed = True
        else:
            new_lines.append(line)
    if not changed:
        return False

    tmp_path = path.parent / f".{path.name}.tmp{os.getpid()}"
    try:
        tmp_path.write_text("".join(new_lines), encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return True


def update_env_value(
    key: str,
    new_value: str,
    *,
    env_candidates: Tuple[Path, ...] = ENV_CANDIDATES,
) -> List[Path]:
    """
    "<key>=" 行が存在する .env ファイルすべてを new_value で更新する
    （他の行・他ファイルは変更しない）。
    戻り値: 実際に更新したファイルのパス一覧。
    """
    updated: List[Path] = []
    for path in env_candidates:
        if _update_env_value_in_file(path, key, new_value):
            updated.append(path)
    return updated


def _alert_internal(message: str) -> None:
    try:
        send_telegram_message(message)
    except Exception as exc:
        LOGGER.warning("internal alert send failed: %s", exc)


def _format_expiry(expires_in_sec: Optional[int]) -> str:
    if expires_in_sec is None:
        return "unknown"
    expires_at = datetime.now() + timedelta(seconds=expires_in_sec)
    days = expires_in_sec / 86400
    return f"{expires_at.isoformat(timespec='seconds')} (about {days:.1f} days)"


def run_refresh(
    *,
    env: Optional[Dict[str, str]] = None,
    env_candidates: Tuple[Path, ...] = ENV_CANDIDATES,
    refresh_threads_fn: Callable[..., RefreshResult] = refresh_threads_token,
    refresh_instagram_fn: Callable[..., RefreshResult] = refresh_instagram_token,
    alert_fn: Optional[Callable[[str], None]] = None,
) -> Dict[str, RefreshResult]:
    """
    Threads/Instagramのトークンをそれぞれ独立にリフレッシュし、成功したものだけ
    .env を更新する。どちらの結果も内部管理Botへ通知する。
    """
    src = env if env is not None else _merged_env()
    alert = alert_fn or _alert_internal

    refresh_fns: Dict[str, Callable[[str], RefreshResult]] = {
        "threads": refresh_threads_fn,
        "instagram": refresh_instagram_fn,
    }

    results: Dict[str, RefreshResult] = {}
    for platform, refresh_fn in refresh_fns.items():
        key = PLATFORM_ENV_KEYS[platform]
        current_token = (src.get(key) or "").strip()
        try:
            result = refresh_fn(current_token)
        except Exception as exc:
            LOGGER.exception("%s token refresh raised: %s", platform, exc)
            result = RefreshResult(
                platform=platform,
                status="error",
                detail=f"unexpected {type(exc).__name__}: {exc}",
            )
        results[platform] = result

        label = PLATFORM_LABELS[platform]
        if result.ok:
            updated_paths = update_env_value(
                key, result.new_access_token, env_candidates=env_candidates
            )
            expiry_text = _format_expiry(result.expires_in_sec)
            LOGGER.info(
                "%s token refresh succeeded. new_expiry=%s updated_files=%s",
                platform,
                expiry_text,
                [str(p) for p in updated_paths],
            )
            alert(
                f"[SNSトークン更新] {label} 成功\n"
                f"新しい有効期限(目安): {expiry_text}\n"
                "更新ファイル: "
                + (", ".join(str(p) for p in updated_paths) or "(なし)")
            )
        else:
            LOGGER.error("%s token refresh failed: %s", platform, result.detail)
            alert(
                f"[ALERT] SNSトークン更新失敗 ({label})\n"
                f"detail={result.detail}\n"
                "現在の .env の値は変更していません。"
            )

    return results


def check_token_status(
    platform: str,
    token: str,
    *,
    http_get_fn: Callable[..., Tuple[int, Dict[str, Any], str]] = _http_get_json,
) -> str:
    """
    リフレッシュは行わず、現在のトークンの有効性・有効期限を確認して
    人間可読な文字列を返す（--check-only 用）。

    Meta debug_token は本来アプリアクセストークンが必要だが、アプリの
    ID/secret を保有していない運用のため、まずは自己検証
    （input_token=access_token=対象トークン自身）で試行する。
    取得できない場合は「確認不可」を返す（有効期限は分からないが、
    リフレッシュAPIは一切呼ばないため既存トークンへの副作用は無い）。
    """
    if not token:
        return "未設定"
    qs = urllib.parse.urlencode({"input_token": token, "access_token": token})
    status, body, raw = http_get_fn(f"{DEBUG_TOKEN_URL}?{qs}")
    data = body.get("data") if isinstance(body, dict) else None
    if status == 200 and isinstance(data, dict) and "expires_at" in data:
        is_valid = data.get("is_valid")
        expires_at = data.get("expires_at")
        try:
            expires_at_int = int(expires_at)
            expires_str = (
                datetime.fromtimestamp(expires_at_int).isoformat(timespec="seconds")
                if expires_at_int > 0
                else "無期限"
            )
        except (TypeError, ValueError):
            expires_str = str(expires_at)
        return f"is_valid={is_valid} expires_at={expires_str}"

    detail = _error_detail(status, body, raw)
    LOGGER.warning("%s debug_token check unavailable: %s", platform, detail)
    return f"確認不可（{detail}）"


def run_check_only(
    *,
    env: Optional[Dict[str, str]] = None,
    check_fn: Callable[[str, str], str] = check_token_status,
    alert_fn: Optional[Callable[[str], None]] = None,
) -> Dict[str, str]:
    src = env if env is not None else _merged_env()
    alert = alert_fn or _alert_internal

    lines = ["[SNSトークン確認 (check-only, リフレッシュ未実行)]"]
    results: Dict[str, str] = {}
    for platform, key in PLATFORM_ENV_KEYS.items():
        label = PLATFORM_LABELS[platform]
        token = (src.get(key) or "").strip()
        try:
            status_text = check_fn(platform, token)
        except Exception as exc:
            LOGGER.exception("%s token check raised: %s", platform, exc)
            status_text = f"確認失敗（unexpected {type(exc).__name__}: {exc}）"
        results[platform] = status_text
        lines.append(f"{label}: {status_text}")
        LOGGER.info("%s token check: %s", platform, status_text)

    alert("\n".join(lines))
    return results


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [refresh_sns_tokens] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Threads/Instagram の長期アクセストークンを自動更新する。"
            "--check-only 指定時はリフレッシュせず有効性のみ確認する。"
        )
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help=(
            "リフレッシュAPIを呼ばず、現在の .env のトークンの有効性/有効期限を"
            "確認してログ出力・Telegram通知するだけにする"
        ),
    )
    args = parser.parse_args(argv)
    _setup_logging()

    if args.check_only:
        LOGGER.info("check-only mode: refresh API will not be called.")
        run_check_only()
        return 0

    results = run_refresh()
    if any(not result.ok for result in results.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
