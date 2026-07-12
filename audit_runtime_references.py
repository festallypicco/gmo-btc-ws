"""
audit_runtime_references.py

目的（案B実装の下調べ専用スクリプト。コードは一切変更しない）:
  Docker化・案B（runtime/フォルダ新設）を実装する前に、プロジェクト全体から
  以下3つの実行時状態ファイルへの参照箇所を機械的に洗い出す。
    - manual_stop.flag   （緊急停止フラグ）
    - live_state.db      （エンジンの最新状態DB）
    - trading_engine.pid （PIDファイル）

  仕様書 4.5節「次回セッション開始時のチェックリスト」の 1. に対応する。
  このスクリプトは grep 相当の検索と結果のレポート化のみを行い、
  ファイルの書き換えは一切行わない。

実行方法:
  プロジェクトルート（gmo-btc-ws/）で実行する。
    python audit_runtime_references.py

  対象ディレクトリを明示したい場合:
    python audit_runtime_references.py --root C:\path\to\gmo-btc-ws

出力:
  - コンソールにサマリーを表示
  - runtime_reference_audit_<YYYYMMDD_HHMMSS>.md をプロジェクトルート直下に生成
    （このファイル自体もGit管理対象外にする想定。必要なら.gitignoreへの追加は
    別途手動で検討する。本スクリプトは.gitignoreの変更も行わない）
"""

import argparse
import re
from datetime import datetime
from pathlib import Path

# ── 検索対象キーワード ──────────────────────────────────────────────
# 各グループ: 表示名 -> 正規表現パターンのリスト
# リテラルなファイル名文字列だけでなく、定数名・変数名らしきパターンも含める。
# ただし "pid" のような一般語は誤検出が多いため、trading_engine系の文脈に
# 限定した形のみを対象とする。
KEYWORD_GROUPS = {
    "manual_stop.flag": [
        r"manual_stop\.flag",
        r"manual_stop_flag",
        r"MANUAL_STOP_FLAG",
        r"ManualStopFlag",
    ],
    "live_state.db": [
        r"live_state\.db",
        r"live_state_db",
        r"LIVE_STATE_DB",
        r"LiveStateDb",
    ],
    "trading_engine.pid": [
        r"trading_engine\.pid",
        r"trading_engine_pid",
        r"ENGINE_PID_FILE",
        r"TRADING_ENGINE_PID",
        r"PID_FILE",
    ],
}

# ── 走査対象から除外するディレクトリ ────────────────────────────────
EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    "log", "docker_test", "htmlcov", ".pytest_cache",
    "dist", "build", ".mypy_cache", ".ruff_cache",
}

# ── 走査対象とする拡張子・ファイル名 ────────────────────────────────
INCLUDE_EXTENSIONS = {
    ".py", ".ps1", ".json", ".md", ".yml", ".yaml",
    ".bat", ".txt", ".cfg", ".ini", ".dockerignore", ".env",
}
INCLUDE_EXACT_NAMES = {
    "Dockerfile", ".gitignore", ".dockerignore", ".env.example",
}

# 仕様書4.5節「叩き台」に記載済みの既知参照ファイル（相対パス、区切りは/で統一）
KNOWN_FILES = {
    "trading_engine.py",
    "btc_trading_tool/virtual_trader.py",
    "btc_trading_tool/dashboard.py",
    "scripts/check_live_state.py",
    "scripts/reset_trading_state.py",
    "scripts/ensure_engine_running.ps1",
    "scripts/restart_engine.ps1",
    "scripts/detect_orphan_engines.ps1",
    "scripts/engine_process_utils.ps1",
}


def iter_target_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        if path.name in INCLUDE_EXACT_NAMES:
            yield path
            continue
        if path.suffix in INCLUDE_EXTENSIONS:
            yield path


def read_lines(path: Path):
    for enc in ("utf-8", "utf-8-sig", "cp932"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.readlines()
        except (UnicodeDecodeError, LookupError):
            continue
    return None  # バイナリ扱いでスキップ


def scan(root: Path):
    """戻り値: {keyword_group: {rel_path: [(line_no, line_text, matched_pattern), ...]}}"""
    compiled = {
        group: [re.compile(p) for p in patterns]
        for group, patterns in KEYWORD_GROUPS.items()
    }
    results = {group: {} for group in KEYWORD_GROUPS}

    for path in iter_target_files(root):
        # 自分自身の出力レポートは対象外
        if path.name.startswith("runtime_reference_audit_"):
            continue
        lines = read_lines(path)
        if lines is None:
            continue
        rel = path.relative_to(root).as_posix()

        for group, patterns in compiled.items():
            hits = []
            for line_no, line in enumerate(lines, start=1):
                for pat in patterns:
                    if pat.search(line):
                        hits.append((line_no, line.rstrip("\n").strip(), pat.pattern))
                        break
            if hits:
                results[group][rel] = hits

    return results


def build_report(root: Path, results: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append("# runtime系ファイル参照箇所 洗い出しレポート")
    lines.append("")
    lines.append(f"生成日時: {now}")
    lines.append(f"走査対象ルート: {root}")
    lines.append("")
    lines.append(
        "本レポートは案B（`runtime/`フォルダ新設）の実装前調査のために自動生成された"
        "ものであり、コードの変更は一切行っていない。"
    )
    lines.append("")

    # サマリー
    lines.append("## サマリー")
    lines.append("")
    lines.append("| キーワード | ヒットファイル数 | ヒット箇所合計 |")
    lines.append("|---|---|---|")
    all_found_files = set()
    for group, per_file in results.items():
        total_hits = sum(len(v) for v in per_file.values())
        lines.append(f"| `{group}` | {len(per_file)} | {total_hits} |")
        all_found_files.update(per_file.keys())
    lines.append("")

    # 既知ファイルとの突き合わせ
    lines.append("## 仕様書記載の既知参照ファイルとの突き合わせ")
    lines.append("")
    missing = sorted(KNOWN_FILES - all_found_files)
    new_files = sorted(all_found_files - KNOWN_FILES)

    if missing:
        lines.append(
            "既知ファイルだが、今回のキーワード検索ではヒットしなかったもの"
            "（間接参照・別名変数経由の可能性があるため手動確認を推奨）:"
        )
        for f in missing:
            lines.append(f"- `{f}`")
    else:
        lines.append("既知ファイルは全てヒットした。")
    lines.append("")

    if new_files:
        lines.append("仕様書には未記載だが、今回新たにヒットしたファイル（要確認）:")
        for f in new_files:
            lines.append(f"- `{f}`")
    else:
        lines.append("仕様書記載範囲外の新規ヒットファイルはなかった。")
    lines.append("")

    # 詳細
    lines.append("## 詳細（キーワード別）")
    for group, per_file in results.items():
        lines.append("")
        lines.append(f"### `{group}`")
        if not per_file:
            lines.append("")
            lines.append("(ヒットなし)")
            continue
        for rel, hits in sorted(per_file.items()):
            lines.append("")
            lines.append(f"**`{rel}`**")
            lines.append("")
            lines.append("| 行番号 | 内容 | マッチしたパターン |")
            lines.append("|---|---|---|")
            for line_no, text, pattern in hits:
                # Markdownテーブルを壊す文字を軽くエスケープ
                safe_text = text.replace("|", "\\|")
                if len(safe_text) > 160:
                    safe_text = safe_text[:157] + "..."
                lines.append(f"| {line_no} | `{safe_text}` | `{pattern}` |")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "## 次のアクション（このレポート自体はまだ何も変更しない）"
    )
    lines.append("")
    lines.append(
        "1. 上記「新たにヒットしたファイル」「ヒットしなかった既知ファイル」を人手で確認する"
    )
    lines.append(
        "2. 確認済みの全参照箇所リストをもとに、`runtime/`フォルダ新設とパス定数の"
        "一括修正をCursorへ指示する（この修正指示は本レポートとは別タスクとする）"
    )
    lines.append(
        "3. 修正後、`docker-compose.yml`を`config/` `log/` `runtime/`の3つのみ"
        "マウントする構成に書き換える"
    )
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="案B下調べ用: manual_stop.flag/live_state.db/trading_engine.pid の参照箇所を洗い出す"
    )
    parser.add_argument(
        "--root",
        type=str,
        default=".",
        help="プロジェクトルートのパス（デフォルト: カレントディレクトリ）",
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()

    if not root.exists():
        print(f"エラー: 指定されたルートが存在しません: {root}")
        return

    print(f"走査対象ルート: {root}")
    print("走査中...")
    results = scan(root)

    report = build_report(root, results)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = root / f"runtime_reference_audit_{ts}.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)

    print()
    print("=== サマリー ===")
    for group, per_file in results.items():
        total_hits = sum(len(v) for v in per_file.values())
        print(f"{group}: {len(per_file)}ファイル / {total_hits}箇所")
    print()
    print(f"詳細レポートを生成しました: {out_path}")


if __name__ == "__main__":
    main()
