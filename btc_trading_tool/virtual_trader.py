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
from typing import Callable, Dict, List, Optional

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
TAKER_FEE_RATE: float =  0.0004  # Taker: 0.04%
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
_MANUAL_STOP_FLAG_PATH = Path(__file__).resolve().parent.parent / "runtime" / "manual_stop.flag"
_GMO_PRIVATE_API_BASE = "https://api.coin.z.com/private"
_GMO_LEVERAGE_SYMBOL = "BTC"
# 建玉差分許容 0.0005 BTC: GMO最小発注単位 0.001 BTC の半分。
# 端数丸め・API反映遅延・未約定指値との一時差を吸収するため。
_RECONCILIATION_DEFAULT_TOLERANCE_BTC = 0.0005
_RECONCILIATION_DEFAULT_TOLERANCE_JPY = 100.0
# ---------------------------------------------------------------------- #


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
    reason:        str        # "ENTRY" | "TAKE_PROFIT" | "STOP_LOSS" | ...
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
    MIN_TRADE_SIZE:   float = 0.001   # GMOコイン レバレッジ取引の最小発注単位（BTC）
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
        self.trade_history: collections.deque = collections.deque(maxlen=TRADE_HISTORY_MAXLEN)
        self._lock = threading.Lock()
        self._position_filled_at: Optional[datetime] = None  # 指値約定タイムスタンプ
        self._safe_mode_until: Optional[datetime] = None
        self._safe_mode_wait_for_recovery: bool = False
        self._last_guard_state: str = "normal"
        self.engine_status: str = "RUNNING"

        # KPI カウンタ（trade_history の maxlen 制限を受けない全履歴集計）
        self._win_count:       int   = 0
        self._loss_count:      int   = 0
        self._total_gross_win: float = 0.0
        self._total_gross_loss:float = 0.0
        self._cumulative_pnl:  float = 0.0   # 決済済み損益の累計

    # ------------------------------------------------------------------ #
    #  Public interface                                                    #
    # ------------------------------------------------------------------ #

    def on_orderbook_update(self, snap: Optional[OrderbookSnapshot]) -> None:
        """
        WebSocket の更新ごとに呼び出すメインエントリー。
        スレッドセーフ（_lock で保護）。snap が None なら何もしない。
        メンテナンス時間帯は最優先で制限を適用する。
        """
        if snap is None:
            return
        with self._lock:
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
        """円残高 + 保有ポジション現在価値"""
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
        LONG : BTC の現在市場価値（size × mid）
        SHORT: エントリー価格と現在価格の差額（含み損益）
        なし : 0
        """
        if self.position.side == "LONG" and self.position.size > 0:
            return self.position.size * mid_price
        if self.position.side == "SHORT" and self.position.entry_price > 0:
            return (self.position.entry_price - mid_price) * self.position.size
        return 0.0

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

    def _calc_trade_size(self, price: float) -> float:
        """
        動的ロット計算:
          1. 割当金額 = 現在の円残高 × POSITION_RATIO (20%)
          2. 割当金額 ÷ 現在価格 で BTC サイズを算出
          3. LOT_UNIT (0.001 BTC) 単位で切り捨て（APIの最小発注単位に合わせる）
          4. 結果が MIN_TRADE_SIZE (0.001 BTC) 未満なら MIN_TRADE_SIZE に固定
          5. config.max_order_size_btc を超える場合は上限でクランプ
          6. config.daily_target_order_size_btc が設定されている場合はさらに上限でクランプ

        例: 残高50,000円, 価格15,000,000円
            割当 = 10,000円  raw = 0.000666...
            floor → 0.000 → 最低値 0.001 BTC に固定
        """
        if price <= 0:
            return self.MIN_TRADE_SIZE
        raw_size     = (self.jpy_balance * self.POSITION_RATIO) / price
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
        Maker 指値の約定シミュレーション。

        LONG 買い指値 P（≈ best_bid + 1円）:
          best_bid_price >= P になった＝自分の指値が現在の最良気配以内に収まり、
          次の成行売りで約定したとみなす。
        SHORT 売り指値 Q（≈ best_ask - 1円）:
          best_ask_price <= Q になった＝自分の指値が現在の最良気配以内に収まり、
          次の成行買いで約定したとみなす。

        未約定のままキャンセル条件（Imbalance 反転）に合致した場合はキャンセル。
        """
        pos = self.position
        filled = (
            (pos.side == "LONG"  and snap.best_bid_price >= pos.entry_price) or
            (pos.side == "SHORT" and snap.best_ask_price <= pos.entry_price)
        )

        if filled:
            tp_price = (
                pos.entry_price * (1 + self.config.take_profit_pct)
                if pos.side == "LONG"
                else pos.entry_price * (1 - self.config.take_profit_pct)
            )
            self._position_filled_at = datetime.now()   # 保有時間の計測開始
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
        else:
            # 既存のキャンセル判定（Imbalance 反転）
            cfg = self.config
            is_imbalance_reversed = (
                (pos.side == "LONG"  and snap.imbalance < cfg.imbalance_cancel_threshold) or
                (pos.side == "SHORT" and snap.imbalance > cfg.imbalance_cancel_threshold)
            )
            
            # 【追加】指値が出た後にスプレッドが許容範囲を超えて拡大した場合も即座にキャンセルする
            is_spread_too_wide = (snap.spread >= cfg.max_allowed_spread)

            if is_imbalance_reversed or is_spread_too_wide:
                self._cancel_order(snap)

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

        """
        pos = self.position
        cfg = self.config

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
                if self.position.is_pending:
                    self._force_cancel_maintenance(snap)
                else:
                    self._force_close_maintenance(snap)
        else:
            self.engine_status = "RUNNING"

        if in_pre and self.position.side is not None and self.maintenance_pre_action == "close":
            if self.position.is_pending:
                self._force_cancel_maintenance(snap)
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

    def _enter_long(self, snap: OrderbookSnapshot) -> None:
        """
        買い指値 (Maker): Best Bid + offset の価格で板に並ぶ（is_pending=True）。
        約定は _check_pending_fill で対向 Ask がタッチしたときに確認する。
        """
        if self._before_entry_order is not None and self._before_entry_order():
            return

        price = snap.best_bid_price + self.config.maker_price_offset_jpy
        size  = self._calc_trade_size(price)
        cost  = price * size
        fee   = int(cost * MAKER_FEE_RATE)   # 1円未満切り捨て（負=リベート）
        total = cost + fee

        if self.jpy_balance < total:
            return

        self.jpy_balance -= total
        self.position = PositionState(side="LONG", entry_price=price, size=size, is_pending=True)

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
        約定は _check_pending_fill で対向 Bid がタッチしたときに確認する。
        """
        if self._before_entry_order is not None and self._before_entry_order():
            return

        price = snap.best_ask_price - self.config.maker_price_offset_jpy
        size  = self._calc_trade_size(price)
        fee   = int(price * size * MAKER_FEE_RATE)  # 1円未満切り捨て（負=リベート）

        self.jpy_balance -= fee   # fee < 0 → 残高が増える（リベート受取）
        self.position = PositionState(side="SHORT", entry_price=price, size=size, is_pending=True)

        self._record_and_print(
            snap=snap, side="SELL", order_type="MAKER",
            price=price, size=size, fee=fee, pnl=-fee, reason="ENTRY",
        )
        if self._on_order_placed is not None:
            self._on_order_placed()

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
        dur = self._calc_duration_sec()
        self.position = PositionState()
        self._position_filled_at = None
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
        dur = self._calc_duration_sec()
        self.position = PositionState()
        self._position_filled_at = None
        self._clear_locked_profile()
        self._record_and_print(
            snap=snap, side=side, order_type=order_type,
            price=exit_price, size=pos.size, fee=fee, pnl=net_pnl, reason="STOP_LOSS",
            duration_sec=dur, cumulative_pnl=self._cumulative_pnl,
            profile_name=resolved_profile_name,
        )

    def _cancel_order(self, snap: OrderbookSnapshot) -> None:
        """
        未約定指値のキャンセル。
        エントリー時の残高変動（コスト拘束 or リベート）を完全に巻き戻す。
        """
        pos = self.position
        resolved_profile_name = self._locked_profile_name or self.active_profile_name
        ts  = datetime.now().strftime("%H:%M:%S")

        if pos.side == "LONG":
            cost = pos.entry_price * pos.size
            fee  = int(cost * MAKER_FEE_RATE)   # エントリー時と同じ計算
            self.jpy_balance += cost + fee       # コスト拘束を返却

        elif pos.side == "SHORT":
            fee = int(pos.entry_price * pos.size * MAKER_FEE_RATE)
            self.jpy_balance += fee              # fee<0 → 残高が減る（リベート返却）

        cancel_side = "BUY" if pos.side == "LONG" else "SELL"
        self.position = PositionState()
        self._position_filled_at = None
        self._clear_locked_profile()
        self._record_and_print(
            snap=snap, side=cancel_side, order_type="MAKER",
            price=pos.entry_price, size=pos.size, fee=0, pnl=0.0, reason="CANCEL_ORDER",
            profile_name=resolved_profile_name,
        )
        self._safe_console_print(
            f"[{ts}] [WARN] [ORDER-CANCEL]"
            f"  side={pos.side}"
            f"  limit={pos.entry_price:,.0f} JPY"
            f"  imbalance={snap.imbalance:.1%}"
            f"  reason=pressure_reversal"
        )

    # ------------------------------------------------------------------ #
    #  メンテナンス時間帯: 強制キャンセル・強制決済                          #
    # ------------------------------------------------------------------ #

    def _force_cancel_maintenance(self, snap: OrderbookSnapshot) -> None:
        """メンテナンス前の安全化として未約定指値を強制キャンセルする。"""
        pos = self.position
        resolved_profile_name = self._locked_profile_name or self.active_profile_name
        ts  = datetime.now().strftime("%H:%M:%S")

        # エントリー時に差し引いた原資・手数料を返戻
        if pos.side == "LONG":
            cost = pos.entry_price * pos.size
            fee  = int(cost * MAKER_FEE_RATE)
            self.jpy_balance += cost + fee
        elif pos.side == "SHORT":
            fee = int(pos.entry_price * pos.size * MAKER_FEE_RATE)
            self.jpy_balance += fee

        cancel_side = "BUY" if pos.side == "LONG" else "SELL"
        self.position = PositionState()
        self._position_filled_at = None
        self._clear_locked_profile()
        self._record_and_print(
            snap=snap, side=cancel_side, order_type="MAKER",
            price=pos.entry_price, size=pos.size, fee=0, pnl=0.0, reason="FORCE_CANCEL_MAINTENANCE",
            profile_name=resolved_profile_name,
        )
        self._safe_console_print(
            f"[{ts}] [WARN] [FORCE-CANCEL: maintenance]"
            f"  side={pos.side}"
            f"  limit={pos.entry_price:,.0f} JPY"
            f"  size={pos.size:.4f} BTC"
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
        dur = self._calc_duration_sec()
        self.position = PositionState()
        self._position_filled_at = None
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
            self._total_gross_win += net_pnl
        elif net_pnl < 0:
            self._loss_count       += 1
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
    with trader._lock:
        pos = trader.position
        if pos.side is None or pos.is_pending:
            signed_size = 0.0
        else:
            signed_size = _signed_position_size(pos.side, pos.size)
        return {
            "position_size_btc": signed_size,
            "jpy_balance": float(trader.jpy_balance),
        }


def _gmo_private_get(path: str) -> Dict[str, object]:
    api_key = os.getenv("GMO_API_KEY", "").strip()
    api_secret = os.getenv("GMO_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("GMO_API_KEY/GMO_API_SECRET が未設定です")

    timestamp = str(int(time.time() * 1000))
    method = "GET"
    text = timestamp + method + path
    sign = hmac.new(api_secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        _GMO_PRIVATE_API_BASE + path,
        headers={
            "API-KEY": api_key,
            "API-TIMESTAMP": timestamp,
            "API-SIGN": sign,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GMO API HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GMO API connection error: {exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("GMO API response is not a JSON object")
    status = payload.get("status")
    if status not in (0, "0"):
        messages = payload.get("messages")
        raise RuntimeError(f"GMO API error status={status} messages={messages}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("GMO API response missing data object")
    return data


def fetch_real_account_state() -> Dict[str, float]:
    """
    GMO private API から建玉と JPY 残高を取得する。
    position_size_btc は LONG=正 / SHORT=負 の符号付きサイズ。
    """
    assets_data = _gmo_private_get("/v1/account/assets")
    asset_list = assets_data.get("list", [])
    if not isinstance(asset_list, list):
        raise RuntimeError("GMO account assets list is invalid")

    jpy_balance = 0.0
    for item in asset_list:
        if not isinstance(item, dict):
            continue
        if str(item.get("symbol", "")).upper() != "JPY":
            continue
        available = item.get("available", item.get("amount", 0))
        jpy_balance = float(available)
        break

    positions_data = _gmo_private_get(
        f"/v1/openPositions?symbol={_GMO_LEVERAGE_SYMBOL}&page=1&count=100"
    )
    position_list = positions_data.get("list", [])
    if not isinstance(position_list, list):
        raise RuntimeError("GMO open positions list is invalid")

    signed_size = 0.0
    for item in position_list:
        if not isinstance(item, dict):
            continue
        size = float(item.get("size", 0))
        side = str(item.get("side", "")).upper()
        if side == "BUY":
            signed_size += size
        elif side == "SELL":
            signed_size -= size

    return {
        "position_size_btc": signed_size,
        "jpy_balance": jpy_balance,
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
    balance_diff = abs(
        float(real_state.get("jpy_balance", 0.0))
        - float(internal_state.get("jpy_balance", 0.0))
    )
    if position_diff <= tolerance_btc and balance_diff <= tolerance_jpy:
        return None
    return {
        "position_diff_btc": position_diff,
        "balance_diff_jpy": balance_diff,
        "real_position_size_btc": float(real_state.get("position_size_btc", 0.0)),
        "internal_position_size_btc": float(internal_state.get("position_size_btc", 0.0)),
        "real_jpy_balance": float(real_state.get("jpy_balance", 0.0)),
        "internal_jpy_balance": float(internal_state.get("jpy_balance", 0.0)),
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
