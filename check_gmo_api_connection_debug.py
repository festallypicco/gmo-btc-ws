"""
check_gmo_api_connection_debug.py
-----------------------------------
GMOコイン Private API のレスポンスを「加工前の生の形」で確認するための
デバッグ用スクリプト。fetch_real_account_state() を経由せず、
直接 /v1/account/assets を叩いて中身を確認する。

実行方法:
    1. プロジェクトルート (gmo-btc-ws) に、このファイルを置く
    2. 環境変数 GMO_API_KEY / GMO_API_SECRET を設定する
    3. python check_gmo_api_connection_debug.py を実行する

発注・取消などの操作は一切行わない(残高取得のみ)。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.request
import urllib.error

API_ROOT = "https://api.coin.z.com/private"
ASSETS_PATH = "/v1/account/assets"


def _sign(timestamp: str, method: str, path: str, api_secret: str) -> str:
    text = timestamp + method + path
    return hmac.new(api_secret.encode("ascii"), text.encode("ascii"), hashlib.sha256).hexdigest()


def main() -> None:
    print("=" * 60)
    print("GMO Coin API 生レスポンス確認 (資産残高取得)")
    print("=" * 60)

    api_key = os.getenv("GMO_API_KEY", "").strip()
    api_secret = os.getenv("GMO_API_SECRET", "").strip()

    if not api_key or not api_secret:
        print("[NG] GMO_API_KEY / GMO_API_SECRET が未設定です。")
        return

    timestamp = str(int(time.time() * 1000))
    method = "GET"
    sign = _sign(timestamp, method, ASSETS_PATH, api_secret)

    headers = {
        "API-KEY": api_key,
        "API-TIMESTAMP": timestamp,
        "API-SIGN": sign,
    }

    url = API_ROOT + ASSETS_PATH
    print(f"\nリクエストURL : {url}")
    print(f"タイムスタンプ : {timestamp}")

    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status_code = resp.getcode()
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        body = exc.read().decode("utf-8")
    except urllib.error.URLError as exc:
        print(f"\n[NG] 接続エラー: {exc}")
        print("     ネットワーク接続、またはファイアウォール設定を確認してください。")
        return

    print(f"\nHTTPステータスコード: {status_code}")
    print("-" * 60)
    print("生レスポンス(JSON):")
    try:
        parsed = json.loads(body)
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(body)
        return
    print("-" * 60)

    status = parsed.get("status")
    messages = parsed.get("messages")
    data = parsed.get("data")

    print("\n--- 診断 ---")
    print(f"status  : {status}  (0=正常, 0以外=エラー)")
    if messages:
        print(f"messages: {messages}")
        print("\n[NG] GMO側がエラーを返しています。上記 messages のエラーコードを確認してください。")
        print("     よくある原因:")
        print("       ERR-5003 / ERR-5004 : APIキーまたは署名が不正 (キー・シークレットの控えミスの可能性)")
        print("       ERR-5106            : APIコール回数制限超過")
        print("       ERR-401 系          : 権限不足 (「資産残高を取得」権限が付いているか再確認)")
        print("       ERR-5011            : タイムスタンプが無効 (PCの時刻がズレている可能性)")
    elif data is None:
        print("[NG] status=0 なのに data が存在しません。GMO側の仕様変更の可能性があります。")
    else:
        print("[OK] 正常にdataが取得できています。")
        print(data)


if __name__ == "__main__":
    main()
