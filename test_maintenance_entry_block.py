"""
test_maintenance_entry_block.py

目的:
  virtual_trader.py の _is_entry_blocked() が、メンテナンス時間帯の境界で
  新規エントリーを正しくブロック／許可するかを、時刻を直接注入して検証する。

  本テストは「実装側を直して通す」ためのものではなく、
  現行実装のエントリーブロック境界を固定する characterization test である。

対象となるメンテ枠（いずれも半開区間、下端含む・上端含まない）:
  - 日次: 毎日 [05:55:00, 06:30:00)
  - 週次: 土曜 [08:55:00, 11:10:00)
      - 開始前プレメンテ: [08:55:00, 09:00:00)（maintenance_prepare_minutes=5）
      - 本メンテ + 終了後プレオープン: [09:00:00, 11:10:00)
        （GMO告知の本メンテは11:00終了、11:00-11:10は取消のみ）

構成:
  - TestDailyMaintenanceWindowBoundaries:
      日次メンテ枠のエントリーブロック境界を検証する。
  - TestWeeklyMaintenanceWindowBoundaries:
      週次メンテ枠（プレメンテ・本枠・終了後プレオープン）の境界を検証する。

時刻注入について:
  _is_entry_blocked() 等は判定対象時刻を引数 now: datetime として受け取るため、
  freezegun なしで任意の時刻を注入できる。
  autouse fixture で _MANUAL_STOP_FLAG_PATH を一時パスに隔離し、
  リポジトリ上の manual_stop.flag が判定を汚染しないようにしている。

実行方法:
  pip install pytest
  pytest test_maintenance_entry_block.py -v
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

# virtual_trader.py は同ディレクトリ内モジュール(profile_config / strategy_logic)を
# トップレベル import しているため、btc_trading_tool を sys.path に追加する。
_BTC_DIR = Path(__file__).resolve().parent / "btc_trading_tool"
if str(_BTC_DIR) not in sys.path:
    sys.path.insert(0, str(_BTC_DIR))

import virtual_trader as vt  # noqa: E402
from virtual_trader import VirtualTrader  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_manual_stop_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """リポジトリ上の manual_stop.flag が判定を汚染しないようにする。"""
    monkeypatch.setattr(vt, "_MANUAL_STOP_FLAG_PATH", tmp_path / "manual_stop.flag")


def _make_trader() -> VirtualTrader:
    """デフォルト構成の VirtualTrader（full_day プロファイル / maintenance_prepare_minutes=5）。"""
    return VirtualTrader()


class TestDailyMaintenanceWindowBoundaries:
    """
    日次メンテ枠 [05:55:00, 06:30:00) の新規エントリーブロック境界を検証する。

    いずれも weekday=水曜(2026-07-08)の時刻で検証する（週次土曜枠と独立）。
    """

    def test_entry_should_be_blocked_at_daily_maintenance_start(self):
        """あるべき挙動: 日次メンテ開始ちょうど(05:55:00)はブロックされるべき。"""
        trader = _make_trader()
        now = datetime(2026, 7, 8, 5, 55, 0)
        assert trader._is_entry_blocked(now) is True

    def test_entry_blocked_immediately_after_restart_during_maintenance(self):
        """
        あるべき挙動: 06:00:13(日次メンテ時間帯内・再起動直後を想定)は
        起動直後の初回判定でもブロックされるべき。これが元インシデントの発生時刻。
        """
        trader = _make_trader()
        now = datetime(2026, 7, 8, 6, 0, 13)
        assert trader._is_entry_blocked(now) is True

    def test_entry_should_be_blocked_just_before_daily_maintenance_end(self):
        """あるべき挙動: 日次メンテ終了直前(06:29:59)はまだブロックされるべき。"""
        trader = _make_trader()
        now = datetime(2026, 7, 8, 6, 29, 59)
        assert trader._is_entry_blocked(now) is True


class TestWeeklyMaintenanceWindowBoundaries:
    """
    毎週土曜メンテ枠と、開始5分前のプレメンテ枠の境界挙動を検証する。

    実効ブロック区間(土曜): [08:55:00, 11:10:00)
      - プレメンテ枠: [08:55:00, 09:00:00)
      - 本メンテ+終了後プレオープン: [09:00:00, 11:10:00)
        （GMO告知の本メンテは11:00終了、11:00-11:10は取消のみのプレオープン）
    いずれも下端を含み上端を含まない半開区間。
    2026-07-11 は土曜日(weekday=5)。
    """

    def test_entry_allowed_before_pre_maintenance_window(self):
        """08:54:59 = プレメンテ開始(08:55)の1秒前 → まだ許可(False)。"""
        trader = _make_trader()
        now = datetime(2026, 7, 11, 8, 54, 59)
        assert trader._is_entry_blocked(now) is False

    def test_entry_blocked_at_pre_maintenance_start_boundary(self):
        """08:55:00 = プレメンテ枠の開始ちょうど → ブロック開始(True)。"""
        trader = _make_trader()
        now = datetime(2026, 7, 11, 8, 55, 0)
        assert trader._is_entry_blocked(now) is True

    def test_entry_blocked_within_pre_maintenance_window(self):
        """08:59:59 = プレメンテ枠内 → ブロック(True)。"""
        trader = _make_trader()
        now = datetime(2026, 7, 11, 8, 59, 59)
        assert trader._is_entry_blocked(now) is True

    def test_entry_blocked_at_maintenance_start_boundary(self):
        """09:00:00 = 本メンテ開始ちょうど → ブロック(True。半開区間の下端は含む)。"""
        trader = _make_trader()
        now = datetime(2026, 7, 11, 9, 0, 0)
        assert trader._is_entry_blocked(now) is True

    def test_entry_blocked_within_maintenance_window(self):
        """10:59:59 = 本メンテ枠内 → ブロック(True)。"""
        trader = _make_trader()
        now = datetime(2026, 7, 11, 10, 59, 59)
        assert trader._is_entry_blocked(now) is True

    def test_entry_blocked_at_announced_maintenance_end_during_post_open(self):
        """11:00:00 = 告知上の本メンテ終了ちょうど。プレオープン中のためブロック継続。"""
        trader = _make_trader()
        now = datetime(2026, 7, 11, 11, 0, 0)
        assert trader._is_entry_blocked(now) is True

    def test_entry_blocked_within_post_open_grace(self):
        """11:09:59 = 終了後プレオープン枠内 → ブロック(True)。"""
        trader = _make_trader()
        now = datetime(2026, 7, 11, 11, 9, 59)
        assert trader._is_entry_blocked(now) is True

    def test_entry_allowed_at_post_open_grace_end_boundary(self):
        """
        11:10:00 = プレオープン終了ちょうど。実装は START<=now<EFFECTIVE_END のため
        この瞬間はブロック解除(False)。
        """
        trader = _make_trader()
        now = datetime(2026, 7, 11, 11, 10, 0)
        assert trader._is_entry_blocked(now) is False

    def test_entry_allowed_just_after_post_open_grace(self):
        """11:10:01 = プレオープン終了1秒後 → 許可(False)。"""
        trader = _make_trader()
        now = datetime(2026, 7, 11, 11, 10, 1)
        assert trader._is_entry_blocked(now) is False

    def test_entry_blocked_without_prior_update_maintenance_state_call(self):
        """
        _update_maintenance_state() を一度も呼ばずに(=起動直後・メインループ未実行を想定)
        _is_entry_blocked() を呼んでも、土曜メンテ枠は now から直接判定されるため
        正しく True を返すことを確認する。
        => 週次メンテのブロックは初期化順序に依存しない(順序問題ではない)ことの可視化。
        """
        trader = _make_trader()
        now = datetime(2026, 7, 11, 9, 0, 0)
        # 意図的に _update_maintenance_state() を呼ばない
        assert trader._is_entry_blocked(now) is True
