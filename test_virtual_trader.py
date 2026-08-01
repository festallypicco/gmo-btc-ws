"""
test_virtual_trader.py

virtual_trader.py の口座照合ヘルパーを検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import virtual_trader as virtual_trader_module  # noqa: E402
from virtual_trader import (  # noqa: E402
    CANCEL_REASON_DEVIATION,
    CANCEL_REASON_IMBALANCE,
    CANCEL_REASON_MAINTENANCE,
    CANCEL_REASON_TIMEOUT,
    ENTRY_COOLDOWN_AFTER_CANCEL_SEC,
    ENTRY_COOLDOWN_AFTER_IMBALANCE_CANCEL_ANY_SIDE_SEC,
    ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC,
    ENTRY_PENDING_DEVIATION_SL_RATIO,
    IMBALANCE_REVERSAL_DEBOUNCE_SEC,
    VirtualTrader,
    calc_trading_day_date,
    compare_with_internal_state,
    get_internal_account_state,
    run_reconciliation_check,
)
from strategy_logic import OrderbookSnapshot, PositionState, StrategyConfig  # noqa: E402
from profile_config import ProfileDefinition  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
import trading_engine  # noqa: E402
from trading_engine import _trigger_safety_stop  # noqa: E402

_INTERNAL_STATE = {
    "position_size_btc": 0.0100,
    "jpy_balance": 50_000.0,
}
_MATCHING_REAL_STATE = {
    "position_size_btc": 0.0100,
    "jpy_balance": 50_000.0,
    "equity_jpy": 50_000.0,
}
_MISMATCH_REAL_STATE = {
    "position_size_btc": 0.0110,
    "jpy_balance": 50_000.0,
    "equity_jpy": 50_000.0,
}
_TOLERANCE_BTC = 0.0005
_TOLERANCE_JPY = 100.0
_PRODUCTION_LOG_DIR = _ROOT_DIR / "log"


@pytest.fixture(autouse=True)
def isolated_trade_csv_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    _record_and_print() が本番 log/ ではなく tmp_path 配下へ CSV を書くようにする。
    virtual_trader.LOG_DIR 定数を差し替える（本番 config / .env は触らない）。
    """
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)
    return log_dir


@pytest.fixture
def trader() -> VirtualTrader:
    return VirtualTrader()


def test_trade_csv_writes_use_isolated_log_dir(isolated_trade_csv_log_dir: Path) -> None:
    """回帰: 取引 CSV の書き込み先が本番 log/ ではないこと。"""
    assert virtual_trader_module.LOG_DIR.resolve() == isolated_trade_csv_log_dir.resolve()
    assert virtual_trader_module.LOG_DIR.resolve() != _PRODUCTION_LOG_DIR.resolve()

    trader = VirtualTrader()
    trader._enter_long(_make_snapshot(bid=10_000_000.0, ask=10_000_100.0))

    written = list(isolated_trade_csv_log_dir.glob("realtime_trading_log_*.csv"))
    assert len(written) == 1


def test_compare_with_internal_state_within_tolerance_returns_none() -> None:
    result = compare_with_internal_state(
        real_state={
            "position_size_btc": 0.0102,
            "jpy_balance": 40_000.0,
            "equity_jpy": 50_050.0,
        },
        internal_state=_INTERNAL_STATE,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert result is None


def test_compare_with_internal_state_position_diff_exceeds_tolerance() -> None:
    result = compare_with_internal_state(
        real_state={
            "position_size_btc": 0.0106,
            "jpy_balance": 50_000.0,
            "equity_jpy": 50_000.0,
        },
        internal_state=_INTERNAL_STATE,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert result is not None
    assert result["position_diff_btc"] == pytest.approx(0.0006)
    assert result["balance_diff_jpy"] == pytest.approx(0.0)


def test_compare_with_internal_state_balance_diff_exceeds_tolerance() -> None:
    result = compare_with_internal_state(
        real_state={
            "position_size_btc": 0.0100,
            "jpy_balance": 50_000.0,
            "equity_jpy": 50_101.0,
        },
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
            "jpy_balance": 999_999.0,
            "equity_jpy": _TOLERANCE_JPY,
        },
        internal_state=internal_state,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert result is None


def test_compare_with_internal_state_mismatch_dict_contains_expected_fields() -> None:
    real_state = {
        "position_size_btc": 0.0200,
        "jpy_balance": 10_000.0,
        "equity_jpy": 49_800.0,
    }
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


def test_compare_uses_equity_jpy_not_available_amount_when_they_differ() -> None:
    """保留注文等で availableAmount と actualProfitLoss が乖離しても equity 側で判定する。"""
    # availableAmount は大きく乖離、equity は内部帳簿と一致 -> mismatch なし
    match = compare_with_internal_state(
        real_state={
            "position_size_btc": 0.0100,
            "jpy_balance": 30_000.0,
            "equity_jpy": 50_000.0,
        },
        internal_state=_INTERNAL_STATE,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert match is None

    # availableAmount は内部と一致、equity だけ乖離 -> mismatch
    mismatch = compare_with_internal_state(
        real_state={
            "position_size_btc": 0.0100,
            "jpy_balance": 50_000.0,
            "equity_jpy": 49_800.0,
        },
        internal_state=_INTERNAL_STATE,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert mismatch is not None
    assert mismatch["balance_diff_jpy"] == pytest.approx(200.0)
    assert mismatch["real_jpy_balance"] == pytest.approx(49_800.0)


def test_compare_open_position_uses_comparable_equity_not_raw_cash() -> None:
    """保有中: 現金だけ見ると乖離しても、comparable_equity が一致すれば mismatch なし。"""
    # real LONG: jpy=49990, 含み=+150 → comparable=50140
    result = compare_with_internal_state(
        real_state={
            "position_size_btc": 0.001,
            "jpy_balance": 40_000.0,
            "equity_jpy": 50_140.0,
        },
        internal_state={
            "position_size_btc": 0.001,
            "jpy_balance": 49_990.0,
            "comparable_equity_jpy": 50_140.0,
        },
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert result is None


def test_compare_open_position_raw_cash_alone_would_false_trip() -> None:
    """回帰: comparable 無しだと旧挙動（現金 vs equity）で誤検知しうる。"""
    # 含み +150 あると現金 49990 vs equity 50140 で 150 円差 > 100
    old_style = compare_with_internal_state(
        real_state={
            "position_size_btc": 0.001,
            "jpy_balance": 40_000.0,
            "equity_jpy": 50_140.0,
        },
        internal_state={
            "position_size_btc": 0.001,
            "jpy_balance": 49_990.0,
        },
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert old_style is not None
    assert old_style["balance_diff_jpy"] == pytest.approx(150.0)


def test_compare_open_position_comparable_mismatch_still_detected() -> None:
    result = compare_with_internal_state(
        real_state={
            "position_size_btc": 0.001,
            "jpy_balance": 40_000.0,
            "equity_jpy": 51_000.0,
        },
        internal_state={
            "position_size_btc": 0.001,
            "jpy_balance": 49_990.0,
            "comparable_equity_jpy": 50_140.0,
        },
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert result is not None
    assert result["balance_diff_jpy"] == pytest.approx(860.0)
    assert result["internal_jpy_balance"] == pytest.approx(50_140.0)


def test_compare_open_position_skips_balance_when_flag_set() -> None:
    """mid 未取得などで skip_balance_check 時は建玉サイズのみ照合。"""
    result = compare_with_internal_state(
        real_state={
            "position_size_btc": 0.001,
            "jpy_balance": 40_000.0,
            "equity_jpy": 99_999.0,
        },
        internal_state={
            "position_size_btc": 0.001,
            "jpy_balance": 49_990.0,
            "skip_balance_check": 1.0,
        },
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert result is None


def test_compare_flat_still_compares_cash_to_equity() -> None:
    """FLAT: 従来通り現金相当と equity の単純比較。"""
    match = compare_with_internal_state(
        real_state={
            "position_size_btc": 0.0,
            "jpy_balance": 45_000.0,
            "equity_jpy": 50_000.0,
        },
        internal_state={
            "position_size_btc": 0.0,
            "jpy_balance": 50_000.0,
            "comparable_equity_jpy": 50_000.0,
        },
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert match is None

    mismatch = compare_with_internal_state(
        real_state={
            "position_size_btc": 0.0,
            "jpy_balance": 45_000.0,
            "equity_jpy": 50_200.0,
        },
        internal_state={
            "position_size_btc": 0.0,
            "jpy_balance": 50_000.0,
            "comparable_equity_jpy": 50_000.0,
        },
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    )
    assert mismatch is not None
    assert mismatch["balance_diff_jpy"] == pytest.approx(200.0)


def test_get_internal_account_state_real_long_comparable_includes_unrealized() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    trader.jpy_balance = 49_990.0
    entry = 10_800_000.0
    size = 0.001
    mid = 10_950_000.0
    trader.position = PositionState(
        side="LONG",
        entry_price=entry,
        size=size,
        is_pending=False,
    )
    trader._latest_orderbook_snap = OrderbookSnapshot(
        best_bid_price=mid - 50.0,
        best_bid_size=0.5,
        best_ask_price=mid + 50.0,
        best_ask_size=0.5,
    )
    state = get_internal_account_state(trader)
    expected = 49_990.0 + (mid - entry) * size
    assert state["jpy_balance"] == pytest.approx(49_990.0)
    assert state["comparable_equity_jpy"] == pytest.approx(expected)
    assert state["position_size_btc"] == pytest.approx(0.001)
    # 現金だけだと equity と 150 円以上乖離しうるが、comparable なら一致
    assert compare_with_internal_state(
        real_state={
            "position_size_btc": 0.001,
            "jpy_balance": 1.0,
            "equity_jpy": expected,
        },
        internal_state=state,
        tolerance_btc=_TOLERANCE_BTC,
        tolerance_jpy=_TOLERANCE_JPY,
    ) is None


def test_pending_total_assets_matches_get_internal_account_state() -> None:
    """pending 中の total_assets / dashboard 計算は comparable_equity_jpy(=現金) と一致。"""
    from portfolio_metrics import compute_total_assets_from_live_state

    for mode in ("real", "virtual"):
        trader = VirtualTrader(initial_jpy=50_000.0, trading_mode=mode)
        trader.jpy_balance = 55_361.0
        entry = 10_497_169.0
        trader.position = PositionState(
            side="LONG",
            entry_price=entry,
            size=0.001,
            is_pending=True,
        )
        mid_high = 10_478_048.0
        mid_low = 10_450_000.0
        assert trader.unrealized_pnl(mid_high) == 0.0
        assert trader.total_assets(mid_high) == pytest.approx(55_361.0)
        assert trader.total_assets(mid_low) == pytest.approx(55_361.0)

        internal = get_internal_account_state(trader)
        assert internal["comparable_equity_jpy"] == pytest.approx(55_361.0)
        assert internal["position_size_btc"] == 0.0
        assert trader.total_assets(mid_high) == pytest.approx(
            internal["comparable_equity_jpy"]
        )

        live = {
            "jpy_balance": 55_361.0,
            "position_side": "LONG",
            "position_size": 0.001,
            "position_entry_price": entry,
            "position_is_pending": 1,
            "best_bid_price": mid_high - 50.0,
            "best_ask_price": mid_high + 50.0,
            "trading_mode": mode,
        }
        assert compute_total_assets_from_live_state(live) == pytest.approx(55_361.0)
        live["best_bid_price"] = mid_low - 50.0
        live["best_ask_price"] = mid_low + 50.0
        assert compute_total_assets_from_live_state(live) == pytest.approx(55_361.0)


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


def _make_snapshot(bid: float, ask: float) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        best_bid_price=bid,
        best_bid_size=1.0,
        best_ask_price=ask,
        best_ask_size=1.0,
    )


def test_entry_cooldown_blocks_same_side_within_window(trader: VirtualTrader, capsys) -> None:
    bid = 10_000_000.0
    entry_price = bid + trader.config.maker_price_offset_jpy
    cancel_at = 1_000.0
    trader._last_cancel_by_side["BUY"] = (
        entry_price,
        cancel_at,
        float(ENTRY_COOLDOWN_AFTER_CANCEL_SEC),
        CANCEL_REASON_IMBALANCE,
    )

    with patch("virtual_trader.time.time", return_value=cancel_at + 1.0):
        trader._enter_long(_make_snapshot(bid=bid, ask=bid + 100.0))

    assert trader.position.side is None
    out = capsys.readouterr().out
    assert "[SKIP] entry blocked by cooldown:" in out
    assert "side=BUY" in out


def test_entry_cooldown_allows_same_side_after_window(trader: VirtualTrader) -> None:
    bid = 10_000_000.0
    entry_price = bid + trader.config.maker_price_offset_jpy
    cancel_at = 1_000.0
    trader._last_cancel_by_side["BUY"] = (
        entry_price,
        cancel_at,
        float(ENTRY_COOLDOWN_AFTER_CANCEL_SEC),
        CANCEL_REASON_IMBALANCE,
    )

    with patch(
        "virtual_trader.time.time",
        return_value=cancel_at + ENTRY_COOLDOWN_AFTER_CANCEL_SEC + 0.1,
    ):
        trader._enter_long(_make_snapshot(bid=bid, ask=bid + 100.0))

    assert trader.position.side == "LONG"
    assert trader.position.entry_price == entry_price
    assert trader.position.is_pending is True


def test_entry_cooldown_blocks_different_price_same_side_within_window(
    trader: VirtualTrader,
) -> None:
    cancel_price = 10_000_001.0
    cancel_at = 1_000.0
    trader._last_cancel_by_side["BUY"] = (
        cancel_price,
        cancel_at,
        float(ENTRY_COOLDOWN_AFTER_CANCEL_SEC),
        CANCEL_REASON_IMBALANCE,
    )
    other_bid = 10_000_100.0

    with patch("virtual_trader.time.time", return_value=cancel_at + 0.5):
        trader._enter_long(_make_snapshot(bid=other_bid, ask=other_bid + 100.0))

    assert trader.position.side is None


def test_entry_cooldown_is_side_specific(trader: VirtualTrader) -> None:
    ask = 10_000_100.0
    sell_entry_price = ask - trader.config.maker_price_offset_jpy
    cancel_at = 1_000.0
    # BUY 方向だけキャンセル履歴がある。SELL エントリーは通る。
    trader._last_cancel_by_side["BUY"] = (
        10_000_001.0,
        cancel_at,
        float(ENTRY_COOLDOWN_AFTER_CANCEL_SEC),
        CANCEL_REASON_IMBALANCE,
    )

    with patch("virtual_trader.time.time", return_value=cancel_at + 0.5):
        trader._enter_short(_make_snapshot(bid=ask - 100.0, ask=ask))

    assert trader.position.side == "SHORT"
    assert trader.position.entry_price == sell_entry_price


def test_entry_cooldown_exact_boundary_allows_entry(trader: VirtualTrader) -> None:
    bid = 10_000_000.0
    entry_price = bid + trader.config.maker_price_offset_jpy
    cancel_at = 1_000.0
    trader._last_cancel_by_side["BUY"] = (
        entry_price,
        cancel_at,
        float(ENTRY_COOLDOWN_AFTER_CANCEL_SEC),
        CANCEL_REASON_IMBALANCE,
    )

    with patch(
        "virtual_trader.time.time",
        return_value=cancel_at + float(ENTRY_COOLDOWN_AFTER_CANCEL_SEC),
    ):
        trader._enter_long(_make_snapshot(bid=bid, ask=bid + 100.0))

    assert trader.position.side == "LONG"
    assert trader.position.entry_price == entry_price


def test_cancel_order_records_last_cancel_by_side(trader: VirtualTrader) -> None:
    bid = 10_000_000.0
    snap = _make_snapshot(bid=bid, ask=bid + 100.0)
    trader._enter_long(snap)
    assert trader.position.side == "LONG"
    entry_price = trader.position.entry_price

    with patch("virtual_trader.time.time", return_value=2_000.0):
        trader._cancel_order(snap)

    assert trader.position.side is None
    assert trader._last_cancel_by_side["BUY"] == (
        entry_price,
        2_000.0,
        float(ENTRY_COOLDOWN_AFTER_CANCEL_SEC),
        CANCEL_REASON_IMBALANCE,
    )
    assert trader._last_cancel_by_side["SELL"] is None


def test_pending_timeout_cancels_after_profile_minutes(trader: VirtualTrader) -> None:
    bid = 10_000_000.0
    # imbalance はキャンセル閾値以上を維持（時間条件のみ発火）
    snap = OrderbookSnapshot(
        best_bid_price=bid,
        best_bid_size=2.0,
        best_ask_price=bid + 100.0,
        best_ask_size=1.0,
    )
    trader._enter_long(snap)
    assert trader.position.is_pending is True
    entry = trader.position.entry_price
    trader._locked_profile_name = "daytime"
    trader._pending_order_placed_at = datetime.now() - timedelta(minutes=61)

    trader._check_pending_fill(snap)

    assert trader.position.side is None
    assert trader.trade_history[-1].reason == "CANCEL_ORDER"
    cancel = trader._last_cancel_by_side["BUY"]
    assert cancel is not None
    assert cancel[0] == entry
    assert cancel[2] == float(ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC)
    assert cancel[3] == CANCEL_REASON_TIMEOUT


def test_pending_deviation_cancels_at_half_stop_loss(trader: VirtualTrader) -> None:
    entry_bid = 10_000_000.0
    enter_snap = OrderbookSnapshot(
        best_bid_price=entry_bid,
        best_bid_size=2.0,
        best_ask_price=entry_bid + 100.0,
        best_ask_size=1.0,
    )
    trader.config.stop_loss_pct = 0.0015
    trader._enter_long(enter_snap)
    assert trader.position.is_pending is True
    entry = trader.position.entry_price
    trader._pending_order_placed_at = datetime.now()  # 時間条件は未達

    # LONG 未約定: 価格が下へ離れる（bid < entry なので約定せず、乖離のみ発火）
    threshold = trader.config.stop_loss_pct * ENTRY_PENDING_DEVIATION_SL_RATIO
    # mid = bid + 50 なので、mid が確実に閾値超えするよう bid を十分下げる
    far_bid = entry * (1.0 - threshold) - 200.0
    far_snap = OrderbookSnapshot(
        best_bid_price=far_bid,
        best_bid_size=2.0,
        best_ask_price=far_bid + 100.0,
        best_ask_size=1.0,
    )
    assert far_snap.best_bid_price < entry
    assert abs(far_snap.mid_price - entry) / entry >= threshold
    trader._check_pending_fill(far_snap)

    assert trader.position.side is None
    assert trader.trade_history[-1].reason == "CANCEL_ORDER"
    cancel = trader._last_cancel_by_side["BUY"]
    assert cancel is not None
    assert cancel[2] == float(ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC)
    assert cancel[3] == CANCEL_REASON_DEVIATION


def test_timeout_cancel_cooldown_blocks_reentry_for_5_minutes(
    trader: VirtualTrader,
) -> None:
    bid = 10_000_000.0
    snap = _make_snapshot(bid=bid, ask=bid + 100.0)
    trader._enter_long(snap)
    entry_price = trader.position.entry_price
    cancel_at = 5_000.0
    with patch("virtual_trader.time.time", return_value=cancel_at):
        trader._cancel_order(snap, cancel_reason=CANCEL_REASON_TIMEOUT)

    with patch("virtual_trader.time.time", return_value=cancel_at + 60.0):
        trader._enter_long(snap)
    assert trader.position.side is None

    with patch(
        "virtual_trader.time.time",
        return_value=cancel_at + float(ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC) + 0.1,
    ):
        trader._enter_long(snap)
    assert trader.position.side == "LONG"
    assert trader.position.entry_price == entry_price


def test_timeout_cancel_cooldown_allows_different_price_within_window(
    trader: VirtualTrader,
) -> None:
    """timeout/deviation は従来どおり同一価格のみブロック（価格が違えば再エントリー可）。"""
    cancel_price = 10_000_001.0
    cancel_at = 1_000.0
    trader._last_cancel_by_side["BUY"] = (
        cancel_price,
        cancel_at,
        float(ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC),
        CANCEL_REASON_TIMEOUT,
    )
    other_bid = 10_000_100.0
    expected_entry = other_bid + trader.config.maker_price_offset_jpy

    with patch("virtual_trader.time.time", return_value=cancel_at + 60.0):
        trader._enter_long(_make_snapshot(bid=other_bid, ask=other_bid + 100.0))

    assert trader.position.side == "LONG"
    assert trader.position.entry_price == expected_entry


def test_imbalance_cancel_keeps_5sec_side_cooldown(trader: VirtualTrader) -> None:
    bid = 10_000_000.0
    snap = _make_snapshot(bid=bid, ask=bid + 100.0)
    trader._enter_long(snap)
    entry_price = trader.position.entry_price
    cancel_at = 6_000.0
    with patch("virtual_trader.time.time", return_value=cancel_at):
        trader._cancel_order(snap, cancel_reason=CANCEL_REASON_IMBALANCE)

    assert trader._last_cancel_by_side["BUY"] == (
        entry_price,
        cancel_at,
        float(ENTRY_COOLDOWN_AFTER_CANCEL_SEC),
        CANCEL_REASON_IMBALANCE,
    )
    other_bid = bid + 50.0
    with patch("virtual_trader.time.time", return_value=cancel_at + 1.0):
        trader._enter_long(_make_snapshot(bid=other_bid, ask=other_bid + 100.0))
    assert trader.position.side is None

    with patch(
        "virtual_trader.time.time",
        return_value=cancel_at + float(ENTRY_COOLDOWN_AFTER_CANCEL_SEC) + 0.1,
    ):
        trader._enter_long(snap)
    assert trader.position.side == "LONG"


def test_imbalance_reversal_debounce_skips_flicker_cancel(trader: VirtualTrader) -> None:
    """反転がデバウンス未満で元に戻ればキャンセルしない。"""
    trader.config.imbalance_cancel_threshold = 0.5
    bid = 10_000_000.0
    # LONG 維持向け: 買い優勢
    hold_snap = OrderbookSnapshot(
        best_bid_price=bid,
        best_bid_size=2.0,
        best_ask_price=bid + 100.0,
        best_ask_size=1.0,
    )
    # LONG キャンセル向け: 売り優勢 (imbalance < 0.5)
    reverse_snap = OrderbookSnapshot(
        best_bid_price=bid,
        best_bid_size=0.2,
        best_ask_price=bid + 100.0,
        best_ask_size=2.0,
    )
    assert reverse_snap.imbalance < trader.config.imbalance_cancel_threshold
    assert hold_snap.imbalance >= trader.config.imbalance_cancel_threshold

    trader._enter_long(hold_snap)
    assert trader.position.is_pending is True
    t0 = 10_000.0
    with patch("virtual_trader.time.time", return_value=t0):
        trader._check_pending_fill(reverse_snap)
    assert trader.position.is_pending is True
    assert trader._imbalance_reversal_since == t0

    with patch(
        "virtual_trader.time.time",
        return_value=t0 + float(IMBALANCE_REVERSAL_DEBOUNCE_SEC) - 0.1,
    ):
        trader._check_pending_fill(reverse_snap)
    assert trader.position.is_pending is True

    # 反転解除 -> デバウンス破棄
    with patch(
        "virtual_trader.time.time",
        return_value=t0 + float(IMBALANCE_REVERSAL_DEBOUNCE_SEC),
    ):
        trader._check_pending_fill(hold_snap)
    assert trader.position.is_pending is True
    assert trader._imbalance_reversal_since is None
    assert all(r.reason != "CANCEL_ORDER" for r in trader.trade_history)


def test_imbalance_reversal_debounce_cancels_after_hold(trader: VirtualTrader) -> None:
    """反転がデバウンス時間以上続けばキャンセルする。"""
    trader.config.imbalance_cancel_threshold = 0.5
    bid = 10_000_000.0
    hold_snap = OrderbookSnapshot(
        best_bid_price=bid,
        best_bid_size=2.0,
        best_ask_price=bid + 100.0,
        best_ask_size=1.0,
    )
    reverse_snap = OrderbookSnapshot(
        best_bid_price=bid,
        best_bid_size=0.2,
        best_ask_price=bid + 100.0,
        best_ask_size=2.0,
    )
    trader._enter_long(hold_snap)
    t0 = 11_000.0
    with patch("virtual_trader.time.time", return_value=t0):
        trader._check_pending_fill(reverse_snap)
    assert trader.position.is_pending is True

    with patch(
        "virtual_trader.time.time",
        return_value=t0 + float(IMBALANCE_REVERSAL_DEBOUNCE_SEC),
    ):
        trader._check_pending_fill(reverse_snap)
    assert trader.position.side is None
    assert trader.trade_history[-1].reason == "CANCEL_ORDER"
    assert trader._last_imbalance_cancel_any_side_ts == pytest.approx(
        t0 + float(IMBALANCE_REVERSAL_DEBOUNCE_SEC)
    )


def test_imbalance_any_side_cooldown_blocks_opposite_direction_reentry(
    trader: VirtualTrader,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """方向が入れ替わる再エントリーも、短時間の方向不問クールダウンでブロックする。"""
    bid = 10_000_000.0
    long_snap = _make_snapshot(bid=bid, ask=bid + 100.0)
    trader._enter_long(long_snap)
    cancel_at = 12_000.0
    with patch("virtual_trader.time.time", return_value=cancel_at):
        trader._cancel_order(long_snap, cancel_reason=CANCEL_REASON_IMBALANCE)

    ask = bid + 200.0
    short_snap = _make_snapshot(bid=ask - 100.0, ask=ask)
    with patch(
        "virtual_trader.time.time",
        return_value=cancel_at
        + float(ENTRY_COOLDOWN_AFTER_IMBALANCE_CANCEL_ANY_SIDE_SEC)
        - 0.1,
    ):
        trader._enter_short(short_snap)
    assert trader.position.side is None
    out = capsys.readouterr().out
    assert "imbalance any-side cooldown" in out

    with patch(
        "virtual_trader.time.time",
        return_value=cancel_at
        + float(ENTRY_COOLDOWN_AFTER_IMBALANCE_CANCEL_ANY_SIDE_SEC)
        + 0.1,
    ):
        trader._enter_short(short_snap)
    assert trader.position.side == "SHORT"


def test_timeout_cancel_does_not_set_imbalance_any_side_cooldown(
    trader: VirtualTrader,
) -> None:
    bid = 10_000_000.0
    snap = _make_snapshot(bid=bid, ask=bid + 100.0)
    trader._enter_long(snap)
    cancel_at = 13_000.0
    with patch("virtual_trader.time.time", return_value=cancel_at):
        trader._cancel_order(snap, cancel_reason=CANCEL_REASON_TIMEOUT)
    assert trader._last_imbalance_cancel_any_side_ts is None

    ask = bid + 200.0
    with patch("virtual_trader.time.time", return_value=cancel_at + 0.5):
        trader._enter_short(_make_snapshot(bid=ask - 100.0, ask=ask))
    assert trader.position.side == "SHORT"


def test_cancel_reason_logged_for_imbalance_and_maintenance(
    trader: VirtualTrader, isolated_trade_csv_log_dir: Path
) -> None:
    bid = 10_000_000.0
    snap = _make_snapshot(bid=bid, ask=bid + 100.0)
    trader._enter_long(snap)
    cancel_at = 7_000.0
    with patch("virtual_trader.time.time", return_value=cancel_at):
        trader._cancel_order(snap, cancel_reason=CANCEL_REASON_IMBALANCE)

    with patch(
        "virtual_trader.time.time",
        return_value=cancel_at + float(ENTRY_COOLDOWN_AFTER_CANCEL_SEC) + 0.1,
    ):
        trader._enter_long(snap)
    trader._force_cancel_maintenance(snap)

    csv_files = list(isolated_trade_csv_log_dir.glob("realtime_trading_log_*.csv"))
    assert len(csv_files) == 1
    text = csv_files[0].read_text(encoding="utf-8")
    assert "cancel_reason" in text.splitlines()[0]
    assert CANCEL_REASON_IMBALANCE in text
    assert CANCEL_REASON_MAINTENANCE in text
    assert "FORCE_CANCEL_MAINTENANCE" in text


def test_pending_timeout_minutes_are_profile_specific(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        virtual_trader_module.ENTRY_PENDING_TIMEOUT_MINUTES_BY_PROFILE,
        "daytime",
        10.0,
    )
    monkeypatch.setitem(
        virtual_trader_module.ENTRY_PENDING_TIMEOUT_MINUTES_BY_PROFILE,
        "night",
        90.0,
    )
    daytime = ProfileDefinition(
        name="daytime",
        start_minute=0,
        end_minute=720,
        config=StrategyConfig(),
    )
    night = ProfileDefinition(
        name="night",
        start_minute=720,
        end_minute=1440,
        config=StrategyConfig(),
    )
    trader = VirtualTrader(profiles=[daytime, night])
    trader._locked_profile_name = "daytime"
    assert trader._pending_timeout_minutes_for_profile() == 10.0
    trader._locked_profile_name = "night"
    assert trader._pending_timeout_minutes_for_profile() == 90.0


def test_calc_trading_day_date_before_and_after_rollover() -> None:
    assert calc_trading_day_date(datetime(2026, 7, 14, 5, 59, 59)) == "2026-07-13"
    assert calc_trading_day_date(datetime(2026, 7, 14, 6, 0, 0)) == "2026-07-14"


def test_daily_loss_limit_below_threshold_does_not_stop() -> None:
    callback = MagicMock()
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        daily_loss_limit_pct=0.10,
        on_daily_loss_limit=callback,
    )
    trader.daily_start_balance = 50_000.0
    trader.daily_realized_pnl = -4_999.0

    assert trader.check_daily_loss_limit() is False
    callback.assert_not_called()


def test_daily_loss_limit_exact_threshold_triggers_stop() -> None:
    callback = MagicMock()
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        daily_loss_limit_pct=0.10,
        on_daily_loss_limit=callback,
    )
    trader.daily_start_balance = 50_000.0
    trader.daily_realized_pnl = -5_000.0

    assert trader.check_daily_loss_limit() is True
    callback.assert_called_once()
    details = callback.call_args.args[0]
    assert details["limit_jpy"] == pytest.approx(5_000.0)
    assert details["daily_realized_pnl"] == pytest.approx(-5_000.0)


def test_daily_loss_limit_above_threshold_triggers_stop() -> None:
    callback = MagicMock()
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        daily_loss_limit_pct=0.10,
        on_daily_loss_limit=callback,
    )
    trader.daily_start_balance = 50_000.0
    trader.daily_realized_pnl = -5_000.01

    assert trader.check_daily_loss_limit() is True
    callback.assert_called_once()


def test_daily_loss_limit_ignores_unrealized_open_position() -> None:
    callback = MagicMock()
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        daily_loss_limit_pct=0.10,
        on_daily_loss_limit=callback,
    )
    trader.daily_start_balance = 50_000.0
    trader.daily_realized_pnl = 0.0
    # 含み損のある未決済ポジションのみ。実現損益は更新しない。
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=False,
    )

    assert trader.check_daily_loss_limit() is False
    callback.assert_not_called()
    assert trader.daily_realized_pnl == 0.0


def test_initialize_daily_loss_state_carries_over_same_trading_day() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0)
    trader.jpy_balance = 40_000.0  # 再起動時点の残高が違っても上書きしない
    now = datetime(2026, 7, 14, 12, 0, 0)
    trader.initialize_daily_loss_state(
        persisted_trading_day_date="2026-07-14",
        persisted_daily_start_balance=48_000.0,
        persisted_daily_realized_pnl=-1_234.0,
        persisted_daily_win_count=2,
        persisted_daily_loss_count=1,
        now=now,
    )

    assert trader.trading_day_date == "2026-07-14"
    assert trader.daily_start_balance == pytest.approx(48_000.0)
    assert trader.daily_realized_pnl == pytest.approx(-1_234.0)
    assert trader._daily_win_count == 2
    assert trader._daily_loss_count == 1


def test_initialize_daily_loss_state_resets_on_new_trading_day() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0)
    trader.jpy_balance = 42_000.0
    trader._daily_win_count = 5
    trader._daily_loss_count = 3
    now = datetime(2026, 7, 14, 6, 0, 0)  # 新サイクル開始
    trader.initialize_daily_loss_state(
        persisted_trading_day_date="2026-07-13",
        persisted_daily_start_balance=48_000.0,
        persisted_daily_realized_pnl=-2_000.0,
        persisted_daily_win_count=5,
        persisted_daily_loss_count=3,
        now=now,
    )

    assert trader.trading_day_date == "2026-07-14"
    assert trader.daily_start_balance == pytest.approx(42_000.0)
    assert trader.daily_realized_pnl == pytest.approx(0.0)
    assert trader._daily_win_count == 0
    assert trader._daily_loss_count == 0


def test_update_kpi_increments_daily_win_and_loss_counts() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0)
    trader._update_kpi(100.0)
    trader._update_kpi(-50.0)
    trader._update_kpi(0.0)
    assert trader._win_count == 1
    assert trader._loss_count == 1
    assert trader._daily_win_count == 1
    assert trader._daily_loss_count == 1
    assert trader._cumulative_pnl == pytest.approx(50.0)


def test_restore_persisted_account_state_overwrites_when_values_present() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0)
    trader.restore_persisted_account_state(
        jpy_balance=47_500.0,
        win_count=3,
        loss_count=2,
        total_gross_win=1_200.0,
        total_gross_loss=800.0,
        cumulative_pnl=400.0,
    )
    assert trader.jpy_balance == pytest.approx(47_500.0)
    assert trader._win_count == 3
    assert trader._loss_count == 2
    assert trader._total_gross_win == pytest.approx(1_200.0)
    assert trader._total_gross_loss == pytest.approx(800.0)
    assert trader._cumulative_pnl == pytest.approx(400.0)


def test_restore_persisted_account_state_keeps_defaults_when_none() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0)
    trader.restore_persisted_account_state(
        jpy_balance=None,
        win_count=None,
        loss_count=None,
        total_gross_win=None,
        total_gross_loss=None,
        cumulative_pnl=None,
    )
    assert trader.jpy_balance == pytest.approx(50_000.0)
    assert trader._win_count == 0
    assert trader._loss_count == 0
    assert trader._total_gross_win == pytest.approx(0.0)
    assert trader._total_gross_loss == pytest.approx(0.0)
    assert trader._cumulative_pnl == pytest.approx(0.0)


def test_restore_then_new_trading_day_uses_restored_jpy_balance() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0)
    trader.restore_persisted_account_state(jpy_balance=42_000.0)
    now = datetime(2026, 7, 14, 6, 0, 0)
    trader.initialize_daily_loss_state(
        persisted_trading_day_date="2026-07-13",
        persisted_daily_start_balance=48_000.0,
        persisted_daily_realized_pnl=-2_000.0,
        now=now,
    )
    assert trader.daily_start_balance == pytest.approx(42_000.0)
    assert trader.daily_realized_pnl == pytest.approx(0.0)


def test_stop_loss_updates_daily_realized_pnl_and_may_trigger() -> None:
    callback = MagicMock()
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        daily_loss_limit_pct=0.10,
        on_daily_loss_limit=callback,
    )
    trader.daily_start_balance = 50_000.0
    trader.daily_realized_pnl = -4_900.0
    # STOP_LOSS で追加損失を出して閾値超過にする（LONG entry high, exit lower）
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.001,
        is_pending=False,
    )
    trader._position_filled_at = datetime(2026, 7, 14, 12, 0, 0)
    snap = _make_snapshot(bid=9_900_000.0, ask=9_900_100.0)

    trader._exit_stop_loss(snap)

    assert trader.daily_realized_pnl < -5_000.0
    callback.assert_called_once()
    assert trader.position.side is None


def test_force_close_maintenance_updates_daily_realized_pnl() -> None:
    callback = MagicMock()
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        daily_loss_limit_pct=0.10,
        on_daily_loss_limit=callback,
    )
    trader.daily_start_balance = 50_000.0
    trader.daily_realized_pnl = 0.0
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.001,
        is_pending=False,
    )
    trader._position_filled_at = datetime(2026, 7, 14, 12, 0, 0)
    snap = _make_snapshot(bid=9_950_000.0, ask=9_950_100.0)

    before = trader.daily_realized_pnl
    trader._force_close_maintenance(snap)

    assert trader.position.side is None
    assert trader.daily_realized_pnl < before
    # 単独では閾値 (-5000) に届かない想定
    assert trader.daily_realized_pnl > -5_000.0
    callback.assert_not_called()


def test_force_close_maintenance_alone_can_trigger_daily_loss_limit() -> None:
    callback = MagicMock()
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        daily_loss_limit_pct=0.10,
        on_daily_loss_limit=callback,
    )
    trader.daily_start_balance = 50_000.0
    trader.daily_realized_pnl = -4_900.0
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.001,
        is_pending=False,
    )
    trader._position_filled_at = datetime(2026, 7, 14, 12, 0, 0)
    snap = _make_snapshot(bid=9_900_000.0, ask=9_900_100.0)

    trader._force_close_maintenance(snap)

    assert trader.daily_realized_pnl < -5_000.0
    callback.assert_called_once()
    assert trader.position.side is None


def test_force_close_maintenance_combined_with_prior_stop_loss_triggers_limit() -> None:
    callback = MagicMock()
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        daily_loss_limit_pct=0.10,
        on_daily_loss_limit=callback,
    )
    trader.daily_start_balance = 50_000.0
    trader.daily_realized_pnl = 0.0

    # 先に STOP_LOSS で一部損失を計上（閾値未満）
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.03,
        is_pending=False,
    )
    trader._position_filled_at = datetime(2026, 7, 14, 11, 0, 0)
    trader._exit_stop_loss(_make_snapshot(bid=9_900_000.0, ask=9_900_100.0))
    assert callback.call_count == 0
    after_stop = trader.daily_realized_pnl
    assert after_stop < 0.0
    assert after_stop > -5_000.0

    # FORCE_CLOSE で合算して閾値超過
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.03,
        is_pending=False,
    )
    trader._position_filled_at = datetime(2026, 7, 14, 12, 0, 0)
    trader._force_close_maintenance(_make_snapshot(bid=9_900_000.0, ask=9_900_100.0))

    assert trader.daily_realized_pnl < after_stop
    assert trader.daily_realized_pnl <= -5_000.0
    callback.assert_called_once()


@pytest.fixture
def isolated_manual_stop_flag(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    flag = tmp_path / "manual_stop.flag"
    reason = tmp_path / "manual_stop_reason.json"
    monkeypatch.setattr(trading_engine, "MANUAL_STOP_FLAG_PATH", flag)
    monkeypatch.setattr(trading_engine, "MANUAL_STOP_REASON_PATH", reason)
    return flag


def _engine_on_reconciliation_mismatch(details: dict) -> None:
    _trigger_safety_stop(
        "reconciliation_mismatch",
        {
            "position_diff_btc": details["position_diff_btc"],
            "real_position_size_btc": details["real_position_size_btc"],
            "internal_position_size_btc": details["internal_position_size_btc"],
            "balance_diff_jpy": details["balance_diff_jpy"],
            "real_jpy_balance": details["real_jpy_balance"],
            "internal_jpy_balance": details["internal_jpy_balance"],
        },
    )


def _engine_on_daily_loss_limit(details: dict) -> None:
    _trigger_safety_stop(
        "daily_loss_limit",
        {
            "daily_realized_pnl": details["daily_realized_pnl"],
            "daily_start_balance": details["daily_start_balance"],
            "daily_loss_limit_pct": details["daily_loss_limit_pct"],
            "limit_jpy": details["limit_jpy"],
        },
    )


@patch("virtual_trader.get_internal_account_state", return_value=_INTERNAL_STATE)
@patch("virtual_trader.fetch_real_account_state", return_value=_MISMATCH_REAL_STATE)
def test_reconciliation_mismatch_notifies_via_trigger_safety_stop(
    _mock_fetch: MagicMock,
    _mock_internal: MagicMock,
    trader: VirtualTrader,
    isolated_manual_stop_flag: Path,
) -> None:
    """3.4.2: 2回連続不一致 -> _trigger_safety_stop 経由で Telegram 通知する。"""
    with patch("trading_engine.send_telegram_message") as mock_send:
        run_reconciliation_check(
            trader=trader,
            tolerance_btc=_TOLERANCE_BTC,
            tolerance_jpy=_TOLERANCE_JPY,
            pending_mismatch=[True],
            on_confirmed_mismatch=_engine_on_reconciliation_mismatch,
        )
    assert isolated_manual_stop_flag.exists()
    mock_send.assert_called_once()
    message = mock_send.call_args.args[0]
    assert "reason=reconciliation_mismatch" in message
    assert "position_diff_btc=" in message
    assert "triggered_at=" in message


def test_daily_loss_limit_notifies_via_trigger_safety_stop(
    isolated_manual_stop_flag: Path,
) -> None:
    """3.4.8: 日次損失上限到達 -> _trigger_safety_stop 経由で Telegram 通知する。"""
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        daily_loss_limit_pct=0.10,
        on_daily_loss_limit=_engine_on_daily_loss_limit,
    )
    trader.daily_start_balance = 50_000.0
    trader.daily_realized_pnl = -5_000.0

    with patch("trading_engine.send_telegram_message") as mock_send:
        assert trader.check_daily_loss_limit() is True

    assert isolated_manual_stop_flag.exists()
    mock_send.assert_called_once()
    message = mock_send.call_args.args[0]
    assert "reason=daily_loss_limit" in message
    assert "daily_realized_pnl=-5000.0" in message
    assert "daily_start_balance=50000.0" in message
    assert "daily_loss_limit_pct=0.1" in message


def test_safety_stop_skips_telegram_when_flag_already_exists(
    isolated_manual_stop_flag: Path,
) -> None:
    isolated_manual_stop_flag.write_text("already-stopped", encoding="utf-8")
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        daily_loss_limit_pct=0.10,
        on_daily_loss_limit=_engine_on_daily_loss_limit,
    )
    trader.daily_start_balance = 50_000.0
    trader.daily_realized_pnl = -5_000.0

    with patch("trading_engine.send_telegram_message") as mock_send:
        assert trader.check_daily_loss_limit() is True
        _trigger_safety_stop("order_rate_limit", {"order_rate_limit_per_minute": 5})

    mock_send.assert_not_called()
    assert isolated_manual_stop_flag.read_text(encoding="utf-8") == "already-stopped"


def test_safety_stop_creates_flag_even_if_telegram_raises(
    isolated_manual_stop_flag: Path,
) -> None:
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        daily_loss_limit_pct=0.10,
        on_daily_loss_limit=_engine_on_daily_loss_limit,
    )
    trader.daily_start_balance = 50_000.0
    trader.daily_realized_pnl = -5_000.0

    with patch(
        "trading_engine.send_telegram_message",
        side_effect=RuntimeError("telegram down"),
    ) as mock_send:
        assert trader.check_daily_loss_limit() is True

    mock_send.assert_called_once()
    assert isolated_manual_stop_flag.exists()


def test_restore_pending_without_placed_at_keeps_none(capsys) -> None:
    trader = VirtualTrader(initial_jpy=50_000.0)
    result = trader.restore_persisted_position(
        position_side="LONG",
        position_entry_price=10_000_001.0,
        position_size=0.001,
        position_is_pending=1,
        position_exit_target=0.0,
        pending_order_placed_at=None,
    )
    assert result["status"] == "restored"
    assert trader.position.is_pending is True
    assert trader._pending_order_placed_at is None
    out = capsys.readouterr().out
    assert "pending order placed_at could not be restored" in out
    assert "time-based entry timeout will be skipped" in out


def test_restore_pending_invalid_placed_at_keeps_none(capsys) -> None:
    trader = VirtualTrader(initial_jpy=50_000.0)
    result = trader.restore_persisted_position(
        position_side="LONG",
        position_entry_price=10_000_001.0,
        position_size=0.001,
        position_is_pending=1,
        pending_order_placed_at="not-a-timestamp",
    )
    assert result["status"] == "restored"
    assert trader._pending_order_placed_at is None
    out = capsys.readouterr().out
    assert "pending order placed_at could not be restored" in out


def test_restore_pending_missing_placed_at_still_allows_deviation_cancel(
    trader: VirtualTrader,
) -> None:
    trader.config.stop_loss_pct = 0.0015
    trader.restore_persisted_position(
        position_side="LONG",
        position_entry_price=10_000_001.0,
        position_size=0.001,
        position_is_pending=1,
        pending_order_placed_at=None,
    )
    assert trader._pending_order_placed_at is None
    entry = trader.position.entry_price
    threshold = trader.config.stop_loss_pct * ENTRY_PENDING_DEVIATION_SL_RATIO
    far_bid = entry * (1.0 - threshold) - 200.0
    far_snap = OrderbookSnapshot(
        best_bid_price=far_bid,
        best_bid_size=2.0,
        best_ask_price=far_bid + 100.0,
        best_ask_size=1.0,
    )
    _, _, time_met, deviation_met = trader._pending_timeout_conditions(far_snap)
    assert time_met is False
    assert deviation_met is True
    trader._check_pending_fill(far_snap)
    assert trader.position.side is None


def test_restore_persisted_position_long_restart() -> None:
    alerts: list[str] = []
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        on_critical_alert=alerts.append,
    )
    trader.restore_persisted_account_state(
        jpy_balance=39_200.0,
        cumulative_pnl=50.0,
    )
    result = trader.restore_persisted_position(
        position_side="LONG",
        position_entry_price=10_800_000.0,
        position_size=0.001,
        position_is_pending=0,
        position_exit_target=10_812_960.0,
        position_filled_at="2026-07-22T12:00:00",
    )
    assert result["status"] == "restored"
    assert trader.position.side == "LONG"
    assert trader.position.entry_price == pytest.approx(10_800_000.0)
    assert trader.position.size == pytest.approx(0.001)
    assert trader.position.is_pending is False
    assert trader.position.exit_price_target == pytest.approx(10_812_960.0)
    assert trader._position_filled_at == datetime(2026, 7, 22, 12, 0, 0)
    assert trader._locked_config is not None
    integrity = trader.check_account_integrity(mid_price=10_800_000.0)
    assert integrity["ok"] is True
    assert alerts == []


def test_restore_persisted_position_short_restart() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0)
    trader.restore_persisted_account_state(
        jpy_balance=50_080.0,
        cumulative_pnl=80.0,
    )
    result = trader.restore_persisted_position(
        position_side="SHORT",
        position_entry_price=10_700_000.0,
        position_size=0.001,
        position_is_pending=False,
        position_exit_target=10_687_160.0,
        position_filled_at="2026-07-23T10:00:00",
    )
    assert result["status"] == "restored"
    assert trader.position.side == "SHORT"
    assert trader.position.size == pytest.approx(0.001)
    assert trader.check_account_integrity(mid_price=10_700_000.0)["ok"] is True


def test_restore_persisted_position_fallback_alerts_when_refund_unknown() -> None:
    alerts: list[str] = []
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        on_critical_alert=alerts.append,
    )
    trader.restore_persisted_account_state(
        jpy_balance=39_200.0,
        cumulative_pnl=50.0,
    )
    # side=LONG だが entry 欠損 -> refund 不可、推定損失をアラート
    result = trader.restore_persisted_position(
        position_side="LONG",
        position_entry_price=0.0,
        position_size=0.001,
        position_is_pending=0,
    )
    assert result["status"] == "fallback"
    assert trader.position.side is None
    assert trader.jpy_balance == pytest.approx(39_200.0)
    assert result["estimated_loss_jpy"] == pytest.approx(10_850.0)
    assert alerts and "position restore failed" in alerts[0]


def test_restore_persisted_position_fallback_refunds_locked_long_capital() -> None:
    """entry+size が分かる場合、フォールバックで LONG 拘束を現金へ戻す。"""
    alerts: list[str] = []
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        on_critical_alert=alerts.append,
    )
    trader.jpy_balance = 39_200.0
    trader._cumulative_pnl = 50.0
    result = trader._fallback_release_locked_capital(
        reason="unit_test",
        position_side="LONG",
        position_entry_price=10_800_000.0,
        position_size=0.001,
    )
    assert result["status"] == "fallback"
    assert trader.jpy_balance == pytest.approx(39_200.0 + 10_800.0)
    assert result["refunded_jpy"] == pytest.approx(10_800.0)
    assert result["estimated_loss_jpy"] == pytest.approx(0.0)
    assert alerts and "position restore failed" in alerts[0]


def test_fallback_release_locked_capital_real_long_does_not_refund_notional() -> None:
    alerts: list[str] = []
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        trading_mode="real",
        on_critical_alert=alerts.append,
    )
    trader.jpy_balance = 49_900.0
    trader._cumulative_pnl = 80.0
    before = trader.jpy_balance
    result = trader._fallback_release_locked_capital(
        reason="unit_test_real",
        position_side="LONG",
        position_entry_price=10_800_000.0,
        position_size=0.001,
    )
    assert result["status"] == "fallback"
    assert trader.jpy_balance == pytest.approx(before)
    assert result["refunded_jpy"] == pytest.approx(0.0)
    # expected_flat = 50000 + 80 = 50080, balance = 49900 -> loss 180
    assert result["estimated_loss_jpy"] == pytest.approx(180.0)
    assert alerts and "position restore failed" in alerts[0]


def test_check_account_integrity_detects_gap_and_alerts() -> None:
    alerts: list[str] = []
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        on_critical_alert=alerts.append,
    )
    trader.restore_persisted_account_state(
        jpy_balance=28_500.0,
        cumulative_pnl=80.0,
    )
    result = trader.check_account_integrity(tolerance_jpy=3_000.0)
    assert result["ok"] is False
    assert result["jpy_gap"] == pytest.approx(28_500.0 - (50_000.0 + 80.0))
    assert alerts and "account integrity check failed" in alerts[0]


def test_check_account_integrity_compares_last_total_assets() -> None:
    alerts: list[str] = []
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        on_critical_alert=alerts.append,
    )
    trader.restore_persisted_account_state(jpy_balance=50_080.0, cumulative_pnl=80.0)
    # 会計は一致するが、前回総資産と大きく乖離
    result = trader.check_account_integrity(
        mid_price=10_000_000.0,
        last_total_assets=40_000.0,
        tolerance_jpy=3_000.0,
    )
    assert result["ok"] is False
    assert result["last_total_gap"] == pytest.approx(10_080.0)
    assert alerts and "account integrity check failed" in alerts[0]


def test_real_long_total_assets_excludes_notional() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    trader.jpy_balance = 49_990.0
    entry = 10_800_000.0
    size = 0.001
    mid = 10_838_500.0
    trader.position = PositionState(
        side="LONG",
        entry_price=entry,
        size=size,
        is_pending=False,
    )
    assert trader.total_assets(mid) == pytest.approx(
        49_990.0 + (mid - entry) * size
    )
    assert trader.unrealized_pnl(mid) == pytest.approx((mid - entry) * size)


def test_virtual_long_total_assets_keeps_notional_mark() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    trader.jpy_balance = 39_046.0
    size = 0.001
    mid = 10_838_500.0
    trader.position = PositionState(
        side="LONG",
        entry_price=10_800_000.0,
        size=size,
        is_pending=False,
    )
    assert trader.total_assets(mid) == pytest.approx(39_046.0 + size * mid)


def test_real_short_total_assets_unchanged_from_virtual_formula() -> None:
    entry = 10_800_000.0
    size = 0.001
    mid = 10_790_000.0
    for mode in ("real", "virtual"):
        trader = VirtualTrader(initial_jpy=50_000.0, trading_mode=mode)
        trader.jpy_balance = 50_000.0
        trader.position = PositionState(
            side="SHORT",
            entry_price=entry,
            size=size,
            is_pending=False,
        )
        assert trader.total_assets(mid) == pytest.approx(
            50_000.0 + (entry - mid) * size
        )


def test_account_integrity_real_long_uses_unrealized_total_assets() -> None:
    """口座整合性の total_assets 比較も real LONG では含み損益のみを使う。"""
    alerts: list[str] = []
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        trading_mode="real",
        on_critical_alert=alerts.append,
    )
    trader.jpy_balance = 49_990.0
    trader._cumulative_pnl = -10.0
    entry = 10_800_000.0
    size = 0.001
    mid = 10_838_500.0
    trader.position = PositionState(
        side="LONG",
        entry_price=entry,
        size=size,
        is_pending=False,
    )
    expected_total = 49_990.0 + (mid - entry) * size
    result = trader.check_account_integrity(
        mid_price=mid,
        last_total_assets=expected_total,
        tolerance_jpy=3_000.0,
    )
    assert result["current_total_assets"] == pytest.approx(expected_total)
    assert result["last_total_gap"] == pytest.approx(0.0)
    assert result["expected_jpy_balance"] == pytest.approx(50_000.0 - 10.0)
    assert result["ok"] is True

    old_style = 49_990.0 + size * mid
    result_old = trader.check_account_integrity(
        mid_price=mid,
        last_total_assets=old_style,
        tolerance_jpy=3_000.0,
    )
    assert result_old["current_total_assets"] == pytest.approx(expected_total)
    assert abs(float(result_old["last_total_gap"])) > 3_000.0
    assert result_old["ok"] is False


def test_expected_jpy_balance_real_long_returns_flat() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    trader._cumulative_pnl = -10.0
    trader.position = PositionState(
        side="LONG",
        entry_price=10_800_000.0,
        size=0.001,
        is_pending=False,
    )
    assert trader.expected_jpy_balance() == pytest.approx(50_000.0 - 10.0)


def test_expected_jpy_balance_virtual_long_subtracts_notional() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    trader._cumulative_pnl = 50.0
    entry = 10_800_000.0
    size = 0.001
    trader.position = PositionState(
        side="LONG",
        entry_price=entry,
        size=size,
        is_pending=False,
    )
    flat = 50_000.0 + 50.0
    assert trader.expected_jpy_balance() == pytest.approx(flat - entry * size)


def test_check_account_integrity_real_long_no_false_notional_gap() -> None:
    """real LONG 保有中に想定元本分の誤ギャップで安全停止しない。"""
    alerts: list[str] = []
    trader = VirtualTrader(
        initial_jpy=50_000.0,
        trading_mode="real",
        on_critical_alert=alerts.append,
    )
    # fee のみ減算した帳簿（想定元本は拘束していない）
    trader.jpy_balance = 49_990.0
    trader._cumulative_pnl = -10.0
    trader.position = PositionState(
        side="LONG",
        entry_price=10_800_000.0,
        size=0.001,
        is_pending=False,
    )
    result = trader.check_account_integrity(
        mid_price=10_838_500.0,
        tolerance_jpy=3_000.0,
    )
    assert result["expected_jpy_balance"] == pytest.approx(49_990.0)
    assert result["jpy_gap"] == pytest.approx(0.0)
    assert result["ok"] is True
    assert alerts == []
    # 旧式なら expected が ~39,190 になり約1万円のギャップになる
    old_expected = (50_000.0 - 10.0) - (10_800_000.0 * 0.001)
    assert abs(float(trader.jpy_balance) - old_expected) > 3_000.0
