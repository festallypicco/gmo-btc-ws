"""
test_config_manager.py

trading_mode のデフォルト補完・バリデーションを検証する。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from config_manager import (  # noqa: E402
    ConfigValidationError,
    apply_engine_safety_defaults,
    load_config_payload,
)
import trading_engine  # noqa: E402
from trading_engine import _safety_settings_from_payload  # noqa: E402
from virtual_trader import VirtualTrader  # noqa: E402


def _minimal_profiles() -> list:
    return [
        {
            "name": "full_day",
            "start_time": "00:00",
            "end_time": "24:00",
            "imbalance_entry_threshold": 0.6,
            "min_entry_wall_btc": 0.05,
            "min_valid_wall_btc": 0.1,
            "max_spread_pct": 0.0003,
            "max_allowed_spread": 3000.0,
            "imbalance_cancel_threshold": 0.5,
            "take_profit_pct": 0.001,
            "stop_loss_pct": 0.001,
            "maker_price_offset_jpy": 1.0,
            "max_order_size_btc": 0.05,
            "daily_target_order_size_btc": None,
        }
    ]


def _write_config(path: Path, **overrides) -> None:
    payload = {
        "version": "test",
        "updated_reason": "test",
        "profiles": _minimal_profiles(),
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_trading_mode_missing_defaults_to_virtual() -> None:
    result = apply_engine_safety_defaults({"profiles": _minimal_profiles()})
    assert result["trading_mode"] == "virtual"


def test_trading_mode_virtual_and_real_accepted(tmp_path: Path) -> None:
    for mode in ("virtual", "real"):
        config_path = tmp_path / f"config_{mode}.json"
        _write_config(config_path, trading_mode=mode)
        payload, _migrated = load_config_payload(config_path)
        assert payload["trading_mode"] == mode


def test_trading_mode_invalid_raises_config_validation_error() -> None:
    with pytest.raises(ConfigValidationError, match="trading_mode"):
        apply_engine_safety_defaults(
            {
                "profiles": _minimal_profiles(),
                "trading_mode": "invalid_value",
            }
        )


def test_trading_mode_invalid_exits_without_touching_pid_or_live_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    pid_path = tmp_path / "trading_engine.pid"
    live_state_path = tmp_path / "live_state.db"

    _write_config(config_path, trading_mode="invalid_value")
    pid_path.write_text("99999\n", encoding="utf-8")
    live_state_path.write_bytes(b"sentinel-db-bytes")
    pid_before = pid_path.read_bytes()
    live_before = live_state_path.read_bytes()
    pid_mtime_before = pid_path.stat().st_mtime_ns
    live_mtime_before = live_state_path.stat().st_mtime_ns

    monkeypatch.setattr(trading_engine, "CONFIG_PATH", config_path)
    monkeypatch.setattr(trading_engine, "PID_PATH", pid_path)
    monkeypatch.setattr(trading_engine, "LIVE_STATE_DB_PATH", live_state_path)

    with pytest.raises(SystemExit) as exc_info:
        trading_engine.main()

    assert exc_info.value.code == 1
    assert pid_path.read_bytes() == pid_before
    assert live_state_path.read_bytes() == live_before
    assert pid_path.stat().st_mtime_ns == pid_mtime_before
    assert live_state_path.stat().st_mtime_ns == live_mtime_before


def test_initial_jpy_missing_defaults_to_50000() -> None:
    result = apply_engine_safety_defaults({"profiles": _minimal_profiles()})
    assert result["initial_jpy"] == 50_000.0


def test_initial_jpy_valid_value_preserved(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path, initial_jpy=50_237.0)
    payload, _migrated = load_config_payload(config_path)
    assert payload["initial_jpy"] == pytest.approx(50_237.0)
    settings = _safety_settings_from_payload(payload)
    assert settings["initial_jpy"] == pytest.approx(50_237.0)
    trader = VirtualTrader(initial_jpy=float(settings["initial_jpy"]))
    assert trader.initial_jpy == pytest.approx(50_237.0)
    assert trader.jpy_balance == pytest.approx(50_237.0)


def test_initial_jpy_invalid_falls_back_to_default() -> None:
    for bad in (-1, 0, "abc", None):
        result = apply_engine_safety_defaults(
            {"profiles": _minimal_profiles(), "initial_jpy": bad}
        )
        assert result["initial_jpy"] == 50_000.0
