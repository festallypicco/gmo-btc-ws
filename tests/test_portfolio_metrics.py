"""portfolio_metrics.py の総資産計算（ダッシュボードと同一定義）のテスト。"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BTC = _ROOT / "btc_trading_tool"
if str(_BTC) not in sys.path:
    sys.path.insert(0, str(_BTC))

from portfolio_metrics import (  # noqa: E402
    compute_total_assets,
    compute_total_assets_from_live_state,
)


def test_total_assets_flat_is_cash_only() -> None:
    assert compute_total_assets(jpy_balance=50_000.0) == 50_000.0


def test_total_assets_virtual_long_matches_dashboard_formula() -> None:
    # jpy 39046 + 0.001 * mid(10838554) ~= 49884.55
    total = compute_total_assets(
        jpy_balance=39_046.0,
        position_side="LONG",
        position_size=0.001,
        best_bid=10_838_000.0,
        best_ask=10_839_108.0,
        trading_mode="virtual",
    )
    assert total == 39_046.0 + 0.001 * ((10_838_000.0 + 10_839_108.0) / 2.0)


def test_total_assets_real_long_uses_unrealized_pnl_only() -> None:
    mid = 10_838_500.0
    entry = 10_800_000.0
    size = 0.001
    total = compute_total_assets(
        jpy_balance=49_990.0,
        position_side="LONG",
        position_size=size,
        position_entry_price=entry,
        mid_price=mid,
        trading_mode="real",
    )
    assert total == 49_990.0 + (mid - entry) * size
    # 旧式（想定元本加算）とは一致しない
    assert total != 49_990.0 + size * mid


def test_total_assets_short_uses_unrealized_pnl_component() -> None:
    total = compute_total_assets(
        jpy_balance=50_000.0,
        position_side="SHORT",
        position_size=0.001,
        position_entry_price=10_800_000.0,
        mid_price=10_790_000.0,
    )
    assert total == 50_000.0 + (10_800_000.0 - 10_790_000.0) * 0.001


def test_total_assets_real_short_unchanged() -> None:
    kwargs = dict(
        jpy_balance=50_000.0,
        position_side="SHORT",
        position_size=0.001,
        position_entry_price=10_800_000.0,
        mid_price=10_790_000.0,
    )
    assert compute_total_assets(**kwargs, trading_mode="real") == compute_total_assets(
        **kwargs, trading_mode="virtual"
    )


def test_total_assets_from_live_state_dict_defaults_virtual() -> None:
    state = {
        "jpy_balance": 39_046.0,
        "position_side": "LONG",
        "position_size": 0.001,
        "position_entry_price": 10_838_554.0,
        "best_bid_price": 10_838_000.0,
        "best_ask_price": 10_839_000.0,
    }
    expected = compute_total_assets(
        jpy_balance=39_046.0,
        position_side="LONG",
        position_size=0.001,
        position_entry_price=10_838_554.0,
        best_bid=10_838_000.0,
        best_ask=10_839_000.0,
        trading_mode="virtual",
    )
    assert compute_total_assets_from_live_state(state) == expected


def test_total_assets_from_live_state_respects_real_mode() -> None:
    state = {
        "jpy_balance": 49_990.0,
        "position_side": "LONG",
        "position_size": 0.001,
        "position_entry_price": 10_800_000.0,
        "best_bid_price": 10_838_000.0,
        "best_ask_price": 10_839_000.0,
        "trading_mode": "real",
    }
    mid = (10_838_000.0 + 10_839_000.0) / 2.0
    expected = 49_990.0 + (mid - 10_800_000.0) * 0.001
    assert compute_total_assets_from_live_state(state) == expected


def test_total_assets_pending_ignores_mid_move_real_and_virtual() -> None:
    """pending 中は mid が動いても jpy_balance のまま（real/virtual 共通）。"""
    base = dict(
        jpy_balance=55_361.0,
        position_side="LONG",
        position_size=0.001,
        position_entry_price=10_497_169.0,
        position_is_pending=True,
    )
    mid_a = 10_478_000.0
    mid_b = 10_450_000.0
    for mode in ("real", "virtual"):
        a = compute_total_assets(**base, mid_price=mid_a, trading_mode=mode)
        b = compute_total_assets(**base, mid_price=mid_b, trading_mode=mode)
        assert a == 55_361.0
        assert b == 55_361.0
        assert a == b


def test_total_assets_from_live_state_pending_stable_across_board() -> None:
    state_high = {
        "jpy_balance": 55_361.0,
        "position_side": "LONG",
        "position_size": 0.001,
        "position_entry_price": 10_497_169.0,
        "position_is_pending": 1,
        "best_bid_price": 10_478_000.0,
        "best_ask_price": 10_479_000.0,
        "trading_mode": "real",
    }
    state_low = dict(state_high)
    state_low["best_bid_price"] = 10_450_000.0
    state_low["best_ask_price"] = 10_451_000.0
    assert compute_total_assets_from_live_state(state_high) == 55_361.0
    assert compute_total_assets_from_live_state(state_low) == 55_361.0
