"""
virtual_trader.py
-----------------
StrategyLogicが生成したシグナルに従い「仮想売買」を実行するクラス。
実際の API 発注は一切行わない。純粋な Python 内計算のみ。

ポジションモデル:
  LONG  … JPY で BTC を購入する現物ロング
  SHORT … 証拠金なしの仮想ショート（P&L のみ JPY 残高に反映）

駆動モデル（v2）:
  on_orderbook_update() は WebSocketManager の _on_message から
  ミリ秒単位でリアルタイムに呼び出される。threading.Lock で保護済み。

注文モデル（v2）:
  エントリーは is_pending=True で板に並んだ状態を表現。
  対向気配値がタッチしたフレームで is_pending=False へ遷移（約定確認）。
  利確は exit_price_target を突き抜けた（>/<）場合のみ約定（楽観バイアス排除）。
  損切りは成行（Taker）で即時約定。

手数料:
  int() で 1 円未満切り捨て（truncate toward zero）。
  例: -0.29円 → 0円、-1.75円 → -1円

メモリ管理:
  trade_history は最新 100 件のみ保持（deque maxlen=100）。
  全履歴は log/realtime_trading_log_YYYY-MM-DD.csv に追記保存。
"""
import collections
import csv
import hashlib
import hmac
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from portfolio_metrics import compute_position_value
from profile_config import ProfileDefinition, get_active_profile
from strategy_logic import (
    OrderbookSnapshot,
    PositionState,
    Signal,
    StrategyConfig,
    evaluate,
)

# ---- GMOコイン 信用取引 手数料レート ---------------------------------- #
MAKER_FEE_RATE: float =  0.0      # Maker: 0%（手数料なし）
TAKER_FEE_RATE: float =  0.0      # Taker: 0%（手数料切り捨てのため無し）
# ---------------------------------------------------------------------- #

# ---- CSV ログ（日次分割）--------------------------------------------- #
LOG_DIR = Path(__file__).resolve().parent.parent / "log"

def _get_csv_log_path() -> str:
    """当日付のログファイルパスを返す（例: log/realtime_trading_log_2026-07-02.csv）"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return str(LOG_DIR / f"realtime_trading_log_{datetime.now().strftime('%Y-%m-%d')}.csv")

_CSV_COLUMNS = [
    "timestamp", "trade_id", "side", "order_type", "reason",
    "price", "size", "fee", "pnl",
    "duration_sec", "cumulative_pnl",
    "config_version", "profile_name",
    "imbalance", "spread_pct", "best_bid_size", "best_ask_size",
    "cfg_imbalance_threshold", "cfg_tp_pct", "cfg_sl_pct",
    "cfg_min_wall_btc", "cfg_max_spread_pct", "cfg_max_order_size_btc",
    "cfg_daily_target_order_size_btc",
    # 未約定キャンセル詳細（AI夜間議論の集計対象外）
    "cancel_reason",
    "cancel_time_condition_met",
    "cancel_deviation_condition_met",
    "cancel_elapsed_minutes",
    "cancel_deviation_pct",
]
# ---------------------------------------------------------------------- #

TRADE_HISTORY_MAXLEN = 100

# ---- GMOコイン メンテナンス回避（JST）---------------------------------- #
# 定期メンテ: 毎日 05:55:00 - 06:30:00
_DAILY_MAINTENANCE_START = dtime(5, 55, 0)
_DAILY_MAINTENANCE_END = dtime(6, 30, 0)
# 定期メンテ: 毎週土曜 09:00:00 - 11:00:00
_WEEKLY_MAINTENANCE_WEEKDAY = 5  # Monday=0 ... Saturday=5
_WEEKLY_MAINTENANCE_START = dtime(9, 0, 0)
_WEEKLY_MAINTENANCE_END = dtime(11, 0, 0)
_SAFE_MODE_COOLDOWN_MINUTES = 15
_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "runtime"
_MANUAL_STOP_FLAG_PATH = _RUNTIME_DIR / "manual_stop.flag"
_REAL_STARTUP_RECONCILE_STATE_PATH = (
    _RUNTIME_DIR / "real_startup_reconcile_state.json"
)
_GMO_PRIVATE_API_BASE = "https://api.coin.z.com/private"
_GMO_LEVERAGE_SYMBOL = "BTC_JPY"
# Private API 認証: trade=発注権限付き / readonly=参照専用
_GMO_CREDENTIAL_SCOPE_TRADE = "trade"
_GMO_CREDENTIAL_SCOPE_READONLY = "readonly"
_GMO_CREDENTIAL_ENV_NAMES = {
    _GMO_CREDENTIAL_SCOPE_TRADE: ("GMO_API_KEY_TRADE", "GMO_API_SECRET_TRADE"),
    _GMO_CREDENTIAL_SCOPE_READONLY: (
        "GMO_API_KEY_READONLY",
        "GMO_API_SECRET_READONLY",
    ),
}
# 建玉差分許容 0.0005 BTC: GMO最小発注単位 0.001 BTC の半分。
# 端数丸め・API反映遅延・未約定指値との一時差を吸収するため。
_RECONCILIATION_DEFAULT_TOLERANCE_BTC = 0.0005
_RECONCILIATION_DEFAULT_TOLERANCE_JPY = 100.0
# 起動時の会計整合: 現金と (初期資産+累積損益-LONG拘束) の許容差。
# 異常検知の時間あたり損失閾値 (3000円) に揃え、黙って数万円消える事態を防ぐ。
ACCOUNT_INTEGRITY_TOLERANCE_JPY = 3_000.0
# imbalance CANCEL 直後の再 ENTRY 抑制（秒・同一方向）。config.json には出さない固定値。
ENTRY_COOLDOWN_AFTER_CANCEL_SEC = 5
# imbalance CANCEL 直後の再 ENTRY 抑制（秒・方向不問）。同一方向クールダウンとは別枠。
ENTRY_COOLDOWN_AFTER_IMBALANCE_CANCEL_ANY_SIDE_SEC = 1.5
# imbalance 反転キャンセルのデバウンス（秒）。反転側がこの時間連続したときのみキャンセル。
IMBALANCE_REVERSAL_DEBOUNCE_SEC = 1.5
# 時間/乖離タイムアウトキャンセル後の再 ENTRY 抑制（秒・同一方向かつ同一価格）。上記とは独立。
ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC = 300
# 未約定エントリー指値の経過時間タイムアウト（分）。プロファイル単位・AI夜間調整対象外。
ENTRY_PENDING_TIMEOUT_MINUTES_DEFAULT = 60.0
ENTRY_PENDING_TIMEOUT_MINUTES_BY_PROFILE: Dict[str, float] = {
    "early_morning": 60.0,
    "daytime": 60.0,
    "night": 60.0,
    "full_day": 60.0,
}
# キャンセル理由（ログ / クールダウン出し分け用）
CANCEL_REASON_IMBALANCE = "IMBALANCE_REVERSAL"
CANCEL_REASON_MAINTENANCE = "MAINTENANCE"
CANCEL_REASON_TIMEOUT = "ENTRY_TIMEOUT"
CANCEL_REASON_DEVIATION = "ENTRY_DEVIATION"
# 乖離キャンセル閾値 = stop_loss_pct に対する倍率（プロファイル SL の 50%）
ENTRY_PENDING_DEVIATION_SL_RATIO = 0.5
# 取引日境界: 毎日 06:00（JST前提）。暦日ではなく 06:00 起点のサイクル。
_TRADING_DAY_ROLLOVER_HOUR = 6
# real mode 強制決済: closeOrder 失敗時の再試行
_FORCE_CLOSE_REAL_MAX_ATTEMPTS = 3
_FORCE_CLOSE_REAL_BACKOFF_BASE_SEC = 1.0
_FORCE_CLOSE_REAL_ALERT_COOLDOWN_SEC = 60.0
# closeOrder 後の openPositions 反映待ち（確認専用）
_FORCE_CLOSE_CONFIRM_MAX_CHECKS = 3
_FORCE_CLOSE_CONFIRM_RETRY_SEC = 1.0
# 板TP: closeOrder 受理後の confirm/settle 再試行
# （2026-08-03: ERR-5008 で settle 中断→内部建玉残留を防ぐ）
_BOARD_TP_SETTLE_MAX_ATTEMPTS = 3
_BOARD_TP_SETTLE_RETRY_SEC = 1.0
# 板TP: closeOrder 後の実約定価格取得リトライ（GMO反映遅延の確認用）
_BOARD_TP_FILL_FETCH_RETRY_SEC = 1.5
# cancelOrder が「すでに約定/取消済み等」で対象外のときの正常系コード
_CANCEL_ORDER_BENIGN_CODES = frozenset({"ERR-5122", "ERR-5123"})
# ---------------------------------------------------------------------- #


def calc_trading_day_date(now: Optional[datetime] = None) -> str:
    """
    直近06:00サイクルの取引日日付（YYYY-MM-DD）を返す。
    例: 07-14 05:59 -> 07-13 / 07-14 06:00 -> 07-14
    """
    current = now if now is not None else datetime.now()
    if current.hour < _TRADING_DAY_ROLLOVER_HOUR:
        current = current - timedelta(days=1)
    return current.date().isoformat()



# =========================================================================== #
#  TradeRecord                                                                  #
# =========================================================================== #

@dataclass
class TradeRecord:
    trade_id:      str
    side:          str        # "BUY" | "SELL"
    order_type:    str        # "MAKER" | "TAKER"
    price:         float
    size:          float
    fee:           int        # 手数料（1円未満切り捨て。負 = リベート）
    pnl:           float      # この取引単体の純損益（円）
    reason:        str        # "ENTRY" | "ENTRY_PENDING" | "TAKE_PROFIT" | "STOP_LOSS" | ...
    imbalance:     float      # 約定時の買い圧力
    spread_pct:    float      # 約定時のスプレッド（%）
    best_bid_size: float      # 約定時の Best Bid 数量（BTC）
    best_ask_size: float      # 約定時の Best Ask 数量（BTC）
    duration_sec:  int        = 0    # ポジション保有秒数（エントリーおよびキャンセル行は 0）
    cumulative_pnl: float     = 0.0  # この決済直後の累積損益（円）
    config_version: str       = "default"
    profile_name: str         = "unknown"
    timestamp:     datetime   = field(default_factory=datetime.now)

    def summary(self) -> str:
        sign     = "+" if self.pnl >= 0 else ""
        fee_sign = "+" if self.fee <= 0 else "-"
        return (
            f"[{self.timestamp.strftime('%H:%M:%S')}]"
            f" {self.side:<4}({self.order_type:<5})"
            f"  価格: {self.price:>13,.0f} 円"
            f"  数量: {self.size:.6f} BTC"
            f"  損益: {sign}{self.pnl:>10,.0f} 円"
            f"  手数料: {fee_sign}{abs(self.fee):} 円"
            f"  Imbalance: {self.imbalance:.1%}"
            f"  [{self.reason}]"
        )


# =========================================================================== #
#  VirtualTrader                                                                #
# =========================================================================== #

class VirtualTrader:
    """
    WebSocket 更新ごとに on_orderbook_update() を呼び出すだけで
    仮想売買が自律的に動作するクラス。スレッドセーフ。
    """

    POSITION_RATIO:   float = 0.20    # 1 回のエントリーに使う円資産の割合（20%）
    MIN_TRADE_SIZE:   float = 0.001   # GMOコイン レバレッジ(BTC_JPY)の最小発注単位（BTC）
    LOT_UNIT:         float = 0.001   # 発注サイズの切り捨て単位（0.001 BTC 刻み）

    @staticmethod
    def _normalize_pre_action(action: str) -> str:
        normalized = str(action).strip().lower()
        return normalized if normalized in {"wait", "close"} else "close"

    def __init__(
        self,
        initial_jpy: float = 50_000.0,
        profiles: Optional[List[ProfileDefinition]] = None,
        maintenance_pre_action: str = "close",
        maintenance_prepare_minutes: int = 5,
        before_entry_order: Optional[Callable[[], bool]] = None,
        on_order_placed: Optional[Callable[[], None]] = None,
        daily_loss_limit_pct: float = 0.10,
        on_daily_loss_limit: Optional[Callable[[Dict[str, float]], None]] = None,
        trading_mode: str = "virtual",
        on_critical_alert: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._initial_jpy   = initial_jpy
        self.jpy_balance:   float          = initial_jpy
        self.position:      PositionState  = PositionState()
        default_profile = ProfileDefinition(
            name="full_day",
            start_minute=0,
            end_minute=1440,
            config=StrategyConfig(),
        )
        self._profiles: List[ProfileDefinition] = sorted(
            profiles or [default_profile],
            key=lambda p: p.start_minute,
        )
        self.config:        StrategyConfig = self._profiles[0].config
        self._locked_config: Optional[StrategyConfig] = None
        self._locked_profile_name: Optional[str] = None
        self.config_version: str          = "default"
        self.maintenance_pre_action: str = self._normalize_pre_action(maintenance_pre_action)
        self.maintenance_prepare_minutes: int = max(0, int(maintenance_prepare_minutes))
        self._before_entry_order = before_entry_order
        self._on_order_placed = on_order_placed
        self.daily_loss_limit_pct: float = float(daily_loss_limit_pct)
        self._on_daily_loss_limit = on_daily_loss_limit
        mode = str(trading_mode).strip().lower()
        self.trading_mode: str = mode if mode in {"virtual", "real"} else "virtual"
        self._on_critical_alert = on_critical_alert
        self._force_close_real_cooldown_until: float = 0.0
        self.trading_day_date: str = calc_trading_day_date()
        self.daily_start_balance: float = float(initial_jpy)
        self.daily_realized_pnl: float = 0.0
        self.trade_history: collections.deque = collections.deque(maxlen=TRADE_HISTORY_MAXLEN)
        self._lock = threading.Lock()
        self._position_filled_at: Optional[datetime] = None  # 指値約定タイムスタンプ
        # 未約定エントリー指値の発注時刻（タイムアウト判定用。永続化しない）
        self._pending_order_placed_at: Optional[datetime] = None
        # Private WS 決済ログ用。on_orderbook_update 未受信時はプレースホルダを使う。
        self._latest_orderbook_snap: Optional[OrderbookSnapshot] = None
        self._safe_mode_until: Optional[datetime] = None
        self._safe_mode_wait_for_recovery: bool = False
        self._last_guard_state: str = "normal"
        self.engine_status: str = "RUNNING"
        # 方向別: 直近 CANCEL の (price, unix_timestamp, cooldown_sec, reason)。永続化しない。
        self._last_cancel_by_side: Dict[str, Optional[tuple]] = {
            "BUY": None,
            "SELL": None,
        }
        # imbalance 反転デバウンス開始時刻（unix）。永続化しない。
        self._imbalance_reversal_since: Optional[float] = None
        # 直近 imbalance CANCEL の時刻（方向不問クールダウン用）。永続化しない。
        self._last_imbalance_cancel_any_side_ts: Optional[float] = None

        # KPI カウンタ（trade_history の maxlen 制限を受けない全履歴集計）
        self._win_count:       int   = 0
        self._loss_count:      int   = 0
        self._daily_win_count: int   = 0
        self._daily_loss_count: int  = 0
        self._total_gross_win: float = 0.0
        self._total_gross_loss:float = 0.0
        self._cumulative_pnl:  float = 0.0   # 決済済み損益の累計

    # ------------------------------------------------------------------ #
    #  Public interface                                                    #
    # ------------------------------------------------------------------ #

    def on_execution_event(self, evt: Dict[str, Any]) -> None:
        """
        Private WS executionEvents コールバック。
        - entry_order_id 一致: pending -> 保有中
        - tp_order_id / sl_order_id 一致: 決済 + 反対側キャンセル（合成 OCO）
        """
        if not isinstance(evt, dict):
            return
        with self._lock:
            if self._apply_exit_execution_unlocked(evt):
                return
            self._apply_entry_execution_unlocked(evt)

    def _snapshot_for_logging(self) -> OrderbookSnapshot:
        """取引ログ用の直近板。未受信ならポジション価格のプレースホルダ。"""
        if self._latest_orderbook_snap is not None:
            return self._latest_orderbook_snap
        px = float(self.position.entry_price or 0.0)
        return OrderbookSnapshot(
            best_bid_price=px,
            best_bid_size=0.0,
            best_ask_price=px,
            best_ask_size=0.0,
        )

    def _parse_execution_order_id(self, evt: Dict[str, Any]) -> Optional[int]:
        raw = evt.get("orderId")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _parse_execution_fill(
        self,
        evt: Dict[str, Any],
        *,
        default_price: float,
        default_size: float,
    ) -> Tuple[float, float]:
        try:
            fill_price = float(evt.get("executionPrice", default_price))
        except (TypeError, ValueError):
            fill_price = float(default_price)
        try:
            if evt.get("orderExecutedSize") is not None:
                fill_size = float(evt.get("orderExecutedSize"))
            else:
                fill_size = float(evt.get("executionSize", default_size))
        except (TypeError, ValueError):
            fill_size = float(default_size)
        return fill_price, fill_size

    def _emit_critical_alert(self, message: str) -> None:
        self._safe_console_print(message)
        if self._on_critical_alert is None:
            return
        try:
            self._on_critical_alert(message)
        except Exception as alert_exc:
            self._safe_console_print(
                f"[WARN] critical alert notify failed: {alert_exc}"
            )

    def _apply_exit_execution_unlocked(self, evt: Dict[str, Any]) -> bool:
        """
        TP/SL 約定通知を処理する。処理したら True。
        合成 OCO: 決済後にもう片方の注文をキャンセルする。
        """
        pos = self.position
        if pos.side is None or pos.is_pending:
            return False

        order_id = self._parse_execution_order_id(evt)
        if order_id is None:
            return False

        is_tp = pos.tp_order_id is not None and order_id == int(pos.tp_order_id)
        is_sl = pos.sl_order_id is not None and order_id == int(pos.sl_order_id)
        if not is_tp and not is_sl:
            return False

        fill_price, fill_size = self._parse_execution_fill(
            evt,
            default_price=pos.entry_price,
            default_size=pos.size,
        )
        if fill_price <= 0 or fill_size <= 0:
            return False

        opposite_id = pos.sl_order_id if is_tp else pos.tp_order_id
        reason = "TAKE_PROFIT" if is_tp else "STOP_LOSS"
        filled_leg = "TP" if is_tp else "SL"

        actual_fee: Optional[int] = None
        if self.trading_mode == "real":
            raw_fee = evt.get("fee")
            if raw_fee is not None:
                try:
                    actual_fee = int(float(raw_fee))
                except (TypeError, ValueError):
                    actual_fee = None

        self._settle_real_exit_from_execution_unlocked(
            fill_price=fill_price,
            fill_size=fill_size,
            reason=reason,
            is_take_profit=is_tp,
            actual_fee=actual_fee,
        )
        if is_sl:
            self._sync_jpy_balance_from_equity_unlocked(context="REAL-SL")

        if opposite_id is not None:
            self._cancel_opposite_exit_order_unlocked(
                int(opposite_id),
                filled_leg=filled_leg,
            )
        return True

    def _settle_real_exit_from_execution_unlocked(
        self,
        *,
        fill_price: float,
        fill_size: float,
        reason: str,
        is_take_profit: bool,
        actual_fee: Optional[int] = None,
    ) -> None:
        """WS 約定による利確/損切り決済（既存板ベース決済と同等の内部更新）。

        actual_fee: GMO executionEvents の fee フィールドから取得した実手数料（JPY整数）。
                    real mode かつ取得できた場合はこの値を使用する。
                    None の場合は理論値にフォールバックする。
        """
        pos = self.position
        snap = self._snapshot_for_logging()
        size = float(fill_size)

        if is_take_profit:
            fee_rate = MAKER_FEE_RATE
            order_type = "MAKER"
        else:
            fee_rate = TAKER_FEE_RATE
            order_type = "TAKER"

        if actual_fee is not None:
            fee = actual_fee
        else:
            fee = int(fill_price * size * fee_rate)
            if self.trading_mode == "real":
                ts = datetime.now().strftime("%H:%M:%S")
                self._safe_console_print(
                    f"[{ts}] [WARN] [REAL-EXIT] actual fee unavailable;"
                    f" using theoretical fee={fee} JPY reason={reason}"
                )

        if pos.side == "LONG":
            gross_pnl = (fill_price - pos.entry_price) * size
            net_pnl = gross_pnl - fee
            self.jpy_balance += net_pnl
            side = "SELL"
        else:
            gross_pnl = (pos.entry_price - fill_price) * size
            net_pnl = gross_pnl - fee
            self.jpy_balance += net_pnl
            side = "BUY"

        resolved_profile_name = self._locked_profile_name or self.active_profile_name
        self._update_kpi(net_pnl)
        self.daily_realized_pnl += net_pnl
        self.check_daily_loss_limit()
        dur = self._calc_duration_sec()
        self.position = PositionState()
        self._position_filled_at = None
        self._pending_order_placed_at = None
        self._clear_locked_profile()
        self._record_and_print(
            snap=snap,
            side=side,
            order_type=order_type,
            price=fill_price,
            size=size,
            fee=fee,
            pnl=net_pnl,
            reason=reason,
            duration_sec=dur,
            cumulative_pnl=self._cumulative_pnl,
            profile_name=resolved_profile_name,
        )
        ts = datetime.now().strftime("%H:%M:%S")
        self._safe_console_print(
            f"[{ts}] [OK] [REAL-EXIT] {reason}"
            f" @ {fill_price:,.0f} JPY"
            f"  size={size:.4f} BTC"
            f"  pnl={net_pnl:+,.0f} JPY"
        )

    def _cancel_opposite_exit_order_unlocked(
        self,
        opposite_order_id: int,
        *,
        filled_leg: str,
    ) -> None:
        """合成 OCO: 約定した側の反対注文をキャンセルする。例外は外へ出さない。"""
        ts = datetime.now().strftime("%H:%M:%S")
        opposite_leg = "SL" if filled_leg == "TP" else "TP"
        try:
            gmo_cancel_order(int(opposite_order_id))
            self._safe_console_print(
                f"[{ts}] [OK] [REAL-OCO] cancel {opposite_leg}"
                f" orderId={opposite_order_id}"
                f" after {filled_leg} fill"
            )
            return
        except GmoApiError as exc:
            if is_benign_cancel_error(exc):
                self._emit_critical_alert(
                    "[ALERT] real OCO unexpected double fill risk\n"
                    f"filled_leg={filled_leg}\n"
                    f"cancel_leg={opposite_leg}\n"
                    f"cancel_order_id={opposite_order_id}\n"
                    f"detail=cancel returned already-done code "
                    f"(other leg may also have filled)\n"
                    f"error={exc}"
                )
                return
            self._emit_critical_alert(
                "[ALERT] real OCO opposite cancel failed\n"
                f"filled_leg={filled_leg}\n"
                f"cancel_leg={opposite_leg}\n"
                f"cancel_order_id={opposite_order_id}\n"
                f"detail=cancel API business error; order may still be live\n"
                f"error={exc}"
            )
            return
        except Exception as exc:
            self._emit_critical_alert(
                "[ALERT] real OCO opposite cancel failed\n"
                f"filled_leg={filled_leg}\n"
                f"cancel_leg={opposite_leg}\n"
                f"cancel_order_id={opposite_order_id}\n"
                f"detail=cancel API error; order may still be live\n"
                f"error={exc}"
            )

    def _apply_entry_execution_unlocked(self, evt: Dict[str, Any]) -> None:
        pos = self.position
        if pos.entry_order_id is None or not pos.is_pending or pos.side is None:
            return

        raw_order_id = evt.get("orderId")
        if raw_order_id is None:
            return
        try:
            order_id = int(raw_order_id)
        except (TypeError, ValueError):
            return
        if order_id != int(pos.entry_order_id):
            return

        try:
            exec_price = float(evt.get("executionPrice", pos.entry_price))
        except (TypeError, ValueError):
            exec_price = float(pos.entry_price)
        try:
            if evt.get("orderExecutedSize") is not None:
                exec_size = float(evt.get("orderExecutedSize"))
            else:
                exec_size = float(evt.get("executionSize", pos.size))
        except (TypeError, ValueError):
            exec_size = float(pos.size)
        if exec_price <= 0 or exec_size <= 0:
            return

        position_id = self._parse_optional_order_id(evt.get("positionId"))

        tp_price = (
            exec_price * (1 + self.config.take_profit_pct)
            if pos.side == "LONG"
            else exec_price * (1 - self.config.take_profit_pct)
        )
        actual_entry_fee: Optional[int] = None
        raw_entry_fee = evt.get("fee")
        if raw_entry_fee is not None:
            try:
                actual_entry_fee = int(float(raw_entry_fee))
            except (TypeError, ValueError):
                actual_entry_fee = None
        if actual_entry_fee is None:
            theoretical_entry_fee = int(exec_price * exec_size * MAKER_FEE_RATE)
            ts_warn = datetime.now().strftime("%H:%M:%S")
            self._safe_console_print(
                f"[{ts_warn}] [WARN] [REAL-FILL] actual fee unavailable;"
                f" using theoretical fee={theoretical_entry_fee} JPY"
            )
            entry_fee = theoretical_entry_fee
        else:
            entry_fee = actual_entry_fee

        self._position_filled_at = datetime.now()
        self._pending_order_placed_at = None
        self.position = PositionState(
            side=pos.side,
            entry_price=exec_price,
            size=exec_size,
            is_pending=False,
            exit_price_target=tp_price,
            entry_order_id=pos.entry_order_id,
            tp_order_id=pos.tp_order_id,
            sl_order_id=pos.sl_order_id,
            position_id=position_id,
        )
        entry_side = "BUY" if pos.side == "LONG" else "SELL"
        snap_entry = self._snapshot_for_logging()
        self._record_and_print(
            snap=snap_entry,
            side=entry_side,
            order_type="MAKER",
            price=exec_price,
            size=exec_size,
            fee=entry_fee,
            pnl=-entry_fee,
            reason="ENTRY",
        )
        ts = datetime.now().strftime("%H:%M:%S")
        self._safe_console_print(
            f"[{ts}] [OK] [REAL-FILL] {pos.side}"
            f" orderId={order_id}"
            f" positionId={position_id}"
            f" @ {exec_price:,.0f} JPY"
            f"  size={exec_size:.4f} BTC"
            f"  TP target: {tp_price:,.0f} JPY"
        )
        self._place_real_tp_sl_orders()

    def on_orderbook_update(self, snap: Optional[OrderbookSnapshot]) -> None:
        """
        WebSocket の更新ごとに呼び出すメインエントリー。
        スレッドセーフ（_lock で保護）。snap が None なら何もしない。
        メンテナンス時間帯は最優先で制限を適用する。
        """
        if snap is None:
            return
        with self._lock:
            self._latest_orderbook_snap = snap
            now = datetime.now()
            self._update_maintenance_state(snap, now)
            entry_blocked = self._is_entry_blocked(now)

            # ポジション未保有時のみ、現在時刻に応じたプロファイルへ切り替える
            if self.position.side is None:
                active_profile = get_active_profile(self._profiles, self._now_minute())
                self.config = active_profile.config
            else:
                # 保有中はエントリー時にロックした設定を固定利用
                if self._locked_config is not None:
                    self.config = self._locked_config

            if self.position.is_pending:
                self._check_pending_fill(snap)
            elif self.position.side is not None:
                self._check_active_position(snap)
            else:
                if not entry_blocked:
                    self._check_new_entry(snap)

    def on_exchange_status(self, status: str, detail: str = "") -> None:
        """
        WebSocketManager から取引所状態通知を受け取る。
        - MAINTENANCE_DETECTED: 503 / メンテ系エラーを検知
        - NORMAL_RESPONSE: 正常レスポンス復帰
        """
        with self._lock:
            now = datetime.now()
            if status == "MAINTENANCE_DETECTED":
                next_until = now + timedelta(minutes=_SAFE_MODE_COOLDOWN_MINUTES)
                prev_until = self._safe_mode_until
                if prev_until is None or next_until > prev_until:
                    self._safe_mode_until = next_until
                self._safe_mode_wait_for_recovery = True
                until_txt = self._safe_mode_until.strftime("%H:%M:%S")
                print(
                    "[WARNING] [Alert] メンテナンス延長/臨時停止を検知。"
                    f"{_SAFE_MODE_COOLDOWN_MINUTES}分間は新規取引を停止します "
                    f"(until {until_txt}) detail={detail}"
                )
            elif status == "NORMAL_RESPONSE" and self._safe_mode_wait_for_recovery:
                self._safe_mode_wait_for_recovery = False
                print("[WARNING] [Maintenance] API正常応答を確認。クールダウン後に取引再開を判定します。")

    def total_assets(self, mid_price: float) -> float:
        """円残高 + 保有ポジション現在価値（pending 中は円残高のみ）。"""
        return self.jpy_balance + self.unrealized_pnl(mid_price)

    @property
    def initial_jpy(self) -> float:
        return self._initial_jpy

    @property
    def realized_pnl(self) -> float:
        """確定済み損益 = 現在の円残高 - 初期資産"""
        return self.jpy_balance - self._initial_jpy

    def unrealized_pnl(self, mid_price: float) -> float:
        """
        LONG (virtual): BTC の現在市場価値（size × mid）
        LONG (real): 含み損益のみ（(mid - entry) × size）
        SHORT: エントリー価格と現在価格の差額（含み損益）
        pending / なし : 0
        """
        if self.position.is_pending:
            return 0.0
        return compute_position_value(
            position_side=self.position.side,
            position_size=float(self.position.size),
            position_entry_price=float(self.position.entry_price),
            mid_price=float(mid_price),
            trading_mode=self.trading_mode,
        )

    # ---- KPI プロパティ ------------------------------------------------ #

    @property
    def exit_trade_count(self) -> int:
        """利確 + 損切りの合計決済回数（deque 制限外の全履歴）"""
        return self._win_count + self._loss_count

    @property
    def closed_trade_count(self) -> int:
        return self.exit_trade_count

    @property
    def win_rate(self) -> Optional[float]:
        """勝率（%）。決済なしは None"""
        n = self.exit_trade_count
        return (self._win_count / n * 100) if n > 0 else None

    @property
    def profit_factor(self) -> Optional[float]:
        """PF = 総利益 / 総損失。損失なしは None"""
        return (self._total_gross_win / self._total_gross_loss) if self._total_gross_loss > 0 else None

    @property
    def avg_pnl(self) -> Optional[float]:
        """平均損益（円）。決済なしは None"""
        n = self.exit_trade_count
        return (self._cumulative_pnl / n) if n > 0 else None

    @property
    def active_profile_name(self) -> str:
        """
        ポジション保有中はロック中のプロファイル名、
        フラット時は現在時刻のプロファイル名を返す。
        """
        if self._locked_profile_name is not None:
            return self._locked_profile_name
        now_minute = self._now_minute()
        return get_active_profile(self._profiles, now_minute).name

    # ------------------------------------------------------------------ #
    #  Trade size calculator                                               #
    # ------------------------------------------------------------------ #

    def _calc_trade_size(self, price: float) -> Optional[float]:
        """
        動的ロット計算:
          1. 割当金額 = 現在の円残高 × POSITION_RATIO (20%)
             real: GMO 実口座の利用可能 JPY（fetch_real_account_state）
             virtual: 内部帳簿 self.jpy_balance
          2. 割当金額 ÷ 現在価格 で BTC サイズを算出
          3. LOT_UNIT (0.001 BTC) 単位で切り捨て（APIの最小発注単位に合わせる）
          4. 結果が MIN_TRADE_SIZE (0.001 BTC) 未満なら MIN_TRADE_SIZE に固定
          5. config.max_order_size_btc を超える場合は上限でクランプ
          6. config.daily_target_order_size_btc が設定されている場合はさらに上限でクランプ

        real で実口座取得に失敗した場合は None を返す（呼び出し側でエントリー見送り）。

        例: 残高50,000円, 価格15,000,000円
            割当 = 10,000円  raw = 0.000666...
            floor → 0.000 → 最低値 0.001 BTC に固定
        """
        if price <= 0:
            return self.MIN_TRADE_SIZE

        if self.trading_mode == "real":
            try:
                real_state = fetch_real_account_state()
                jpy_balance = float(real_state["jpy_balance"])
            except Exception as exc:
                ts = datetime.now().strftime("%H:%M:%S")
                self._safe_console_print(
                    f"[{ts}] [SKIP] trade size calc failed"
                    f" (real account balance unavailable): {exc}"
                )
                return None
        else:
            jpy_balance = float(self.jpy_balance)

        raw_size     = (jpy_balance * self.POSITION_RATIO) / price
        floored_size = math.floor(raw_size / self.LOT_UNIT) * self.LOT_UNIT
        computed_size = max(floored_size, self.MIN_TRADE_SIZE)
        max_size = float(self.config.max_order_size_btc)
        candidate_limits = [max_size]
        daily_target_size = self.config.daily_target_order_size_btc
        if daily_target_size is not None:
            candidate_limits.append(float(daily_target_size))
        effective_limit = min(candidate_limits)
        size = min(computed_size, effective_limit)

        if computed_size > effective_limit:
            ts = datetime.now().strftime("%H:%M:%S")
            self._safe_console_print(
                f"[{ts}] [WARN] 注文サイズを上限 {effective_limit:.4f} BTC に制限しました "
                f"(computed={computed_size:.4f} BTC)"
            )

        return size

    # ------------------------------------------------------------------ #
    #  Phase 1: 指値注文の約定確認（is_pending=True のとき）              #
    # ------------------------------------------------------------------ #

    def _check_pending_fill(self, snap: OrderbookSnapshot) -> None:
        """
        Maker 指値の約定シミュレーションと、未約定時のキャンセル判定。

        LONG 買い指値 P（≈ best_bid + 1円）:
          best_bid_price >= P になった＝自分の指値が現在の最良気配以内に収まり、
          次の成行売りで約定したとみなす。
        SHORT 売り指値 Q（≈ best_ask - 1円）:
          best_ask_price <= Q になった＝自分の指値が現在の最良気配以内に収まり、
          次の成行買いで約定したとみなす。

        real mode:
          板タッチ仮想約定は行わず Private WS execution に一本化する。
          ただしキャンセル条件（Imbalance / 乖離 / タイムアウト）は板タッチ有無に
          関わらず常に評価する（価格が指値を超えて離れた状態でも安全装置を残す）。
        """
        pos = self.position
        filled = (
            (pos.side == "LONG"  and snap.best_bid_price >= pos.entry_price) or
            (pos.side == "SHORT" and snap.best_ask_price <= pos.entry_price)
        )

        # virtual のみ: 板タッチで仮想約定。real は WS 約定に任せ、ここではスキップ。
        if filled and self.trading_mode != "real":
            tp_price = (
                pos.entry_price * (1 + self.config.take_profit_pct)
                if pos.side == "LONG"
                else pos.entry_price * (1 - self.config.take_profit_pct)
            )
            self._position_filled_at = datetime.now()   # 保有時間の計測開始
            self._pending_order_placed_at = None
            self.position = PositionState(
                side              = pos.side,
                entry_price       = pos.entry_price,
                size              = pos.size,
                is_pending        = False,
                exit_price_target = tp_price,
            )
            ts = datetime.now().strftime("%H:%M:%S")
            self._safe_console_print(
                f"[{ts}] [OK] [LIMIT-FILL] {pos.side}"
                f" @ {pos.entry_price:,.0f} JPY"
                f"  TP target: {tp_price:,.0f} JPY"
            )
            self._reset_imbalance_reversal_debounce()
            return

        # virtual(未約定) / real(常に): キャンセル条件を評価
        cfg = self.config
        is_imbalance_reversed = (
            (pos.side == "LONG"  and snap.imbalance < cfg.imbalance_cancel_threshold) or
            (pos.side == "SHORT" and snap.imbalance > cfg.imbalance_cancel_threshold)
        )

        # 指値が出た後にスプレッドが許容範囲を超えて拡大した場合も即座にキャンセルする
        is_spread_too_wide = (snap.spread >= cfg.max_allowed_spread)

        elapsed_minutes, deviation_pct, time_met, deviation_met = (
            self._pending_timeout_conditions(snap)
        )

        if is_spread_too_wide:
            self._reset_imbalance_reversal_debounce()
            self._cancel_order(
                snap,
                cancel_reason=CANCEL_REASON_IMBALANCE,
            )
        elif is_imbalance_reversed:
            if self._imbalance_reversal_debounce_elapsed():
                self._reset_imbalance_reversal_debounce()
                self._cancel_order(
                    snap,
                    cancel_reason=CANCEL_REASON_IMBALANCE,
                )
        else:
            self._reset_imbalance_reversal_debounce()

        if self.position.side is None or not self.position.is_pending:
            return

        if time_met or deviation_met:
            cancel_reason = (
                CANCEL_REASON_TIMEOUT if time_met else CANCEL_REASON_DEVIATION
            )
            self._reset_imbalance_reversal_debounce()
            self._cancel_order(
                snap,
                cancel_reason=cancel_reason,
                time_condition_met=time_met,
                deviation_condition_met=deviation_met,
                elapsed_minutes=elapsed_minutes,
                deviation_pct=deviation_pct,
            )

    # ------------------------------------------------------------------ #
    #  Phase 2: アクティブポジションの利確・損切り判定                    #
    # ------------------------------------------------------------------ #

    def _check_active_position(self, snap: OrderbookSnapshot) -> None:
        """
        利確 (Maker 指値):
          LONG  売り指値 T: bid が T 以上になった＝誰かが T 以上で買いに来た → 約定
          SHORT 買い指値 T: ask が T 以下になった＝誰かが T 以下で売りに来た → 約定
          約定価格は exit_price_target（指値価格）で固定。楽観バイアスなし。

        損切り (Taker 成行):
          LONG : bid が SL ライン以下に達したら即時成行売り
          SHORT: ask が SL ライン以上に達したら即時成行買い

        real mode:
          - TP のみ板監視。到達時は SL 常設注文をキャンセルし closeOrder(MARKET) で決済。
          - SL は GMO 常設の closeOrder(STOP) に任せ、板ベースでは判定しない。
        """
        pos = self.position
        cfg = self.config

        if self.trading_mode == "real":
            if pos.side == "LONG":
                if pos.exit_price_target > 0 and snap.best_bid_price >= pos.exit_price_target:
                    self._execute_real_board_take_profit_unlocked(snap)
            elif pos.side == "SHORT":
                if pos.exit_price_target > 0 and snap.best_ask_price <= pos.exit_price_target:
                    self._execute_real_board_take_profit_unlocked(snap)
            return

        if pos.side == "LONG":
            sl_thresh = pos.entry_price * (1 - cfg.stop_loss_pct)
            if pos.exit_price_target > 0 and snap.best_bid_price >= pos.exit_price_target:
                self._exit_take_profit(snap, fill_price=pos.exit_price_target)
            elif snap.best_bid_price <= sl_thresh:
                self._exit_stop_loss(snap)

        elif pos.side == "SHORT":
            sl_thresh = pos.entry_price * (1 + cfg.stop_loss_pct)
            if pos.exit_price_target > 0 and snap.best_ask_price <= pos.exit_price_target:
                self._exit_take_profit(snap, fill_price=pos.exit_price_target)
            elif snap.best_ask_price >= sl_thresh:
                self._exit_stop_loss(snap)

    def _is_weekly_maintenance_window(self, now: datetime) -> bool:
        if now.weekday() != _WEEKLY_MAINTENANCE_WEEKDAY:
            return False
        now_t = now.time()
        return _WEEKLY_MAINTENANCE_START <= now_t < _WEEKLY_MAINTENANCE_END

    def _is_daily_maintenance_window(self, now: datetime) -> bool:
        now_t = now.time()
        return _DAILY_MAINTENANCE_START <= now_t < _DAILY_MAINTENANCE_END

    def _is_weekly_pre_maintenance_window(self, now: datetime) -> bool:
        if now.weekday() != _WEEKLY_MAINTENANCE_WEEKDAY:
            return False
        start = datetime.combine(now.date(), _WEEKLY_MAINTENANCE_START)
        pre_start = start - timedelta(minutes=self.maintenance_prepare_minutes)
        return pre_start <= now < start

    def _is_safe_mode_active(self, now: datetime) -> bool:
        if self._safe_mode_until is None:
            return False
        return now < self._safe_mode_until or self._safe_mode_wait_for_recovery

    def _is_entry_blocked(self, now: datetime) -> bool:
        return (
            self._is_weekly_pre_maintenance_window(now)
            or self._is_weekly_maintenance_window(now)
            or self._is_daily_maintenance_window(now)
            or self._is_safe_mode_active(now)
            or _MANUAL_STOP_FLAG_PATH.exists()
        )

    def _update_maintenance_state(self, snap: OrderbookSnapshot, now: datetime) -> None:
        """
        メンテナンス状態を更新し、必要に応じてポジション安全化とログ出力を行う。
        """
        if self._safe_mode_until is not None and now >= self._safe_mode_until and not self._safe_mode_wait_for_recovery:
            self._safe_mode_until = None

        in_pre = self._is_weekly_pre_maintenance_window(now)
        in_regular = self._is_weekly_maintenance_window(now)
        in_daily = self._is_daily_maintenance_window(now)
        in_safe = self._is_safe_mode_active(now)
        manual_stop = _MANUAL_STOP_FLAG_PATH.exists()

        if manual_stop:
            self.engine_status = "STOPPING"
            if self.position.side is not None:
                if self.trading_mode == "real":
                    if self.position.is_pending:
                        # pending エントリーは建玉決済ではなく cancel/adopt 経路
                        order_id = self.position.entry_order_id
                        side = self.position.side
                        self._force_cancel_maintenance(snap)
                        if self.position.is_pending:
                            self._emit_critical_alert(
                                "[ALERT] manual stop: pending entry cancel aborted\n"
                                f"orderId={order_id}\n"
                                f"side={side}\n"
                                "detail=entry order may still be live on GMO; will retry"
                            )
                        elif self.position.side is None:
                            self._emit_critical_alert(
                                "[ALERT] manual stop: pending entry order cancelled\n"
                                f"orderId={order_id}\n"
                                f"side={side}"
                            )
                        # adopted_fill: _adopt_open_position_after_failed_cancel が通知済み
                    else:
                        self._force_close_real(snap)
                elif self.position.is_pending:
                    self._force_cancel_maintenance(snap)
                else:
                    self._force_close_maintenance(snap)
        else:
            self.engine_status = "RUNNING"

        if in_pre and self.position.side is not None and self.maintenance_pre_action == "close":
            if self.position.is_pending:
                self._force_cancel_maintenance(snap)
            elif self.trading_mode == "real":
                self._force_close_real(snap)
            else:
                self._force_close_maintenance(snap)

        if in_pre and self.position.side is not None and self.maintenance_pre_action == "wait":
            # wait モードではポジションを維持して監視のみ実施
            pass

        states = []
        if in_pre:
            states.append("pre")
        if in_regular:
            states.append("regular")
        if in_daily:
            states.append("daily")
        if in_safe:
            states.append("safe")
        if manual_stop:
            states.append("manual")
        state = "+".join(states) if states else "normal"
        if state == self._last_guard_state:
            return

        self._last_guard_state = state
        if state == "normal":
            print("[Maintenance] メンテナンス制限を解除。通常取引を再開します。")
            return

        if in_pre:
            if self.maintenance_pre_action == "close":
                print(
                    "[WARNING] [Maintenance] 定期メンテ開始前のため新規エントリーを停止。"
                    "既存ポジションは安全優先でクローズ/キャンセルします。"
                )
            else:
                print(
                    "[WARNING] [Maintenance] 定期メンテ開始前のため新規エントリーを停止。"
                    "既存ポジションは wait モードで監視継続します。"
                )
        if in_regular:
            print("[WARNING] [Maintenance] 定期メンテナンスのため待機中（毎週土曜 09:00-11:00 JST）。")
        if in_daily:
            print("[WARNING] [Maintenance] 定期メンテナンスのため待機中（毎日 05:55-06:30 JST）。")
        if in_safe:
            until_txt = self._safe_mode_until.strftime("%H:%M:%S") if self._safe_mode_until else "unknown"
            if self._safe_mode_wait_for_recovery:
                print(
                    "[WARNING] [Alert] メンテナンス延長を検知。"
                    f"最低待機期限 {until_txt} を過ぎても、正常応答確認まで新規取引を停止します。"
                )
            else:
                print(f"[WARNING] [Alert] セーフモード継続中。新規取引停止（minimum until {until_txt}）。")

    # ------------------------------------------------------------------ #
    #  Phase 3: 新規エントリー判定                                        #
    # ------------------------------------------------------------------ #

    def _check_new_entry(self, snap: OrderbookSnapshot) -> None:
        active_profile = get_active_profile(self._profiles, self._now_minute())
        cfg = active_profile.config
        self.config = cfg
        signal = evaluate(snap, self.position, self.config)
        if signal == Signal.BUY_ENTRY:
            self._enter_long(snap)
            if self.position.side is not None:
                self._locked_config = cfg
                self._locked_profile_name = active_profile.name
                self.config = cfg
        elif signal == Signal.SELL_ENTRY:
            self._enter_short(snap)
            if self.position.side is not None:
                self._locked_config = cfg
                self._locked_profile_name = active_profile.name
                self.config = cfg

    # ------------------------------------------------------------------ #
    #  Entry handlers                                                      #
    # ------------------------------------------------------------------ #

    def initialize_daily_loss_state(
        self,
        *,
        persisted_trading_day_date: Optional[str] = None,
        persisted_daily_start_balance: Optional[float] = None,
        persisted_daily_realized_pnl: Optional[float] = None,
        persisted_daily_win_count: Optional[int] = None,
        persisted_daily_loss_count: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """
        live_state.db から読み出した日次損失追跡状態を引き継ぐ／新規サイクル開始する。
        trading_day_date が現在サイクルと一致すれば残高・実現損益を保持。異なればリセット。
        """
        current_day = calc_trading_day_date(now)
        if (
            persisted_trading_day_date == current_day
            and persisted_daily_start_balance is not None
        ):
            self.trading_day_date = current_day
            self.daily_start_balance = float(persisted_daily_start_balance)
            self.daily_realized_pnl = float(persisted_daily_realized_pnl or 0.0)
            self._daily_win_count = int(persisted_daily_win_count or 0)
            self._daily_loss_count = int(persisted_daily_loss_count or 0)
            return

        self.trading_day_date = current_day
        self.daily_start_balance = float(self.jpy_balance)
        self.daily_realized_pnl = 0.0
        self._daily_win_count = 0
        self._daily_loss_count = 0

    def restore_persisted_account_state(
        self,
        *,
        jpy_balance: Optional[float] = None,
        win_count: Optional[int] = None,
        loss_count: Optional[int] = None,
        total_gross_win: Optional[float] = None,
        total_gross_loss: Optional[float] = None,
        cumulative_pnl: Optional[float] = None,
    ) -> None:
        """
        live_state.db から読み出した口座残高・成績カウンタを復元する。
        値が None の項目はコンストラクタ既定値のまま維持する。
        """
        if jpy_balance is not None:
            self.jpy_balance = float(jpy_balance)
        if win_count is not None:
            self._win_count = int(win_count)
        if loss_count is not None:
            self._loss_count = int(loss_count)
        if total_gross_win is not None:
            self._total_gross_win = float(total_gross_win)
        if total_gross_loss is not None:
            self._total_gross_loss = float(total_gross_loss)
        if cumulative_pnl is not None:
            self._cumulative_pnl = float(cumulative_pnl)

    @staticmethod
    def _normalize_persisted_side(side: Optional[str]) -> Optional[str]:
        if side is None:
            return None
        normalized = str(side).strip().upper()
        if normalized in {"", "NONE", "FLAT", "NULL"}:
            return None
        if normalized in {"LONG", "SHORT"}:
            return normalized
        return None

    @staticmethod
    def _parse_persisted_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    def expected_jpy_balance(self) -> float:
        """
        会計恒等式に基づく期待現金残高。
        FLAT/SHORT: initial_jpy + cumulative_pnl
        LONG (virtual): 上記から entry_price * size を差し引いた拘束後残高
        LONG (real): 想定元本を拘束しないため flat のまま
        """
        flat = float(self._initial_jpy) + float(self._cumulative_pnl)
        pos = self.position
        if (
            pos.side == "LONG"
            and pos.entry_price > 0
            and pos.size > 0
            and self.trading_mode != "real"
        ):
            return flat - (pos.entry_price * pos.size)
        return flat

    def _lock_profile_for_restored_position(
        self,
        locked_profile_name: Optional[str] = None,
    ) -> None:
        """復元したオープンポジション向けにプロファイル設定をロックする。"""
        target_name = str(locked_profile_name or "").strip()
        if target_name:
            for profile in self._profiles:
                if profile.name == target_name:
                    self._locked_config = profile.config
                    self._locked_profile_name = profile.name
                    self.config = profile.config
                    return
        active = get_active_profile(self._profiles, self._now_minute())
        self._locked_config = active.config
        self._locked_profile_name = active.name
        self.config = active.config

    def _alert_position_restore_failure(
        self,
        *,
        reason: str,
        estimated_loss_jpy: Optional[float],
        details: Dict[str, Any],
    ) -> None:
        loss_txt = (
            f"{estimated_loss_jpy:,.0f}"
            if estimated_loss_jpy is not None
            else "unknown"
        )
        message = (
            "[ALERT] position restore failed\n"
            f"reason={reason}\n"
            f"estimated_loss_jpy={loss_txt}\n"
            f"jpy_balance={self.jpy_balance:,.2f}\n"
            f"cumulative_pnl={self._cumulative_pnl:,.2f}\n"
            f"details={details}"
        )
        self._safe_console_print(message)
        if self._on_critical_alert is not None:
            try:
                self._on_critical_alert(message)
            except Exception as exc:
                self._safe_console_print(
                    f"[WARN] critical alert notify failed: {exc}"
                )

    def _fallback_release_locked_capital(
        self,
        *,
        reason: str,
        position_side: Optional[str],
        position_entry_price: Optional[float],
        position_size: Optional[float],
    ) -> Dict[str, Any]:
        """
        ポジション復元不能時: 分かる範囲で拘束資金を現金へ戻し、FLAT にする。
        金額を機械算出できない場合も黙殺せずアラートする。
        """
        side = self._normalize_persisted_side(position_side)
        entry = float(position_entry_price or 0.0)
        size = float(position_size or 0.0)
        refunded = 0.0
        estimated_loss: Optional[float] = None

        # real はエントリー時に想定元本を拘束しないため、LONG でも返却しない
        if (
            self.trading_mode != "real"
            and side == "LONG"
            and entry > 0
            and size > 0
        ):
            refunded = entry * size
            self.jpy_balance += refunded
            estimated_loss = 0.0
        else:
            # 拘束額不明: 期待FLAT残高との差を推定損失として報告
            expected_flat = float(self._initial_jpy) + float(self._cumulative_pnl)
            estimated_loss = max(0.0, expected_flat - float(self.jpy_balance))

        self.position = PositionState()
        self._position_filled_at = None
        self._pending_order_placed_at = None
        self._clear_locked_profile()
        details = {
            "position_side": side,
            "position_entry_price": entry if entry > 0 else None,
            "position_size": size if size > 0 else None,
            "refunded_jpy": refunded,
        }
        self._alert_position_restore_failure(
            reason=reason,
            estimated_loss_jpy=estimated_loss,
            details=details,
        )
        result = {
            "status": "fallback",
            "reason": reason,
            "refunded_jpy": refunded,
            "estimated_loss_jpy": estimated_loss,
            "details": details,
        }
        self._record_position_restore_event(result)
        return result

    def _record_position_restore_event(self, result: Dict[str, Any]) -> None:
        """起動時ポジション復元の発生を日次レポート用に jsonl へ残す。"""
        status = str(result.get("status") or "")
        if status not in {"restored", "fallback"}:
            return
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / "position_restore_events.jsonl"
            record = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "trading_day": calc_trading_day_date(),
                "status": status,
                "side": result.get("side") or (result.get("details") or {}).get("position_side"),
                "reason": result.get("reason"),
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            self._safe_console_print(
                f"[WARN] position restore event log failed: {exc}"
            )

    @staticmethod
    def _parse_optional_order_id(value: Optional[object]) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def restore_persisted_position(
        self,
        *,
        position_side: Optional[str] = None,
        position_entry_price: Optional[float] = None,
        position_size: Optional[float] = None,
        position_is_pending: Optional[object] = None,
        position_exit_target: Optional[float] = None,
        position_filled_at: Optional[str] = None,
        pending_order_placed_at: Optional[str] = None,
        entry_order_id: Optional[object] = None,
        tp_order_id: Optional[object] = None,
        sl_order_id: Optional[object] = None,
        position_id: Optional[object] = None,
        locked_profile_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        live_state.db のオープンポジションを VirtualTrader.position へ復元する。
        復元不能時は拘束資金の返却（可能な範囲）と Telegram アラートを行う。
        """
        raw_side = position_side
        side = self._normalize_persisted_side(position_side)
        if side is None:
            raw = str(raw_side).strip() if raw_side is not None else ""
            if raw and raw.upper() not in {"", "NONE", "FLAT", "NULL"}:
                return self._fallback_release_locked_capital(
                    reason="invalid_position_side",
                    position_side=None,
                    position_entry_price=position_entry_price,
                    position_size=position_size,
                )
            self.position = PositionState()
            self._position_filled_at = None
            self._pending_order_placed_at = None
            return {"status": "flat", "reason": "no_open_position"}

        try:
            entry = float(position_entry_price) if position_entry_price is not None else 0.0
            size = float(position_size) if position_size is not None else 0.0
        except (TypeError, ValueError):
            return self._fallback_release_locked_capital(
                reason="invalid_numeric_fields",
                position_side=side,
                position_entry_price=None,
                position_size=None,
            )

        if entry <= 0 or size <= 0:
            return self._fallback_release_locked_capital(
                reason="incomplete_position_fields",
                position_side=side,
                position_entry_price=entry,
                position_size=size,
            )

        is_pending = bool(int(position_is_pending or 0))
        try:
            exit_target = (
                float(position_exit_target)
                if position_exit_target is not None
                else 0.0
            )
        except (TypeError, ValueError):
            exit_target = 0.0

        self._lock_profile_for_restored_position(locked_profile_name)
        if not is_pending and exit_target <= 0:
            exit_target = (
                entry * (1 + self.config.take_profit_pct)
                if side == "LONG"
                else entry * (1 - self.config.take_profit_pct)
            )

        self.position = PositionState(
            side=side,
            entry_price=entry,
            size=size,
            is_pending=is_pending,
            exit_price_target=exit_target,
            entry_order_id=self._parse_optional_order_id(entry_order_id),
            tp_order_id=self._parse_optional_order_id(tp_order_id),
            sl_order_id=self._parse_optional_order_id(sl_order_id),
            position_id=self._parse_optional_order_id(position_id),
        )
        if is_pending:
            self._position_filled_at = None
            placed_at = self._parse_persisted_datetime(pending_order_placed_at)
            self._pending_order_placed_at = placed_at
            if placed_at is None:
                self._safe_console_print(
                    "[WARN] pending order placed_at could not be restored;"
                    " time-based entry timeout will be skipped for this order"
                    " (deviation and imbalance cancel still apply)"
                )
        else:
            filled_at = self._parse_persisted_datetime(position_filled_at)
            self._position_filled_at = filled_at if filled_at is not None else datetime.now()
            self._pending_order_placed_at = None

        self._safe_console_print(
            "[OK] restored open position:"
            f" side={side} size={size:.6f} entry={entry:,.0f}"
            f" pending={is_pending} exit_target={exit_target:,.0f}"
        )
        result = {
            "status": "restored",
            "side": side,
            "size": size,
            "entry_price": entry,
            "is_pending": is_pending,
            "exit_price_target": exit_target,
        }
        self._record_position_restore_event(result)
        return result

    def reconcile_real_state_on_startup(
        self,
        *,
        trigger_safety_stop: Optional[
            Callable[[str, Optional[Dict[str, Any]]], None]
        ] = None,
        state_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """
        real mode 起動時: live_state 復元結果を GMO 建玉・有効注文と突き合わせる。
        virtual では何もしない。
        """
        if self.trading_mode != "real":
            return {"status": "skipped", "reason": "not_real"}

        path = state_path or _REAL_STARTUP_RECONCILE_STATE_PATH
        try:
            open_positions = fetch_open_positions()
            active_orders = fetch_active_orders()
        except Exception as exc:
            self._emit_critical_alert(
                "[ALERT] startup reconcile: failed to fetch GMO state\n"
                f"error={exc}"
            )
            return {"status": "error", "reason": "gmo_fetch_failed", "error": str(exc)}

        with self._lock:
            pos = self.position
            if pos.side is None:
                _clear_startup_reconcile_state(path)
                return {"status": "flat"}

            if pos.is_pending:
                return self._reconcile_pending_on_startup_unlocked(
                    open_positions=open_positions,
                    active_orders=active_orders,
                    state_path=path,
                )
            return self._reconcile_held_on_startup_unlocked(
                open_positions=open_positions,
                active_orders=active_orders,
                state_path=path,
                trigger_safety_stop=trigger_safety_stop,
            )

    def _reconcile_pending_on_startup_unlocked(
        self,
        *,
        open_positions: List[Dict[str, Any]],
        active_orders: List[Dict[str, Any]],
        state_path: Path,
    ) -> Dict[str, Any]:
        pos = self.position
        active_ids = _active_order_id_set(active_orders)
        if pos.entry_order_id is not None and int(pos.entry_order_id) in active_ids:
            _clear_startup_reconcile_state(state_path)
            self._safe_console_print(
                "[OK] startup reconcile: pending entry order still active"
                f" orderId={pos.entry_order_id}"
            )
            return {"status": "ok", "case": "pending_order_live"}

        matched = _match_open_position_for_pending_entry(pos, open_positions)
        if matched is not None:
            self._sync_held_position_from_gmo_open(
                matched,
                context="startup_reconcile_pending_filled",
                place_tp_sl=True,
            )
            _clear_startup_reconcile_state(state_path)
            return {
                "status": "adopted_fill",
                "case": "pending_filled_on_exchange",
                "position_id": matched.get("positionId"),
            }

        self._fallback_release_locked_capital(
            reason="startup_reconcile_pending_gone",
            position_side=pos.side,
            position_entry_price=pos.entry_price,
            position_size=pos.size,
        )
        _clear_startup_reconcile_state(state_path)
        return {"status": "cleared", "case": "pending_gone"}

    def _reconcile_held_on_startup_unlocked(
        self,
        *,
        open_positions: List[Dict[str, Any]],
        active_orders: List[Dict[str, Any]],
        state_path: Path,
        trigger_safety_stop: Optional[
            Callable[[str, Optional[Dict[str, Any]]], None]
        ],
    ) -> Dict[str, Any]:
        pos = self.position
        matched = _match_open_position_for_held_entry(pos, open_positions)
        if matched is None:
            side = pos.side
            size = pos.size
            entry = pos.entry_price
            self.position = PositionState()
            self._position_filled_at = None
            self._clear_locked_profile()
            self._emit_critical_alert(
                "[ALERT] startup reconcile: held position missing on GMO\n"
                f"side={side}\n"
                f"entry_price={entry:,.0f}\n"
                f"size={size:.4f}\n"
                "detail=cleared local position; verify account balance manually"
            )
            _clear_startup_reconcile_state(state_path)
            return {"status": "cleared", "case": "held_missing_on_exchange"}

        active_ids = _active_order_id_set(active_orders)
        # 新設計: TP は常設しないため tp_order_id は判定対象外。SL のみ欠落判定する。
        missing_sl = (
            pos.sl_order_id is None or int(pos.sl_order_id) not in active_ids
        )
        if not missing_sl:
            _clear_startup_reconcile_state(state_path)
            self._safe_console_print(
                "[OK] startup reconcile: held position and SL order match"
            )
            return {"status": "ok", "case": "held_orders_live"}

        missing_legs = ["sl"]
        fingerprint = _open_position_fingerprint(matched, pos.side, pos.size)
        prev = _load_startup_reconcile_state(state_path)
        if (
            isinstance(prev, dict)
            and prev.get("fingerprint") == fingerprint
            and prev.get("reordered") is True
        ):
            details = {
                "fingerprint": fingerprint,
                "position_id": matched.get("positionId"),
                "missing": missing_legs,
                "side": pos.side,
                "size": pos.size,
            }
            self._safe_console_print(
                "[WARN] startup reconcile: persistent SL mismatch after reorder;"
                " triggering safety stop"
            )
            if trigger_safety_stop is not None:
                try:
                    trigger_safety_stop(
                        "startup_reconcile_persistent_mismatch",
                        details,
                    )
                except Exception as exc:
                    self._safe_console_print(
                        f"[WARN] safety stop trigger failed: {exc}"
                    )
            return {"status": "safety_stop", "case": "persistent_mismatch", **details}

        # 建玉価格を基準に SL のみ再発注（TP は常設しない）
        try:
            fill_price = float(matched.get("price", pos.entry_price))
        except (TypeError, ValueError):
            fill_price = float(pos.entry_price)
        try:
            fill_size = float(matched.get("size", pos.size))
        except (TypeError, ValueError):
            fill_size = float(pos.size)
        if fill_price <= 0:
            fill_price = float(pos.entry_price)
        if fill_size <= 0:
            fill_size = float(pos.size)
        tp_price = (
            fill_price * (1 + self.config.take_profit_pct)
            if pos.side == "LONG"
            else fill_price * (1 - self.config.take_profit_pct)
        )
        self.position = PositionState(
            side=pos.side,
            entry_price=fill_price,
            size=fill_size,
            is_pending=False,
            exit_price_target=tp_price,
            entry_order_id=pos.entry_order_id,
            tp_order_id=None,
            sl_order_id=None,
            position_id=self._parse_optional_order_id(matched.get("positionId")),
        )
        if self._position_filled_at is None:
            self._position_filled_at = datetime.now()
        self._pending_order_placed_at = None
        self._place_real_tp_sl_orders(place_tp=False, place_sl=True)
        _save_startup_reconcile_state(
            state_path,
            {
                "fingerprint": fingerprint,
                "position_id": matched.get("positionId"),
                "side": self.position.side,
                "size": self.position.size,
                "missing": missing_legs,
                "detected_at": datetime.now().isoformat(timespec="seconds"),
                "reordered": True,
            },
        )
        self._safe_console_print(
            "[OK] startup reconcile: reordered missing exit orders"
            f" missing={missing_legs}"
            f" fingerprint={fingerprint}"
        )
        return {
            "status": "reordered",
            "case": "held_missing_exit_orders",
            "missing": missing_legs,
            "fingerprint": fingerprint,
        }

    def _sync_held_position_from_gmo_open(
        self,
        item: Dict[str, Any],
        *,
        context: str,
        place_tp_sl: bool,
    ) -> None:
        """GMO 建玉を保有中ポジションとして内部へ同期する。"""
        pos = self.position
        api_side = str(item.get("side", "")).upper()
        if api_side == "BUY":
            position_side = "LONG"
        elif api_side == "SELL":
            position_side = "SHORT"
        else:
            position_side = pos.side or "LONG"
        try:
            fill_price = float(item.get("price", pos.entry_price))
        except (TypeError, ValueError):
            fill_price = float(pos.entry_price)
        try:
            fill_size = float(item.get("size", pos.size))
        except (TypeError, ValueError):
            fill_size = float(pos.size)
        if fill_price <= 0:
            fill_price = float(pos.entry_price)
        if fill_size <= 0:
            fill_size = float(pos.size)
        tp_price = (
            fill_price * (1 + self.config.take_profit_pct)
            if position_side == "LONG"
            else fill_price * (1 - self.config.take_profit_pct)
        )
        self._position_filled_at = datetime.now()
        self._pending_order_placed_at = None
        self.position = PositionState(
            side=position_side,
            entry_price=fill_price,
            size=fill_size,
            is_pending=False,
            exit_price_target=tp_price,
            entry_order_id=pos.entry_order_id,
            tp_order_id=None,
            sl_order_id=None,
            position_id=self._parse_optional_order_id(item.get("positionId")),
        )
        self._safe_console_print(
            f"[OK] synced held position from GMO ({context}):"
            f" side={position_side}"
            f" entry={fill_price:,.0f}"
            f" size={fill_size:.4f}"
            f" positionId={item.get('positionId')}"
        )
        if place_tp_sl:
            self._place_real_tp_sl_orders()

    def check_account_integrity(
        self,
        *,
        mid_price: Optional[float] = None,
        last_total_assets: Optional[float] = None,
        tolerance_jpy: float = ACCOUNT_INTEGRITY_TOLERANCE_JPY,
    ) -> Dict[str, Any]:
        """
        起動時の簡易整合性チェック。
        1) 現金 vs 会計上の期待現金
           - real: daily_start_balance + daily_realized_pnl
             （日次リセット時の実残高基準。initial_jpy の手動更新を不要にする）
           - virtual: expected_jpy_balance()
             （initial_jpy + cumulative_pnl、LONG は想定元本差し引き）
        2) (任意) 復元後総資産 vs 前回 live_state 書き込み時点の総資産
        """
        if self.trading_mode == "real":
            expected_jpy = (
                float(self.daily_start_balance) + float(self.daily_realized_pnl)
            )
        else:
            expected_jpy = self.expected_jpy_balance()
        jpy_gap = float(self.jpy_balance) - expected_jpy
        mid = float(mid_price) if mid_price is not None and mid_price > 0 else 0.0
        current_total = self.total_assets(mid) if mid > 0 else float(self.jpy_balance)
        last_gap: Optional[float] = None
        if last_total_assets is not None:
            last_gap = current_total - float(last_total_assets)

        breached = abs(jpy_gap) > float(tolerance_jpy)
        if last_gap is not None and abs(last_gap) > float(tolerance_jpy):
            breached = True

        result = {
            "ok": not breached,
            "jpy_balance": float(self.jpy_balance),
            "expected_jpy_balance": expected_jpy,
            "jpy_gap": jpy_gap,
            "current_total_assets": current_total,
            "last_total_assets": (
                float(last_total_assets) if last_total_assets is not None else None
            ),
            "last_total_gap": last_gap,
            "tolerance_jpy": float(tolerance_jpy),
            "position_side": self.position.side,
        }
        if breached:
            message = (
                "[ALERT] account integrity check failed\n"
                f"jpy_balance={self.jpy_balance:,.2f}\n"
                f"expected_jpy={expected_jpy:,.2f}\n"
                f"jpy_gap={jpy_gap:,.2f}\n"
                f"current_total_assets={current_total:,.2f}\n"
                f"last_total_assets="
                f"{last_total_assets if last_total_assets is not None else 'n/a'}\n"
                f"last_total_gap="
                f"{last_gap if last_gap is not None else 'n/a'}\n"
                f"tolerance_jpy={tolerance_jpy:,.0f}\n"
                f"position_side={self.position.side}"
            )
            self._safe_console_print(message)
            if self._on_critical_alert is not None:
                try:
                    self._on_critical_alert(message)
                except Exception as exc:
                    self._safe_console_print(
                        f"[WARN] critical alert notify failed: {exc}"
                    )
        else:
            self._safe_console_print(
                "[OK] account integrity check:"
                f" jpy_gap={jpy_gap:,.2f}"
                f" tolerance={tolerance_jpy:,.0f}"
            )
        return result

    def check_daily_loss_limit(self) -> bool:
        """
        実現損益が日次損失上限に達／超過していれば True。
        True の場合、既存の manual_stop 経路（on_daily_loss_limit -> trading_engine._trigger_safety_stop）を呼ぶ。
        """
        limit_jpy = float(self.daily_start_balance) * float(self.daily_loss_limit_pct)
        if self.daily_realized_pnl > -limit_jpy:
            return False

        details = {
            "daily_realized_pnl": float(self.daily_realized_pnl),
            "daily_start_balance": float(self.daily_start_balance),
            "daily_loss_limit_pct": float(self.daily_loss_limit_pct),
            "limit_jpy": float(limit_jpy),
        }
        self._safe_console_print(
            "[ALERT] daily loss limit reached:"
            f" realized_pnl={self.daily_realized_pnl:,.0f}"
            f" limit=-{limit_jpy:,.0f}"
            f" start_balance={self.daily_start_balance:,.0f}"
            f" pct={self.daily_loss_limit_pct:.2%}"
        )
        if self._on_daily_loss_limit is not None:
            self._on_daily_loss_limit(details)
        return True

    def _reset_imbalance_reversal_debounce(self) -> None:
        self._imbalance_reversal_since = None

    def _imbalance_reversal_debounce_elapsed(self) -> bool:
        """
        imbalance 反転がデバウンス時間以上連続していれば True。
        初回観測時は開始時刻を記録して False を返す。
        """
        now = time.time()
        if self._imbalance_reversal_since is None:
            self._imbalance_reversal_since = now
            return False
        return (now - float(self._imbalance_reversal_since)) >= float(
            IMBALANCE_REVERSAL_DEBOUNCE_SEC
        )

    def _should_block_entry_by_imbalance_any_side_cooldown(self) -> bool:
        """
        imbalance CANCEL 直後の方向不問クールダウン。
        True ならエントリー試行をブロック（on_order_placed / レート制限カウント対象外）。
        """
        if self._last_imbalance_cancel_any_side_ts is None:
            return False
        elapsed = time.time() - float(self._last_imbalance_cancel_any_side_ts)
        cooldown_sec = float(ENTRY_COOLDOWN_AFTER_IMBALANCE_CANCEL_ANY_SIDE_SEC)
        if elapsed >= cooldown_sec:
            return False
        self._safe_console_print(
            f"[SKIP] entry blocked by imbalance any-side cooldown:"
            f" elapsed={elapsed:.3f} cooldown_sec={cooldown_sec:g}"
        )
        return True

    def _should_block_entry_by_cancel_cooldown(self, side: str, price: float) -> bool:
        """
        CANCEL_ORDER 直後なら True（エントリー試行をブロック）。
        - imbalance: 同一方向なら価格が異なってもクールダウン対象
        - timeout/deviation: 従来どおり同一方向かつ同一価格のみ対象
        ブロック時は発注しないため on_order_placed / レート制限カウントの対象外。
        """
        last = self._last_cancel_by_side.get(side)
        if last is None:
            return False
        cancel_price = last[0]
        cancel_ts = last[1]
        if len(last) >= 3:
            cooldown_sec = float(last[2])
        else:
            cooldown_sec = float(ENTRY_COOLDOWN_AFTER_CANCEL_SEC)
        cancel_reason = last[3] if len(last) >= 4 else None

        require_same_price = cancel_reason in {
            CANCEL_REASON_TIMEOUT,
            CANCEL_REASON_DEVIATION,
        }
        if cancel_reason is None and cooldown_sec >= float(
            ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC
        ):
            # 旧形式タプル互換: 長いクールダウンは timeout/deviation 扱い
            require_same_price = True
        if require_same_price and cancel_price != price:
            return False

        elapsed = time.time() - float(cancel_ts)
        if elapsed >= cooldown_sec:
            return False
        self._safe_console_print(
            f"[SKIP] entry blocked by cooldown:"
            f" side={side} price={price:,.0f} elapsed={elapsed:.3f}"
            f" cooldown_sec={cooldown_sec:g}"
        )
        return True

    def _pending_timeout_minutes_for_profile(self) -> float:
        profile_name = self._locked_profile_name or self.active_profile_name
        return float(
            ENTRY_PENDING_TIMEOUT_MINUTES_BY_PROFILE.get(
                str(profile_name),
                ENTRY_PENDING_TIMEOUT_MINUTES_DEFAULT,
            )
        )

    def _pending_timeout_conditions(
        self, snap: OrderbookSnapshot
    ) -> Tuple[Optional[float], Optional[float], bool, bool]:
        """
        未約定指値の時間・乖離条件を評価する。
        戻り値: (elapsed_minutes, deviation_pct, time_met, deviation_met)
        """
        pos = self.position
        elapsed_minutes: Optional[float] = None
        time_met = False
        if self._pending_order_placed_at is not None:
            elapsed_sec = (datetime.now() - self._pending_order_placed_at).total_seconds()
            elapsed_minutes = max(0.0, elapsed_sec / 60.0)
            time_met = elapsed_minutes >= self._pending_timeout_minutes_for_profile()

        deviation_pct: Optional[float] = None
        deviation_met = False
        entry = float(pos.entry_price)
        if entry > 0:
            deviation_ratio = abs(float(snap.mid_price) - entry) / entry
            deviation_pct = deviation_ratio * 100.0
            threshold = float(self.config.stop_loss_pct) * ENTRY_PENDING_DEVIATION_SL_RATIO
            deviation_met = deviation_ratio >= threshold

        return elapsed_minutes, deviation_pct, time_met, deviation_met

    def _cooldown_sec_for_cancel_reason(self, cancel_reason: str) -> float:
        if cancel_reason in {CANCEL_REASON_TIMEOUT, CANCEL_REASON_DEVIATION}:
            return float(ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC)
        return float(ENTRY_COOLDOWN_AFTER_CANCEL_SEC)

    def _enter_long(self, snap: OrderbookSnapshot) -> None:
        """
        買い指値 (Maker): Best Bid + offset の価格で板に並ぶ（is_pending=True）。
        virtual: 約定は _check_pending_fill で確認する。
        real: GMO へ指値発注し、約定は Private WS execution で確認する。
        """
        if self._before_entry_order is not None and self._before_entry_order():
            return

        price = snap.best_bid_price + self.config.maker_price_offset_jpy
        if self._should_block_entry_by_imbalance_any_side_cooldown():
            return
        if self._should_block_entry_by_cancel_cooldown("BUY", price):
            return
        size = self._calc_trade_size(price)
        if size is None:
            return
        cost  = price * size
        fee   = int(cost * MAKER_FEE_RATE)   # 1円未満切り捨て（負=リベート）
        total = cost + fee

        if self.jpy_balance < total:
            return

        if self.trading_mode == "real":
            self._enter_real_limit(
                snap,
                gmo_side="BUY",
                position_side="LONG",
                price=price,
                size=size,
                balance_delta=-fee,
            )
            return

        self.jpy_balance -= total
        self.position = PositionState(side="LONG", entry_price=price, size=size, is_pending=True)
        self._pending_order_placed_at = datetime.now()

        self._record_and_print(
            snap=snap, side="BUY", order_type="MAKER",
            price=price, size=size, fee=fee, pnl=-fee, reason="ENTRY",
        )
        if self._on_order_placed is not None:
            self._on_order_placed()

    def _enter_short(self, snap: OrderbookSnapshot) -> None:
        """
        売り指値 (Maker): Best Ask - offset の価格で板に並ぶ（is_pending=True）。
        ショートは仮想マージン取引。リベートのみ即時加算。
        virtual: 約定は _check_pending_fill で確認する。
        real: GMO へ指値発注し、約定は Private WS execution で確認する。
        """
        if self._before_entry_order is not None and self._before_entry_order():
            return

        price = snap.best_ask_price - self.config.maker_price_offset_jpy
        if self._should_block_entry_by_imbalance_any_side_cooldown():
            return
        if self._should_block_entry_by_cancel_cooldown("SELL", price):
            return
        size = self._calc_trade_size(price)
        if size is None:
            return
        fee   = int(price * size * MAKER_FEE_RATE)  # 1円未満切り捨て（負=リベート）

        if self.trading_mode == "real":
            self._enter_real_limit(
                snap,
                gmo_side="SELL",
                position_side="SHORT",
                price=price,
                size=size,
                balance_delta=-fee,
            )
            return

        self.jpy_balance -= fee   # fee < 0 → 残高が増える（リベート受取）
        self.position = PositionState(side="SHORT", entry_price=price, size=size, is_pending=True)
        self._pending_order_placed_at = datetime.now()

        self._record_and_print(
            snap=snap, side="SELL", order_type="MAKER",
            price=price, size=size, fee=fee, pnl=-fee, reason="ENTRY",
        )
        if self._on_order_placed is not None:
            self._on_order_placed()

    def _enter_real_limit(
        self,
        snap: OrderbookSnapshot,
        *,
        gmo_side: str,
        position_side: str,
        price: float,
        size: float,
        balance_delta: float,
    ) -> None:
        """
        real mode: GMO へ Maker 指値（LIMIT + SOK）を発注し、pending 状態を保持する。
        発注失敗時はエントリーをスキップ（例外は外へ伝播しない）。
        """
        try:
            order_id_raw = gmo_order(
                side=gmo_side,
                execution_type="LIMIT",
                time_in_force="SOK",
                price=price,
                size=size,
            )
            order_id = int(order_id_raw)
        except Exception as exc:
            ts = datetime.now().strftime("%H:%M:%S")
            self._safe_console_print(
                f"[{ts}] [SKIP] real entry order failed:"
                f" side={gmo_side} price={price:,.0f} size={size:.4f}: {exc}"
            )
            return

        self.jpy_balance += balance_delta
        self.position = PositionState(
            side=position_side,
            entry_price=price,
            size=size,
            is_pending=True,
            entry_order_id=order_id,
        )
        self._pending_order_placed_at = datetime.now()
        entry_side = "BUY" if position_side == "LONG" else "SELL"
        place_fee = int(price * size * MAKER_FEE_RATE)
        self._record_and_print(
            snap=snap,
            side=entry_side,
            order_type="MAKER",
            price=price,
            size=size,
            fee=place_fee,
            pnl=-place_fee,
            reason="ENTRY_PENDING",
        )
        if self._on_order_placed is not None:
            self._on_order_placed()
        ts = datetime.now().strftime("%H:%M:%S")
        self._safe_console_print(
            f"[{ts}] [OK] [REAL-ORDER] {position_side}"
            f" orderId={order_id}"
            f" @ {price:,.0f} JPY"
            f"  size={size:.4f} BTC"
            f"  pending=True"
        )

    # ------------------------------------------------------------------ #
    #  Exit handlers                                                       #
    # ------------------------------------------------------------------ #

    def _exit_take_profit(self, snap: OrderbookSnapshot, fill_price: float) -> None:
        """
        利確決済 (Maker)。fill_price は exit_price_target（指値価格）。
        「突き抜け」が確認されてから fill_price で約定したとみなすことで
        楽観バイアスを排除する。
        """
        pos = self.position

        if pos.side == "LONG":
            fee       = int(fill_price * pos.size * MAKER_FEE_RATE)
            gross_pnl = (fill_price - pos.entry_price) * pos.size
            net_pnl   = gross_pnl - fee
            self.jpy_balance += fill_price * pos.size - fee
            order_type, side = "MAKER", "SELL"
        else:  # SHORT
            fee       = int(fill_price * pos.size * MAKER_FEE_RATE)
            gross_pnl = (pos.entry_price - fill_price) * pos.size
            net_pnl   = gross_pnl - fee
            self.jpy_balance += net_pnl
            order_type, side = "MAKER", "BUY"

        resolved_profile_name = self._locked_profile_name or self.active_profile_name
        self._update_kpi(net_pnl)
        self.daily_realized_pnl += net_pnl
        self.check_daily_loss_limit()
        dur = self._calc_duration_sec()
        self.position = PositionState()
        self._position_filled_at = None
        self._pending_order_placed_at = None
        self._clear_locked_profile()
        self._record_and_print(
            snap=snap, side=side, order_type=order_type,
            price=fill_price, size=pos.size, fee=fee, pnl=net_pnl, reason="TAKE_PROFIT",
            duration_sec=dur, cumulative_pnl=self._cumulative_pnl,
            profile_name=resolved_profile_name,
        )

    def _exit_stop_loss(self, snap: OrderbookSnapshot) -> None:
        """損切り決済 (Taker)。現在の最良気配値で即時成行約定"""
        pos = self.position

        if pos.side == "LONG":
            exit_price = snap.best_bid_price
            fee        = int(exit_price * pos.size * TAKER_FEE_RATE)
            gross_pnl  = (exit_price - pos.entry_price) * pos.size
            net_pnl    = gross_pnl - fee
            self.jpy_balance += exit_price * pos.size - fee
            order_type, side = "TAKER", "SELL"
        else:  # SHORT
            exit_price = snap.best_ask_price
            fee        = int(exit_price * pos.size * TAKER_FEE_RATE)
            gross_pnl  = (pos.entry_price - exit_price) * pos.size
            net_pnl    = gross_pnl - fee
            self.jpy_balance += net_pnl
            order_type, side = "TAKER", "BUY"

        resolved_profile_name = self._locked_profile_name or self.active_profile_name
        self._update_kpi(net_pnl)
        self.daily_realized_pnl += net_pnl
        self.check_daily_loss_limit()
        dur = self._calc_duration_sec()
        self.position = PositionState()
        self._position_filled_at = None
        self._pending_order_placed_at = None
        self._clear_locked_profile()
        self._record_and_print(
            snap=snap, side=side, order_type=order_type,
            price=exit_price, size=pos.size, fee=fee, pnl=net_pnl, reason="STOP_LOSS",
            duration_sec=dur, cumulative_pnl=self._cumulative_pnl,
            profile_name=resolved_profile_name,
        )

    def _place_real_tp_sl_orders(
        self,
        *,
        place_tp: bool = True,
        place_sl: bool = True,
    ) -> None:
        """
        real mode: 保有中遷移直後に SL のみ closeOrder(STOP) で常設する。
        TP は板監視 + MARKET 決済のため常設しない（tp_order_id は常に None）。
        place_sl=False のときは既存 sl_order_id を維持する。
        place_tp は互換のため残すが無視する。
        例外は外へ出さず、SL 失敗時は緊急通知する。virtual では何もしない。
        """
        if self.trading_mode != "real":
            return
        pos = self.position
        if pos.side is None or pos.is_pending:
            return
        if not place_sl:
            # TP は常設しない。既存 tp_order_id があってもクリアする。
            if pos.tp_order_id is not None:
                self.position = PositionState(
                    side=pos.side,
                    entry_price=pos.entry_price,
                    size=pos.size,
                    is_pending=False,
                    exit_price_target=pos.exit_price_target,
                    entry_order_id=pos.entry_order_id,
                    tp_order_id=None,
                    sl_order_id=pos.sl_order_id,
                    position_id=pos.position_id,
                )
            return

        cfg = self.config
        size = float(pos.size)
        entry = float(pos.entry_price)
        if pos.side == "LONG":
            exit_side = "SELL"
            sl_price = entry * (1 - cfg.stop_loss_pct)
        else:
            exit_side = "BUY"
            sl_price = entry * (1 + cfg.stop_loss_pct)

        sl_order_id: Optional[int] = None
        sl_error: Optional[BaseException] = None
        ts = datetime.now().strftime("%H:%M:%S")
        position_id = pos.position_id

        if position_id is None:
            sl_error = RuntimeError("position_id is None; cannot place SL closeOrder")
            self._safe_console_print(
                f"[{ts}] [WARN] [REAL-SL] skipped: position_id is None"
            )
        else:
            try:
                # STOP は timeInForce 未指定（API デフォルト FAK）
                sl_order_id = int(
                    gmo_close_order(
                        side=exit_side,
                        execution_type="STOP",
                        price=sl_price,
                        time_in_force=None,
                        settle_position={
                            "positionId": int(position_id),
                            "size": str(size),
                        },
                    )
                )
                self._safe_console_print(
                    f"[{ts}] [OK] [REAL-SL] {pos.side}"
                    f" orderId={sl_order_id}"
                    f" side={exit_side} STOP closeOrder @ {sl_price:,.0f}"
                    f" size={size:.4f}"
                    f" positionId={position_id}"
                )
            except Exception as exc:
                sl_error = exc
                self._safe_console_print(
                    f"[{ts}] [WARN] [REAL-SL] closeOrder failed: {exc}"
                )

        self.position = PositionState(
            side=pos.side,
            entry_price=pos.entry_price,
            size=pos.size,
            is_pending=False,
            exit_price_target=pos.exit_price_target,
            entry_order_id=pos.entry_order_id,
            tp_order_id=None,
            sl_order_id=sl_order_id,
            position_id=pos.position_id,
        )

        if sl_error is None:
            return

        message = (
            "[ALERT] real SL placement failed (unprotected position)\n"
            f"detail=SL closeOrder(STOP) placement failed\n"
            f"side={pos.side}\n"
            f"entry_price={entry:,.0f}\n"
            f"size={size:.4f}\n"
            f"position_id={position_id}\n"
            f"sl_order_id={sl_order_id}\n"
            f"sl_error={sl_error}"
        )
        self._emit_critical_alert(message)

    def _sync_jpy_balance_from_equity_unlocked(self, *, context: str) -> None:
        """決済成功後: equity_jpy を内部残高へ同期。失敗しても例外は外へ出さない。"""
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            real_state = fetch_real_account_state()
            self.jpy_balance = float(real_state["equity_jpy"])
            self._safe_console_print(
                f"[{ts}] [OK] [{context}] synced jpy_balance"
                f" from equity_jpy={self.jpy_balance:,.0f}"
            )
        except Exception as sync_exc:
            self._safe_console_print(
                f"[{ts}] [WARN] [{context}] equity sync failed: {sync_exc}"
            )
            self._emit_critical_alert(
                "\n".join(
                    [
                        f"[ALERT] {context} equity sync failed",
                        "position cleared but jpy_balance was not updated",
                        f"error={sync_exc}",
                    ]
                )
            )

    def _confirm_real_position_closed_unlocked(
        self,
        *,
        position_id: int,
        context: str,
    ) -> bool:
        """openPositions から指定建玉が消えたことを確認する。"""
        ts = datetime.now().strftime("%H:%M:%S")
        for confirm_i in range(1, _FORCE_CLOSE_CONFIRM_MAX_CHECKS + 1):
            remaining = fetch_open_positions()
            still = any(
                int(item.get("positionId", -1)) == int(position_id)
                for item in remaining
            )
            if not still:
                return True
            self._safe_console_print(
                f"[{ts}] [WARN] [{context}] position still present"
                f" after closeOrder"
                f" (confirm {confirm_i}/{_FORCE_CLOSE_CONFIRM_MAX_CHECKS}"
                f" positionId={position_id})"
            )
            if confirm_i < _FORCE_CLOSE_CONFIRM_MAX_CHECKS:
                time.sleep(_FORCE_CLOSE_CONFIRM_RETRY_SEC)
        return False

    def _execute_real_board_take_profit_unlocked(
        self,
        snap: OrderbookSnapshot,
    ) -> None:
        """
        real mode 板監視 TP:
          1) SL 常設注文をキャンセル
          2) closeOrder(MARKET) で成行決済
          3) 建玉消滅確認 + 内部決済 + equity 同期
        """
        pos = self.position
        if pos.side is None or pos.is_pending:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        position_id = pos.position_id
        sl_order_id = pos.sl_order_id
        size = float(pos.size)
        fill_price = float(pos.exit_price_target)
        position_side = pos.side
        if pos.side == "LONG":
            exit_side = "SELL"
        else:
            exit_side = "BUY"

        if position_id is None:
            self._emit_critical_alert(
                "[CRITICAL] REAL MODE BOARD TP ABORTED\n"
                "detail=position_id is None; cannot market close\n"
                f"side={pos.side}\n"
                f"sl_order_id={sl_order_id}"
            )
            return

        # a. SL キャンセル
        if sl_order_id is not None:
            try:
                gmo_cancel_order(int(sl_order_id))
                self._safe_console_print(
                    f"[{ts}] [OK] [REAL-TP] cancelled SL orderId={sl_order_id}"
                )
            except GmoApiError as exc:
                if is_benign_cancel_error(exc):
                    # 2026-08-03: benign(5122/5123)でも内部建玉が残ると
                    # return のみでは宙に浮く。openPositions で分岐する。
                    try:
                        open_positions = fetch_open_positions()
                    except Exception as fetch_exc:
                        self._emit_critical_alert(
                            "[CRITICAL] REAL MODE BOARD TP SL CANCEL BENIGN"
                            " BUT OPEN POSITIONS FETCH FAILED\n"
                            "detail=cannot decide settle vs market close\n"
                            f"side={position_side}\n"
                            f"position_id={position_id}\n"
                            f"sl_order_id={sl_order_id}\n"
                            f"error={fetch_exc}"
                        )
                        return
                    still_open = any(
                        int(item.get("positionId", -1)) == int(position_id)
                        for item in open_positions
                    )
                    if not still_open:
                        self._safe_console_print(
                            f"[{ts}] [OK] [REAL-TP] SL cancel benign"
                            f" and position flat; settle internal"
                            f" orderId={sl_order_id} codes={exc.message_codes}"
                        )
                        self._finalize_real_board_tp_settle_unlocked(
                            snap=snap,
                            position_side=position_side,
                            size=size,
                            fill_price=fill_price,
                            close_oid=None,
                        )
                        return
                    self._safe_console_print(
                        f"[{ts}] [OK] [REAL-TP] SL cancel benign"
                        f" but position remains; continue market close"
                        f" orderId={sl_order_id} codes={exc.message_codes}"
                    )
                else:
                    self._emit_critical_alert(
                        "[CRITICAL] REAL MODE BOARD TP SL CANCEL FAILED\n"
                        "detail=SL cancel failed; position may be unprotected\n"
                        f"side={pos.side}\n"
                        f"position_id={position_id}\n"
                        f"sl_order_id={sl_order_id}\n"
                        f"error={exc}"
                    )
                    return
            except Exception as exc:
                self._emit_critical_alert(
                    "[CRITICAL] REAL MODE BOARD TP SL CANCEL FAILED\n"
                    "detail=SL cancel failed; position may be unprotected\n"
                    f"side={pos.side}\n"
                    f"position_id={position_id}\n"
                    f"sl_order_id={sl_order_id}\n"
                    f"error={exc}"
                )
                return

        # b. MARKET 決済
        try:
            close_oid = gmo_close_order(
                side=exit_side,
                execution_type="MARKET",
                settle_position={
                    "positionId": int(position_id),
                    "size": str(size),
                },
            )
            self._safe_console_print(
                f"[{ts}] [OK] [REAL-TP] MARKET closeOrder accepted"
                f" orderId={close_oid} positionId={position_id}"
            )
        except Exception as exc:
            self._emit_critical_alert(
                "[CRITICAL] REAL MODE BOARD TP MARKET CLOSE FAILED\n"
                "detail=SL cancelled (or absent) but market close failed;"
                " position unprotected\n"
                f"side={pos.side}\n"
                f"position_id={position_id}\n"
                f"sl_order_id={sl_order_id}\n"
                f"error={exc}"
            )
            return

        # c. closeOrder 受理後の confirm/settle は独立保護
        # （2026-08-03: コールバック内 ERR-5008 で settle が中断された）
        settle_ok = False
        last_settle_exc: Optional[BaseException] = None
        for settle_i in range(1, _BOARD_TP_SETTLE_MAX_ATTEMPTS + 1):
            try:
                if self.position.side is None or self.position.is_pending:
                    # 直前試行で settle 済み（sync のみ失敗したケース）
                    self._sync_jpy_balance_from_equity_unlocked(context="REAL-TP")
                    settle_ok = True
                    break
                if not self._confirm_real_position_closed_unlocked(
                    position_id=int(position_id),
                    context="REAL-TP",
                ):
                    raise RuntimeError(
                        "market close sent but position still open"
                    )
                self._finalize_real_board_tp_settle_unlocked(
                    snap=snap,
                    position_side=position_side,
                    size=size,
                    fill_price=fill_price,
                    close_oid=close_oid,
                )
                settle_ok = True
                break
            except Exception as settle_exc:
                last_settle_exc = settle_exc
                self._safe_console_print(
                    f"[{ts}] [WARN] [REAL-TP] confirm/settle attempt"
                    f" {settle_i}/{_BOARD_TP_SETTLE_MAX_ATTEMPTS}"
                    f" failed: {settle_exc}"
                )
                if settle_i < _BOARD_TP_SETTLE_MAX_ATTEMPTS:
                    time.sleep(_BOARD_TP_SETTLE_RETRY_SEC)
        if not settle_ok:
            self._emit_critical_alert(
                "[CRITICAL] REAL MODE BOARD TP SETTLE FAILED\n"
                "detail=closeOrder accepted but confirm/settle did not"
                " complete; internal position may remain\n"
                f"side={position_side}\n"
                f"position_id={position_id}\n"
                f"sl_order_id={sl_order_id}\n"
                f"close_order_id={close_oid}\n"
                f"error={last_settle_exc}"
            )

    def _finalize_real_board_tp_settle_unlocked(
        self,
        *,
        snap: OrderbookSnapshot,
        position_side: str,
        size: float,
        fill_price: float,
        close_oid: Optional[Any],
    ) -> None:
        """板TPの内部決済 + equity 同期（closeOrder 有無どちらからも呼ぶ）。"""
        ts = datetime.now().strftime("%H:%M:%S")
        if fill_price <= 0:
            fill_price = (
                float(snap.best_bid_price)
                if position_side == "LONG"
                else float(snap.best_ask_price)
            )
        target_fill_price = fill_price
        actual_price: Optional[float] = None
        actual_fee: Optional[int] = None
        if close_oid is not None:
            actual_price, actual_fee = gmo_fetch_order_execution_fill(
                int(close_oid)
            )
            # GMO executions 反映遅延の可能性: 初回失敗時のみ 1 回リトライ
            if actual_price is None or actual_price <= 0:
                time.sleep(_BOARD_TP_FILL_FETCH_RETRY_SEC)
                actual_price, actual_fee = gmo_fetch_order_execution_fill(
                    int(close_oid)
                )
        if actual_price is not None and actual_price > 0:
            fill_price = float(actual_price)
        else:
            self._safe_console_print(
                f"[{ts}] [WARN] [REAL-TP] actual fill price unavailable;"
                f" using target price={target_fill_price:,.0f} JPY"
            )
        self._settle_real_exit_from_execution_unlocked(
            fill_price=fill_price,
            fill_size=size,
            reason="TAKE_PROFIT",
            is_take_profit=True,
            actual_fee=actual_fee,
        )
        self._sync_jpy_balance_from_equity_unlocked(context="REAL-TP")

    def _cancel_real_entry_order_or_adopt_fill(self, *, context: str) -> str:
        """
        real mode のエントリー指値キャンセル前処理。

        戻り値:
          "proceed_cancel" … 内部状態をキャンセル完了として戻してよい
          "adopted_fill"   … 建玉を保有中として反映済み（内部キャンセル処理は行わない）
          "abort"          … 内部状態を変更せず pending を維持する
        """
        pos = self.position
        ts = datetime.now().strftime("%H:%M:%S")
        if pos.entry_order_id is None:
            self._safe_console_print(
                f"[{ts}] [WARN] real entry cancel skipped:"
                f" entry_order_id is None context={context}"
            )
            return "proceed_cancel"

        order_id = int(pos.entry_order_id)
        try:
            gmo_cancel_order(order_id)
            self._safe_console_print(
                f"[{ts}] [OK] real entry cancel accepted:"
                f" orderId={order_id} context={context}"
            )
            return "proceed_cancel"
        except GmoApiError as exc:
            if not is_benign_cancel_error(exc):
                self._safe_console_print(
                    f"[{ts}] [WARN] real entry cancel failed;"
                    f" keeping pending orderId={order_id}"
                    f" context={context}: {exc}"
                )
                return "abort"
            # 既に約定/取消済み等: 建玉の有無で分岐
            try:
                open_positions = fetch_open_positions()
            except Exception as fetch_exc:
                self._safe_console_print(
                    f"[{ts}] [WARN] real entry cancel benign but openPositions failed;"
                    f" keeping pending orderId={order_id}"
                    f" context={context}: {fetch_exc}"
                )
                return "abort"

            matched = _match_open_position_for_pending_entry(pos, open_positions)
            if matched is None:
                self._safe_console_print(
                    f"[{ts}] [OK] real entry cancel benign and no open position;"
                    f" treat as cancelled orderId={order_id} context={context}"
                )
                return "proceed_cancel"

            self._adopt_open_position_after_failed_cancel(
                matched,
                context=context,
                order_id=order_id,
                benign_exc=exc,
            )
            return "adopted_fill"
        except Exception as exc:
            self._safe_console_print(
                f"[{ts}] [WARN] real entry cancel failed;"
                f" keeping pending orderId={order_id}"
                f" context={context}: {exc}"
            )
            return "abort"

    def _adopt_open_position_after_failed_cancel(
        self,
        item: Dict[str, Any],
        *,
        context: str,
        order_id: int,
        benign_exc: "GmoApiError",
    ) -> None:
        """キャンセル不可かつ建玉あり: pending を保有中へ同期し緊急通知する。"""
        pos = self.position
        api_side = str(item.get("side", "")).upper()
        if api_side == "BUY":
            position_side = "LONG"
        elif api_side == "SELL":
            position_side = "SHORT"
        else:
            position_side = pos.side or "LONG"

        try:
            fill_price = float(item.get("price", pos.entry_price))
        except (TypeError, ValueError):
            fill_price = float(pos.entry_price)
        try:
            fill_size = float(item.get("size", pos.size))
        except (TypeError, ValueError):
            fill_size = float(pos.size)
        if fill_price <= 0:
            fill_price = float(pos.entry_price)
        if fill_size <= 0:
            fill_size = float(pos.size)

        tp_price = (
            fill_price * (1 + self.config.take_profit_pct)
            if position_side == "LONG"
            else fill_price * (1 - self.config.take_profit_pct)
        )
        self._position_filled_at = datetime.now()
        self._pending_order_placed_at = None
        self.position = PositionState(
            side=position_side,
            entry_price=fill_price,
            size=fill_size,
            is_pending=False,
            exit_price_target=tp_price,
            entry_order_id=pos.entry_order_id,
            tp_order_id=pos.tp_order_id,
            sl_order_id=pos.sl_order_id,
            position_id=self._parse_optional_order_id(item.get("positionId")),
        )
        message = (
            "[ALERT] real entry cancel blocked; adopted open position\n"
            f"context={context}\n"
            f"orderId={order_id}\n"
            f"side={position_side}\n"
            f"entry_price={fill_price:,.0f}\n"
            f"size={fill_size:.4f}\n"
            f"positionId={item.get('positionId')}\n"
            f"cancel_error={benign_exc}"
        )
        self._safe_console_print(message)
        if self._on_critical_alert is not None:
            try:
                self._on_critical_alert(message)
            except Exception as alert_exc:
                self._safe_console_print(
                    f"[WARN] critical alert notify failed: {alert_exc}"
                )
        self._place_real_tp_sl_orders()

    def _cancel_order(
        self,
        snap: OrderbookSnapshot,
        *,
        cancel_reason: str = CANCEL_REASON_IMBALANCE,
        time_condition_met: Optional[bool] = None,
        deviation_condition_met: Optional[bool] = None,
        elapsed_minutes: Optional[float] = None,
        deviation_pct: Optional[float] = None,
        apply_cooldown: bool = True,
        trade_reason: str = "CANCEL_ORDER",
        real_cancel_context: Optional[str] = None,
    ) -> None:
        """
        未約定指値のキャンセル。
        エントリー時の残高変動（コスト拘束 or リベート）を完全に巻き戻す。
        real mode では先に GMO cancelOrder を呼び、結果に応じて状態遷移する。
        """
        context = real_cancel_context or cancel_reason
        if self.trading_mode == "real":
            outcome = self._cancel_real_entry_order_or_adopt_fill(context=context)
            if outcome == "abort" or outcome == "adopted_fill":
                return

        pos = self.position
        resolved_profile_name = self._locked_profile_name or self.active_profile_name
        ts  = datetime.now().strftime("%H:%M:%S")

        if pos.side == "LONG":
            cost = pos.entry_price * pos.size
            fee  = int(cost * MAKER_FEE_RATE)   # エントリー時と同じ計算
            if self.trading_mode == "real":
                self.jpy_balance += fee          # real: エントリー時も fee のみ
            else:
                self.jpy_balance += cost + fee   # virtual: コスト拘束を返却

        elif pos.side == "SHORT":
            fee = int(pos.entry_price * pos.size * MAKER_FEE_RATE)
            self.jpy_balance += fee              # fee<0 → 残高が減る（リベート返却）

        cancel_side = "BUY" if pos.side == "LONG" else "SELL"
        if apply_cooldown:
            cooldown_sec = self._cooldown_sec_for_cancel_reason(cancel_reason)
            self._last_cancel_by_side[cancel_side] = (
                pos.entry_price,
                time.time(),
                cooldown_sec,
                cancel_reason,
            )
            if cancel_reason == CANCEL_REASON_IMBALANCE:
                self._last_imbalance_cancel_any_side_ts = time.time()
        self._reset_imbalance_reversal_debounce()
        self.position = PositionState()
        self._position_filled_at = None
        self._pending_order_placed_at = None
        self._clear_locked_profile()
        self._record_and_print(
            snap=snap, side=cancel_side, order_type="MAKER",
            price=pos.entry_price, size=pos.size, fee=0, pnl=0.0, reason=trade_reason,
            profile_name=resolved_profile_name,
            cancel_reason=cancel_reason,
            cancel_time_condition_met=time_condition_met,
            cancel_deviation_condition_met=deviation_condition_met,
            cancel_elapsed_minutes=elapsed_minutes,
            cancel_deviation_pct=deviation_pct,
        )
        if cancel_reason == CANCEL_REASON_MAINTENANCE:
            self._safe_console_print(
                f"[{ts}] [WARN] [FORCE-CANCEL: maintenance]"
                f"  side={pos.side}"
                f"  limit={pos.entry_price:,.0f} JPY"
                f"  size={pos.size:.4f} BTC"
                f"  cancel_reason={cancel_reason}"
            )
        else:
            self._safe_console_print(
                f"[{ts}] [WARN] [ORDER-CANCEL]"
                f"  side={pos.side}"
                f"  limit={pos.entry_price:,.0f} JPY"
                f"  imbalance={snap.imbalance:.1%}"
                f"  cancel_reason={cancel_reason}"
            )

    # ------------------------------------------------------------------ #
    #  メンテナンス時間帯: 強制キャンセル・強制決済                          #
    # ------------------------------------------------------------------ #

    def _force_cancel_maintenance(self, snap: OrderbookSnapshot) -> None:
        """メンテナンス前の安全化として未約定指値を強制キャンセルする。"""
        self._cancel_order(
            snap,
            cancel_reason=CANCEL_REASON_MAINTENANCE,
            apply_cooldown=False,
            trade_reason="FORCE_CANCEL_MAINTENANCE",
            real_cancel_context="force_cancel_maintenance",
        )

    def _force_close_maintenance(self, snap: OrderbookSnapshot) -> None:
        """メンテナンス前の安全化としてアクティブポジションを強制決済する。"""
        pos = self.position
        resolved_profile_name = self._locked_profile_name or self.active_profile_name
        ts  = datetime.now().strftime("%H:%M:%S")

        if pos.side == "LONG":
            exit_price = snap.best_bid_price
            fee        = int(exit_price * pos.size * TAKER_FEE_RATE)
            gross_pnl  = (exit_price - pos.entry_price) * pos.size
            net_pnl    = gross_pnl - fee
            self.jpy_balance += exit_price * pos.size - fee
            side, order_type = "SELL", "TAKER"
        else:  # SHORT
            exit_price = snap.best_ask_price
            fee        = int(exit_price * pos.size * TAKER_FEE_RATE)
            gross_pnl  = (pos.entry_price - exit_price) * pos.size
            net_pnl    = gross_pnl - fee
            self.jpy_balance += net_pnl
            side, order_type = "BUY", "TAKER"

        self._update_kpi(net_pnl)
        self.daily_realized_pnl += net_pnl
        self.check_daily_loss_limit()
        dur = self._calc_duration_sec()
        self.position = PositionState()
        self._position_filled_at = None
        self._pending_order_placed_at = None
        self._clear_locked_profile()
        self._record_and_print(
            snap=snap,
            side=side,
            order_type=order_type,
            price=exit_price,
            size=pos.size,
            fee=fee,
            pnl=net_pnl,
            reason="FORCE_CLOSE_MAINTENANCE",
            duration_sec=dur,
            cumulative_pnl=self._cumulative_pnl,
            profile_name=resolved_profile_name,
        )
        pnl_tag = "[+]" if net_pnl >= 0 else "[-]"
        self._safe_console_print(
            f"[{ts}] [WARN] [FORCE-CLOSE: maintenance] {pnl_tag}"
            f"  {pos.side} @ {exit_price:,.0f} JPY"
            f"  size={pos.size:.4f} BTC"
            f"  pnl={net_pnl:+,.0f} JPY"
        )

    def _force_close_real(self, snap: OrderbookSnapshot) -> None:
        """
        real mode 緊急停止: TP/SL 注文をキャンセルし、残建玉を成行決済する。
        決済成立確認は REST（closeOrder 応答 + openPositions 再取得）で行う。
        """
        now_ts = time.time()
        if now_ts < self._force_close_real_cooldown_until:
            return

        pos = self.position
        ts = datetime.now().strftime("%H:%M:%S")
        self._safe_console_print(
            f"[{ts}] [WARN] [FORCE-CLOSE: real] start"
            f"  side={pos.side} pending={pos.is_pending}"
            f"  tp_order_id={pos.tp_order_id} sl_order_id={pos.sl_order_id}"
        )

        # a. TP/SL 指値があればキャンセル（すでに約定済み等は正常系）
        for label, order_id in (("tp", pos.tp_order_id), ("sl", pos.sl_order_id)):
            if order_id is None:
                continue
            try:
                gmo_cancel_order(int(order_id))
                self._safe_console_print(
                    f"[{ts}] [OK] [FORCE-CLOSE: real] cancel {label} orderId={order_id}"
                )
            except GmoApiError as exc:
                if is_benign_cancel_error(exc):
                    self._safe_console_print(
                        f"[{ts}] [OK] [FORCE-CLOSE: real] cancel {label} skipped"
                        f" (already done) orderId={order_id} codes={exc.message_codes}"
                    )
                else:
                    self._safe_console_print(
                        f"[{ts}] [WARN] [FORCE-CLOSE: real] cancel {label} failed"
                        f" orderId={order_id}: {exc}"
                    )
            except Exception as exc:
                self._safe_console_print(
                    f"[{ts}] [WARN] [FORCE-CLOSE: real] cancel {label} failed"
                    f" orderId={order_id}: {exc}"
                )

        def _sync_jpy_balance_from_equity() -> None:
            self._sync_jpy_balance_from_equity_unlocked(context="FORCE-CLOSE: real")

        last_error: Optional[BaseException] = None
        for attempt in range(1, _FORCE_CLOSE_REAL_MAX_ATTEMPTS + 1):
            try:
                # b. 建玉の有無を再確認
                open_positions = fetch_open_positions()
                if not open_positions:
                    self._safe_console_print(
                        f"[{ts}] [OK] [FORCE-CLOSE: real] no open positions; skip closeOrder"
                    )
                    self.position = PositionState()
                    self._position_filled_at = None
                    self._pending_order_placed_at = None
                    self._clear_locked_profile()
                    _sync_jpy_balance_from_equity()
                    return

                # c. 残建玉を closeOrder(MARKET) で決済
                close_order_ids: List[int] = []
                for item in open_positions:
                    position_id = int(item["positionId"])
                    size = str(item["size"])
                    pos_side = str(item["side"]).upper()
                    close_side = "SELL" if pos_side == "BUY" else "BUY"
                    order_id = gmo_close_order(
                        side=close_side,
                        execution_type="MARKET",
                        settle_position={"positionId": position_id, "size": size},
                    )
                    try:
                        close_order_ids.append(int(order_id))
                    except (TypeError, ValueError):
                        pass
                    self._safe_console_print(
                        f"[{ts}] [OK] [FORCE-CLOSE: real] closeOrder accepted"
                        f" positionId={position_id} close_side={close_side}"
                        f" size={size} orderId={order_id}"
                    )

                # d. REST で建玉消滅を確認（反映遅延吸収のため短い再確認あり）
                remaining: List[Dict[str, Any]] = []
                for confirm_i in range(1, _FORCE_CLOSE_CONFIRM_MAX_CHECKS + 1):
                    remaining = fetch_open_positions()
                    if not remaining:
                        break
                    self._safe_console_print(
                        f"[{ts}] [WARN] [FORCE-CLOSE: real] openPositions still present"
                        f" after closeOrder"
                        f" (confirm {confirm_i}/{_FORCE_CLOSE_CONFIRM_MAX_CHECKS}"
                        f" count={len(remaining)})"
                    )
                    if confirm_i < _FORCE_CLOSE_CONFIRM_MAX_CHECKS:
                        time.sleep(_FORCE_CLOSE_CONFIRM_RETRY_SEC)

                if remaining:
                    raise RuntimeError(
                        f"openPositions still present after closeOrder: count={len(remaining)}"
                    )

                self._safe_console_print(
                    f"[{ts}] [OK] [FORCE-CLOSE: real] settlement confirmed via openPositions"
                )
                # 決済 PnL / KPI / 日次損失を板・WS 決済と同様に反映してから equity 同期
                if pos.side == "LONG":
                    exit_price = float(snap.best_bid_price)
                else:
                    exit_price = float(snap.best_ask_price)
                if exit_price <= 0:
                    exit_price = float(pos.entry_price)
                actual_fee: Optional[int] = None
                fetched_fees: List[int] = []
                for close_oid in close_order_ids:
                    fee_val = gmo_fetch_order_execution_fee(close_oid)
                    if fee_val is not None:
                        fetched_fees.append(fee_val)
                if fetched_fees:
                    actual_fee = sum(fetched_fees)
                self._settle_real_exit_from_execution_unlocked(
                    fill_price=exit_price,
                    fill_size=float(pos.size),
                    reason="FORCE_CLOSE_REAL",
                    is_take_profit=False,
                    actual_fee=actual_fee,
                )
                _sync_jpy_balance_from_equity()
                return
            except Exception as exc:
                last_error = exc
                self._safe_console_print(
                    f"[{ts}] [WARN] [FORCE-CLOSE: real] attempt {attempt}/"
                    f"{_FORCE_CLOSE_REAL_MAX_ATTEMPTS} failed: {exc}"
                )
                if attempt < _FORCE_CLOSE_REAL_MAX_ATTEMPTS:
                    wait_sec = _FORCE_CLOSE_REAL_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
                    time.sleep(wait_sec)

        # e. 3回失敗: 強い扱いで介入要求
        self._force_close_real_cooldown_until = time.time() + _FORCE_CLOSE_REAL_ALERT_COOLDOWN_SEC
        message = "\n".join(
            [
                "[CRITICAL] REAL MODE FORCE CLOSE FAILED",
                "manual intervention required immediately",
                f"attempts={_FORCE_CLOSE_REAL_MAX_ATTEMPTS}",
                f"error={last_error}",
                f"tp_order_id={pos.tp_order_id}",
                f"sl_order_id={pos.sl_order_id}",
                f"internal_side={pos.side}",
                f"internal_size={pos.size}",
                f"next_retry_after_sec={int(_FORCE_CLOSE_REAL_ALERT_COOLDOWN_SEC)}",
            ]
        )
        self._safe_console_print(message)
        if self._on_critical_alert is not None:
            try:
                self._on_critical_alert(message)
            except Exception as alert_exc:
                self._safe_console_print(
                    f"[WARN] critical alert notify failed: {alert_exc}"
                )

    # ------------------------------------------------------------------ #
    #  ユーティリティ                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _now_minute() -> int:
        now = datetime.now()
        return now.hour * 60 + now.minute

    def _clear_locked_profile(self) -> None:
        self._locked_config = None
        self._locked_profile_name = None

    @staticmethod
    def _safe_console_print(message: str) -> None:
        try:
            print(message)
        except Exception as exc:
            try:
                print(f"[WARN] console output failed: {exc!r}")
            except Exception:
                pass

    def _calc_duration_sec(self) -> int:
        """指値約定から現在までの保有秒数を返す。計測開始前は 0。"""
        if self._position_filled_at is None:
            return 0
        return int((datetime.now() - self._position_filled_at).total_seconds())

    # ------------------------------------------------------------------ #
    #  KPI 集計                                                            #
    # ------------------------------------------------------------------ #

    def _update_kpi(self, net_pnl: float) -> None:
        self._cumulative_pnl += net_pnl
        if net_pnl > 0:
            self._win_count       += 1
            self._daily_win_count += 1
            self._total_gross_win += net_pnl
        elif net_pnl < 0:
            self._loss_count       += 1
            self._daily_loss_count += 1
            self._total_gross_loss += abs(net_pnl)

    # ------------------------------------------------------------------ #
    #  Logging                                                             #
    # ------------------------------------------------------------------ #

    def _record_and_print(
        self,
        snap:           OrderbookSnapshot,
        side:           str,
        order_type:     str,
        price:          float,
        size:           float,
        fee:            int,
        pnl:            float,
        reason:         str,
        duration_sec:   int   = 0,
        cumulative_pnl: float = 0.0,
        profile_name:   Optional[str] = None,
        cancel_reason: Optional[str] = None,
        cancel_time_condition_met: Optional[bool] = None,
        cancel_deviation_condition_met: Optional[bool] = None,
        cancel_elapsed_minutes: Optional[float] = None,
        cancel_deviation_pct: Optional[float] = None,
    ) -> None:
        resolved_profile_name = (
            profile_name
            or self._locked_profile_name
            or self.active_profile_name
        )
        rec = TradeRecord(
            trade_id       = uuid.uuid4().hex[:8],
            side           = side,
            order_type     = order_type,
            price          = price,
            size           = size,
            fee            = fee,
            pnl            = pnl,
            reason         = reason,
            imbalance      = snap.imbalance,
            spread_pct     = snap.spread_pct,
            best_bid_size  = snap.best_bid_size,
            best_ask_size  = snap.best_ask_size,
            duration_sec   = duration_sec,
            cumulative_pnl = cumulative_pnl,
            config_version = self.config_version,
            profile_name   = resolved_profile_name,
        )
        self.trade_history.append(rec)   # deque が maxlen を超えると古い方を自動破棄

        def _fmt_optional_bool(value: Optional[bool]) -> str:
            if value is None:
                return ""
            return "true" if value else "false"

        def _fmt_optional_float(value: Optional[float], digits: int) -> str:
            if value is None:
                return ""
            return f"{value:.{digits}f}"

        # ---- CSV リアルタイム追記（StrategyConfig の設定値も記録）---- #
        csv_path     = _get_csv_log_path()
        write_header = not os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow({
                "timestamp":              rec.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "trade_id":               rec.trade_id,
                "side":                   rec.side,
                "order_type":             rec.order_type,
                "reason":                 rec.reason,
                "price":                  rec.price,
                "size":                   f"{rec.size:.8f}",
                "fee":                    rec.fee,
                "pnl":                    f"{rec.pnl:.2f}",
                "duration_sec":           rec.duration_sec,
                "cumulative_pnl":         f"{rec.cumulative_pnl:.2f}",
                "config_version":         rec.config_version,
                "profile_name":           rec.profile_name,
                "imbalance":              f"{rec.imbalance:.4f}",
                "spread_pct":             f"{rec.spread_pct:.6f}",
                "best_bid_size":          f"{rec.best_bid_size:.4f}",
                "best_ask_size":          f"{rec.best_ask_size:.4f}",
                "cfg_imbalance_threshold":self.config.imbalance_entry_threshold,
                "cfg_tp_pct":             self.config.take_profit_pct,
                "cfg_sl_pct":             self.config.stop_loss_pct,
                "cfg_min_wall_btc":       self.config.min_entry_wall_btc,
                "cfg_max_spread_pct":     self.config.max_spread_pct,
                "cfg_max_order_size_btc": self.config.max_order_size_btc,
                "cfg_daily_target_order_size_btc": (
                    ""
                    if self.config.daily_target_order_size_btc is None
                    else self.config.daily_target_order_size_btc
                ),
                "cancel_reason": cancel_reason or "",
                "cancel_time_condition_met": _fmt_optional_bool(
                    cancel_time_condition_met
                ),
                "cancel_deviation_condition_met": _fmt_optional_bool(
                    cancel_deviation_condition_met
                ),
                "cancel_elapsed_minutes": _fmt_optional_float(
                    cancel_elapsed_minutes, 2
                ),
                "cancel_deviation_pct": _fmt_optional_float(
                    cancel_deviation_pct, 6
                ),
            })

        # ---- コンソール出力 ------------------------------------------ #
        assets = self.total_assets(snap.mid_price)
        pnl_tag = "[+]" if pnl >= 0 else "[-]"
        console_message = (
            f"{pnl_tag} {rec.summary()}"
            f"  | total_assets: {assets:>13,.0f} JPY"
            f"  | realized_pnl: {self.realized_pnl:>+10,.0f} JPY"
        )
        self._safe_console_print(console_message)


# =========================================================================== #
#  Account reconciliation (GMO private API vs internal state)                 #
# =========================================================================== #

def _signed_position_size(side: Optional[str], size: float) -> float:
    if side is None or size <= 0:
        return 0.0
    if side == "LONG":
        return size
    if side == "SHORT":
        return -size
    return 0.0


def get_internal_account_state(trader: VirtualTrader) -> Dict[str, float]:
    """
    照合用の内部状態。
    comparable_equity_jpy:
      FLAT/pending … jpy_balance
      保有中 … total_assets(mid) = 現金 + ポジション評価（mode 対応の含み/時価）
      保有中だが mid 未取得 … 金額照合をスキップするため skip_balance_check=1
    """
    with trader._lock:
        pos = trader.position
        jpy = float(trader.jpy_balance)
        if pos.side is None or pos.is_pending:
            return {
                "position_size_btc": 0.0,
                "jpy_balance": jpy,
                "comparable_equity_jpy": jpy,
            }
        signed_size = _signed_position_size(pos.side, pos.size)
        snap = trader._latest_orderbook_snap
        mid = float(snap.mid_price) if snap is not None else 0.0
        if mid > 0:
            return {
                "position_size_btc": signed_size,
                "jpy_balance": jpy,
                "comparable_equity_jpy": float(trader.total_assets(mid)),
            }
        return {
            "position_size_btc": signed_size,
            "jpy_balance": jpy,
            "skip_balance_check": 1.0,
        }


class GmoApiError(RuntimeError):
    """GMO Private API の業務エラー（HTTP 自体は成功だが status!=0）。"""

    def __init__(self, status: Any, messages: Any) -> None:
        self.status = status
        self.messages = messages if isinstance(messages, list) else []
        super().__init__(f"GMO API error status={status} messages={messages}")

    @property
    def message_codes(self) -> List[str]:
        codes: List[str] = []
        for item in self.messages:
            if isinstance(item, dict) and item.get("message_code") is not None:
                codes.append(str(item.get("message_code")))
        return codes


def is_benign_cancel_error(exc: GmoApiError) -> bool:
    """すでに約定済み/取消済み等で cancel 対象が無いケース。"""
    return any(code in _CANCEL_ORDER_BENIGN_CODES for code in exc.message_codes)


def _match_open_position_for_pending_entry(
    pos: PositionState,
    open_positions: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """pending エントリー側に対応する建玉を探す。無ければ None。"""
    if not open_positions:
        return None
    want = None
    if pos.side == "LONG":
        want = "BUY"
    elif pos.side == "SHORT":
        want = "SELL"
    if want is not None:
        for item in open_positions:
            if str(item.get("side", "")).upper() == want:
                return item
    return open_positions[0]


def _match_open_position_for_held_entry(
    pos: PositionState,
    open_positions: List[Dict[str, Any]],
    *,
    tolerance_btc: float = _RECONCILIATION_DEFAULT_TOLERANCE_BTC,
) -> Optional[Dict[str, Any]]:
    """保有中ポジションに対応する建玉（サイド・数量が概ね一致）を探す。"""
    if not open_positions or pos.side is None:
        return None
    want = None
    if pos.side == "LONG":
        want = "BUY"
    elif pos.side == "SHORT":
        want = "SELL"
    if want is None:
        return None
    try:
        want_size = float(pos.size)
    except (TypeError, ValueError):
        return None
    for item in open_positions:
        if str(item.get("side", "")).upper() != want:
            continue
        try:
            size = float(item.get("size", 0))
        except (TypeError, ValueError):
            continue
        if abs(size - want_size) <= tolerance_btc:
            return item
    return None


def _active_order_id_set(active_orders: List[Dict[str, Any]]) -> set:
    ids: set = set()
    for item in active_orders:
        if not isinstance(item, dict):
            continue
        raw = item.get("orderId", item.get("order_id"))
        if raw is None:
            continue
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            continue
    return ids


def _open_position_fingerprint(
    item: Dict[str, Any],
    side: Optional[str],
    size: float,
) -> str:
    pid = item.get("positionId", item.get("position_id"))
    if pid is not None:
        return f"positionId={pid}"
    try:
        size_s = f"{float(size):.6f}"
    except (TypeError, ValueError):
        size_s = "0"
    return f"side={side}|size={size_s}"


def _load_startup_reconcile_state(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _save_startup_reconcile_state(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _clear_startup_reconcile_state(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _resolve_gmo_api_credentials(
    credential_scope: str = _GMO_CREDENTIAL_SCOPE_TRADE,
) -> Tuple[str, str]:
    """
    credential_scope に応じた API Key/Secret を返す。
    trade -> GMO_API_KEY_TRADE / GMO_API_SECRET_TRADE
    readonly -> GMO_API_KEY_READONLY / GMO_API_SECRET_READONLY
    """
    scope = str(credential_scope or _GMO_CREDENTIAL_SCOPE_TRADE).strip().lower()
    names = _GMO_CREDENTIAL_ENV_NAMES.get(scope)
    if names is None:
        raise RuntimeError(
            f"invalid GMO credential_scope: {credential_scope!r}"
            f" (allowed: {sorted(_GMO_CREDENTIAL_ENV_NAMES)})"
        )
    key_name, secret_name = names
    api_key = os.getenv(key_name, "").strip()
    api_secret = os.getenv(secret_name, "").strip()
    if not api_key or not api_secret:
        raise RuntimeError(f"{key_name}/{secret_name} が未設定です")
    return api_key, api_secret


def _gmo_private_request(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    *,
    credential_scope: str = _GMO_CREDENTIAL_SCOPE_TRADE,
) -> Any:
    api_key, api_secret = _resolve_gmo_api_credentials(credential_scope)

    timestamp = str(int(time.time() * 1000))
    payload_obj = body if body is not None else {}
    payload = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False) if body is not None else ""
    method_u = method.upper()
    if method_u == "GET":
        # GMO仕様: 署名対象のパスはクエリパラメータを含まない。
        # 実際のリクエストURLには path（クエリ付き）をそのまま使う。
        sign_path = path.split("?", 1)[0]
        text = timestamp + method_u + sign_path
        data_bytes = None
    else:
        text = timestamp + method_u + path + payload
        data_bytes = payload.encode("utf-8")

    sign = hmac.new(api_secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "API-KEY": api_key,
        "API-TIMESTAMP": timestamp,
        "API-SIGN": sign,
    }
    if data_bytes is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        _GMO_PRIVATE_API_BASE + path,
        data=data_bytes,
        headers=headers,
        method=method_u,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GMO API HTTP {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GMO API connection error: {exc}") from exc

    try:
        payload_doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GMO API response JSON decode failed: {raw}") from exc

    if not isinstance(payload_doc, dict):
        raise RuntimeError("GMO API response is not a JSON object")
    status = payload_doc.get("status")
    if status not in (0, "0"):
        raise GmoApiError(status=status, messages=payload_doc.get("messages"))
    return payload_doc.get("data")


def _gmo_private_get(
    path: str,
    *,
    credential_scope: str = _GMO_CREDENTIAL_SCOPE_TRADE,
) -> Dict[str, object]:
    data = _gmo_private_request(
        "GET",
        path,
        credential_scope=credential_scope,
    )
    if not isinstance(data, dict):
        raise RuntimeError("GMO API response missing data object")
    return data


def gmo_cancel_order(order_id: int) -> Any:
    """POST /v1/cancelOrder"""
    return _gmo_private_request("POST", "/v1/cancelOrder", {"orderId": int(order_id)})


def gmo_order(
    *,
    side: str,
    execution_type: str,
    price: float,
    size: float,
    time_in_force: Optional[str] = "SOK",
    symbol: str = _GMO_LEVERAGE_SYMBOL,
) -> str:
    """
    POST /v1/order（新規建玉 / 決済用指値・逆指値）。
    Maker 指値は execution_type=LIMIT / time_in_force=SOK（Post-Only）。
    STOP では time_in_force に SOK を渡さないこと（未指定時は API デフォルト）。
    戻り値: 注文 orderId（文字列）
    """
    body: Dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "executionType": execution_type,
        "price": str(int(round(float(price)))),
        "size": str(size),
    }
    if time_in_force is not None:
        body["timeInForce"] = time_in_force
    data = _gmo_private_request("POST", "/v1/order", body)
    return str(data)


def gmo_close_order(
    *,
    side: str,
    execution_type: str,
    settle_position: Dict[str, Any],
    price: Optional[float] = None,
    time_in_force: Optional[str] = None,
    symbol: str = _GMO_LEVERAGE_SYMBOL,
) -> str:
    """
    POST /v1/closeOrder
    settle_position: {"positionId": <int>, "size": "<str>"}
    LIMIT/STOP では price 必須。time_in_force は LIMIT のみ指定可（STOP/MARKET は None）。
    戻り値: 決済注文 orderId（文字列）
    """
    body: Dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "executionType": execution_type,
        "settlePosition": [
            {
                "positionId": int(settle_position["positionId"]),
                "size": str(settle_position["size"]),
            }
        ],
    }
    if price is not None:
        body["price"] = str(int(round(float(price))))
    if time_in_force is not None:
        body["timeInForce"] = time_in_force
    data = _gmo_private_request("POST", "/v1/closeOrder", body)
    return str(data)


def gmo_fetch_order_execution_fill(
    order_id: int,
) -> tuple[Optional[float], Optional[int]]:
    """
    GET /v1/executions?orderId=... から数量加重平均約定価格と手数料合計を返す。

    戻り値: (avg_price, total_fee)。価格または手数料が取れない場合、
    対応する要素は None（呼び出し側は目標値/理論手数料へフォールバック）。

    注: GMO API で orderId 指定による約定取得は /v1/executions
    （/v1/latestExecutions ではない）。
    """
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        data = _gmo_private_get(f"/v1/executions?orderId={int(order_id)}")
        items = data.get("list", [])
        if not isinstance(items, list) or not items:
            print(
                f"[{ts}] [WARN] gmo_fetch_order_execution_fill failed:"
                f" order_id={int(order_id)} reason=empty_list"
            )
            return None, None

        notional = 0.0
        size_sum = 0.0
        price_found = False
        fee_total = 0
        fee_found = False
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_price = item.get("price")
            raw_size = item.get("size")
            if raw_price is not None and raw_size is not None:
                try:
                    price = float(raw_price)
                    size = float(raw_size)
                except (TypeError, ValueError):
                    price = 0.0
                    size = 0.0
                if price > 0 and size > 0:
                    notional += price * size
                    size_sum += size
                    price_found = True
            raw_fee = item.get("fee")
            if raw_fee is not None:
                try:
                    fee_total += int(float(raw_fee))
                    fee_found = True
                except (TypeError, ValueError):
                    pass

        avg_price = (notional / size_sum) if price_found and size_sum > 0 else None
        fee = fee_total if fee_found else None
        if avg_price is None and fee is None:
            print(
                f"[{ts}] [WARN] gmo_fetch_order_execution_fill failed:"
                f" order_id={int(order_id)} reason=price_and_fee_unavailable"
            )
        elif avg_price is None:
            print(
                f"[{ts}] [WARN] gmo_fetch_order_execution_fill failed:"
                f" order_id={int(order_id)} reason=price_unavailable"
            )
        elif fee is None:
            print(
                f"[{ts}] [WARN] gmo_fetch_order_execution_fill failed:"
                f" order_id={int(order_id)} reason=fee_unavailable"
            )
        return avg_price, fee
    except Exception as exc:
        print(
            f"[{ts}] [WARN] gmo_fetch_order_execution_fill failed:"
            f" order_id={int(order_id)} reason=exception: {exc}"
        )
        return None, None


def gmo_fetch_order_execution_fee(order_id: int) -> Optional[int]:
    """
    GET /v1/executions?orderId=... から当該注文の手数料合計（JPY整数）を返す。

    注: GMO API で orderId 指定による約定取得は /v1/executions（/v1/latestExecutions ではない）。
    レスポンスが空・fee 欠損・例外発生時は None（呼び出し側は理論値フォールバックへ）。
    """
    _avg_price, fee = gmo_fetch_order_execution_fill(order_id)
    return fee


def fetch_open_positions(
    symbol: str = _GMO_LEVERAGE_SYMBOL,
    *,
    credential_scope: str = _GMO_CREDENTIAL_SCOPE_TRADE,
) -> List[Dict[str, Any]]:
    """GET /v1/openPositions の list を返す。"""
    positions_data = _gmo_private_get(
        f"/v1/openPositions?symbol={symbol}&page=1&count=100",
        credential_scope=credential_scope,
    )
    position_list = positions_data.get("list", [])
    if not isinstance(position_list, list):
        raise RuntimeError("GMO open positions list is invalid")
    out: List[Dict[str, Any]] = []
    for item in position_list:
        if isinstance(item, dict):
            out.append(item)
    return out


def fetch_active_orders(
    symbol: str = _GMO_LEVERAGE_SYMBOL,
    *,
    credential_scope: str = _GMO_CREDENTIAL_SCOPE_TRADE,
) -> List[Dict[str, Any]]:
    """GET /v1/activeOrders の list を返す。"""
    orders_data = _gmo_private_get(
        f"/v1/activeOrders?symbol={symbol}&page=1&count=100",
        credential_scope=credential_scope,
    )
    order_list = orders_data.get("list", [])
    if not isinstance(order_list, list):
        raise RuntimeError("GMO active orders list is invalid")
    out: List[Dict[str, Any]] = []
    for item in order_list:
        if isinstance(item, dict):
            out.append(item)
    return out


def fetch_real_account_state(
    *,
    credential_scope: str = _GMO_CREDENTIAL_SCOPE_TRADE,
) -> Dict[str, float]:
    """
    GMO private API から建玉とレバレッジ余力（JPY）を取得する。
    jpy_balance は GET /v1/account/margin の availableAmount（発注余力）。
    equity_jpy は同レスポンスの actualProfitLoss（時価評価総額）。
    position_size_btc は LONG=正 / SHORT=負 の符号付きサイズ。
    """
    margin_data = _gmo_private_get(
        "/v1/account/margin",
        credential_scope=credential_scope,
    )
    try:
        jpy_balance = float(margin_data.get("availableAmount", 0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"GMO account margin availableAmount is invalid: {margin_data!r}"
        ) from exc
    try:
        equity_jpy = float(margin_data.get("actualProfitLoss", 0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"GMO account margin actualProfitLoss is invalid: {margin_data!r}"
        ) from exc

    signed_size = 0.0
    for item in fetch_open_positions(credential_scope=credential_scope):
        size = float(item.get("size", 0))
        side = str(item.get("side", "")).upper()
        if side == "BUY":
            signed_size += size
        elif side == "SELL":
            signed_size -= size

    return {
        "position_size_btc": signed_size,
        "jpy_balance": jpy_balance,
        "equity_jpy": equity_jpy,
    }


def compare_with_internal_state(
    real_state: Dict[str, float],
    internal_state: Dict[str, float],
    tolerance_btc: float = _RECONCILIATION_DEFAULT_TOLERANCE_BTC,
    tolerance_jpy: float = _RECONCILIATION_DEFAULT_TOLERANCE_JPY,
) -> Optional[Dict[str, float]]:
    position_diff = abs(
        float(real_state.get("position_size_btc", 0.0))
        - float(internal_state.get("position_size_btc", 0.0))
    )
    # 残高比較は発注余力(availableAmount/jpy_balance)ではなく
    # 時価評価総額(actualProfitLoss/equity_jpy)を使う。
    # 保有中は comparable_equity_jpy（現金+含み/評価）があればそちらを使う。
    # 未指定時は従来互換で jpy_balance と比較する。
    real_equity = float(real_state.get("equity_jpy", 0.0))
    if float(internal_state.get("skip_balance_check", 0.0)) != 0.0:
        balance_diff = 0.0
        internal_comparable = float(internal_state.get("jpy_balance", 0.0))
    elif "comparable_equity_jpy" in internal_state:
        internal_comparable = float(internal_state["comparable_equity_jpy"])
        balance_diff = abs(real_equity - internal_comparable)
    else:
        internal_comparable = float(internal_state.get("jpy_balance", 0.0))
        balance_diff = abs(real_equity - internal_comparable)
    if position_diff <= tolerance_btc and balance_diff <= tolerance_jpy:
        return None
    return {
        "position_diff_btc": position_diff,
        "balance_diff_jpy": balance_diff,
        "real_position_size_btc": float(real_state.get("position_size_btc", 0.0)),
        "internal_position_size_btc": float(internal_state.get("position_size_btc", 0.0)),
        "real_jpy_balance": real_equity,
        "internal_jpy_balance": internal_comparable,
    }


def run_reconciliation_check(
    trader: VirtualTrader,
    tolerance_btc: float,
    tolerance_jpy: float,
    pending_mismatch: List[bool],
    on_confirmed_mismatch: Callable[[Dict[str, float]], None],
) -> None:
    """
    内部状態と GMO 実口座を照合する。
    不一致時は1回だけ再取得し、2回連続で不一致なら on_confirmed_mismatch を呼ぶ。
    """
    internal_state = get_internal_account_state(trader)
    try:
        real_state = fetch_real_account_state()
    except Exception as exc:
        print(f"[WARN] [Reconciliation] GMO口座取得失敗: {exc}")
        return

    mismatch = compare_with_internal_state(
        real_state, internal_state, tolerance_btc, tolerance_jpy
    )
    if mismatch is None:
        pending_mismatch[0] = False
        return

    try:
        real_state_retry = fetch_real_account_state()
    except Exception as exc:
        print(f"[WARN] [Reconciliation] 再取得失敗: {exc}")
        return

    mismatch_retry = compare_with_internal_state(
        real_state_retry, internal_state, tolerance_btc, tolerance_jpy
    )
    if mismatch_retry is None:
        pending_mismatch[0] = False
        return

    if pending_mismatch[0]:
        on_confirmed_mismatch(mismatch_retry)
        pending_mismatch[0] = False
        return

    pending_mismatch[0] = True
    print(
        "[WARN] [Reconciliation] 口座不一致を検知（1回目）。"
        f" position_diff={mismatch_retry['position_diff_btc']:.6f} BTC"
        f" balance_diff={mismatch_retry['balance_diff_jpy']:.0f} JPY"
        " 次回も不一致なら manual_stop を発動します。"
    )
