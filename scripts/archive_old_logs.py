"""Archive dated log files older than the retention period into monthly zip files.

log/ 直下の「ファイル名に YYYY-MM-DD を含む」ログのみを対象に、
90日より古い「まるまる1ヶ月分」を月単位の zip (log/archive/log_YYYY-MM.zip) へ
まとめ、破損チェック (testzip) 通過を確認した後にのみ元ファイルを削除する。

config_history.jsonl / update_log.jsonl / ai_validation_failures.jsonl のような
日付を含まない追記型ファイルには一切触れない。
ロック機構は PS1 ラッパー (run_archive_old_logs.ps1) 側でのみ管理する。
"""
from __future__ import annotations

import calendar
import logging
import os
import re
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT_DIR / "log"
ARCHIVE_DIR = LOG_DIR / "archive"
RETENTION_DAYS = 90

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")

LOGGER = logging.getLogger("archive_old_logs")


def find_dated_log_files(log_dir: Path) -> Dict[str, List[Path]]:
    """
    log_dir 直下（サブフォルダ除く）のファイルのうち、ファイル名に
    YYYY-MM-DD 形式の日付を含むものを {"YYYY-MM": [Path, ...]} でグルーピングする。

    - 日付を含まないファイル（config_history.jsonl 等）は対象外
    - log_dir/archive/ サブフォルダ自体とその中身は対象外
      （iterdir はサブフォルダの中身を辿らず、ディレクトリは is_file で除外される）
    """
    grouped: Dict[str, List[Path]] = {}
    if not log_dir.exists():
        return grouped
    for path in sorted(log_dir.iterdir()):
        if not path.is_file():
            continue
        match = _DATE_PATTERN.search(path.name)
        if match is None:
            continue
        date_text = match.group(0)
        try:
            parsed = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError:
            # 正規表現には一致するが実在しない日付 (例: 2026-99-99) は対象外
            continue
        year_month = f"{parsed.year:04d}-{parsed.month:02d}"
        grouped.setdefault(year_month, []).append(path)
    return grouped


def eligible_months(
    grouped: Dict[str, List[Path]],
    today: date,
    retention_days: int = RETENTION_DAYS,
) -> List[str]:
    """
    月の最終日が (today - retention_days日) より前の年月のみを返す。
    月の途中経過分は「まるまる1ヶ月分」ルールにより対象外。
    """
    cutoff = today - timedelta(days=retention_days)
    result: List[str] = []
    for year_month in sorted(grouped.keys()):
        year_text, month_text = year_month.split("-", 1)
        year = int(year_text)
        month = int(month_text)
        last_day = calendar.monthrange(year, month)[1]
        month_end = date(year, month, last_day)
        if month_end < cutoff:
            result.append(year_month)
    return result


def _verify_zip(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        bad_name = zf.testzip()
        if bad_name is not None:
            raise RuntimeError(f"zip corruption detected: {zip_path} bad_member={bad_name}")


def archive_month(
    log_dir: Path,
    archive_dir: Path,
    year_month: str,
    files: List[Path],
) -> Dict[str, Any]:
    """
    1ヶ月分のログファイルを log_{year_month}.zip へまとめる。

    元ファイルの削除は、zip 書き込み完了と testzip() による破損チェック通過を
    確認した後にのみ行う。この順序は絶対に変更しないこと。
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    zip_path = archive_dir / f"log_{year_month}.zip"
    remaining = [f for f in files if f.exists()]

    if zip_path.exists():
        if not remaining:
            LOGGER.info("month=%s already archived; nothing to do", year_month)
            return {"year_month": year_month, "archived_count": 0, "status": "skipped"}

        # 前回実行が途中で中断したケース: 既存 zip に未収録分のみ追記する
        with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as zf:
            existing_names = set(zf.namelist())
            for path in remaining:
                if path.name not in existing_names:
                    zf.write(path, arcname=path.name)
        _verify_zip(zip_path)
        for path in remaining:
            path.unlink()
        LOGGER.info(
            "month=%s resumed archive; remaining files archived and removed count=%d",
            year_month,
            len(remaining),
        )
        return {
            "year_month": year_month,
            "archived_count": len(remaining),
            "status": "resumed",
        }

    tmp_path = archive_dir / f"log_{year_month}.zip.tmp"
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in remaining:
                zf.write(path, arcname=path.name)
        _verify_zip(tmp_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    os.replace(tmp_path, zip_path)
    if not zip_path.exists():
        raise RuntimeError(f"zip file missing after rename: {zip_path}")

    for path in remaining:
        path.unlink()
    LOGGER.info(
        "month=%s archived count=%d zip=%s",
        year_month,
        len(remaining),
        zip_path,
    )
    return {
        "year_month": year_month,
        "archived_count": len(remaining),
        "status": "done",
    }


def _setup_logging(log_path: Path) -> logging.FileHandler:
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [archive_old_logs] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    return handler


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"archive_old_logs_{date.today().isoformat()}.log"
    handler = _setup_logging(log_path)
    try:
        grouped = find_dated_log_files(LOG_DIR)
        months = eligible_months(grouped, date.today(), RETENTION_DAYS)
        if not months:
            LOGGER.info(
                "No months eligible for archiving (retention_days=%d).",
                RETENTION_DAYS,
            )
            return 0
        for year_month in months:
            result = archive_month(LOG_DIR, ARCHIVE_DIR, year_month, grouped[year_month])
            LOGGER.info(
                "month=%s status=%s archived_count=%d",
                result["year_month"],
                result["status"],
                result["archived_count"],
            )
        return 0
    except Exception as exc:
        LOGGER.error("archive_old_logs failed: %s", exc)
        return 1
    finally:
        LOGGER.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
