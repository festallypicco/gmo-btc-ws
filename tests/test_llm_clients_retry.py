"""
tests/test_llm_clients_retry.py

LLM再試行ポリシー（503/タイムアウトは再試行、429は即終了）を検証する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_AI = _ROOT / "ai_review"
if str(_AI) not in sys.path:
    sys.path.insert(0, str(_AI))

import llm_clients as lc  # noqa: E402


def test_classify_llm_error_kinds() -> None:
    assert lc.classify_llm_error_kind(TimeoutError("LLM request timed out after 120s")) == "タイムアウト"
    assert lc.classify_llm_error_kind(RuntimeError("503 UNAVAILABLE high demand")) == "混雑"
    assert (
        lc.classify_llm_error_kind(
            RuntimeError(
                "429 RESOURCE_EXHAUSTED requestsperday free_tier"
            )
        )
        == "クォータ超過"
    )
    assert lc.classify_llm_error_kind(RuntimeError("boom")) == "その他"


def test_retry_on_timeout_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(lc, "_RETRY_SLEEP_SEC", 0)
    monkeypatch.setattr(lc.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("LLM request timed out after 120s")
        return "ok"

    assert lc._call_with_retry(flaky, timeout_sec=1) == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_no_retry_on_daily_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(lc.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def always_quota() -> str:
        calls["n"] += 1
        raise RuntimeError(
            "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
        )

    with pytest.raises(RuntimeError, match="429"):
        lc._call_with_retry(always_quota, timeout_sec=1)
    assert calls["n"] == 1
    assert sleeps == []


def test_retry_on_503(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(lc, "_RETRY_SLEEP_SEC", 0)
    monkeypatch.setattr(lc.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(
                "503 UNAVAILABLE. This model is currently experiencing high demand."
            )
        return "recovered"

    assert lc._call_with_retry(flaky, timeout_sec=1) == "recovered"
    assert calls["n"] == 2
    assert len(sleeps) == 1
