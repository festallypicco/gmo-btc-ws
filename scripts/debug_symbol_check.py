"""Temporary GMO symbol check (openPositions / activeOrders). Delete after use."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BTC_DIR = ROOT / "btc_trading_tool"
if str(BTC_DIR) not in sys.path:
    sys.path.insert(0, str(BTC_DIR))

# Load root .env into os.environ when not already set (Docker env_file usually sets these).
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

from virtual_trader import fetch_active_orders, fetch_open_positions  # noqa: E402


def _run(label: str, fn, symbol: str) -> None:
    print("=" * 60)
    print(f"[TRY] {label} symbol={symbol!r}")
    try:
        result = fn(symbol=symbol)
        print(f"[OK] count={len(result)}")
        print(f"[OK] sample={result[:2]!r}")
    except Exception as exc:
        print(f"[ERR] type={type(exc).__name__}")
        print(f"[ERR] {exc}")
        messages = getattr(exc, "messages", None)
        if messages is not None:
            print(f"[ERR] messages={messages!r}")
        codes = getattr(exc, "message_codes", None)
        if codes is not None:
            print(f"[ERR] message_codes={codes!r}")


def main() -> int:
    trade_key = (os.environ.get("GMO_API_KEY_TRADE") or "").strip()
    trade_secret = (os.environ.get("GMO_API_SECRET_TRADE") or "").strip()
    print(
        f"[INFO] GMO_API_KEY_TRADE set={bool(trade_key)}"
        f" GMO_API_SECRET_TRADE set={bool(trade_secret)}"
    )
    _run("fetch_open_positions", fetch_open_positions, "BTC")
    _run("fetch_open_positions", fetch_open_positions, "BTC_JPY")
    _run("fetch_active_orders", fetch_active_orders, "BTC")
    _run("fetch_active_orders", fetch_active_orders, "BTC_JPY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
