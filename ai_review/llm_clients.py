from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Callable, TypeVar

from dotenv import load_dotenv

T = TypeVar("T")

_DOTENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_DOTENV_PATH)

_REQUEST_TIMEOUT_SEC = 120
# 503 / タイムアウトなど一時障害向け。初回失敗後の再試行回数（合計試行=これ+1）。
_MAX_RETRIES = 3
_RETRY_SLEEP_SEC = 30
_MAX_INCOMPLETE_RETRIES = 2
_GEMINI_MAX_OUTPUT_TOKENS_CAP = 8192

_ACCEPTABLE_FINISH_REASONS = {"STOP", "FINISH_REASON_UNSPECIFIED"}
_RETRY_FINISH_REASONS = {
    "MAX_TOKENS",
    "MALFORMED_FUNCTION_CALL",
    "NO_CANDIDATES",
    "UNKNOWN",
}


class IncompleteGeminiResponseError(RuntimeError):
    """Gemini response ended before normal completion (e.g. MAX_TOKENS)."""


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} が未設定です。ai_review/.env を作成して API キーを設定してください。"
        )
    return value


def _is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


# 一時的なサーバー側エラー（高負荷・ゲートウェイ障害など）。時間をおけば回復する見込みがある。
_RETRYABLE_SERVER_STATUS_CODES = {500, 502, 503, 504}


def _is_transient_server_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code in _RETRYABLE_SERVER_STATUS_CODES:
        return True
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) in _RETRYABLE_SERVER_STATUS_CODES:
        return True
    text = str(exc).lower()
    if any(str(code) in text for code in _RETRYABLE_SERVER_STATUS_CODES):
        return True
    return (
        "unavailable" in text
        or "internal server error" in text
        or "bad gateway" in text
        or "gateway timeout" in text
        or "service unavailable" in text
    )


def _is_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text or "deadline" in text


def _is_daily_quota_exceeded_error(exc: Exception) -> bool:
    text = str(exc).lower()
    has_resource_exhausted = "resource_exhausted" in text
    has_per_day_metric = "requestsperday" in text or "perday" in text
    return has_resource_exhausted and has_per_day_metric


def classify_llm_error_kind(exc: BaseException) -> str:
    """
    日次レポート向けの粗い失敗分類。
    戻り値: タイムアウト / 混雑 / クォータ超過 / その他
    """
    as_exc = exc if isinstance(exc, Exception) else Exception(str(exc))
    if _is_daily_quota_exceeded_error(as_exc) or _is_rate_limit_error(as_exc):
        return "クォータ超過"
    if _is_transient_server_error(as_exc):
        return "混雑"
    if _is_timeout_error(as_exc):
        return "タイムアウト"
    return "その他"


def _is_retryable_error(exc: Exception) -> bool:
    """
    再試行対象は一時的な混雑(5xx/UNAVAILABLE)とタイムアウトのみ。
    429（日次クォータ含む）は再試行しない。
    """
    if _is_rate_limit_error(exc) or _is_daily_quota_exceeded_error(exc):
        return False
    return _is_transient_server_error(exc) or _is_timeout_error(exc)


def _extract_finish_reason(response: object) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "NO_CANDIDATES"
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return "UNKNOWN"
    if hasattr(reason, "name"):
        return str(reason.name)
    return str(reason)


def _run_with_timeout(fn: Callable[[], T], timeout_sec: int) -> T:
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_sec)
        except FutureTimeoutError as exc:
            raise TimeoutError(f"LLM request timed out after {timeout_sec}s") from exc


def _call_with_retry(fn: Callable[[], str], timeout_sec: int = _REQUEST_TIMEOUT_SEC) -> str:
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return _run_with_timeout(fn, timeout_sec=timeout_sec)
        except Exception as exc:
            # 429（日次クォータ含む）は再試行しても当日中は無駄なので即終了。
            if _is_daily_quota_exceeded_error(exc) or _is_rate_limit_error(exc):
                print("[LLM] quota/rate-limit exceeded. no retry.")
                raise
            if not _is_retryable_error(exc) or attempt >= _MAX_RETRIES:
                raise
            kind = classify_llm_error_kind(exc)
            print(
                f"[LLM] retryable failure kind={kind} "
                f"attempt={attempt + 1}/{_MAX_RETRIES}; "
                f"sleep {_RETRY_SLEEP_SEC}s"
            )
            time.sleep(_RETRY_SLEEP_SEC)
    raise RuntimeError("unexpected retry loop termination")


def call_groq(prompt: str, system: str, max_tokens: int = 1000) -> str:
    """openai/gpt-oss-120b を呼び出し、テキスト応答を返す"""
    api_key = _require_env("GROQ_API_KEY")
    from groq import Groq

    client = Groq(api_key=api_key)

    def _once() -> str:
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        content = response.choices[0].message.content
        if isinstance(content, list):
            content = "".join(str(item) for item in content)
        text = (content or "").strip()
        if not text:
            raise RuntimeError("Groq response is empty")
        return text

    return _call_with_retry(_once, timeout_sec=_REQUEST_TIMEOUT_SEC)


def call_gemini(
    prompt: str,
    system: str,
    max_tokens: int = 4096,
    json_mode: bool = False,
) -> str:
    """gemini-3.5-flash を呼び出し、テキスト応答を返す"""
    api_key = _require_env("GEMINI_API_KEY")
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    effective_max_tokens = max_tokens

    for incomplete_attempt in range(_MAX_INCOMPLETE_RETRIES + 1):
        def _once() -> str:
            config_kwargs: dict[str, object] = {
                "system_instruction": system,
                "max_output_tokens": effective_max_tokens,
                "temperature": 0.2,
            }
            if json_mode:
                config_kwargs["response_mime_type"] = "application/json"

            response = client.models.generate_content(
                model="gemini-3.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            finish_reason = _extract_finish_reason(response)
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError(
                    f"Gemini response is empty (finish_reason={finish_reason})"
                )
            if finish_reason in _ACCEPTABLE_FINISH_REASONS:
                return text
            if finish_reason in _RETRY_FINISH_REASONS:
                raise IncompleteGeminiResponseError(
                    f"Gemini finish_reason={finish_reason} "
                    f"(max_output_tokens={effective_max_tokens})"
                )
            raise RuntimeError(
                f"Gemini response blocked or failed: finish_reason={finish_reason}"
            )

        try:
            return _call_with_retry(_once, timeout_sec=_REQUEST_TIMEOUT_SEC)
        except IncompleteGeminiResponseError:
            if incomplete_attempt >= _MAX_INCOMPLETE_RETRIES:
                raise
            effective_max_tokens = min(
                effective_max_tokens * 2,
                _GEMINI_MAX_OUTPUT_TOKENS_CAP,
            )
            time.sleep(1)

    raise RuntimeError("unexpected Gemini incomplete retry loop termination")
