"""
test_trading_engine.py

trading_engine.py の発注レート制限ヘルパーを検証する。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import trading_engine  # noqa: E402
from trading_engine import (  # noqa: E402
    _trigger_safety_stop,
    check_order_rate_limit,
    record_order_event,
)


@pytest.fixture(autouse=True)
def _clear_order_event_timestamps() -> None:
    trading_engine._order_event_timestamps.clear()
    yield
    trading_engine._order_event_timestamps.clear()


@pytest.fixture
def isolated_manual_stop_flag(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    flag = tmp_path / "manual_stop.flag"
    reason = tmp_path / "manual_stop_reason.json"
    monkeypatch.setattr(trading_engine, "MANUAL_STOP_FLAG_PATH", flag)
    monkeypatch.setattr(trading_engine, "MANUAL_STOP_REASON_PATH", reason)
    return flag


def test_check_order_rate_limit_under_limit_returns_false() -> None:
    with patch("trading_engine.time.time", return_value=1_000.0):
        for _ in range(4):
            record_order_event()
        assert check_order_rate_limit(5) is False


def test_check_order_rate_limit_over_limit_returns_true() -> None:
    with patch("trading_engine.time.time", return_value=1_000.0):
        for _ in range(6):
            record_order_event()
        assert check_order_rate_limit(5) is True


def test_check_order_rate_limit_exactly_at_limit_returns_false() -> None:
    with patch("trading_engine.time.time", return_value=1_000.0):
        for _ in range(5):
            record_order_event()
        assert check_order_rate_limit(5) is False


def test_check_order_rate_limit_ignores_events_older_than_sixty_one_seconds() -> None:
    with patch("trading_engine.time.time", return_value=100.0):
        record_order_event()

    with patch("trading_engine.time.time", return_value=161.0):
        assert check_order_rate_limit(5) is False


def test_trigger_safety_stop_order_rate_limit_sends_telegram(
    isolated_manual_stop_flag: Path,
) -> None:
    with patch("trading_engine.send_telegram_message") as mock_send:
        _trigger_safety_stop(
            "order_rate_limit",
            {
                "order_rate_limit_per_minute": 5,
                "recent_order_count": 6,
            },
        )
    assert isolated_manual_stop_flag.exists()
    mock_send.assert_called_once()
    message = mock_send.call_args.args[0]
    assert "reason=order_rate_limit" in message
    assert "order_rate_limit_per_minute=5" in message
    assert "recent_order_count=6" in message
    assert "triggered_at=" in message


def test_trigger_safety_stop_reconciliation_mismatch_sends_telegram(
    isolated_manual_stop_flag: Path,
) -> None:
    with patch("trading_engine.send_telegram_message") as mock_send:
        _trigger_safety_stop(
            "reconciliation_mismatch",
            {
                "position_diff_btc": 0.001,
                "real_position_size_btc": 0.011,
                "internal_position_size_btc": 0.010,
                "balance_diff_jpy": 200.0,
                "real_jpy_balance": 49_800.0,
                "internal_jpy_balance": 50_000.0,
            },
        )
    assert isolated_manual_stop_flag.exists()
    mock_send.assert_called_once()
    message = mock_send.call_args.args[0]
    assert "reason=reconciliation_mismatch" in message
    assert "position_diff_btc=0.001" in message


def test_trigger_safety_stop_daily_loss_limit_sends_telegram(
    isolated_manual_stop_flag: Path,
) -> None:
    with patch("trading_engine.send_telegram_message") as mock_send:
        _trigger_safety_stop(
            "daily_loss_limit",
            {
                "daily_realized_pnl": -5_000.0,
                "daily_start_balance": 50_000.0,
                "daily_loss_limit_pct": 0.10,
                "limit_jpy": 5_000.0,
            },
        )
    assert isolated_manual_stop_flag.exists()
    mock_send.assert_called_once()
    message = mock_send.call_args.args[0]
    assert "reason=daily_loss_limit" in message
    assert "daily_realized_pnl=-5000.0" in message
    assert "daily_start_balance=50000.0" in message


def test_trigger_safety_stop_skips_when_flag_already_exists(
    isolated_manual_stop_flag: Path,
) -> None:
    isolated_manual_stop_flag.write_text("already-stopped", encoding="utf-8")
    with patch("trading_engine.send_telegram_message") as mock_send:
        _trigger_safety_stop("order_rate_limit", {"order_rate_limit_per_minute": 5})
    mock_send.assert_not_called()
    assert isolated_manual_stop_flag.read_text(encoding="utf-8") == "already-stopped"


def test_trigger_safety_stop_creates_flag_even_if_telegram_raises(
    isolated_manual_stop_flag: Path,
) -> None:
    with patch(
        "trading_engine.send_telegram_message",
        side_effect=RuntimeError("telegram down"),
    ) as mock_send:
        _trigger_safety_stop(
            "order_rate_limit",
            {"order_rate_limit_per_minute": 5, "recent_order_count": 9},
        )
    mock_send.assert_called_once()
    assert isolated_manual_stop_flag.exists()


def test_order_rate_limit_path_notifies_via_trigger_safety_stop(
    isolated_manual_stop_flag: Path,
) -> None:
    """3.4.1: レート超過判定後に _trigger_safety_stop へ渡す経路を再現。"""
    with patch("trading_engine.time.time", return_value=1_000.0):
        for _ in range(6):
            record_order_event()
        assert check_order_rate_limit(5) is True
        recent_count = sum(
            1 for ts in trading_engine._order_event_timestamps if ts >= 1_000.0 - 60.0
        )
        with patch("trading_engine.send_telegram_message") as mock_send:
            _trigger_safety_stop(
                "order_rate_limit",
                {
                    "order_rate_limit_per_minute": 5,
                    "recent_order_count": recent_count,
                },
            )
    mock_send.assert_called_once()
    assert "reason=order_rate_limit" in mock_send.call_args.args[0]


@pytest.fixture
def isolated_trade_key_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lock_path = tmp_path / "runtime" / "gmo_trade_key.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(trading_engine, "GMO_TRADE_KEY_LOCK_PATH", lock_path)
    trading_engine._gmo_trade_key_lock_held = False
    trading_engine._gmo_trade_key_lock_fd = None
    yield lock_path
    trading_engine.release_gmo_trade_key_lock(lock_path)
    trading_engine._gmo_trade_key_lock_held = False
    trading_engine._gmo_trade_key_lock_fd = None


def test_acquire_gmo_trade_key_lock_when_absent(
    isolated_trade_key_lock: Path,
) -> None:
    assert trading_engine.acquire_gmo_trade_key_lock(
        lock_path=isolated_trade_key_lock
    ) is True
    assert isolated_trade_key_lock.exists()
    data = json.loads(isolated_trade_key_lock.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert "started_at" in data
    assert trading_engine._gmo_trade_key_lock_held is True
    assert trading_engine._gmo_trade_key_lock_fd is not None


def test_acquire_gmo_trade_key_lock_blocked_when_flock_held(
    isolated_trade_key_lock: Path,
) -> None:
    alerts: list[tuple[int, dict]] = []
    assert trading_engine.acquire_gmo_trade_key_lock(
        lock_path=isolated_trade_key_lock
    ) is True
    held_fd = trading_engine._gmo_trade_key_lock_fd
    ok = trading_engine.acquire_gmo_trade_key_lock(
        lock_path=isolated_trade_key_lock,
        on_blocked=lambda pid, data: alerts.append((pid, data)),
    )
    assert ok is False
    assert len(alerts) == 1
    assert alerts[0][0] == os.getpid()
    # 最初の取得状態は維持される（2回目失敗で解放しない）
    assert trading_engine._gmo_trade_key_lock_held is True
    assert trading_engine._gmo_trade_key_lock_fd is held_fd


def test_acquire_gmo_trade_key_lock_succeeds_after_holder_gone(
    isolated_trade_key_lock: Path,
) -> None:
    # 強制終了後相当: ロックファイルだけ残り、flock 保持者はいない
    trading_engine._write_gmo_trade_key_lock(
        pid=999_999_999,
        started_at="2026-07-27T00:00:00",
        lock_path=isolated_trade_key_lock,
    )
    ok = trading_engine.acquire_gmo_trade_key_lock(
        lock_path=isolated_trade_key_lock
    )
    assert ok is True
    data = json.loads(isolated_trade_key_lock.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert trading_engine._gmo_trade_key_lock_held is True


def test_acquire_gmo_trade_key_lock_or_exit_alerts_on_block(
    isolated_trade_key_lock: Path,
) -> None:
    assert trading_engine.acquire_gmo_trade_key_lock(
        lock_path=isolated_trade_key_lock
    ) is True
    with patch("trading_engine.send_telegram_message") as mock_send:
        with pytest.raises(SystemExit) as exc_info:
            trading_engine._acquire_gmo_trade_key_lock_or_exit()
    assert exc_info.value.code == 1
    mock_send.assert_called_once()
    message = mock_send.call_args.args[0]
    assert "real mode duplicate start blocked" in message
    assert f"existing_pid={os.getpid()}" in message


def test_reject_real_mode_on_windows_or_exit_alerts_and_exits() -> None:
    with patch("trading_engine.platform.system", return_value="Windows"):
        with patch("trading_engine.send_telegram_message") as mock_send:
            with pytest.raises(SystemExit) as exc_info:
                trading_engine._reject_real_mode_on_windows_or_exit()
    assert exc_info.value.code == 1
    mock_send.assert_called_once()
    message = mock_send.call_args.args[0]
    assert "real mode start blocked on Windows native" in message
    assert "real mode is Docker/Linux only" in message
    assert "Windows native start was attempted" in message

