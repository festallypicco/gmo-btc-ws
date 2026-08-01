"""
strategy_logic.py
-----------------
板情報スナップショットとポジション状態を受け取り、売買シグナルを返す純粋関数モジュール。
外部 I/O・副作用なし。VirtualTrader / WebSocketManager から独立して単体テスト可能。
"""
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


# =========================================================================== #
#  Signal                                                                       #
# =========================================================================== #

class Signal(Enum):
    HOLD         = auto()
    BUY_ENTRY    = auto()   # 買い指値エントリー (Maker)
    SELL_ENTRY   = auto()   # 売り指値エントリー (Maker)
    TAKE_PROFIT  = auto()   # 利確決済
    STOP_LOSS    = auto()   # 損切り決済 (Taker)
    CANCEL_ORDER = auto()   # 未約定指値のキャンセル


# =========================================================================== #
#  Data classes                                                                 #
# =========================================================================== #

@dataclass(frozen=True)
class OrderbookSnapshot:
    """WebSocket 受信データから生成する板の 1 フレーム分"""
    best_bid_price: float
    best_bid_size:  float
    best_ask_price: float
    best_ask_size:  float

    @property
    def spread(self) -> float:
        return self.best_ask_price - self.best_bid_price

    @property
    def spread_pct(self) -> float:
        if self.best_bid_price == 0:
            return float("inf")
        return self.spread / self.best_bid_price

    @property
    def imbalance(self) -> float:
        """買い圧力 = best_bid_size / (best_bid_size + best_ask_size)"""
        total = self.best_bid_size + self.best_ask_size
        if total == 0:
            return 0.5
        return self.best_bid_size / total

    @property
    def mid_price(self) -> float:
        return (self.best_bid_price + self.best_ask_price) / 2.0


@dataclass
class StrategyConfig:
    # ---- エントリー条件 -------------------------------------------- #
    imbalance_entry_threshold: float = 0.55   # 買い(売り)圧力がこれを超えたらエントリー候補
    min_entry_wall_btc:        float = 0.05   # 最良気配の厚みが最低この BTC 以上必要

    # ---- 見送り条件 ------------------------------------------------ #
    min_valid_wall_btc:  float = 0.10         # 壁がこれ以下なら板が薄すぎで見送り
    max_spread_pct:      float = 0.0003       # スプレッドがこれ以上 (0.03%) なら見送り
    max_allowed_spread:  float = 3000.0       # スプレッドがこれ以上（円）なら見送り

    # ---- キャンセル条件 -------------------------------------------- #
    imbalance_cancel_threshold: float = 0.50  # 未約定注文: 圧力がこれを下回ったらキャンセル

    # ---- エグジット条件 -------------------------------------------- #
    take_profit_pct: float = 0.0015           # 含み益がエントリー比これ以上 (0.15%) で利確
    stop_loss_pct:   float = 0.0015           # 含み損がエントリー比これ以上 (0.15%) で損切り

    # ---- Maker 指値の価格優遇幅 ------------------------------------ #
    maker_price_offset_jpy: float = 1.0       # Best Bid/Ask から内側に寄せる円数

    # ---- 発注サイズ上限 --------------------------------------------- #
    max_order_size_btc: float = 0.05          # 1 回の発注サイズ上限（BTC）
    daily_target_order_size_btc: Optional[float] = None  # 日次の任意サイズ上限（未設定時は無効）


@dataclass
class PositionState:
    side:               Optional[str] = None   # "LONG" | "SHORT" | None
    entry_price:        float = 0.0
    size:               float = 0.0
    is_pending:         bool  = False          # True = 指値注文が未約定状態
    exit_price_target:  float = 0.0            # 利確指値価格（約定確認後に設定）
    entry_order_id:     Optional[int] = None   # real mode エントリー注文ID（未実装時は None）
    tp_order_id:        Optional[int] = None   # real mode 利確注文ID（未実装時は None）
    sl_order_id:        Optional[int] = None   # real mode 損切り注文ID（未実装時は None）
    position_id:        Optional[int] = None   # real mode GMO 建玉ID（未取得時は None）


# =========================================================================== #
#  Private helpers                                                              #
# =========================================================================== #

def _should_skip(snap: OrderbookSnapshot, cfg: StrategyConfig) -> bool:
    """
    共通の見送り条件。
    壁の厚みチェックは方向ごとに異なるため、エントリー判定側で個別に行う。

    スプレッドは % と 円の両方で上限チェックする。
    円建て上限（max_allowed_spread）はレバレッジ取引で重要:
    スプレッドが広すぎると Maker エントリー直後の含み損が SL 幅を超えてしまう。
    """
    if snap.spread_pct >= cfg.max_spread_pct:
        return True
    if snap.spread >= cfg.max_allowed_spread:
        return True
    return False


def _check_exit_long(snap: OrderbookSnapshot, pos: PositionState, cfg: StrategyConfig) -> Optional[Signal]:
    pnl_pct = (snap.best_bid_price - pos.entry_price) / pos.entry_price
    if pnl_pct >= cfg.take_profit_pct:
        return Signal.TAKE_PROFIT
    if pnl_pct <= -cfg.stop_loss_pct:
        return Signal.STOP_LOSS
    return None


def _check_exit_short(snap: OrderbookSnapshot, pos: PositionState, cfg: StrategyConfig) -> Optional[Signal]:
    pnl_pct = (pos.entry_price - snap.best_ask_price) / pos.entry_price
    if pnl_pct >= cfg.take_profit_pct:
        return Signal.TAKE_PROFIT
    if pnl_pct <= -cfg.stop_loss_pct:
        return Signal.STOP_LOSS
    return None


# =========================================================================== #
#  Public API                                                                   #
# =========================================================================== #

def evaluate(
    snap:     OrderbookSnapshot,
    position: PositionState,
    config:   StrategyConfig,
) -> Signal:
    """
    現在の板スナップショットとポジション状態からシグナルを返す。

    優先順位
      1. エグジット判定  (TAKE_PROFIT / STOP_LOSS)  ← 最優先
      2. 指値キャンセル  (CANCEL_ORDER)
      3. 新規エントリー  (BUY_ENTRY / SELL_ENTRY)
      4. 静観            (HOLD)
    """
    # 1. エグジット ------------------------------------------------------- #
    if position.side == "LONG" and position.entry_price > 0:
        sig = _check_exit_long(snap, position, config)
        if sig:
            return sig

    if position.side == "SHORT" and position.entry_price > 0:
        sig = _check_exit_short(snap, position, config)
        if sig:
            return sig

    # 2. 未約定注文のキャンセル判定（圧力が転換した場合） ----------------- #
    if position.is_pending:
        # ロング指値: 買い圧力が閾値を下回ったらキャンセル
        if position.side == "LONG" and snap.imbalance < config.imbalance_cancel_threshold:
            return Signal.CANCEL_ORDER
        # ショート指値: 売り圧力が閾値を下回った = 買い圧力が上回ったらキャンセル
        if position.side == "SHORT" and snap.imbalance > config.imbalance_cancel_threshold:
            return Signal.CANCEL_ORDER

    # ポジション保有中は新規エントリー禁止 --------------------------------- #
    if position.side is not None:
        return Signal.HOLD

    # 3. 見送り条件 -------------------------------------------------------- #
    if _should_skip(snap, config):
        return Signal.HOLD

    # 3. エントリー判定 ---------------------------------------------------- #
    if (snap.imbalance > config.imbalance_entry_threshold
            and snap.best_bid_size >= config.min_entry_wall_btc):
        return Signal.BUY_ENTRY

    sell_pressure = 1.0 - snap.imbalance
    if (sell_pressure > config.imbalance_entry_threshold
            and snap.best_ask_size >= config.min_entry_wall_btc):
        return Signal.SELL_ENTRY

    return Signal.HOLD
