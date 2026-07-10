#!/usr/bin/env python3
"""
verify_maintenance_entry_block.py

目的:
  GMOコインのメンテナンス時間帯(毎日05:55-06:30、毎週土曜09:00-11:00)中に
  新規エントリー(reason=ENTRY)が発生していないかを、実際の取引ログから機械的に検証する。

  _is_entry_blocked() / _update_maintenance_state() が正しく機能していれば、
  このウィンドウ内に reason=ENTRY のレコードは一件も存在しないはず。
  1件でも検出された場合は、再起動直後のメンテナンス判定に不具合がある可能性が高い。

使い方:
  python verify_maintenance_entry_block.py --log-dir log/
  python verify_maintenance_entry_block.py --log-dir log/ --pattern "realtime_trading_log_2026-07-*.csv"

終了コード:
  0 = 違反なし
  1 = 違反あり
  2 = ログファイルが見つからない等のエラー
"""

import argparse
import csv
import glob
import os
import sys
from datetime import datetime, time
from typing import Optional

# --- メンテナンス時間帯の定義 (仕様書 3.2節に基づく) ---
DAILY_MAINTENANCE_START = time(5, 55)
DAILY_MAINTENANCE_END = time(6, 30)

WEEKLY_MAINTENANCE_WEEKDAY = 5  # 土曜 (Monday=0 ... Saturday=5)
WEEKLY_MAINTENANCE_START = time(9, 0)
WEEKLY_MAINTENANCE_END = time(11, 0)

# ログの列位置 (仕様書3.3節 + 実データ観測に基づく。先頭からのインデックス。ヘッダー行なし前提)
COL_TIMESTAMP = 0
COL_ORDER_ID = 1
COL_SIDE = 2
COL_ORDER_TYPE = 3
COL_REASON = 4
COL_PRICE = 5

# メンテナンス時間帯に出てはいけないreason
ENTRY_REASONS = {"ENTRY"}


def is_in_maintenance_window(ts: datetime) -> Optional[str]:
    """タイムスタンプがメンテナンス時間帯内かどうかを判定し、種別を返す。"""
    t = ts.time()
    if DAILY_MAINTENANCE_START <= t <= DAILY_MAINTENANCE_END:
        return "daily"
    if ts.weekday() == WEEKLY_MAINTENANCE_WEEKDAY and WEEKLY_MAINTENANCE_START <= t <= WEEKLY_MAINTENANCE_END:
        return "weekly"
    return None


def parse_log_file(path: str):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for line_num, row in enumerate(reader, start=1):
            if len(row) <= COL_PRICE:
                continue
            try:
                ts = datetime.strptime(row[COL_TIMESTAMP].strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue  # ヘッダー行やフォーマット崩れの行はスキップ
            rows.append((line_num, ts, row))
    return rows


def main():
    parser = argparse.ArgumentParser(description="メンテナンス時間帯中の新規エントリーブロックが機能しているか検証")
    parser.add_argument("--log-dir", default="log", help="ログファイルが格納されているディレクトリ (default: log)")
    parser.add_argument("--pattern", default="realtime_trading_log_*.csv", help="対象ファイルのglobパターン")
    args = parser.parse_args()

    files = sorted(glob.glob(os.path.join(args.log_dir, args.pattern)))

    if not files:
        print(f"[ERROR] ログファイルが見つかりません: {os.path.join(args.log_dir, args.pattern)}")
        sys.exit(2)

    violations = []
    total_checked = 0

    for path in files:
        for line_num, ts, row in parse_log_file(path):
            window = is_in_maintenance_window(ts)
            if window is None:
                continue
            total_checked += 1
            reason = row[COL_REASON].strip()
            if reason in ENTRY_REASONS:
                violations.append({
                    "file": os.path.basename(path),
                    "line": line_num,
                    "timestamp": ts,
                    "window": window,
                    "side": row[COL_SIDE],
                    "reason": reason,
                    "price": row[COL_PRICE],
                    "order_id": row[COL_ORDER_ID],
                })

    print("=" * 70)
    print("メンテナンス時間帯 新規エントリーブロック 検証結果")
    print("=" * 70)
    print(f"対象ファイル数: {len(files)}")
    print(f"メンテナンス時間帯内のレコード総数: {total_checked}")
    print(f"違反件数 (ENTRY発生): {len(violations)}")
    print()

    if violations:
        by_date = {}
        for v in violations:
            by_date.setdefault(v["timestamp"].date(), []).append(v)

        print("--- 違反詳細 ---")
        for d in sorted(by_date):
            vs = by_date[d]
            print(f"\n{d} ({len(vs)}件):")
            for v in vs:
                print(
                    f"  {v['timestamp'].strftime('%H:%M:%S')}  [{v['window']:6s}]  "
                    f"{v['side']:4s} {v['reason']:6s}  price={v['price']:>12s}  "
                    f"order_id={v['order_id']}  ({v['file']}:{v['line']})"
                )

        print()
        print(f"[NG] {len(by_date)}日分、計{len(violations)}件のメンテナンス中エントリーを検出しました。")
        print("      _is_entry_blocked() / _update_maintenance_state() の再起動直後の挙動を確認してください。")
        sys.exit(1)
    else:
        print("[OK] メンテナンス時間帯中のENTRYは検出されませんでした。")
        sys.exit(0)


if __name__ == "__main__":
    main()
