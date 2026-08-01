from __future__ import annotations

import io
import sys
from datetime import date
from pathlib import Path

import pytest

_ROOT_DIR = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _ROOT_DIR / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from process_launcher import (  # noqa: E402
    attach_engine_file_logs,
    engine_log_paths_for_day,
)


def test_engine_log_paths_for_day_match_native_naming(tmp_path: Path) -> None:
    log_path, err_path = engine_log_paths_for_day(tmp_path, day=date(2026, 7, 30))
    assert log_path == tmp_path / "engine_2026-07-30.log"
    assert err_path == tmp_path / "engine_2026-07-30.err.log"


def test_attach_engine_file_logs_tees_stdout_and_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_out = io.StringIO()
    fake_err = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)

    log_path, err_path = attach_engine_file_logs(tmp_path, day=date(2026, 7, 30))
    assert log_path.exists()
    assert err_path.exists()

    print("[OK] stdout line")
    print("[WARN] stderr line", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()

    assert "[OK] stdout line" in fake_out.getvalue()
    assert "[WARN] stderr line" in fake_err.getvalue()
    assert "[OK] stdout line" in log_path.read_text(encoding="utf-8")
    assert "[WARN] stderr line" in err_path.read_text(encoding="utf-8")


def test_attach_engine_file_logs_appends_without_truncate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = date(2026, 7, 30)
    log_path, err_path = engine_log_paths_for_day(tmp_path, day=day)
    log_path.write_text("prior stdout\n", encoding="utf-8")
    err_path.write_text("prior stderr\n", encoding="utf-8")

    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    attach_engine_file_logs(tmp_path, day=day)
    print("new stdout")
    print("new stderr", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()

    out_text = log_path.read_text(encoding="utf-8")
    err_text = err_path.read_text(encoding="utf-8")
    assert out_text.startswith("prior stdout\n")
    assert "new stdout" in out_text
    assert err_text.startswith("prior stderr\n")
    assert "new stderr" in err_text
