"""
tests/test_equity_guard.py

equity ガード（止血ルール、2026-11-06）の単体テスト。
- 起点は最初の実測 equity で1回だけ取得し、自動で付け替えない
- equity <= 起点 - 5,000円 で停止し、自動では再開しない
- 停止は新規エントリー遮断のみで、強制決済はしない
- API 取得失敗時は発動しない
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

_ROOT_DIR = Path(__file__).resolve().parent.parent
_BTC_DIR = _ROOT_DIR / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as virtual_trader_module  # noqa: E402
from equity_guard import EquityGuard  # noqa: E402
from virtual_trader import VirtualTrader  # noqa: E402
from strategy_logic import OrderbookSnapshot, PositionState  # noqa: E402

# メンテナンス枠（毎日 05:55-06:30 / 土曜 09:00-11:10）にかからない固定時刻
_FIXED_NOW = datetime(2026, 7, 15, 12, 0, 0)


# --------------------------------------------------------------------- #
#  EquityGuard 単体                                                      #
# --------------------------------------------------------------------- #


def test_baseline_captured_once_on_first_evaluation(tmp_path: Path) -> None:
    guard = EquityGuard(tmp_path / "equity_guard.json")
    assert guard.evaluate(43_887.0, position_open=False) is False

    state = guard.state_snapshot()
    assert state is not None
    assert state["baseline_equity"] == 43_887.0
    assert state["stop_equity"] == 38_887.0
    assert state["state"] == "armed"
    assert state["history"][0]["event"] == "armed"

    # 2回目の観測では起点を付け替えない
    assert guard.evaluate(43_000.0, position_open=True) is False
    state = guard.state_snapshot()
    assert state is not None
    assert state["baseline_equity"] == 43_887.0
    assert state["last_equity"] == 43_000.0


def test_trigger_at_stop_equity_and_no_auto_rearm(tmp_path: Path) -> None:
    guard = EquityGuard(tmp_path / "equity_guard.json")
    guard.evaluate(43_887.0, position_open=False)

    assert guard.evaluate(38_888.0, position_open=False) is False
    assert guard.is_stopped() is False

    # 停止ラインちょうどで発動する
    assert guard.evaluate(38_887.0, position_open=False) is True
    assert guard.is_stopped() is True
    state = guard.state_snapshot()
    assert state is not None
    assert state["stopped_at"] is not None

    # equity が回復しても自動では再開しない
    assert guard.evaluate(43_887.0, position_open=False) is False
    assert guard.is_stopped() is True


def test_clear_rearms_without_moving_baseline(tmp_path: Path) -> None:
    guard = EquityGuard(tmp_path / "equity_guard.json")
    guard.evaluate(43_887.0, position_open=False)
    guard.evaluate(38_000.0, position_open=False)
    assert guard.is_stopped() is True

    state = guard.clear()
    assert state is not None
    assert state["state"] == "armed"
    assert state["baseline_equity"] == 43_887.0
    assert state["stop_equity"] == 38_887.0
    events = [h["event"] for h in state["history"]]
    assert events == ["armed", "stopped", "cleared"]


def test_rebaseline_moves_baseline_as_explicit_operation(tmp_path: Path) -> None:
    guard = EquityGuard(tmp_path / "equity_guard.json")
    guard.evaluate(43_887.0, position_open=False)
    guard.evaluate(38_000.0, position_open=False)

    state = guard.rebaseline(50_000.0, position_open=True)
    assert state is not None
    assert state["baseline_equity"] == 50_000.0
    assert state["stop_equity"] == 45_000.0
    assert state["state"] == "armed"
    assert state["baseline_position_open"] is True
    events = [h["event"] for h in state["history"]]
    assert events == ["armed", "stopped", "rebaselined"]


def test_state_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "equity_guard.json"
    guard = EquityGuard(path)
    guard.evaluate(43_887.0, position_open=False)
    guard.evaluate(38_000.0, position_open=False)

    reloaded = EquityGuard(path)
    assert reloaded.is_stopped() is True
    state = reloaded.state_snapshot()
    assert state is not None
    assert state["baseline_equity"] == 43_887.0


def test_invalid_state_file_never_triggers_and_is_not_overwritten(
    tmp_path: Path,
) -> None:
    path = tmp_path / "equity_guard.json"
    path.write_text("not json", encoding="utf-8")
    guard = EquityGuard(path)

    assert guard.evaluate(1.0, position_open=False) is False
    assert guard.is_stopped() is False
    assert path.read_text(encoding="utf-8") == "not json"


def test_custom_drawdown(tmp_path: Path) -> None:
    guard = EquityGuard(tmp_path / "equity_guard.json", drawdown_jpy=3_000.0)
    guard.evaluate(10_000.0, position_open=False)
    state = guard.state_snapshot()
    assert state is not None
    assert state["stop_equity"] == 7_000.0


# --------------------------------------------------------------------- #
#  VirtualTrader 統合                                                    #
# --------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(virtual_trader_module, "LOG_DIR", log_dir)
    monkeypatch.setattr(
        virtual_trader_module,
        "_MANUAL_STOP_FLAG_PATH",
        tmp_path / "manual_stop.flag",
    )
    guard_path = tmp_path / "equity_guard.json"
    monkeypatch.setattr(
        virtual_trader_module, "_EQUITY_GUARD_STATE_PATH", guard_path
    )
    return guard_path


def _real_trader(alerts: List[str]) -> VirtualTrader:
    return VirtualTrader(
        trading_mode="real",
        on_critical_alert=lambda msg: alerts.append(msg),
    )


def test_trader_guard_trigger_blocks_new_entries(_isolated_paths: Path) -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)

    assert trader._is_entry_blocked(_FIXED_NOW) is False
    assert (
        trader.evaluate_equity_guard(
            {"equity_jpy": 43_887.0, "position_size_btc": 0.0}
        )
        is False
    )
    assert trader._is_entry_blocked(_FIXED_NOW) is False

    assert (
        trader.evaluate_equity_guard(
            {"equity_jpy": 38_800.0, "position_size_btc": 0.0}
        )
        is True
    )
    assert trader._is_entry_blocked(_FIXED_NOW) is True
    assert len(alerts) == 1
    assert "equity guard" in alerts[0]
    assert "38,887" in alerts[0]

    # 再評価してもアラートは繰り返さない
    assert (
        trader.evaluate_equity_guard(
            {"equity_jpy": 38_000.0, "position_size_btc": 0.0}
        )
        is False
    )
    assert len(alerts) == 1


def test_trader_guard_not_applied_in_virtual_mode(
    _isolated_paths: Path,
) -> None:
    guard = EquityGuard(_isolated_paths)
    guard.evaluate(43_887.0, position_open=False)
    guard.evaluate(38_000.0, position_open=False)
    assert guard.is_stopped() is True

    trader = VirtualTrader(trading_mode="virtual")
    assert trader._is_entry_blocked(_FIXED_NOW) is False
    assert (
        trader.evaluate_equity_guard(
            {"equity_jpy": 1.0, "position_size_btc": 0.0}
        )
        is False
    )


def test_post_settlement_sync_triggers_guard(_isolated_paths: Path) -> None:
    alerts: List[str] = []
    trader = _real_trader(alerts)
    trader.evaluate_equity_guard({"equity_jpy": 43_887.0, "position_size_btc": 0.0})

    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 38_500.0,
            "equity_jpy": 38_500.0,
            "position_size_btc": 0.0,
        },
    ):
        trader._sync_jpy_balance_from_equity_unlocked(context="TEST")

    assert trader.jpy_balance == 38_500.0
    assert trader._equity_guard.is_stopped() is True
    assert len(alerts) == 1


def test_periodic_check_captures_baseline(_isolated_paths: Path) -> None:
    trader = _real_trader([])
    with patch(
        "virtual_trader.fetch_real_account_state",
        return_value={
            "jpy_balance": 44_000.0,
            "equity_jpy": 44_000.0,
            "position_size_btc": 0.001,
        },
    ):
        trader.run_equity_guard_check()

    state = json.loads(_isolated_paths.read_text(encoding="utf-8"))
    assert state["baseline_equity"] == 44_000.0
    assert state["baseline_position_open"] is True
    assert state["state"] == "armed"


def test_periodic_check_fetch_failure_does_not_trigger(
    _isolated_paths: Path,
) -> None:
    trader = _real_trader([])
    with patch(
        "virtual_trader.fetch_real_account_state",
        side_effect=RuntimeError("network down"),
    ):
        trader.run_equity_guard_check()

    # 取得失敗時は起点も取らず、発動もしない
    assert not _isolated_paths.exists()
    assert trader._equity_guard.is_stopped() is False


# --------------------------------------------------------------------- #
#  欠落 equity / スキーマ不全 / プロセス間ロック                          #
# --------------------------------------------------------------------- #


def test_guard_evaluate_rejects_invalid_equity(tmp_path: Path) -> None:
    path = tmp_path / "equity_guard.json"
    guard = EquityGuard(path)

    # 0以下は「取得失敗」と同じ扱いで、起点も取らない・発動もしない
    assert guard.evaluate(0.0, position_open=False) is False
    assert guard.evaluate(-100.0, position_open=False) is False
    assert guard.state_snapshot() is None
    assert not path.exists()


def test_empty_state_file_is_treated_as_no_data(tmp_path: Path) -> None:
    path = tmp_path / "equity_guard.json"
    path.write_text("{}", encoding="utf-8")
    guard = EquityGuard(path)

    assert guard.evaluate(1.0, position_open=False) is False
    assert guard.is_stopped() is False
    assert guard.state_snapshot() is None
    # スキーマ不全のファイルは自動で上書きしない
    assert path.read_text(encoding="utf-8") == "{}"


def test_state_without_numbers_is_not_stopped(tmp_path: Path) -> None:
    path = tmp_path / "equity_guard.json"
    path.write_text(json.dumps({"state": "stopped"}), encoding="utf-8")
    guard = EquityGuard(path)

    # state=stopped でも起点・停止ラインが数値でなければ「データなし」扱い
    assert guard.is_stopped() is False
    assert guard.evaluate(1.0, position_open=False) is False
    assert path.read_text(encoding="utf-8") == json.dumps({"state": "stopped"})


def test_file_lock_shared_between_instances(tmp_path: Path) -> None:
    path = tmp_path / "equity_guard.json"
    engine_side = EquityGuard(path)
    cli_side = EquityGuard(path)

    engine_side.evaluate(43_887.0, position_open=False)
    # プロセス間ロック用の .lock ファイルが同じディレクトリに作られる
    assert (tmp_path / "equity_guard.json.lock").exists()

    # 別インスタンス（CLI 相当）との交互操作でも状態が壊れない
    assert cli_side.clear() is not None
    engine_side.evaluate(38_000.0, position_open=False)
    assert cli_side.is_stopped() is True
    state = cli_side.clear()
    assert state is not None
    assert state["state"] == "armed"
    # 別インスタンスの読みは mtime キャッシュ経由のため、新規インスタンスで
    # ファイル実態を確認する
    assert EquityGuard(path).is_stopped() is False


def test_trader_missing_equity_never_captures_baseline(
    _isolated_paths: Path,
) -> None:
    trader = _real_trader([])

    # equity 欠落 / None / 0 は「取得失敗」と同じ扱い
    assert trader.evaluate_equity_guard({}) is False
    assert trader.evaluate_equity_guard({"equity_jpy": None}) is False
    assert (
        trader.evaluate_equity_guard(
            {"equity_jpy": 0.0, "position_size_btc": 0.0}
        )
        is False
    )
    assert not _isolated_paths.exists()
    assert trader._equity_guard.is_stopped() is False

    # 正常な equity が来れば起点を取得できる
    assert (
        trader.evaluate_equity_guard(
            {"equity_jpy": 43_887.0, "position_size_btc": 0.0}
        )
        is False
    )
    state = trader._equity_guard.state_snapshot()
    assert state is not None
    assert state["baseline_equity"] == 43_887.0


# --------------------------------------------------------------------- #
#  pending エントリーと遮断 / 停止時キャンセル                            #
# --------------------------------------------------------------------- #


def _fill_snap() -> OrderbookSnapshot:
    # LONG 指値（entry=10,000,000）にタッチする板。
    # スプレッド・乖離・imbalance のキャンセル条件には当たらない値にする
    return OrderbookSnapshot(
        best_bid_price=10_000_100.0,
        best_bid_size=0.9,
        best_ask_price=10_000_200.0,
        best_ask_size=0.1,
    )


def _neutralize_maintenance_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    # 実行時刻（定期メンテ枠）に依存しないよう、暦ベースの判定だけ無効化する。
    # ゲート自体（セーフモード / 手動停止 / ガード）の検証は実コードで行う
    for name in (
        "is_gmo_scheduled_maintenance_window",
        "is_weekly_maintenance_window",
        "is_daily_maintenance_window",
        "is_weekly_pre_maintenance_window",
    ):
        monkeypatch.setattr(
            virtual_trader_module, name, lambda *args, **kwargs: False
        )


def _pending_virtual_trader() -> VirtualTrader:
    trader = VirtualTrader(initial_jpy=50_000.0, trading_mode="virtual")
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.001,
        is_pending=True,
    )
    return trader


def test_pending_virtual_entry_fills_when_not_blocked(
    _isolated_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _neutralize_maintenance_windows(monkeypatch)
    trader = _pending_virtual_trader()
    assert trader._is_entry_blocked(datetime.now()) is False
    trader.on_orderbook_update(_fill_snap())

    # 遮断がなければ板タッチで約定する（対照ケース）
    assert trader.position.is_pending is False
    assert trader.position.side == "LONG"


def test_pending_virtual_entry_not_filled_while_entry_blocked(
    _isolated_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _neutralize_maintenance_windows(monkeypatch)
    trader = _pending_virtual_trader()
    # セーフモードで共有ゲート（entry_blocked）を遮断状態にする
    trader._safe_mode_until = datetime.now() + timedelta(hours=1)
    assert trader._is_entry_blocked(datetime.now()) is True

    trader.on_orderbook_update(_fill_snap())

    # 約定せず pending のまま（キャンセル条件にも当たらない板）
    assert trader.position.is_pending is True
    assert trader.position.side == "LONG"


_ENTRY_ORDER_ID = 777001


def _pending_real_trader_with_guard(alerts: List[str]) -> VirtualTrader:
    trader = _real_trader(alerts)
    trader.evaluate_equity_guard(
        {"equity_jpy": 43_887.0, "position_size_btc": 0.0}
    )
    trader.position = PositionState(
        side="LONG",
        entry_price=10_000_000.0,
        size=0.001,
        is_pending=True,
        entry_order_id=_ENTRY_ORDER_ID,
    )
    return trader


def test_guard_stop_cancels_pending_entry_on_next_update(
    _isolated_paths: Path,
) -> None:
    alerts: List[str] = []
    trader = _pending_real_trader_with_guard(alerts)
    cancel_ids: List[int] = []

    with patch(
        "virtual_trader.gmo_cancel_order",
        side_effect=lambda oid: cancel_ids.append(int(oid)),
    ):
        assert (
            trader.evaluate_equity_guard(
                {"equity_jpy": 38_800.0, "position_size_btc": 0.0}
            )
            is True
        )
        # 停止発動の時点ではまだ取り消さない（次の板更新で取り消す）
        assert cancel_ids == []
        assert trader.position.is_pending is True

        trader.on_orderbook_update(_fill_snap())

    assert cancel_ids == [_ENTRY_ORDER_ID]
    assert trader.position.side is None
    assert trader.position.is_pending is False


def test_guard_stop_cancel_failure_keeps_pending(
    _isolated_paths: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    alerts: List[str] = []
    trader = _pending_real_trader_with_guard(alerts)

    error = virtual_trader_module.GmoApiError(
        status=1,
        messages=[{"message_code": "ERR-9999", "message_string": "boom"}],
    )
    with patch("virtual_trader.gmo_cancel_order", side_effect=error):
        assert (
            trader.evaluate_equity_guard(
                {"equity_jpy": 38_800.0, "position_size_btc": 0.0}
            )
            is True
        )
        trader.on_orderbook_update(_fill_snap())

    # 取消に失敗したら pending を維持し、WARN を残す
    assert trader.position.is_pending is True
    out = capsys.readouterr().out
    assert "pending entry cancel did not complete" in out
