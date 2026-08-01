"""Temporary ERR-200 probe: dual closeOrder LIMIT+STOP on one position.

Delete after use. Does not modify trading engine / VirtualTrader production paths.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
BTC_DIR = ROOT / "btc_trading_tool"
if str(BTC_DIR) not in sys.path:
    sys.path.insert(0, str(BTC_DIR))

_ENV_PATH = ROOT / ".env"
if _ENV_PATH.exists():
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and not (os.environ.get(key) or "").strip():
            os.environ[key] = value

from virtual_trader import (  # noqa: E402
    GmoApiError,
    _GMO_LEVERAGE_SYMBOL,
    _gmo_private_request,
    fetch_active_orders,
    fetch_open_positions,
    gmo_cancel_order,
    gmo_order,
)

SIZE = "0.001"
SYMBOL = _GMO_LEVERAGE_SYMBOL
PUBLIC_TICKER = "https://api.coin.z.com/public/v1/ticker?symbol=BTC"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _fetch_mid_price() -> float:
    req = urllib.request.Request(PUBLIC_TICKER, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    data = body.get("data") or []
    if not data:
        raise RuntimeError(f"empty ticker: {body!r}")
    row = data[0]
    bid = float(row["bid"])
    ask = float(row["ask"])
    mid = (bid + ask) / 2.0
    _log(f"[INFO] ticker bid={bid:,.0f} ask={ask:,.0f} mid={mid:,.0f}")
    return mid


def _close_order(
    *,
    side: str,
    execution_type: str,
    price: Optional[float],
    position_id: int,
    size: str,
    time_in_force: Optional[str] = None,
) -> Any:
    body: Dict[str, Any] = {
        "symbol": SYMBOL,
        "side": side,
        "executionType": execution_type,
        "settlePosition": [
            {"positionId": int(position_id), "size": str(size)},
        ],
    }
    if price is not None:
        body["price"] = str(int(round(float(price))))
    if time_in_force is not None:
        body["timeInForce"] = time_in_force
    return _gmo_private_request("POST", "/v1/closeOrder", body)


def _wait_for_position(
    *,
    want_side: str,
    timeout_sec: float = 90.0,
) -> Dict[str, Any]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        positions = fetch_open_positions()
        _log(f"[INFO] openPositions count={len(positions)}")
        for item in positions:
            side = str(item.get("side", "")).upper()
            size = str(item.get("size", ""))
            if side == want_side and float(size) >= float(SIZE) - 1e-12:
                return item
        time.sleep(2.0)
    raise RuntimeError(f"timed out waiting for {want_side} position size>={SIZE}")


def _dump_state(label: str) -> None:
    positions = fetch_open_positions()
    orders = fetch_active_orders()
    _log(f"=== {label}: openPositions ({len(positions)}) ===")
    _log(json.dumps(positions, ensure_ascii=False, indent=2))
    _log(f"=== {label}: activeOrders ({len(orders)}) ===")
    _log(json.dumps(orders, ensure_ascii=False, indent=2))


def _cancel_ids(order_ids: List[int]) -> None:
    for oid in order_ids:
        if oid is None:
            continue
        try:
            gmo_cancel_order(int(oid))
            _log(f"[OK] cancelled orderId={oid}")
        except GmoApiError as exc:
            _log(f"[WARN] cancel GmoApiError orderId={oid}: {exc}")
        except Exception as exc:
            _log(f"[WARN] cancel failed orderId={oid}: {exc}")


def main() -> int:
    trade_key = (os.environ.get("GMO_API_KEY_TRADE") or "").strip()
    trade_secret = (os.environ.get("GMO_API_SECRET_TRADE") or "").strip()
    _log(f"[INFO] GMO_API_KEY_TRADE set={bool(trade_key)}")
    _log(f"[INFO] GMO_API_SECRET_TRADE set={bool(trade_secret)}")
    if not trade_key or not trade_secret:
        _log("[ERR] TRADE credentials missing")
        return 1

    created_order_ids: List[int] = []
    entry_order_id: Optional[int] = None
    position_id: Optional[int] = None
    stop_result: str = "not_attempted"

    try:
        _dump_state("BEFORE")
        mid = _fetch_mid_price()

        # 1) MARKET BUY entry (minimal lot) so we get a fill quickly
        _log("[STEP1] MARKET BUY entry size=0.001")
        entry_raw = _gmo_private_request(
            "POST",
            "/v1/order",
            {
                "symbol": SYMBOL,
                "side": "BUY",
                "executionType": "MARKET",
                "size": SIZE,
            },
        )
        entry_order_id = int(entry_raw)
        created_order_ids.append(entry_order_id)
        _log(f"[OK] entry orderId={entry_order_id}")

        # 2) wait for positionId
        _log("[STEP2] wait for openPositions BUY")
        pos = _wait_for_position(want_side="BUY")
        position_id = int(pos["positionId"])
        pos_size = str(pos.get("size", SIZE))
        pos_price = float(pos.get("price", mid))
        _log(
            f"[OK] positionId={position_id}"
            f" side={pos.get('side')}"
            f" size={pos_size}"
            f" price={pos_price:,.0f}"
            f" orderdSize={pos.get('orderdSize')}"
        )

        # Prices that should NOT fill immediately for SELL close of LONG:
        # LIMIT (TP-like): well above market
        # STOP (SL-like): well below market
        limit_price = int(round(pos_price * 1.05))
        stop_price = int(round(pos_price * 0.95))
        _log(f"[INFO] close LIMIT price={limit_price:,} STOP price={stop_price:,}")

        # 3) LIMIT close full size
        _log("[STEP3] closeOrder LIMIT full size")
        try:
            limit_raw = _close_order(
                side="SELL",
                execution_type="LIMIT",
                price=float(limit_price),
                position_id=position_id,
                size=SIZE,
                time_in_force="FAS",
            )
            limit_oid = int(limit_raw)
            created_order_ids.append(limit_oid)
            _log(f"[OK] LIMIT close orderId={limit_oid}")
            limit_ok = True
        except GmoApiError as exc:
            _log(f"[ERR] LIMIT close failed: {exc}")
            _log(f"[ERR] codes={exc.message_codes}")
            limit_ok = False
            stop_result = "limit_failed_skip_stop"
            raise

        time.sleep(1.0)
        after_limit = fetch_open_positions()
        matched = next(
            (p for p in after_limit if int(p.get("positionId", -1)) == position_id),
            None,
        )
        _log(
            f"[INFO] after LIMIT: position still present={matched is not None}"
            f" orderdSize={None if matched is None else matched.get('orderdSize')}"
        )

        # 4) STOP close full size (the ERR-200 probe)
        _log("[STEP4] closeOrder STOP full size (ERR-200 probe)")
        try:
            stop_raw = _close_order(
                side="SELL",
                execution_type="STOP",
                price=float(stop_price),
                position_id=position_id,
                size=SIZE,
                time_in_force=None,
            )
            stop_oid = int(stop_raw)
            created_order_ids.append(stop_oid)
            stop_result = f"SUCCESS orderId={stop_oid}"
            _log(f"[RESULT] STOP close SUCCEEDED orderId={stop_oid}")
        except GmoApiError as exc:
            codes = exc.message_codes
            stop_result = f"GmoApiError status={exc.status} codes={codes} messages={exc.messages}"
            _log(f"[RESULT] STOP close FAILED: {stop_result}")
            if any(c == "ERR-200" for c in codes):
                _log("[RESULT] ERR-200 confirmed")
            else:
                _log("[RESULT] failed with non-ERR-200 code")
        except Exception as exc:
            stop_result = f"Exception {type(exc).__name__}: {exc}"
            _log(f"[RESULT] STOP close FAILED: {stop_result}")

        _dump_state("AFTER_STEP4")

    finally:
        # 6) cancel all created orders and report final state
        _log("[STEP6] cleanup: cancel created orders")
        # Prefer cancelling active close orders; also try known ids
        try:
            active = fetch_active_orders()
            active_ids = []
            for o in active:
                try:
                    active_ids.append(int(o.get("orderId")))
                except (TypeError, ValueError):
                    pass
            _cancel_ids(sorted(set(created_order_ids + active_ids)))
        except Exception as exc:
            _log(f"[WARN] cleanup list/cancel failed: {exc}")
            _cancel_ids(created_order_ids)

        time.sleep(2.0)
        _dump_state("FINAL")
        _log(f"[SUMMARY] entry_order_id={entry_order_id}")
        _log(f"[SUMMARY] position_id={position_id}")
        _log(f"[SUMMARY] STOP second close result: {stop_result}")

        # Leave position open? User asked to verify position still remains
        # (not accidentally closed). Do NOT market-close the entry position
        # unless user wants flat account - they said confirm position remains.
        # Report only.
        try:
            positions = fetch_open_positions()
            if position_id is not None:
                still = any(int(p.get("positionId", -1)) == int(position_id) for p in positions)
                _log(f"[SUMMARY] original position still open={still}")
            _log(f"[SUMMARY] remaining openPositions count={len(positions)}")
            if positions:
                _log(
                    "[WARN] open position remains after probe;"
                    " manual close via dashboard/force-close may be needed"
                )
        except Exception as exc:
            _log(f"[WARN] final position check failed: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
