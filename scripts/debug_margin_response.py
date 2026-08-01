"""Temporary GET /v1/account/margin raw response dump. Delete after use."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_ROOT = "https://api.coin.z.com/private"
MARGIN_PATH = "/v1/account/margin"

ROOT = Path(__file__).resolve().parent.parent
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


def main() -> int:
    api_key = (os.environ.get("GMO_API_KEY_TRADE") or "").strip()
    api_secret = (os.environ.get("GMO_API_SECRET_TRADE") or "").strip()
    print(f"[INFO] GMO_API_KEY_TRADE set={bool(api_key)}")
    print(f"[INFO] GMO_API_SECRET_TRADE set={bool(api_secret)}")
    if not api_key or not api_secret:
        print("[ERR] GMO_API_KEY_TRADE / GMO_API_SECRET_TRADE are not set")
        return 1

    timestamp = str(int(time.time() * 1000))
    method = "GET"
    sign = hmac.new(
        api_secret.encode("utf-8"),
        (timestamp + method + MARGIN_PATH).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "API-KEY": api_key,
        "API-TIMESTAMP": timestamp,
        "API-SIGN": sign,
    }
    url = API_ROOT + MARGIN_PATH
    print(f"[INFO] GET {url}")
    req = urllib.request.Request(url, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status_code = resp.getcode()
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        body = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        print(f"[ERR] connection error: {exc}")
        return 1

    print(f"[INFO] HTTP status={status_code}")
    print("--- raw response ---")
    try:
        parsed = json.loads(body)
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(body)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
