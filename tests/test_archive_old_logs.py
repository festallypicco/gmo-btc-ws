from __future__ import annotations

import sys
import zipfile
from datetime import date
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _ROOT_DIR / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import archive_old_logs as aol  # noqa: E402
from archive_old_logs import (  # noqa: E402
    archive_month,
    eligible_months,
    find_dated_log_files,
)

APPEND_ONLY_FILES = [
    "config_history.jsonl",
    "update_log.jsonl",
    "ai_validation_failures.jsonl",
]


def _touch(path: Path, content: str = "line1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
#  find_dated_log_files
# --------------------------------------------------------------------------- #

def test_find_dated_log_files_groups_by_year_month(tmp_path: Path) -> None:
    f1 = _touch(tmp_path / "engine_2026-03-01.log")
    f2 = _touch(tmp_path / "market_snapshot_2026-03-31.csv")
    f3 = _touch(tmp_path / "realtime_trading_log_2026-04-15.csv")
    grouped = find_dated_log_files(tmp_path)
    assert set(grouped.keys()) == {"2026-03", "2026-04"}
    assert set(grouped["2026-03"]) == {f1, f2}
    assert grouped["2026-04"] == [f3]


def test_find_dated_log_files_excludes_undated_files(tmp_path: Path) -> None:
    _touch(tmp_path / "engine_2026-03-01.log")
    for name in APPEND_ONLY_FILES:
        _touch(tmp_path / name)
    grouped = find_dated_log_files(tmp_path)
    all_names = {p.name for paths in grouped.values() for p in paths}
    for name in APPEND_ONLY_FILES:
        assert name not in all_names
    assert all_names == {"engine_2026-03-01.log"}


def test_find_dated_log_files_excludes_archive_subfolder(tmp_path: Path) -> None:
    _touch(tmp_path / "engine_2026-03-01.log")
    _touch(tmp_path / "archive" / "log_2025-12.zip")
    _touch(tmp_path / "archive" / "engine_2025-12-01.log")
    grouped = find_dated_log_files(tmp_path)
    all_paths = [p for paths in grouped.values() for p in paths]
    assert all(p.parent == tmp_path for p in all_paths)
    assert {p.name for p in all_paths} == {"engine_2026-03-01.log"}


# --------------------------------------------------------------------------- #
#  eligible_months
# --------------------------------------------------------------------------- #

def test_eligible_months_includes_fully_old_month(tmp_path: Path) -> None:
    # today=2026-07-17, cutoff=2026-04-18。3月末(2026-03-31) < cutoff なので対象。
    grouped = {"2026-03": [tmp_path / "engine_2026-03-01.log"]}
    assert eligible_months(grouped, date(2026, 7, 17), 90) == ["2026-03"]


def test_eligible_months_excludes_current_and_recent_months(tmp_path: Path) -> None:
    grouped = {
        "2026-07": [tmp_path / "engine_2026-07-01.log"],
        "2026-06": [tmp_path / "engine_2026-06-01.log"],
        "2026-05": [tmp_path / "engine_2026-05-01.log"],
    }
    # cutoff=2026-04-18。5月末(2026-05-31)以降はいずれも cutoff より後なので対象外。
    assert eligible_months(grouped, date(2026, 7, 17), 90) == []


def test_eligible_months_boundary_month_end_exactly_cutoff(tmp_path: Path) -> None:
    # today=2026-06-29, retention=90 -> cutoff=2026-03-31。
    # 3月末 == cutoff は「最終日 < cutoff」を満たさないため対象外。
    grouped = {"2026-03": [tmp_path / "engine_2026-03-31.log"]}
    assert eligible_months(grouped, date(2026, 6, 29), 90) == []
    # 翌日 today=2026-06-30 -> cutoff=2026-04-01 になり対象になる。
    assert eligible_months(grouped, date(2026, 6, 30), 90) == ["2026-03"]


# --------------------------------------------------------------------------- #
#  archive_month
# --------------------------------------------------------------------------- #

def _make_month_files(log_dir: Path) -> List[Path]:
    return [
        _touch(log_dir / "engine_2026-03-01.log", "engine day1\n"),
        _touch(log_dir / "engine_2026-03-02.log", "engine day2\n"),
        _touch(log_dir / "market_snapshot_2026-03-01.csv", "snap\n"),
    ]


def test_archive_month_creates_zip_and_removes_sources(tmp_path: Path) -> None:
    log_dir = tmp_path
    archive_dir = tmp_path / "archive"
    files = _make_month_files(log_dir)

    result = archive_month(log_dir, archive_dir, "2026-03", files)

    assert result["status"] == "done"
    assert result["archived_count"] == 3
    zip_path = archive_dir / "log_2026-03.zip"
    assert zip_path.exists()
    assert not (archive_dir / "log_2026-03.zip.tmp").exists()
    with zipfile.ZipFile(zip_path, "r") as zf:
        assert set(zf.namelist()) == {f.name for f in files}
        assert zf.testzip() is None
    for f in files:
        assert not f.exists()


def test_archive_month_skips_when_zip_exists_and_no_sources_remain(tmp_path: Path) -> None:
    log_dir = tmp_path
    archive_dir = tmp_path / "archive"
    files = _make_month_files(log_dir)
    archive_month(log_dir, archive_dir, "2026-03", files)
    zip_path = archive_dir / "log_2026-03.zip"
    mtime_before = zip_path.stat().st_mtime_ns

    result = archive_month(log_dir, archive_dir, "2026-03", files)

    assert result["status"] == "skipped"
    assert result["archived_count"] == 0
    assert zip_path.stat().st_mtime_ns == mtime_before


def test_archive_month_resumes_with_remaining_files(tmp_path: Path) -> None:
    log_dir = tmp_path
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    zip_path = archive_dir / "log_2026-03.zip"

    # 前回実行で day1 のみ zip 化・削除済み、day2/snap が残っている状況を再現
    day1 = _touch(log_dir / "engine_2026-03-01.log", "engine day1\n")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(day1, arcname=day1.name)
    day1.unlink()
    day2 = _touch(log_dir / "engine_2026-03-02.log", "engine day2\n")
    snap = _touch(log_dir / "market_snapshot_2026-03-01.csv", "snap\n")

    files = [log_dir / "engine_2026-03-01.log", day2, snap]
    result = archive_month(log_dir, archive_dir, "2026-03", files)

    assert result["status"] == "resumed"
    assert result["archived_count"] == 2
    with zipfile.ZipFile(zip_path, "r") as zf:
        assert set(zf.namelist()) == {
            "engine_2026-03-01.log",
            "engine_2026-03-02.log",
            "market_snapshot_2026-03-01.csv",
        }
        assert zf.testzip() is None
    assert not day2.exists()
    assert not snap.exists()


def test_archive_month_write_failure_keeps_sources(tmp_path: Path) -> None:
    log_dir = tmp_path
    archive_dir = tmp_path / "archive"
    files = _make_month_files(log_dir)

    original_write = zipfile.ZipFile.write

    def _failing_write(self, filename, arcname=None, *args, **kwargs):
        if arcname == "engine_2026-03-02.log":
            raise OSError("simulated write failure")
        return original_write(self, filename, arcname, *args, **kwargs)

    with patch.object(zipfile.ZipFile, "write", _failing_write):
        with pytest.raises(OSError, match="simulated write failure"):
            archive_month(log_dir, archive_dir, "2026-03", files)

    for f in files:
        assert f.exists()
    assert not (archive_dir / "log_2026-03.zip").exists()
    assert not (archive_dir / "log_2026-03.zip.tmp").exists()


def test_archive_month_testzip_failure_keeps_sources(tmp_path: Path) -> None:
    log_dir = tmp_path
    archive_dir = tmp_path / "archive"
    files = _make_month_files(log_dir)

    with patch.object(zipfile.ZipFile, "testzip", lambda self: "engine_2026-03-01.log"):
        with pytest.raises(RuntimeError, match="zip corruption detected"):
            archive_month(log_dir, archive_dir, "2026-03", files)

    for f in files:
        assert f.exists()
    assert not (archive_dir / "log_2026-03.zip").exists()
    assert not (archive_dir / "log_2026-03.zip.tmp").exists()


# --------------------------------------------------------------------------- #
#  統合テスト: 追記型ファイルに一切触れないこと
# --------------------------------------------------------------------------- #

def test_main_never_touches_append_only_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_dir = tmp_path / "log"
    archive_dir = log_dir / "archive"
    log_dir.mkdir(parents=True)

    append_only_contents = {}
    for name in APPEND_ONLY_FILES:
        content = f"append-only content of {name}\n"
        _touch(log_dir / name, content)
        append_only_contents[name] = content

    old_file = _touch(log_dir / "engine_2026-01-15.log", "old engine log\n")
    recent_file = _touch(
        log_dir / f"engine_{date.today().isoformat()}.log", "recent engine log\n"
    )

    monkeypatch.setattr(aol, "LOG_DIR", log_dir)
    monkeypatch.setattr(aol, "ARCHIVE_DIR", archive_dir)

    exit_code = aol.main()
    assert exit_code == 0

    # 追記型ファイルは削除も変更もされていない
    for name, content in append_only_contents.items():
        path = log_dir / name
        assert path.exists()
        assert path.read_text(encoding="utf-8") == content

    # 90日より古い月のファイルはアーカイブされて削除されている
    assert not old_file.exists()
    zip_path = archive_dir / "log_2026-01.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path, "r") as zf:
        assert zf.namelist() == ["engine_2026-01-15.log"]

    # 直近のファイルは残っている
    assert recent_file.exists()
