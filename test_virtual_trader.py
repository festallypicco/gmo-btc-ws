"""
test_virtual_trader.py

virtual_trader.py の口座照合ヘルパーを検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BTC_DIR = Path(__file__).resolve().parent / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

from virtual_trader import (  # noqa: E402
    VirtualTrader,
    compare_with_internal_state,
    run_reconciliation_check,
)

_INTERNAL_STATE = {
    "position_size_btc": 0.0100,
    "jpy_balance": 50_000.0,
}
_MATCHING_REAL_STATE = {
    "position_size_btc": 0.0100,
    "jpy_balance": 50_000.0,
}
_MISMATCH_REAL_STATE = {
    "position_size_btc": 0.0110,
    "jpy_balance": 50_000.0,
}
_TOLERANCE_BTC = 0.0005
_TOLERANCE_JPY = 100.0


@pytest.fixture
def trader() -> VirtualTrader:
    return VirtualTrader()


def test_compare_with_internal_state_within_tolerance_returns_none() -> None:
    result = compare_with_internal_state(
        real_state={"position_size_btc": 0.0102, "jpy_balance": 50_050.0},
        internal_state=_INTERNAL_STATE,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert result is None


def test_compare_with_internal_state_position_diff_exceeds_tolerance() -> None:
    result = compare_with_internal_state(
        real_state={"position_size_btc": 0.0106, "jpy_balance": 50_000.0},
        internal_state=_INTERNAL_STATE,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert result is not None
    assert result["position_diff_btc"] == pytest.approx(0.0006)
    assert result["balance_diff_jpy"] == pytest.approx(0.0)


def test_compare_with_internal_state_balance_diff_exceeds_tolerance() -> None:
    result = compare_with_internal_state(
        real_state={"position_size_btc": 0.0100, "jpy_balance": 50_101.0},
        internal_state=_INTERNAL_STATE,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert result is not None
    assert result["position_diff_btc"] == pytest.approx(0.0)
    assert result["balance_diff_jpy"] == pytest.approx(101.0)


def test_compare_with_internal_state_exact_tolerance_boundary_returns_none() -> None:
    internal_state = {"position_size_btc": 0.0, "jpy_balance": 0.0}
    result = compare_with_internal_state(
        real_state={
            "position_size_btc": _TOLERANCE_BTC,
            "jpy_balance": _TOLERANCE_JPY,
        },
        internal_state=internal_state,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert result is None


def test_compare_with_internal_state_mismatch_dict_contains_expected_fields() -> None:
    real_state = {"position_size_btc": 0.0200, "jpy_balance": 49_800.0}
    internal_state = {"position_size_btc": 0.0100, "jpy_balance": 50_000.0}
    result = compare_with_internal_state(
        real_state=real_state,
        internal_state=internal_state,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert result is not None
    assert result == {
        "position_diff_btc": pytest.approx(0.01),
        "balance_diff_jpy": pytest.approx(200.0),
        "real_position_size_btc": 0.0200,
        "internal_position_size_btc": 0.0100,
        "real_jpy_balance": 49_800.0,
        "internal_jpy_balance": 50_000.0,
    }


@patch("virtual_trader.get_internal_account_state", return_value=_INTERNAL_STATE)
@patch("virtual_trader.fetch_real_account_state", return_value=_MATCHING_REAL_STATE)
def test_run_reconciliation_check_match_does_not_call_callback(
    _mock_fetch: MagicMock,
    _mock_internal: MagicMock,
    trader: VirtualTrader,
) -> None:
    callback = MagicMock()
    pending_mismatch = [False]

    run_reconciliation_check(
        trader=trader,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
        pending_mismatch=pending_mismatch,
        on_confirmed_mismatch=callback,
    )

    callback.assert_not_called()
    assert pending_mismatch[0] is False


@patch("virtual_trader.get_internal_account_state", return_value=_INTERNAL_STATE)
@patch("virtual_trader.fetch_real_account_state", return_value=_MISMATCH_REAL_STATE)
def test_run_reconciliation_check_first_mismatch_sets_pending_only(
    _mock_fetch: MagicMock,
    _mock_internal: MagicMock,
    trader: VirtualTrader,
) -> None:
    callback = MagicMock()
    pending_mismatch = [False]

    run_reconciliation_check(
        trader=trader,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
        pending_mismatch=pending_mismatch,
        on_confirmed_mismatch=callback,
    )

    callback.assert_not_called()
    assert pending_mismatch[0] is True


@patch("virtual_trader.get_internal_account_state", return_value=_INTERNAL_STATE)
@patch("virtual_trader.fetch_real_account_state", return_value=_MISMATCH_REAL_STATE)
def test_run_reconciliation_check_second_consecutive_mismatch_calls_callback(
    _mock_fetch: MagicMock,
    _mock_internal: MagicMock,
    trader: VirtualTrader,
) -> None:
    callback = MagicMock()
    pending_mismatch = [True]

    run_reconciliation_check(
        trader=trader,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
        pending_mismatch=pending_mismatch,
        on_confirmed_mismatch=callback,
    )

    callback.assert_called_once()
    mismatch_arg = callback.call_args[0][0]
    assert mismatch_arg["position_diff_btc"] == pytest.approx(0.001)
    assert pending_mismatch[0] is False


@patch("virtual_trader.get_internal_account_state", return_value=_INTERNAL_STATE)
@patch(
    "virtual_trader.fetch_real_account_state",
    side_effect=[_MISMATCH_REAL_STATE, _MATCHING_REAL_STATE],
)
def test_run_reconciliation_check_retry_match_resets_pending(
    _mock_fetch: MagicMock,
    _mock_internal: MagicMock,
    trader: VirtualTrader,
) -> None:
    callback = MagicMock()
    pending_mismatch = [True]

    run_reconciliation_check(
        trader=trader,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
        pending_mismatch=pending_mismatch,
        on_confirmed_mismatch=callback,
    )

    callback.assert_not_called()
    assert pending_mismatch[0] is False
    assert _mock_fetch.call_count == 2


@patch("virtual_trader.get_internal_account_state", return_value=_INTERNAL_STATE)
@patch("virtual_trader.fetch_real_account_state", side_effect=RuntimeError("api down"))
def test_run_reconciliation_check_fetch_error_does_not_propagate(
    _mock_fetch: MagicMock,
    _mock_internal: MagicMock,
    trader: VirtualTrader,
) -> None:
    callback = MagicMock()
    pending_mismatch = [True]

    run_reconciliation_check(
        trader=trader,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
        pending_mismatch=pending_mismatch,
        on_confirmed_mismatch=callback,
    )

    callback.assert_not_called()
    assert pending_mismatch[0] is True
