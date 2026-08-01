from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODULE_DIR = PROJECT_ROOT / "btc_trading_tool"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

AI_REVIEW_DIR = PROJECT_ROOT / "ai_review"
if str(AI_REVIEW_DIR) not in sys.path:
    sys.path.insert(0, str(AI_REVIEW_DIR))

from config_manager import (  # noqa: E402
    PARAMETER_ABSOLUTE_BOUNDS,
    ConfigValidationError,
    PARAMETER_MIN_STEP,
    build_profile_definitions,
)
from llm_clients import call_gemini, call_groq, classify_llm_error_kind  # noqa: E402
from profile_config import validate_profiles  # noqa: E402
from prompts import (  # noqa: E402
    build_moderator_prompt,
    build_proposer_prompt,
    build_skeptic_prompt,
)
from telegram_notifier import send_telegram_message  # noqa: E402
from backtest_verifier import run_backtest_check  # noqa: E402

LOG_DIR = PROJECT_ROOT / "log"
CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"
VALIDATION_FAILURE_PATH = LOG_DIR / "ai_validation_failures.jsonl"
UPDATE_LOG_PATH = LOG_DIR / "update_log.jsonl"

MAX_MODERATOR_ATTEMPTS = 3
CHANGE_LIMIT_RATIO = 0.15
OUTLIER_ZSCORE_THRESHOLD = 2.0
ROLLING_STATS_DAYS = 14
OUTLIER_DIVERGENCE_DAYS = 3
OUTLIER_DIVERGENCE_THRESHOLD = 0.20
ROLLOUT_TOTAL_DAYS = 3
ROLLOUT_RATIOS = [0.3, 0.6, 1.0]


def _today_iso() -> str:
    return datetime.now().date().isoformat()


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _parse_param_path(param_path: str) -> Tuple[str, str]:
    """
    param_path: "profiles.<profile_name>.<param_key>" のみ対応。
    戻り値: (profile_name, param_key)
    """
    parts = str(param_path).split(".")
    if len(parts) != 3 or parts[0] != "profiles" or not parts[1] or not parts[2]:
        raise ValueError(f"unsupported param_path: {param_path}")
    return parts[1], parts[2]


def _get_profile_value(config: Dict[str, Any], profile_name: str, param_key: str) -> Optional[float]:
    profiles = config.get("profiles", [])
    if not isinstance(profiles, list):
        return None
    for p in profiles:
        if not isinstance(p, dict):
            continue
        if p.get("name") != profile_name:
            continue
        val = p.get(param_key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
        if val is None:
            return None
        return None
    return None


def _set_profile_value(config: Dict[str, Any], profile_name: str, param_key: str, value: float) -> Dict[str, Any]:
    new_payload = dict(config)
    profiles = new_payload.get("profiles", [])
    if not isinstance(profiles, list):
        raise ValueError("profiles must be a list")
    new_profiles: List[Dict[str, Any]] = []
    found = False
    for p in profiles:
        if not isinstance(p, dict):
            continue
        cp = dict(p)
        if cp.get("name") == profile_name:
            cp[param_key] = float(value)
            found = True
        new_profiles.append(cp)
    if not found:
        raise ValueError(f"profile not found: {profile_name}")
    new_payload["profiles"] = new_profiles
    return new_payload


def _apply_single_param_update(
    current_config: Dict[str, Any],
    param_path: str,
    applied_value: float,
    updated_reason: str,
) -> Tuple[Dict[str, Any], List[str], List[str], List[str]]:
    """
    safe_update_config の既存ロジック（±15%・絶対範囲・daily_target特例）を再利用し、
    1パラメータのみを書き込むための薄いラッパー。
    """
    profile_name, param_key = _parse_param_path(param_path)
    candidate_payload = _set_profile_value(current_config, profile_name, param_key, applied_value)
    moderator_payload = {
        "updated_reason": updated_reason,
        "profiles": candidate_payload.get("profiles", []),
    }
    normalized_profiles = normalize_profiles_from_candidate(moderator_payload)
    return safe_update_config(
        current_config=current_config,
        moderator_payload=moderator_payload,
        normalized_profiles=normalized_profiles,
    )


def calc_rolling_stats(param_path: str) -> Dict[str, Any]:
    """
    update_log.jsonl から直近14日分の該当パラメータの適用実績を集計する。
    集計対象は「configへ反映された実績値（value_before/value_after）」の変化幅。
    """
    cutoff_dt = datetime.now() - timedelta(days=ROLLING_STATS_DAYS)
    change_pcts: List[float] = []
    directions: List[str] = []
    values_last_3d: List[Tuple[date, float]] = []

    for row in _iter_jsonl(UPDATE_LOG_PATH):
        if row.get("param_path") != param_path:
            continue
        applied_at = row.get("applied_at")
        if isinstance(applied_at, str):
            try:
                applied_dt = datetime.fromisoformat(applied_at)
            except ValueError:
                continue
        else:
            continue
        if applied_dt < cutoff_dt:
            continue

        vb = row.get("value_before")
        va = row.get("value_after")
        if not (_is_number(vb) and _is_number(va)):
            continue
        vb_f = float(vb)
        va_f = float(va)
        if vb_f == 0.0:
            continue
        delta_pct = abs((va_f - vb_f) / vb_f)
        change_pcts.append(delta_pct)
        if va_f > vb_f:
            directions.append("up")
        elif va_f < vb_f:
            directions.append("down")

        if applied_dt.date() >= (datetime.now().date() - timedelta(days=OUTLIER_DIVERGENCE_DAYS)):
            values_last_3d.append((applied_dt.date(), va_f))

    n = len(change_pcts)
    if n == 0:
        mean = 0.0
        stdev = 0.0
    elif n == 1:
        mean = float(change_pcts[0])
        stdev = 0.0
    else:
        mean = float(statistics.mean(change_pcts))
        stdev = float(statistics.pstdev(change_pcts))

    last_direction = directions[-1] if directions else "none"

    divergence_pct = 0.0
    if values_last_3d:
        vals = [v for _d, v in values_last_3d]
        lo = min(vals)
        hi = max(vals)
        if lo > 0:
            divergence_pct = (hi - lo) / lo
        else:
            divergence_pct = float("inf") if hi != lo else 0.0

    return {
        "param_path": param_path,
        "mean_change_pct": mean,
        "stdev_change_pct": stdev,
        "last_direction": last_direction,
        "n": n,
        "divergence_3d_pct": divergence_pct,
    }


def judge_outlier(param_path: str, current_value: float, proposed_value: float) -> Dict[str, Any]:
    stats = calc_rolling_stats(param_path)
    n = int(stats.get("n", 0))
    mean = float(stats.get("mean_change_pct", 0.0))
    stdev = float(stats.get("stdev_change_pct", 0.0))
    last_direction = str(stats.get("last_direction", "none"))
    divergence_3d_pct = float(stats.get("divergence_3d_pct", 0.0))

    reasons: List[str] = []

    if current_value == 0.0:
        delta_pct = float("inf")
        direction = "up" if proposed_value > current_value else "down" if proposed_value < current_value else "flat"
    else:
        delta_pct = abs((proposed_value - current_value) / current_value)
        direction = "up" if proposed_value > current_value else "down" if proposed_value < current_value else "flat"

    if stdev == 0.0:
        z = float("inf") if delta_pct > mean else 0.0
    else:
        z = (delta_pct - mean) / stdev

    if abs(z) > OUTLIER_ZSCORE_THRESHOLD:
        reasons.append(f"zscore={z:.2f}")
    if n < 5:
        reasons.append(f"insufficient_data n={n}")
    if last_direction in {"up", "down"} and direction in {"up", "down"} and last_direction != direction:
        reasons.append(f"reverse_direction last={last_direction} now={direction}")
    if divergence_3d_pct > OUTLIER_DIVERGENCE_THRESHOLD:
        reasons.append(f"divergence_3d={divergence_3d_pct:.1%}")

    is_outlier = bool(reasons)
    return {
        "param_path": param_path,
        "is_outlier": is_outlier,
        "zscore": z,
        "n": n,
        "direction": direction,
        "divergence_3d_pct": divergence_3d_pct,
        "reason": "; ".join(reasons) if reasons else "",
    }


def create_or_replace_pending_rollout(
    param_path: str,
    current_value: float,
    target_value: float,
    reason: str,
) -> Dict[str, Any]:
    try:
        _profile_name, param_key = _parse_param_path(param_path)
    except ValueError as exc:
        return {"action": "apply_full", "skip_reason": str(exc)}

    min_step = float(PARAMETER_MIN_STEP.get(param_key, 0.0))
    if abs(target_value - current_value) < min_step:
        return {
            "action": "apply_full",
            "skip_reason": f"delta<{min_step}",
            "min_step": min_step,
        }

    direction = "up" if target_value > current_value else "down" if target_value < current_value else "flat"
    current_applied_value = current_value + (target_value - current_value) * ROLLOUT_RATIOS[0]
    entry = {
        "start_value": float(current_value),
        "target_value": float(target_value),
        "current_applied_value": float(current_applied_value),
        "start_date": _today_iso(),
        "day_index": 1,
        "total_days": ROLLOUT_TOTAL_DAYS,
        "rollout_ratios": list(ROLLOUT_RATIOS),
        "direction": direction,
        "reason": str(reason),
        "status": "in_progress",
    }
    return {
        "action": "rollout",
        "param_path": param_path,
        "entry": entry,
    }


def apply_daily_rollouts() -> None:
    if not CONFIG_PATH.exists():
        return
    config = read_json(CONFIG_PATH)
    pending = config.get("pending_rollouts", {})
    if not isinstance(pending, dict) or not pending:
        return

    updated_pending: Dict[str, Any] = dict(pending)
    changed: List[Tuple[str, float, float]] = []

    for param_path, entry in list(pending.items()):
        if not isinstance(entry, dict):
            updated_pending.pop(param_path, None)
            continue
        try:
            start_value = float(entry["start_value"])
            target_value = float(entry["target_value"])
            day_index = int(entry.get("day_index", 0)) + 1
            total_days = int(entry.get("total_days", ROLLOUT_TOTAL_DAYS))
            ratios = entry.get("rollout_ratios", ROLLOUT_RATIOS)
            if not isinstance(ratios, list) or len(ratios) < total_days:
                ratios = list(ROLLOUT_RATIOS)
        except Exception:
            updated_pending.pop(param_path, None)
            continue

        if day_index >= total_days:
            applied_value = float(target_value)
        else:
            ratio = float(ratios[day_index - 1])
            applied_value = float(start_value + (target_value - start_value) * ratio)

        updated_reason = f"pending_rollout day={day_index}/{total_days} {param_path}"
        current_before = _get_profile_value(config, *_parse_param_path(param_path))
        if current_before is None:
            updated_pending.pop(param_path, None)
            continue

        new_payload, reverted, _clamped, _rejected = _apply_single_param_update(
            current_config=config,
            param_path=param_path,
            applied_value=applied_value,
            updated_reason=updated_reason,
        )
        write_atomic_json(CONFIG_PATH, new_payload)
        config = new_payload

        profile_name, param_key = _parse_param_path(param_path)
        actual_after = _get_profile_value(config, profile_name, param_key)
        if actual_after is None:
            actual_after = float(current_before)
        changed.append((param_path, float(current_before), float(actual_after)))

        reverted_param_paths = {f"profiles.{item}" for item in reverted}
        if param_path in reverted_param_paths:
            next_entry = dict(entry)
            next_entry["status"] = "reverted"
            next_entry["current_applied_value"] = float(actual_after)
            updated_pending[param_path] = next_entry
            continue

        if day_index >= total_days:
            updated_pending.pop(param_path, None)
            continue

        next_entry = dict(entry)
        next_entry["day_index"] = day_index
        next_entry["current_applied_value"] = float(actual_after)
        updated_pending[param_path] = next_entry

    final_payload = dict(config)
    final_payload["pending_rollouts"] = updated_pending
    write_atomic_json(CONFIG_PATH, final_payload)

    for param_path, before, after in changed:
        applied_delta_pct = 0.0 if before == 0.0 else abs((after - before) / before)
        append_jsonl(
            UPDATE_LOG_PATH,
            {
                "applied_at": datetime.now().isoformat(timespec="seconds"),
                "param_path": param_path,
                "value_before": before,
                "value_after": after,
                "applied_delta_pct": applied_delta_pct,
                "source": "pending_rollout",
            },
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI review discussion pipeline")
    parser.add_argument("--target-date", help="対象日 YYYY-MM-DD。省略時は前日")
    parser.add_argument(
        "--test-mode",
        choices=["live", "invalid_json", "extreme_change", "stub_success", "name_mismatch"],
        default="live",
        help="検証用モード。通常運用は live",
    )
    return parser.parse_args()


def resolve_target_date(raw: Optional[str]) -> str:
    if not raw:
        return (datetime.now().date() - timedelta(days=1)).isoformat()
    datetime.strptime(raw, "%Y-%m-%d")
    return raw


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def minute_to_hhmm(minute: int) -> str:
    if minute == 1440:
        return "24:00"
    h, m = divmod(minute, 60)
    return f"{h:02d}:{m:02d}"


def normalize_profiles_from_candidate(candidate_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    profiles = build_profile_definitions(candidate_payload)
    validation_error = validate_profiles(profiles)
    if validation_error is not None:
        raise ValueError(validation_error)

    normalized: List[Dict[str, Any]] = []
    for p in sorted(profiles, key=lambda x: x.start_minute):
        row = {
            "name": p.name,
            "start_time": minute_to_hhmm(p.start_minute),
            "end_time": minute_to_hhmm(p.end_minute),
        }
        row.update(asdict(p.config))
        normalized.append(row)
    return normalized


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _apply_daily_target_order_size_validation(
    current_payload: Dict[str, Any],
    adjusted_profiles: List[Dict[str, Any]],
    source_profiles: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    bounds = PARAMETER_ABSOLUTE_BOUNDS["daily_target_order_size_btc"]
    lower, upper = bounds
    current_map = {
        p.get("name"): p
        for p in current_payload.get("profiles", [])
        if isinstance(p, dict) and p.get("name")
    }
    source_map = {
        p.get("name"): p
        for p in source_profiles
        if isinstance(p, dict) and p.get("name")
    }
    rejected_reasons: List[str] = []
    validated: List[Dict[str, Any]] = []
    for p in adjusted_profiles:
        candidate = dict(p)
        name = candidate.get("name")
        source = source_map.get(name, {})
        current_value = None
        current_reasoning = None
        current_profile = current_map.get(name)
        if isinstance(current_profile, dict):
            current_value = current_profile.get("daily_target_order_size_btc")
            current_reasoning = current_profile.get("daily_target_order_size_reasoning")
        if not (isinstance(source, dict) and "daily_target_order_size_btc" in source):
            candidate["daily_target_order_size_btc"] = current_value
            if current_reasoning is not None:
                candidate["daily_target_order_size_reasoning"] = current_reasoning
            else:
                candidate.pop("daily_target_order_size_reasoning", None)
            validated.append(candidate)
            continue
        if isinstance(source, dict) and "daily_target_order_size_btc" in source:
            source_value = source.get("daily_target_order_size_btc")
            if source_value is None:
                candidate["daily_target_order_size_btc"] = None
                candidate.pop("daily_target_order_size_reasoning", None)
            elif _is_number(source_value):
                numeric = float(source_value)
                if not (lower <= numeric <= upper):
                    candidate["daily_target_order_size_btc"] = current_value
                    if current_reasoning is not None:
                        candidate["daily_target_order_size_reasoning"] = current_reasoning
                    else:
                        candidate.pop("daily_target_order_size_reasoning", None)
                    rejected_reasons.append(
                        f"{name}.daily_target_order_size_btc={numeric} は範囲外 "
                        f"({lower:.3f}-{upper:.2f}) のため据え置き"
                    )
                else:
                    source_reasoning = source.get("daily_target_order_size_reasoning")
                    if source_reasoning is None:
                        candidate.pop("daily_target_order_size_reasoning", None)
                    else:
                        candidate["daily_target_order_size_reasoning"] = str(source_reasoning)
        validated.append(candidate)
    return validated, rejected_reasons


def clamp_profile_changes(
    current_payload: Dict[str, Any],
    new_profiles: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    current_map = {
        p.get("name"): p
        for p in current_payload.get("profiles", [])
        if isinstance(p, dict) and p.get("name")
    }

    reverted: List[str] = []
    clamped_to_bounds: List[str] = []
    adjusted: List[Dict[str, Any]] = []
    for p in new_profiles:
        candidate = dict(p)
        old = current_map.get(candidate.get("name"))
        if isinstance(old, dict):
            for key, new_val in list(candidate.items()):
                if key in {"name", "start_time", "end_time"}:
                    continue
                old_val = old.get(key)
                if not (_is_number(old_val) and _is_number(new_val)):
                    continue
                if float(old_val) == 0.0:
                    continue
                ratio = abs((float(new_val) - float(old_val)) / float(old_val))
                if ratio > CHANGE_LIMIT_RATIO:
                    candidate[key] = old_val
                    reverted.append(f"{candidate.get('name')}.{key}")
        for key, bounds in PARAMETER_ABSOLUTE_BOUNDS.items():
            if key not in candidate:
                continue
            val = candidate.get(key)
            if not _is_number(val):
                continue
            lower, upper = bounds
            clamped_val = min(max(float(val), lower), upper)
            if clamped_val != float(val):
                candidate[key] = clamped_val
                clamped_to_bounds.append(f"{candidate.get('name')}.{key}")
        adjusted.append(candidate)
    return adjusted, sorted(set(reverted)), sorted(set(clamped_to_bounds))


def safe_update_config(
    current_config: Dict[str, Any],
    moderator_payload: Dict[str, Any],
    normalized_profiles: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[str], List[str], List[str]]:
    adjusted_profiles, reverted_fields, clamped_to_bounds_fields = clamp_profile_changes(
        current_config,
        normalized_profiles,
    )
    adjusted_profiles, rejected_daily_target_reasons = _apply_daily_target_order_size_validation(
        current_payload=current_config,
        adjusted_profiles=adjusted_profiles,
        source_profiles=(
            moderator_payload.get("profiles", [])
            if isinstance(moderator_payload.get("profiles", []), list)
            else []
        ),
    )
    updated_reason = str(moderator_payload.get("updated_reason", "")).strip() or "AI review update"
    if reverted_fields:
        updated_reason += (
            " (一部項目は変更幅上限のため据え置き: "
            + ", ".join(reverted_fields)
            + ")"
        )
    if clamped_to_bounds_fields:
        updated_reason += (
            " (絶対範囲外のため補正: "
            + ", ".join(clamped_to_bounds_fields)
            + ")"
        )
    if rejected_daily_target_reasons:
        updated_reason += (
            " (daily_target_order_size_btc を拒否: "
            + "; ".join(rejected_daily_target_reasons)
            + ")"
        )

    new_payload = dict(current_config)
    new_payload["version"] = datetime.now().strftime("%Y-%m-%d_%H-%M")
    new_payload["updated_reason"] = updated_reason
    new_payload["profiles"] = adjusted_profiles
    return (
        new_payload,
        reverted_fields,
        clamped_to_bounds_fields,
        rejected_daily_target_reasons,
    )


def write_atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _call_proposer(summary: Dict[str, Any], test_mode: str) -> str:
    if test_mode != "live":
        return "Stub proposer: adjust thresholds based on recent trend."
    system, prompt = build_proposer_prompt(summary)
    return call_groq(prompt=prompt, system=system, max_tokens=2000)


def _call_skeptic(summary: Dict[str, Any], proposer_output: str, test_mode: str) -> str:
    if test_mode != "live":
        return "Stub skeptic: ensure confidence and regime consistency."
    system, prompt = build_skeptic_prompt(summary, proposer_output)
    return call_gemini(prompt=prompt, system=system, max_tokens=4096)


def _call_moderator(
    summary: Dict[str, Any],
    proposer_output: str,
    skeptic_output: str,
    test_mode: str,
    retry_context: Optional[Dict[str, Any]],
) -> str:
    if test_mode == "invalid_json":
        return '{"updated_reason": "broken", "profiles": ['

    if test_mode in {"extreme_change", "stub_success", "name_mismatch"}:
        current_profiles = summary.get("current_config", {}).get("profiles", [])
        proposed: List[Dict[str, Any]] = []
        for idx, p in enumerate(current_profiles):
            if not isinstance(p, dict):
                continue
            cp = dict(p)
            if test_mode == "extreme_change":
                for key in (
                    "imbalance_entry_threshold",
                    "take_profit_pct",
                    "stop_loss_pct",
                    "max_spread_pct",
                    "max_allowed_spread",
                    "max_order_size_btc",
                ):
                    if _is_number(cp.get(key)):
                        cp[key] = float(cp[key]) * 1.5
            elif test_mode == "stub_success":
                if _is_number(cp.get("take_profit_pct")):
                    cp["take_profit_pct"] = float(cp["take_profit_pct"]) * 1.05
            elif test_mode == "name_mismatch" and idx == 0:
                cp["name"] = f"{cp.get('name')}_v2"
            proposed.append(cp)
        return json.dumps(
            {
                "updated_reason": f"test-mode-{test_mode}",
                "profiles": proposed,
            },
            ensure_ascii=False,
        )

    system, prompt = build_moderator_prompt(
        summary=summary,
        proposer_output=proposer_output,
        skeptic_output=skeptic_output,
        retry_context=retry_context,
    )
    return call_gemini(prompt=prompt, system=system, max_tokens=4096, json_mode=True)


def _parse_and_validate_moderator_output(text: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Moderator output must be a JSON object")
    if "profiles" not in parsed:
        raise ValueError("Moderator output missing 'profiles'")
    normalized_profiles = normalize_profiles_from_candidate(parsed)
    return parsed, normalized_profiles


def _write_decision_log(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _notify_non_blocking(message: str) -> None:
    try:
        send_telegram_message(message)
    except Exception as exc:  # pragma: no cover - defensive fallback
        print(f"[WARNING] failed to send Telegram notification: {exc}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    target_date = resolve_target_date(args.target_date)
    decision_path = LOG_DIR / f"ai_review_decision_{target_date}.json"
    decision_doc: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "target_date": target_date,
        "test_mode": args.test_mode,
        "summary_path": "",
        "status": "started",
        "proposer_output": None,
        "skeptic_output": None,
        "moderator_attempts": [],
        "final_payload": None,
        "reverted_fields": [],
        "clamped_to_bounds_fields": [],
        "backtest_gated_fields": [],
    }

    summary_path = LOG_DIR / f"ai_review_summary_{target_date}.json"
    decision_doc["summary_path"] = str(summary_path)
    if not summary_path.exists():
        decision_doc["status"] = "missing_summary_file"
        _write_decision_log(decision_path, decision_doc)
        _notify_non_blocking(
            "\n".join(
                [
                    "[BTC AI議論] エラー",
                    f"{target_date}分の集計サマリーが見つかりません。",
                    "build_ai_review_summary.py が正常に実行されたか確認してください。",
                ]
            )
        )
        print(f"[ERROR] summary file not found: {summary_path}", file=sys.stderr)
        return 1

    try:
        summary = read_json(summary_path)
        current_config = read_json(CONFIG_PATH)
    except Exception as exc:
        print(f"[ERROR] failed to load summary/config: {exc}", file=sys.stderr)
        return 1

    # 既存 pending_rollouts の当日分を先に適用（新規提案が無い場合も継続させる）
    try:
        apply_daily_rollouts()
        current_config = read_json(CONFIG_PATH)
    except Exception as exc:
        print(f"[WARNING] apply_daily_rollouts failed (non-fatal): {exc}", file=sys.stderr)

    try:
        proposer_output = _call_proposer(summary, args.test_mode)
        decision_doc["proposer_output"] = proposer_output

        skeptic_output = _call_skeptic(summary, proposer_output, args.test_mode)
        decision_doc["skeptic_output"] = skeptic_output
    except Exception as exc:
        decision_doc["status"] = "failed_before_moderator"
        decision_doc["error"] = str(exc)
        decision_doc["error_kind"] = classify_llm_error_kind(exc)
        _write_decision_log(decision_path, decision_doc)
        # LLM呼び出し失敗の深夜リアルタイム通知は送らない（日次レポートへ集約）
        print(f"[ERROR] proposer/skeptic call failed: {exc}", file=sys.stderr)
        return 1

    retry_context: Optional[Dict[str, Any]] = None
    final_error: Optional[str] = None
    final_output: Optional[str] = None
    normalized_profiles: Optional[List[Dict[str, Any]]] = None
    moderator_payload: Optional[Dict[str, Any]] = None

    for attempt in range(1, MAX_MODERATOR_ATTEMPTS + 1):
        try:
            output_text = _call_moderator(
                summary=summary,
                proposer_output=proposer_output,
                skeptic_output=skeptic_output,
                test_mode=args.test_mode,
                retry_context=retry_context,
            )
            final_output = output_text
            parsed, normalized = _parse_and_validate_moderator_output(output_text)
            moderator_payload = parsed
            normalized_profiles = normalized
            decision_doc["moderator_attempts"].append(
                {"attempt": attempt, "status": "ok", "output": output_text}
            )
            final_error = None
            break
        except (json.JSONDecodeError, ConfigValidationError, ValueError) as exc:
            final_error = str(exc)
            decision_doc["moderator_attempts"].append(
                {
                    "attempt": attempt,
                    "status": "validation_error",
                    "error": final_error,
                    "output": final_output,
                }
            )
            retry_context = {
                "last_output": final_output,
                "error": final_error,
            }
            if attempt >= MAX_MODERATOR_ATTEMPTS:
                break
        except Exception as exc:
            decision_doc["status"] = "failed_moderator_call"
            decision_doc["error"] = str(exc)
            decision_doc["error_kind"] = classify_llm_error_kind(exc)
            _write_decision_log(decision_path, decision_doc)
            # LLM呼び出し失敗の深夜リアルタイム通知は送らない（日次レポートへ集約）
            print(f"[ERROR] moderator call failed: {exc}", file=sys.stderr)
            return 1

    if normalized_profiles is None or moderator_payload is None:
        append_jsonl(
            VALIDATION_FAILURE_PATH,
            {
                "date": target_date,
                "attempts": MAX_MODERATOR_ATTEMPTS,
                "final_error": final_error or "unknown",
                "last_output": final_output or "",
                "lesson": "validate_profilesまたはJSONパースに失敗。プロファイルの連続性・必須フィールドの過不足を確認すること",
            },
        )
        decision_doc["status"] = "no_update_validation_failed"
        decision_doc["final_error"] = final_error
        _write_decision_log(decision_path, decision_doc)
        _notify_non_blocking(
            "\n".join(
                [
                    "[BTC AI議論] 通知（見送り）",
                    f"{target_date}: AIの提案が3回ともバリデーションに失敗したため、",
                    "今夜の設定更新を安全に見送りました。config.jsonは変更していません。",
                    f"最終エラー: {final_error}",
                ]
            )
        )
        print("[INFO] moderator output validation failed; config update skipped safely")
        return 0

    current_names = {
        p.get("name")
        for p in current_config.get("profiles", [])
        if isinstance(p, dict) and p.get("name")
    }
    new_names = {
        p.get("name")
        for p in normalized_profiles
        if isinstance(p, dict) and p.get("name")
    }
    if current_names != new_names:
        added_names = sorted(list(new_names - current_names))
        removed_names = sorted(list(current_names - new_names))
        decision_doc["status"] = "held_profile_name_mismatch"
        decision_doc["current_names"] = sorted(list(current_names))
        decision_doc["new_names"] = sorted(list(new_names))
        decision_doc["added_names"] = added_names
        decision_doc["removed_names"] = removed_names
        _write_decision_log(decision_path, decision_doc)
        print("[INFO] profile name set changed; update held for manual review")
        return 0

    # 外れ値判定（該当パラメータのみ3日段階適用）
    current_profile_map: Dict[str, Dict[str, Any]] = {
        p.get("name"): p
        for p in current_config.get("profiles", [])
        if isinstance(p, dict) and p.get("name")
    }
    proposed_profile_map: Dict[str, Dict[str, Any]] = {
        p.get("name"): p
        for p in normalized_profiles
        if isinstance(p, dict) and p.get("name")
    }

    pending_rollouts = current_config.get("pending_rollouts", {})
    if not isinstance(pending_rollouts, dict):
        pending_rollouts = {}
    next_pending_rollouts: Dict[str, Any] = dict(pending_rollouts)

    outlier_marks: Dict[str, Dict[str, Any]] = {}
    backtest_results: Dict[str, Dict[str, Any]] = {}
    adjusted_profiles_for_apply: List[Dict[str, Any]] = []

    for profile_name, proposed_p in proposed_profile_map.items():
        current_p = current_profile_map.get(profile_name, {})
        cp = dict(proposed_p)

        for key, new_val in list(cp.items()):
            if key in {"name", "start_time", "end_time"}:
                continue
            old_val = current_p.get(key)
            if not (_is_number(old_val) and _is_number(new_val)):
                continue
            if float(old_val) == float(new_val):
                continue

            param_path = f"profiles.{profile_name}.{key}"
            outlier = judge_outlier(param_path, float(old_val), float(new_val))
            if not outlier.get("is_outlier"):
                continue

            rollout_decision = create_or_replace_pending_rollout(
                param_path=param_path,
                current_value=float(old_val),
                target_value=float(new_val),
                reason=str(outlier.get("reason", "")),
            )
            if rollout_decision.get("action") != "rollout":
                continue

            entry = rollout_decision["entry"]
            cp[key] = float(entry["current_applied_value"])
            next_pending_rollouts[param_path] = entry
            outlier_marks[param_path] = {
                "reason": entry.get("reason", ""),
                "start_value": entry.get("start_value"),
                "target_value": entry.get("target_value"),
                "current_applied_value": entry.get("current_applied_value"),
                "day_index": entry.get("day_index"),
                "total_days": entry.get("total_days"),
            }

        backtest_result = run_backtest_check(
            profile_name=profile_name,
            current_profile=current_p,
            proposed_profile=cp,
            log_dir=LOG_DIR,
            target_date=target_date,
        )
        if backtest_result.get("ran"):
            backtest_results[profile_name] = backtest_result
        if backtest_result.get("ran") and backtest_result.get("gated"):
            for gated_key in backtest_result.get("changed_keys", []):
                if gated_key in {"imbalance_entry_threshold", "take_profit_pct", "stop_loss_pct"}:
                    if isinstance(current_p.get(gated_key), (int, float)):
                        cp[gated_key] = float(current_p[gated_key])

        adjusted_profiles_for_apply.append(cp)

    (
        new_payload,
        reverted_fields,
        clamped_to_bounds_fields,
        rejected_daily_target_reasons,
    ) = safe_update_config(
        current_config=current_config,
        moderator_payload=moderator_payload,
        normalized_profiles=adjusted_profiles_for_apply,
    )
    new_payload["pending_rollouts"] = next_pending_rollouts

    try:
        write_atomic_json(CONFIG_PATH, new_payload)
    except Exception as exc:
        decision_doc["status"] = "failed_config_write"
        decision_doc["error"] = str(exc)
        _write_decision_log(decision_path, decision_doc)
        _notify_non_blocking(
            "\n".join(
                [
                    "[BTC AI議論] エラー（重要）",
                    f"{target_date}: バリデーション済みの新設定をconfig.jsonへ",
                    "書き込む際にエラーが発生しました。",
                    f"エラー内容: {decision_doc['error']}",
                    "config.jsonが破損していないか、手動で確認してください。",
                ]
            )
        )
        print(f"[ERROR] failed writing config atomically: {exc}", file=sys.stderr)
        return 1

    decision_doc["status"] = "applied"
    decision_doc["final_payload"] = new_payload
    decision_doc["reverted_fields"] = reverted_fields
    decision_doc["clamped_to_bounds_fields"] = clamped_to_bounds_fields
    decision_doc["rejected_daily_target_order_size_reasons"] = rejected_daily_target_reasons
    decision_doc["outlier_rollouts"] = outlier_marks
    backtest_gated_fields: List[str] = []
    for bt_profile_name, bt in sorted(backtest_results.items()):
        if not bt.get("gated"):
            continue
        for key in bt.get("changed_keys", []):
            backtest_gated_fields.append(f"profiles.{bt_profile_name}.{key}")
    decision_doc["backtest_gated_fields"] = backtest_gated_fields
    decision_doc["backtest_results"] = backtest_results
    _write_decision_log(decision_path, decision_doc)
    print(f"[INFO] review pipeline completed. config updated: {CONFIG_PATH}")

    # update_log.jsonl へ適用実績（value_before/value_after）を追記
    try:
        after_config = read_json(CONFIG_PATH)
        after_map: Dict[str, Dict[str, Any]] = {
            p.get("name"): p
            for p in after_config.get("profiles", [])
            if isinstance(p, dict) and p.get("name")
        }
        for profile_name, proposed_p in proposed_profile_map.items():
            before_p = current_profile_map.get(profile_name, {})
            after_p = after_map.get(profile_name, {})
            for key, _new_val in list(proposed_p.items()):
                if key in {"name", "start_time", "end_time"}:
                    continue
                before_val = before_p.get(key)
                after_val = after_p.get(key)
                if not (_is_number(before_val) and _is_number(after_val)):
                    continue
                if float(before_val) == float(after_val):
                    continue
                param_path = f"profiles.{profile_name}.{key}"
                applied_delta_pct = 0.0 if float(before_val) == 0.0 else abs((float(after_val) - float(before_val)) / float(before_val))
                append_jsonl(
                    UPDATE_LOG_PATH,
                    {
                        "applied_at": datetime.now().isoformat(timespec="seconds"),
                        "param_path": param_path,
                        "value_before": float(before_val),
                        "value_after": float(after_val),
                        "applied_delta_pct": applied_delta_pct,
                        "source": "ai_review",
                    },
                )
    except Exception as exc:
        print(f"[WARNING] failed to append update_log: {exc}", file=sys.stderr)

    # Telegram: 事後報告（外れ値があれば reason を含めて強調）
    try:
        lines: List[str] = []
        lines.append("[BTC AI議論] 本日の変更内容をお知らせします")
        lines.append(f"date={target_date}")
        if outlier_marks:
            lines.append("outlier_rollout:")
            for param_path, info in sorted(outlier_marks.items()):
                lines.append(
                    f"- {param_path} day {info.get('day_index')}/{info.get('total_days')}"
                    f" applied={info.get('current_applied_value')} target={info.get('target_value')}"
                    f" reason={info.get('reason')}"
                )
        if reverted_fields:
            lines.append("reverted(>15% cap): " + ", ".join(reverted_fields))
        if clamped_to_bounds_fields:
            lines.append("clamped(bounds): " + ", ".join(clamped_to_bounds_fields))
        if rejected_daily_target_reasons:
            lines.append("rejected_daily_target: " + "; ".join(rejected_daily_target_reasons))
        gated_profiles = [
            (name, bt)
            for name, bt in sorted(backtest_results.items())
            if bt.get("gated")
        ]
        if gated_profiles:
            lines.append("backtest_gated:")
            for name, bt in gated_profiles:
                reverted_keys = [
                    k
                    for k in bt.get("changed_keys", [])
                    if k in {"imbalance_entry_threshold", "take_profit_pct", "stop_loss_pct"}
                ]
                lines.append(
                    f"profiles.{name} "
                    f"old_pnl_pct={float(bt.get('old', {}).get('total_pnl_pct', 0.0)):.4f} "
                    f"new_pnl_pct={float(bt.get('new', {}).get('total_pnl_pct', 0.0)):.4f} "
                    f"reverted={','.join(reverted_keys)}"
                )
        send_telegram_message("\n".join(lines))
    except Exception as exc:
        print(f"[WARNING] failed to send Telegram notification: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
