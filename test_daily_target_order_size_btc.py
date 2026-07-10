from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

from ai_review.review_pipeline import safe_update_config

_BTC_DIR = Path(__file__).resolve().parent / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

from strategy_logic import StrategyConfig  # noqa: E402
from virtual_trader import VirtualTrader  # noqa: E402


def _legacy_calc_trade_size(trader: VirtualTrader, price: float) -> float:
    if price <= 0:
        return trader.MIN_TRADE_SIZE
    raw_size = (trader.jpy_balance * trader.POSITION_RATIO) / price
    floored_size = int(raw_size / trader.LOT_UNIT) * trader.LOT_UNIT
    computed_size = max(floored_size, trader.MIN_TRADE_SIZE)
    return min(computed_size, float(trader.config.max_order_size_btc))


def _profile_with_defaults() -> dict:
    return {
        "name": "full_day",
        "start_time": "00:00",
        "end_time": "24:00",
        **asdict(StrategyConfig()),
    }


def test_daily_target_none_keeps_legacy_trade_size_logic() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0)
    trader.config = StrategyConfig(
        max_order_size_btc=0.05,
        daily_target_order_size_btc=None,
    )
    for price in (500_000.0, 1_000_000.0, 15_000_000.0):
        expected = _legacy_calc_trade_size(trader, price)
        actual = trader._calc_trade_size(price)
        assert actual == expected


def test_daily_target_larger_than_asset_pct_cap_uses_asset_pct_rule() -> None:
    """a) daily_target が資産20%相当より大きい日 -> 20%ルールが効く。"""
    trader = VirtualTrader(initial_jpy=50_000.0)
    price = 500_000.0
    asset_pct_cap = (trader.jpy_balance * trader.POSITION_RATIO) / price  # 0.02 BTC
    trader.config = StrategyConfig(
        max_order_size_btc=0.05,
        daily_target_order_size_btc=0.04,  # 20%相当(0.02)より大きい
    )
    size = trader._calc_trade_size(price)
    assert asset_pct_cap == 0.02
    assert size == 0.02


def test_daily_target_smaller_than_asset_pct_cap_uses_daily_target() -> None:
    """b) daily_target が資産20%相当より小さい日 -> daily_target が効く。"""
    trader = VirtualTrader(initial_jpy=50_000.0)
    price = 500_000.0
    asset_pct_cap = (trader.jpy_balance * trader.POSITION_RATIO) / price  # 0.02 BTC
    trader.config = StrategyConfig(
        max_order_size_btc=0.05,
        daily_target_order_size_btc=0.005,  # 20%相当(0.02)より小さい
    )
    size = trader._calc_trade_size(price)
    assert asset_pct_cap == 0.02
    assert size == 0.005


def test_daily_target_applies_as_additional_cap() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0)
    trader.config = StrategyConfig(
        max_order_size_btc=0.05,
        daily_target_order_size_btc=0.005,
    )
    size = trader._calc_trade_size(price=500_000.0)
    assert size == 0.005


def test_safe_update_rejects_out_of_range_daily_target_and_keeps_existing() -> None:
    current_profile = _profile_with_defaults()
    current_profile["daily_target_order_size_btc"] = None
    current_payload = {
        "version": "test",
        "updated_reason": "before",
        "profiles": [current_profile],
    }

    candidate_profile = dict(current_profile)
    candidate_profile["daily_target_order_size_btc"] = 0.2
    new_payload, _, _, rejected_reasons = safe_update_config(
        current_config=current_payload,
        moderator_payload={
            "updated_reason": "test update",
            "profiles": [{"name": "full_day", "daily_target_order_size_btc": 0.2}],
        },
        normalized_profiles=[candidate_profile],
    )

    assert new_payload["profiles"][0]["daily_target_order_size_btc"] is None
    assert rejected_reasons
    assert "daily_target_order_size_btc" in rejected_reasons[0]


def test_safe_update_keeps_existing_when_daily_target_omitted() -> None:
    current_profile = _profile_with_defaults()
    current_profile["daily_target_order_size_btc"] = 0.007
    current_payload = {
        "version": "test",
        "updated_reason": "before",
        "profiles": [current_profile],
    }

    candidate_profile = dict(current_profile)
    candidate_profile["daily_target_order_size_btc"] = None
    new_payload, _, _, rejected_reasons = safe_update_config(
        current_config=current_payload,
        moderator_payload={
            "updated_reason": "test update",
            "profiles": [{"name": "full_day"}],
        },
        normalized_profiles=[candidate_profile],
    )

    assert new_payload["profiles"][0]["daily_target_order_size_btc"] == 0.007
    assert rejected_reasons == []


def test_safe_update_stores_reasoning_inside_profile() -> None:
    current_profile = _profile_with_defaults()
    current_profile["daily_target_order_size_btc"] = None
    current_payload = {
        "version": "test",
        "updated_reason": "before",
        "profiles": [current_profile],
    }

    candidate_profile = dict(current_profile)
    candidate_profile["daily_target_order_size_btc"] = 0.006
    new_payload, _, _, _ = safe_update_config(
        current_config=current_payload,
        moderator_payload={
            "updated_reason": "test update",
            "profiles": [
                {
                    "name": "full_day",
                    "daily_target_order_size_btc": 0.006,
                    "daily_target_order_size_reasoning": "volatility is elevated",
                }
            ],
        },
        normalized_profiles=[candidate_profile],
    )

    profile = new_payload["profiles"][0]
    assert profile["daily_target_order_size_btc"] == 0.006
    assert profile["daily_target_order_size_reasoning"] == "volatility is elevated"
    assert "daily_target_order_size_reasoning" not in new_payload


def test_safe_update_omits_reasoning_when_daily_target_not_set() -> None:
    current_profile = _profile_with_defaults()
    current_profile["daily_target_order_size_btc"] = None
    current_payload = {
        "version": "test",
        "updated_reason": "before",
        "profiles": [current_profile],
    }

    candidate_profile = dict(current_profile)
    candidate_profile["daily_target_order_size_btc"] = None
    new_payload, _, _, _ = safe_update_config(
        current_config=current_payload,
        moderator_payload={
            "updated_reason": "test update",
            "profiles": [
                {
                    "name": "full_day",
                    "daily_target_order_size_btc": None,
                    "daily_target_order_size_reasoning": "should be ignored",
                }
            ],
        },
        normalized_profiles=[candidate_profile],
    )

    profile = new_payload["profiles"][0]
    assert profile["daily_target_order_size_btc"] is None
    assert "daily_target_order_size_reasoning" not in profile
