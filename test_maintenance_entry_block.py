"""
test_maintenance_entry_block.py

目的:
  virtual_trader.py の _is_entry_blocked() / _update_maintenance_state() が、
  メンテナンス時間帯を正しく検知して新規エントリーをブロックするかどうかを、
  実際の関数を直接呼び出して検証する。

  本テストは「実装側を修正して通す」ためのものではなく、
  「今の実装が実際にどう動くか」を可視化する characterization test である。

重要な前提（実装調査で判明した事実）:
  - 仕様(verify_maintenance_entry_block.py の docstring 等)では
    メンテナンス時間帯は「毎日 05:55-06:30」と「毎週土曜 09:00-11:00」の
    2種類とされている。
  - しかし現行の virtual_trader.py には「毎週土曜 09:00-11:00」
    （+開始5分前のプレメンテ枠）しか実装されていない。
    「毎日 05:55-06:30」の判定は実装に一切存在しない。
  => したがって 05:55-06:30 の時間帯では新規エントリーがブロックされず、
     これが 06:00 前後で ENTRY が漏れた今回のインシデントの根本原因である。

構成:
  - TestDailyMaintenanceWindowNotImplemented:
      日次メンテ枠(05:55-06:30)。「あるべき挙動(ブロック=True)」を assert しつつ、
      現状は未実装で False を返すため xfail(strict=True) で明示する。
  - TestWeeklyMaintenanceWindowBoundaries:
      実装済みの土曜09:00-11:00 枠およびプレメンテ枠(08:55-09:00)の境界を
      通常の assert で検証する。

freezegun について（テスト側の工夫 / 依存排除）:
  _is_entry_blocked() / _is_weekly_maintenance_window() /
  _is_weekly_pre_maintenance_window() はいずれも判定対象時刻を引数
  now: datetime として受け取る設計のため、システム時刻を凍結せずとも
  任意の時刻を直接注入して検証できる。よって freezegun への依存は排除した。
  （安全モード状態 self._safe_mode_until と manual_stop.flag のみ外部要因。
    フラットな新規インスタンスでは _safe_mode_until=None であり、
    リポジトリに manual_stop.flag が存在しない前提で、判定は now のみに依存する。）

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

from virtual_trader import VirtualTrader  # noqa: E402


def _make_trader() -> VirtualTrader:
    """デフォルト構成の VirtualTrader（full_day プロファイル / maintenance_prepare_minutes=5）。"""
    return VirtualTrader()


class TestDailyMaintenanceWindowNotImplemented:
    """
    日次メンテ枠(05:55-06:30)は現行の virtual_trader.py に未実装。
    「あるべき挙動(ブロック=True)」を assert するが、現状は False を返すため
    xfail(strict=True) とする。実装が追加されて True を返すようになると XPASS となり、
    strict=True によりテストが失敗し「xfail を外す必要がある」ことを通知する。

    いずれも weekday=水曜(2026-07-08)の時刻で検証する。現行実装は土曜(weekday=5)の
    09:00-11:00 しか見ないため、平日朝のこの時間帯では確実に False(ブロックなし)となる。
    """

    _DAILY_REASON = (
        "日次メンテ枠(05:55-06:30)が virtual_trader.py に未実装のため現状は"
        "ブロックされない(False)。実装は毎週土曜09:00-11:00のみ。この未実装が、"
        "06:00前後の日次メンテ時間帯で新規ENTRYが漏れた今回のインシデントの根本原因。"
    )

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
    実装済みの毎週土曜 09:00-11:00 メンテ枠と、開始5分前のプレメンテ枠
    (maintenance_prepare_minutes=5 → 08:55-09:00)の境界挙動を検証する。

    実効ブロック区間(土曜): [08:55:00, 11:00:00)
      - プレメンテ枠: [08:55:00, 09:00:00)  (_is_weekly_pre_maintenance_window: pre_start<=now<start)
      - 本メンテ枠:   [09:00:00, 11:00:00)  (_is_weekly_maintenance_window:  START<=now<END)
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

    def test_entry_allowed_at_maintenance_end_boundary(self):
        """
        11:00:00 = 本メンテ終了ちょうど。実装は START<=now<END の半開区間のため、
        この瞬間は既にブロック解除(False)。境界は「終了時刻は含まない」。
        """
        trader = _make_trader()
        now = datetime(2026, 7, 11, 11, 0, 0)
        assert trader._is_entry_blocked(now) is False

    def test_entry_allowed_just_after_maintenance_window(self):
        """11:00:01 = 本メンテ終了1秒後 → 許可(False)。"""
        trader = _make_trader()
        now = datetime(2026, 7, 11, 11, 0, 1)
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
