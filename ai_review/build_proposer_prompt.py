"""build_proposer_prompt.py

提案役（Proposer）プロンプトのファイル化。

夜間AIレビューで Groq に渡していたのと同一のプロンプト
（prompts.build_proposer_prompt の生成結果）を
log/ai_review_proposer_prompt_<target_date>.json へ書き出す。
提案役を外部LLM以外（じじ）が担う運用のための受け渡しファイルで、
中身は {target_date, generated_at, system, prompt}。

使い方:
    python ai_review/build_proposer_prompt.py [--target-date YYYY-MM-DD]
対象日を省略した場合は前日。
サマリー（log/ai_review_summary_<target_date>.json）が無い場合は exit 1。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AI_REVIEW_DIR = PROJECT_ROOT / "ai_review"
if str(AI_REVIEW_DIR) not in sys.path:
    sys.path.insert(0, str(AI_REVIEW_DIR))

from prompts import build_proposer_prompt  # noqa: E402

LOG_DIR = PROJECT_ROOT / "log"


def resolve_target_date(raw: Optional[str]) -> str:
    if not raw:
        return (datetime.now().date() - timedelta(days=1)).isoformat()
    datetime.strptime(raw, "%Y-%m-%d")
    return raw


def build_prompt_document(summary: Dict[str, Any], target_date: str) -> Dict[str, Any]:
    system, prompt = build_proposer_prompt(summary)
    return {
        "target_date": target_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "system": system,
        "prompt": prompt,
    }


def write_prompt_file(summary: Dict[str, Any], target_date: str, out_path: Path) -> Path:
    doc = build_prompt_document(summary, target_date)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f"{out_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)
    return out_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build proposer prompt file for AI review")
    parser.add_argument("--target-date", help="対象日 YYYY-MM-DD。省略時は前日")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        target_date = resolve_target_date(args.target_date)
    except ValueError as exc:
        print(f"[ERROR] invalid --target-date: {exc}", file=sys.stderr)
        return 1
    summary_path = LOG_DIR / f"ai_review_summary_{target_date}.json"
    if not summary_path.exists():
        print(f"[ERROR] summary file not found: {summary_path}", file=sys.stderr)
        return 1
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        out_path = LOG_DIR / f"ai_review_proposer_prompt_{target_date}.json"
        write_prompt_file(summary, target_date, out_path)
    except Exception as exc:
        print(f"[ERROR] failed to build proposer prompt: {exc}", file=sys.stderr)
        return 1
    print(f"[INFO] proposer prompt written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
