"""check_account_integrity の比較基準（real / virtual）を検証する。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULE_DIR = ROOT / "btc_trading_tool"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from virtual_trader import (  # noqa: E402
    ACCOUNT_INTEGRITY_TOLERANCE_JPY,
    PositionState,
    VirtualTrader,
    calc_trading_day_date,
)


@pytest.fixture(autouse=True)
def _isolate_virtual_trader_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("virtual_trader.LOG_DIR", log_dir)
    return log_dir


def test_calc_trading_day_date_rollover_at_06() -> None:
    assert calc_trading_day_date(datetime(2026, 8, 2, 5, 59, 59)) == "2026-08-01"
    assert calc_trading_day_date(datetime(2026, 8, 2, 6, 0, 0)) == "2026-08-02"


def test_virtual_integrity_uses_initial_plus_cumulative() -> None:
    """virtual は従来どおり initial_jpy + cumulative_pnl（LONG は notional 差し引き）。"""
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    trader.jpy_balance = 39_250.0
    trader._cumulative_pnl = 50.0
    # 日次側をずらしても virtual では無視されること
    trader.daily_start_balance = 55_518.0
    trader.daily_realized_pnl = 999.0
    trader.position = PositionState(
        side="LONG",
        entry_price=10_800_000.0,
        size=0.001,
        is_pending=False,
        exit_price_target=10_812_960.0,
    )

    result = trader.check_account_integrity(mid_price=10_801_000.0)
    # expected = 50000 + 50 - 10800 = 39250
    assert result["expected_jpy_balance"] == pytest.approx(39_250.0)
    assert result["jpy_gap"] == pytest.approx(0.0)
    assert result["ok"] is True


def test_real_integrity_uses_daily_start_plus_daily_realized() -> None:
    """real は daily_start + daily_realized。stale な initial_jpy では誤検知しない。"""
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    trader.jpy_balance = 55_518.0
    trader._cumulative_pnl = 0.0
    trader.daily_start_balance = 55_518.0
    trader.daily_realized_pnl = 0.0

    result = trader.check_account_integrity()
    assert result["expected_jpy_balance"] == pytest.approx(55_518.0)
    assert result["jpy_gap"] == pytest.approx(0.0)
    assert result["ok"] is True

    # 旧基準 (initial + cum = 50000) なら gap=5518 でアラートになる値
    old_gap = trader.jpy_balance - (trader._initial_jpy + trader._cumulative_pnl)
    assert abs(old_gap) > ACCOUNT_INTEGRITY_TOLERANCE_JPY


def test_real_integrity_long_does_not_subtract_notional() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    trader.jpy_balance = 55_000.0
    trader.daily_start_balance = 55_000.0
    trader.daily_realized_pnl = 0.0
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.01,
        is_pending=False,
        exit_price_target=10_012_000.0,
    )

    result = trader.check_account_integrity(mid_price=10_005_000.0)
    assert result["expected_jpy_balance"] == pytest.approx(55_000.0)
    assert result["ok"] is True


def test_real_integrity_after_trading_day_rollover() -> None:
    """trading_day_date が変わると daily_start が jpy から再設定され、整合性が取れる。"""
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    trader.jpy_balance = 60_000.0
    trader._cumulative_pnl = 10_000.0  # 入金相当が cum に載っていないケース想定

    # 前日サイクルとして保持されていた状態
    trader.initialize_daily_loss_state(
        persisted_trading_day_date="2026-08-01",
        persisted_daily_start_balance=54_000.0,
        persisted_daily_realized_pnl=200.0,
        now=datetime(2026, 8, 1, 12, 0, 0),
    )
    assert trader.trading_day_date == "2026-08-01"
    assert trader.daily_start_balance == pytest.approx(54_000.0)
    assert trader.daily_realized_pnl == pytest.approx(200.0)

    # 同日中: expected = 54000 + 200 = 54200, jpy=60000 -> gap > tolerance
    same_day = trader.check_account_integrity()
    assert same_day["expected_jpy_balance"] == pytest.approx(54_200.0)
    assert abs(same_day["jpy_gap"]) > ACCOUNT_INTEGRITY_TOLERANCE_JPY
    assert same_day["ok"] is False

    # 06:00 以降の新サイクル: daily_start = 現在 jpy, daily_realized = 0
    trader.initialize_daily_loss_state(
        persisted_trading_day_date="2026-08-01",
        persisted_daily_start_balance=54_000.0,
        persisted_daily_realized_pnl=200.0,
        now=datetime(2026, 8, 2, 6, 0, 0),
    )
    assert trader.trading_day_date == "2026-08-02"
    assert trader.daily_start_balance == pytest.approx(60_000.0)
    assert trader.daily_realized_pnl == pytest.approx(0.0)

    after_rollover = trader.check_account_integrity()
    assert after_rollover["expected_jpy_balance"] == pytest.approx(60_000.0)
    assert after_rollover["jpy_gap"] == pytest.approx(0.0)
    assert after_rollover["ok"] is True


def test_real_integrity_same_day_carry_includes_daily_realized() -> None:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="real")
    trader.jpy_balance = 55_300.0
    trader.initialize_daily_loss_state(
        persisted_trading_day_date="2026-08-02",
        persisted_daily_start_balance=55_518.0,
        persisted_daily_realized_pnl=-218.0,
        now=datetime(2026, 8, 2, 15, 0, 0),
    )

    result = trader.check_account_integrity()
    assert result["expected_jpy_balance"] == pytest.approx(55_300.0)
    assert result["ok"] is True
