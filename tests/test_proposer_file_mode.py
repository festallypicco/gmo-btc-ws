"""
test_proposer_file_mode.py

review_pipeline.py の --proposer-file 経路（提案役をファイルから読む）と
build_proposer_prompt.py のプロンプトファイル書き出しを検証する。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT_DIR = Path(__file__).resolve().parent.parent
_AI_REVIEW_DIR = _ROOT_DIR / "ai_review"
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
for path in (_AI_REVIEW_DIR, _BTC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import review_pipeline  # noqa: E402
import build_proposer_prompt as bpp  # noqa: E402


def _groq_must_not_be_called(**kwargs):  # type: ignore[no-untyped-def]
    raise AssertionError("call_groq must not be called in proposer-file mode")


def test_proposer_file_returns_content_without_groq(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(review_pipeline, "call_groq", _groq_must_not_be_called)
    proposer_file = tmp_path / "proposer.txt"
    proposer_file.write_text("  提案テキスト\n", encoding="utf-8")
    result = review_pipeline._call_proposer({}, "live", str(proposer_file))
    assert result == "提案テキスト"


def test_proposer_file_missing_raises(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(review_pipeline, "call_groq", _groq_must_not_be_called)
    missing = tmp_path / "does_not_exist.txt"
    with pytest.raises(FileNotFoundError):
        review_pipeline._call_proposer({}, "live", str(missing))


def test_proposer_file_empty_raises(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(review_pipeline, "call_groq", _groq_must_not_be_called)
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError):
        review_pipeline._call_proposer({}, "live", str(empty))


def test_stub_mode_takes_precedence_over_file(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(review_pipeline, "call_groq", _groq_must_not_be_called)
    proposer_file = tmp_path / "proposer.txt"
    proposer_file.write_text("提案テキスト", encoding="utf-8")
    result = review_pipeline._call_proposer({}, "stub_success", str(proposer_file))
    assert result.startswith("Stub proposer:")


def test_groq_path_unchanged_without_file(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        review_pipeline, "build_proposer_prompt", lambda summary: ("SYS", "PROMPT")
    )
    captured = {}

    def fake_groq(prompt, system, max_tokens):  # type: ignore[no-untyped-def]
        captured["prompt"] = prompt
        captured["system"] = system
        captured["max_tokens"] = max_tokens
        return "groq output"

    monkeypatch.setattr(review_pipeline, "call_groq", fake_groq)
    result = review_pipeline._call_proposer({"any": "summary"}, "live")
    assert result == "groq output"
    assert captured == {"prompt": "PROMPT", "system": "SYS", "max_tokens": 2000}


def test_write_prompt_file_contents(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(bpp, "build_proposer_prompt", lambda summary: ("SYS", "PROMPT"))
    out_path = tmp_path / "ai_review_proposer_prompt_2026-11-22.json"
    bpp.write_prompt_file({"k": "v"}, "2026-11-22", out_path)
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["target_date"] == "2026-11-22"
    assert doc["system"] == "SYS"
    assert doc["prompt"] == "PROMPT"
    assert "generated_at" in doc


def test_resolve_target_date_explicit() -> None:
    assert bpp.resolve_target_date("2026-11-22") == "2026-11-22"
    with pytest.raises(ValueError):
        bpp.resolve_target_date("2026/11/22")


def test_resolve_target_date_default_is_yesterday() -> None:
    from datetime import date, timedelta

    assert bpp.resolve_target_date(None) == (date.today() - timedelta(days=1)).isoformat()


def test_proposer_file_name_must_match_target_date(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(review_pipeline, "call_groq", _groq_must_not_be_called)
    wrong = tmp_path / "ai_review_proposer_output_2026-11-21.txt"
    wrong.write_text("昨日の提案", encoding="utf-8")
    with pytest.raises(ValueError):
        review_pipeline._call_proposer({}, "live", str(wrong), "2026-11-22")
    right = tmp_path / "ai_review_proposer_output_2026-11-22.txt"
    right.write_text("当日の提案", encoding="utf-8")
    assert review_pipeline._call_proposer({}, "live", str(right), "2026-11-22") == "当日の提案"


def test_proposer_file_empty_string_is_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(review_pipeline, "call_groq", _groq_must_not_be_called)
    with pytest.raises(ValueError):
        review_pipeline._call_proposer({}, "live", "", "2026-11-22")


def test_proposer_file_too_large_raises(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(review_pipeline, "call_groq", _groq_must_not_be_called)
    big = tmp_path / "ai_review_proposer_output_2026-11-22.txt"
    big.write_bytes(b"x" * (review_pipeline.PROPOSER_FILE_MAX_BYTES + 1))
    with pytest.raises(ValueError):
        review_pipeline._call_proposer({}, "live", str(big), "2026-11-22")


def test_proposer_file_bom_is_stripped(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(review_pipeline, "call_groq", _groq_must_not_be_called)
    bom = tmp_path / "ai_review_proposer_output_2026-11-22.txt"
    bom.write_bytes(b"\xef\xbb\xbf" + "提案テキスト".encode("utf-8"))
    assert review_pipeline._call_proposer({}, "live", str(bom), "2026-11-22") == "提案テキスト"


def test_main_missing_proposer_file_fails_safe(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"profiles": [], "version": "test"}), encoding="utf-8"
    )
    (log_dir / "ai_review_summary_2026-11-22.json").write_text(
        json.dumps({"target_date": "2026-11-22"}), encoding="utf-8"
    )
    monkeypatch.setattr(review_pipeline, "LOG_DIR", log_dir)
    monkeypatch.setattr(review_pipeline, "CONFIG_PATH", config_path)
    monkeypatch.setattr(
        review_pipeline, "VALIDATION_FAILURE_PATH", log_dir / "ai_validation_failures.jsonl"
    )
    monkeypatch.setattr(review_pipeline, "UPDATE_LOG_PATH", log_dir / "update_log.jsonl")
    monkeypatch.setattr(review_pipeline, "apply_daily_rollouts", lambda: None)
    monkeypatch.setattr(review_pipeline, "_notify_non_blocking", lambda msg: None)
    monkeypatch.setattr(review_pipeline, "call_groq", _groq_must_not_be_called)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review_pipeline.py",
            "--target-date",
            "2026-11-22",
            "--proposer-file",
            str(log_dir / "ai_review_proposer_output_2026-11-22.txt"),
        ],
    )
    assert review_pipeline.main() == 1
    doc = json.loads(
        (log_dir / "ai_review_decision_2026-11-22.json").read_text(encoding="utf-8")
    )
    assert doc["status"] == "failed_before_moderator"
    assert doc["proposer_source"] == "file"
    assert doc["proposer_output"] is None
    # config は書き換えられていない
    assert json.loads(config_path.read_text(encoding="utf-8"))["version"] == "test"


def test_build_proposer_prompt_main_missing_summary(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(bpp, "LOG_DIR", tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["build_proposer_prompt.py", "--target-date", "2026-11-22"]
    )
    assert bpp.main() == 1
