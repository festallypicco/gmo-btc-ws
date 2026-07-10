"""
test_trading_engine.py

trading_engine.py の発注レート制限ヘルパーを検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import trading_engine  # noqa: E402
from trading_engine import check_order_rate_limit, record_order_event  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_order_event_timestamps() -> None:
    trading_engine._order_event_timestamps.clear()
    yield
    trading_engine._order_event_timestamps.clear()


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
