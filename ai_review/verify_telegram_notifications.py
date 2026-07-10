"""One-off verification helper for AI review Telegram notifications."""
from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

logging.basicConfig(level=logging.WARNING)

ROOT = Path(__file__).resolve().parent.parent
AI_REVIEW_DIR = ROOT / "ai_review"
MODULE_DIR = ROOT / "btc_trading_tool"
for path in (AI_REVIEW_DIR, MODULE_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import build_ai_review_summary as bas  # noqa: E402
import review_pipeline as rp  # noqa: E402
import telegram_notifier as tn  # noqa: E402

BASE_SUMMARY = ROOT / "log" / "ai_review_summary_2026-07-06.json"


def prepare_summary(target_date: str) -> None:
    dst = ROOT / "log" / f"ai_review_summary_{target_date}.json"
    shutil.copy2(BASE_SUMMARY, dst)


def run_case(name: str, runner) -> tuple[bool, str]:
    try:
        ok, detail = runner()
        return ok, detail
    except Exception as exc:  # pragma: no cover - verification helper
        return False, f"{name} raised {exc!r}"


def main() -> int:
    if not BASE_SUMMARY.exists():
        print(f"[FAIL] prerequisite summary missing: {BASE_SUMMARY}")
        return 1

    sent_messages: list[str] = []

    def capture_send(text: str, timeout_sec: int = 15) -> bool:
        sent_messages.append(text)
        return tn.send_telegram_message(text, timeout_sec=timeout_sec)

    results: list[tuple[str, bool, str]] = []

    # 1) failed_before_moderator
    target = "2099-03-11"
    prepare_summary(target)
    sent_messages.clear()

    def case_failed_before_moderator() -> tuple[bool, str]:
        with patch.object(rp, "_call_proposer", side_effect=RuntimeError("simulated proposer failure")):
            with patch.object(rp, "send_telegram_message", side_effect=capture_send):
                sys.argv = ["review_pipeline.py", "--target-date", target]
                rc = rp.main()
        decision = json.loads((ROOT / "log" / f"ai_review_decision_{target}.json").read_text(encoding="utf-8"))
        ok = (
            rc == 1
            and decision["status"] == "failed_before_moderator"
            and len(sent_messages) == 1
            and "Proposer/Skeptic" in sent_messages[0]
        )
        return ok, f"exit={rc}, status={decision['status']}, messages={len(sent_messages)}"

    results.append(("failed_before_moderator", *run_case("failed_before_moderator", case_failed_before_moderator)))

    # 2) failed_moderator_call
    target = "2099-03-12"
    prepare_summary(target)
    sent_messages.clear()

    def case_failed_moderator_call() -> tuple[bool, str]:
        with patch.object(rp, "_call_moderator", side_effect=RuntimeError("simulated moderator API failure")):
            with patch.object(rp, "send_telegram_message", side_effect=capture_send):
                sys.argv = ["review_pipeline.py", "--target-date", target, "--test-mode", "invalid_json"]
                rc = rp.main()
        decision = json.loads((ROOT / "log" / f"ai_review_decision_{target}.json").read_text(encoding="utf-8"))
        ok = (
            rc == 1
            and decision["status"] == "failed_moderator_call"
            and len(sent_messages) == 1
            and "Moderator" in sent_messages[0]
        )
        return ok, f"exit={rc}, status={decision['status']}, messages={len(sent_messages)}"

    results.append(("failed_moderator_call", *run_case("failed_moderator_call", case_failed_moderator_call)))

    # 3) failed_config_write
    target = "2099-03-13"
    prepare_summary(target)
    sent_messages.clear()

    def case_failed_config_write() -> tuple[bool, str]:
        with patch.object(rp, "write_atomic_json", side_effect=OSError("simulated config write failure")):
            with patch.object(rp, "send_telegram_message", side_effect=capture_send):
                sys.argv = ["review_pipeline.py", "--target-date", target, "--test-mode", "stub_success"]
                rc = rp.main()
        decision = json.loads((ROOT / "log" / f"ai_review_decision_{target}.json").read_text(encoding="utf-8"))
        ok = (
            rc == 1
            and decision["status"] == "failed_config_write"
            and len(sent_messages) == 1
            and "エラー（重要）" in sent_messages[0]
        )
        return ok, f"exit={rc}, status={decision['status']}, messages={len(sent_messages)}"

    results.append(("failed_config_write", *run_case("failed_config_write", case_failed_config_write)))

    # 4) no_update_validation_failed
    target = "2099-03-14"
    prepare_summary(target)
    sent_messages.clear()

    def case_validation_skipped() -> tuple[bool, str]:
        with patch.object(rp, "send_telegram_message", side_effect=capture_send):
            sys.argv = ["review_pipeline.py", "--target-date", target, "--test-mode", "invalid_json"]
            rc = rp.main()
        decision = json.loads((ROOT / "log" / f"ai_review_decision_{target}.json").read_text(encoding="utf-8"))
        ok = (
            rc == 0
            and decision["status"] == "no_update_validation_failed"
            and len(sent_messages) == 1
            and "通知（見送り）" in sent_messages[0]
        )
        return ok, f"exit={rc}, status={decision['status']}, messages={len(sent_messages)}"

    results.append(("no_update_validation_failed", *run_case("no_update_validation_failed", case_validation_skipped)))

    # 5) missing summary
    target = "2099-03-15"
    sent_messages.clear()

    def case_missing_summary() -> tuple[bool, str]:
        with patch.object(rp, "send_telegram_message", side_effect=capture_send):
            sys.argv = ["review_pipeline.py", "--target-date", target]
            rc = rp.main()
        decision = json.loads((ROOT / "log" / f"ai_review_decision_{target}.json").read_text(encoding="utf-8"))
        ok = rc == 1 and decision["status"] == "missing_summary_file" and len(sent_messages) == 1
        return ok, f"exit={rc}, status={decision['status']}, messages={len(sent_messages)}"

    results.append(("missing_summary_file", *run_case("missing_summary_file", case_missing_summary)))

    # 6) build summary config error
    sent_messages.clear()

    def case_build_summary_config_error() -> tuple[bool, str]:
        with patch.object(bas, "send_telegram_message", side_effect=capture_send):
            bas.CONFIG_PATH = ROOT / "config" / "missing_config_for_verify.json"
            rc = bas.main()
        ok = rc == 1 and len(sent_messages) == 1 and "config/config.json" in sent_messages[0]
        return ok, f"exit={rc}, messages={len(sent_messages)}"

    results.append(("build_summary_config_error", *run_case("build_summary_config_error", case_build_summary_config_error)))

    # 7) unconfigured non-blocking
    target = "2099-03-16"
    sent_messages.clear()

    def case_unconfigured() -> tuple[bool, str]:
        with patch.object(tn, "_get_telegram_config", return_value=("", "")):
            with patch.object(rp, "send_telegram_message", side_effect=tn.send_telegram_message):
                sys.argv = ["review_pipeline.py", "--target-date", target]
                rc = rp.main()
        ok = rc == 1
        return ok, f"exit={rc}"

    results.append(("unconfigured_non_blocking", *run_case("unconfigured_non_blocking", case_unconfigured)))

    print("=== verification summary ===")
    all_ok = True
    for name, ok, detail in results:
        mark = "OK" if ok else "FAIL"
        print(f"[{mark}] {name}: {detail}")
        all_ok = all_ok and ok

    if all_ok:
        print("ALL CHECKS PASSED")
        return 0
    print("SOME CHECKS FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
