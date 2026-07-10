"""
config_manager.py
-----------------
config.json の読み込み・マイグレーション・バリデーションを行う。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

from profile_config import ProfileDefinition, parse_hhmm_to_minute
from strategy_logic import StrategyConfig


class ConfigValidationError(ValueError):
    """設定ファイルの内容が不正な場合に送出する。"""


_STRATEGY_DEFAULTS = asdict(StrategyConfig())
_STRATEGY_KEYS = tuple(_STRATEGY_DEFAULTS.keys())

DEFAULT_CONFIG_VERSION = "default"

DEFAULT_ORDER_RATE_LIMIT_PER_MINUTE = 5
DEFAULT_RECONCILIATION_INTERVAL_MINUTES = 5
DEFAULT_RECONCILIATION_TOLERANCE_BTC = 0.0005
DEFAULT_RECONCILIATION_TOLERANCE_JPY = 100.0

# review_pipeline.py の段階適用(pending_rollouts)用。profiles.<name>.<param> のみ想定。
DEFAULT_PENDING_ROLLOUTS: Dict[str, Any] = {}

# 段階適用をスキップするための最小ステップ幅（パラメータごと）
PARAMETER_MIN_STEP: Dict[str, float] = {
    "imbalance_entry_threshold": 0.01,
    "imbalance_cancel_threshold": 0.01,
    "take_profit_pct": 0.0001,
    "stop_loss_pct": 0.0001,
    "max_spread_pct": 0.00001,
    "max_allowed_spread": 100.0,
    "maker_price_offset_jpy": 1.0,
    "min_entry_wall_btc": 0.001,
    "min_valid_wall_btc": 0.001,
    "max_order_size_btc": 0.001,
    "daily_target_order_size_btc": 0.001,
}

PARAMETER_ABSOLUTE_BOUNDS: Dict[str, Tuple[float, float]] = {
    "imbalance_entry_threshold": (0.50, 0.80),
    "min_entry_wall_btc": (0.01, 0.50),
    "min_valid_wall_btc": (0.01, 0.50),
    "max_spread_pct": (0.0001, 0.0010),
    "max_allowed_spread": (500.0, 10000.0),
    "imbalance_cancel_threshold": (0.30, 0.60),
    "take_profit_pct": (0.0005, 0.0050),
    "stop_loss_pct": (0.0005, 0.0050),
    "maker_price_offset_jpy": (0.0, 10.0),
    "max_order_size_btc": (0.01, 0.20),
    "daily_target_order_size_btc": (0.001, 0.05),
}


def _build_default_profile() -> Dict[str, Any]:
    return {
        "name": "full_day",
        "start_time": "00:00",
        "end_time": "24:00",
        **_STRATEGY_DEFAULTS,
    }


def default_config_payload() -> Dict[str, Any]:
    return {
        "version": DEFAULT_CONFIG_VERSION,
        "updated_reason": "auto-generated default config",
        "maintenance_pre_action": "close",
        "maintenance_prepare_minutes": 5,
        "order_rate_limit_per_minute": DEFAULT_ORDER_RATE_LIMIT_PER_MINUTE,
        "reconciliation_interval_minutes": DEFAULT_RECONCILIATION_INTERVAL_MINUTES,
        "reconciliation_tolerance_btc": DEFAULT_RECONCILIATION_TOLERANCE_BTC,
        "reconciliation_tolerance_jpy": DEFAULT_RECONCILIATION_TOLERANCE_JPY,
        "pending_rollouts": {},
        "profiles": [_build_default_profile()],
    }


def _as_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _as_positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def apply_engine_safety_defaults(payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(payload)
    normalized.setdefault("order_rate_limit_per_minute", DEFAULT_ORDER_RATE_LIMIT_PER_MINUTE)
    normalized.setdefault("reconciliation_interval_minutes", DEFAULT_RECONCILIATION_INTERVAL_MINUTES)
    normalized.setdefault("reconciliation_tolerance_btc", DEFAULT_RECONCILIATION_TOLERANCE_BTC)
    normalized.setdefault("reconciliation_tolerance_jpy", DEFAULT_RECONCILIATION_TOLERANCE_JPY)
    normalized["order_rate_limit_per_minute"] = _as_positive_int(
        normalized.get("order_rate_limit_per_minute"),
        DEFAULT_ORDER_RATE_LIMIT_PER_MINUTE,
    )
    normalized["reconciliation_interval_minutes"] = _as_positive_int(
        normalized.get("reconciliation_interval_minutes"),
        DEFAULT_RECONCILIATION_INTERVAL_MINUTES,
    )
    normalized["reconciliation_tolerance_btc"] = _as_positive_float(
        normalized.get("reconciliation_tolerance_btc"),
        DEFAULT_RECONCILIATION_TOLERANCE_BTC,
    )
    normalized["reconciliation_tolerance_jpy"] = _as_positive_float(
        normalized.get("reconciliation_tolerance_jpy"),
        DEFAULT_RECONCILIATION_TOLERANCE_JPY,
    )
    pending_rollouts = normalized.get("pending_rollouts")
    if not isinstance(pending_rollouts, dict):
        normalized["pending_rollouts"] = {}
    else:
        normalized["pending_rollouts"] = pending_rollouts
    return normalized


def _strategy_dict_from_source(src: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, default_value in _STRATEGY_DEFAULTS.items():
        val = src.get(key, default_value)
        if key == "daily_target_order_size_btc":
            if val is None:
                out[key] = None
            else:
                try:
                    out[key] = float(val)
                except (TypeError, ValueError) as exc:
                    raise ConfigValidationError(f"{key} は数値または null である必要があります: {val!r}") from exc
            continue
        if isinstance(default_value, float):
            try:
                out[key] = float(val)
            except (TypeError, ValueError) as exc:
                raise ConfigValidationError(f"{key} は数値である必要があります: {val!r}") from exc
        else:
            out[key] = val
    return out


def _migrate_flat_payload_to_profiles(raw_payload: Dict[str, Any]) -> Dict[str, Any]:
    migrated = {
        "version": raw_payload.get("version", DEFAULT_CONFIG_VERSION),
        "updated_reason": raw_payload.get(
            "updated_reason",
            "config format migrated to multi-profile schema (auto)",
        ),
        "profiles": [
            {
                "name": "full_day",
                "start_time": "00:00",
                "end_time": "24:00",
                **_strategy_dict_from_source(raw_payload),
            }
        ],
    }
    return migrated


def _normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if "profiles" not in payload:
        return _migrate_flat_payload_to_profiles(payload)

    normalized = dict(payload)
    normalized.setdefault("version", DEFAULT_CONFIG_VERSION)
    normalized.setdefault("updated_reason", "")
    return apply_engine_safety_defaults(normalized)


def load_config_payload(config_path: Path) -> Tuple[Dict[str, Any], bool]:
    """
    config.json を読み込み、新スキーマへ正規化して返す。
    戻り値: (payload, migrated)
    """
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    migrated = False

    if not config_path.exists():
        payload = default_config_payload()
        config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload, False

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigValidationError(f"config.json の JSON 形式が不正です: {config_path}") from exc

    if not isinstance(raw, dict):
        raise ConfigValidationError("config.json は JSON オブジェクトである必要があります")

    payload = _normalize_payload(raw)
    migrated = ("profiles" not in raw)

    # マイグレーション時は即座に新形式で保存
    if migrated:
        config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return payload, migrated


def build_profile_definitions(payload: Dict[str, Any]) -> List[ProfileDefinition]:
    profiles_raw = payload.get("profiles")
    if not isinstance(profiles_raw, list) or not profiles_raw:
        raise ConfigValidationError("profiles は 1 件以上の配列である必要があります")

    profiles: List[ProfileDefinition] = []
    for idx, p in enumerate(profiles_raw):
        if not isinstance(p, dict):
            raise ConfigValidationError(f"profiles[{idx}] はオブジェクトである必要があります")

        name = p.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigValidationError(f"profiles[{idx}].name は空でない文字列である必要があります")

        start_time = p.get("start_time")
        end_time = p.get("end_time")
        if not isinstance(start_time, str) or not isinstance(end_time, str):
            raise ConfigValidationError(f"profiles[{idx}] の start_time/end_time は文字列である必要があります")

        try:
            start_minute = parse_hhmm_to_minute(start_time)
            end_minute = parse_hhmm_to_minute(end_time)
        except ValueError as exc:
            raise ConfigValidationError(f"profiles[{idx}] の時刻が不正です: {exc}") from exc

        strategy_dict = _strategy_dict_from_source(p)
        cfg = StrategyConfig(**strategy_dict)
        profiles.append(
            ProfileDefinition(
                name=name.strip(),
                start_minute=start_minute,
                end_minute=end_minute,
                config=cfg,
            )
        )

    return profiles


def payload_to_history_snapshot(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version": payload.get("version", DEFAULT_CONFIG_VERSION),
        "updated_reason": payload.get("updated_reason", ""),
        "maintenance_pre_action": payload.get("maintenance_pre_action", "close"),
        "maintenance_prepare_minutes": payload.get("maintenance_prepare_minutes", 5),
        "order_rate_limit_per_minute": payload.get(
            "order_rate_limit_per_minute", DEFAULT_ORDER_RATE_LIMIT_PER_MINUTE
        ),
        "reconciliation_interval_minutes": payload.get(
            "reconciliation_interval_minutes", DEFAULT_RECONCILIATION_INTERVAL_MINUTES
        ),
        "reconciliation_tolerance_btc": payload.get(
            "reconciliation_tolerance_btc", DEFAULT_RECONCILIATION_TOLERANCE_BTC
        ),
        "reconciliation_tolerance_jpy": payload.get(
            "reconciliation_tolerance_jpy", DEFAULT_RECONCILIATION_TOLERANCE_JPY
        ),
        "pending_rollouts": payload.get("pending_rollouts", {}),
        "profiles": payload.get("profiles", []),
    }
