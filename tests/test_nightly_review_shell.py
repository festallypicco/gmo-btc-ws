"""
test_nightly_review_shell.py

run_nightly_review.sh の AI_REVIEW_PROPOSER_MODE 分岐と
run_pipeline_locked.sh のロック動作を、python スタブで検証する。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")

_ROOT_DIR = Path(__file__).resolve().parent.parent
_AI_REVIEW_SRC = _ROOT_DIR / "ai_review"


def _setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    ai_dir = tmp_path / "ai_review"
    ai_dir.mkdir()
    for name in ("run_nightly_review.sh", "run_pipeline_locked.sh"):
        shutil.copy(_AI_REVIEW_SRC / name, ai_dir / name)
    for name in (
        "build_change_outcomes.py",
        "build_ai_review_summary.py",
        "build_proposer_prompt.py",
        "review_pipeline.py",
    ):
        (ai_dir / name).write_text("# stub\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    fake_python = bin_dir / "python"
    fake_python.write_text(
        '#!/bin/bash\necho "$@" >> "' + str(calls) + '"\nexit 0\n', encoding="utf-8"
    )
    fake_python.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return ai_dir, calls, env


def _calls_text(calls: Path) -> str:
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


def test_nightly_default_mode_runs_pipeline(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ai_dir, calls, env = _setup(tmp_path)
    env.pop("AI_REVIEW_PROPOSER_MODE", None)
    result = subprocess.run(
        ["bash", str(ai_dir / "run_nightly_review.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    text = _calls_text(calls)
    assert "review_pipeline.py" in text
    assert "build_proposer_prompt.py" not in text


def test_nightly_jiji_mode_stops_at_prompt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ai_dir, calls, env = _setup(tmp_path)
    env["AI_REVIEW_PROPOSER_MODE"] = "jiji"
    result = subprocess.run(
        ["bash", str(ai_dir / "run_nightly_review.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    text = _calls_text(calls)
    assert "build_proposer_prompt.py" in text
    assert "review_pipeline.py" not in text


def test_nightly_unknown_mode_fails_loud(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ai_dir, calls, env = _setup(tmp_path)
    env["AI_REVIEW_PROPOSER_MODE"] = "Jiji"
    result = subprocess.run(
        ["bash", str(ai_dir / "run_nightly_review.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "unknown AI_REVIEW_PROPOSER_MODE" in result.stderr
    assert _calls_text(calls) == ""


def test_pipeline_locked_runs_and_releases(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ai_dir, calls, env = _setup(tmp_path)
    result = subprocess.run(
        [
            "bash",
            str(ai_dir / "run_pipeline_locked.sh"),
            "--target-date",
            "2026-11-22",
            "--proposer-file",
            "/app/log/ai_review_proposer_output_2026-11-22.txt",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    text = _calls_text(calls)
    assert "review_pipeline.py" in text
    assert "--proposer-file" in text
    assert not (tmp_path / "log" / "run_nightly_review.lock").exists()


def test_pipeline_locked_blocked_by_fresh_lock(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ai_dir, calls, env = _setup(tmp_path)
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    (log_dir / "run_nightly_review.lock").write_text("2026-11-23T03:30:00+09:00\n")
    result = subprocess.run(
        ["bash", str(ai_dir / "run_pipeline_locked.sh"), "--target-date", "2026-11-22"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "review_pipeline.py" not in _calls_text(calls)
