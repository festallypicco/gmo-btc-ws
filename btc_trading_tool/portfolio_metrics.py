"""
portfolio_metrics.py
--------------------
live_state 行からダッシュボードと同じ定義で総資産（現金 + ポジション評価）を算出する。

LONG の評価は trading_mode で分岐する:
  - virtual: size * mid（エントリー時に想定元本を拘束する会計）
  - real: (mid - entry) * size（含み損益のみ）
SHORT は mode によらず (entry - mid) * size。

pending（未約定指値）中は建玉評価を含めず jpy_balance のみ
（get_internal_account_state の comparable_equity_jpy と同一定義）。
"""
from __future__ import annotations

from typing import Mapping, Optional, Union


StateLike = Mapping[str, Union[object, None]]


def normalize_trading_mode(trading_mode: Optional[str]) -> str:
    mode = str(trading_mode or "virtual").strip().lower()
    return mode if mode in {"virtual", "real"} else "virtual"


def compute_mid_price(
    best_bid: Optional[float],
    best_ask: Optional[float],
) -> float:
    if best_bid is not None and best_ask is not None:
        return (best_bid + best_ask) / 2.0
    return 0.0


def compute_position_value(
    *,
    position_side: Optional[str],
    position_size: float,
    position_entry_price: float,
    mid_price: float,
    trading_mode: str = "virtual",
) -> float:
    """
    ポジション評価額（VirtualTrader.unrealized_pnl と同一定義）。
    LONG のみ trading_mode で分岐する。
    """
    side = str(position_side or "").strip().upper() or None
    mode = normalize_trading_mode(trading_mode)
    if side == "LONG" and position_size > 0:
        if mode == "real":
            if position_entry_price > 0:
                return (mid_price - position_entry_price) * position_size
            return 0.0
        return position_size * mid_price
    if side == "SHORT" and position_size > 0 and position_entry_price > 0:
        return (position_entry_price - mid_price) * position_size
    return 0.0


def compute_total_assets(
    *,
    jpy_balance: float,
    position_side: Optional[str] = None,
    position_size: float = 0.0,
    position_entry_price: float = 0.0,
    best_bid: Optional[float] = None,
    best_ask: Optional[float] = None,
    mid_price: Optional[float] = None,
    trading_mode: str = "virtual",
    position_is_pending: bool = False,
) -> float:
    """現金残高 + ポジション評価額（ダッシュボード「総資産 (円換算)」と同じ）。

    pending（未約定指値）中は建玉評価を含めず jpy_balance のみ返す
    （get_internal_account_state の comparable_equity_jpy と同一定義）。
    """
    if position_is_pending:
        return float(jpy_balance)
    mid = compute_mid_price(best_bid, best_ask) if mid_price is None else mid_price
    position_value = compute_position_value(
        position_side=position_side,
        position_size=position_size,
        position_entry_price=position_entry_price,
        mid_price=mid,
        trading_mode=trading_mode,
    )
    return jpy_balance + position_value


def _optional_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy_pending(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) != 0
    text = str(value).strip().lower()
    if text in {"", "0", "false", "none", "null"}:
        return False
    return True


def compute_total_assets_from_live_state(state: StateLike) -> float:
    """live_state.db の1行（dict / sqlite3.Row）から総資産を算出する。"""
    jpy_balance = float(state.get("jpy_balance") or 0.0)
    bid = _optional_float(state.get("best_bid_price"))
    ask = _optional_float(state.get("best_ask_price"))
    trading_mode = normalize_trading_mode(
        str(state.get("trading_mode") or "") or None
    )
    return compute_total_assets(
        jpy_balance=jpy_balance,
        position_side=str(state.get("position_side") or "") or None,
        position_size=float(state.get("position_size") or 0.0),
        position_entry_price=float(state.get("position_entry_price") or 0.0),
        best_bid=bid,
        best_ask=ask,
        trading_mode=trading_mode,
        position_is_pending=_truthy_pending(state.get("position_is_pending")),
    )
