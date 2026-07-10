"""
check_gmo_api_connection.py
----------------------------
GMOコイン Private API との疎通・レスポンス形式を確認するための
単独実行スクリプト。trading_engine / dashboard は起動不要。

実行方法:
    1. プロジェクトルート (gmo-btc-ws) に、このファイルを置く
    2. 環境変数 GMO_API_KEY / GMO_API_SECRET を設定する
       (PowerShellの場合)
         $env:GMO_API_KEY = "xxxxx"
         $env:GMO_API_SECRET = "xxxxx"
    3. python check_gmo_api_connection.py を実行する

このスクリプトは発注・取消などの操作は一切行わず、
残高・建玉の参照のみ行う。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
BTC_DIR = ROOT_DIR / "btc_trading_tool"
if str(BTC_DIR) not in sys.path:
    sys.path.insert(0, str(BTC_DIR))

import os  # noqa: E402


def main() -> None:
    print("=" * 60)
    print("GMO Coin Private API 疎通確認")
    print("=" * 60)

    api_key = os.getenv("GMO_API_KEY", "").strip()
    api_secret = os.getenv("GMO_API_SECRET", "").strip()
    print(f"GMO_API_KEY 設定状況    : {'OK (' + str(len(api_key)) + '文字)' if api_key else '未設定'}")
    print(f"GMO_API_SECRET 設定状況 : {'OK (' + str(len(api_secret)) + '文字)' if api_secret else '未設定'}")

    if not api_key or not api_secret:
        print("\n[NG] 環境変数が未設定のため、API呼び出しをスキップします。")
        print("     PowerShellで $env:GMO_API_KEY / $env:GMO_API_SECRET を設定してから再実行してください。")
        return

    try:
        from virtual_trader import fetch_real_account_state
    except ImportError as exc:
        print(f"\n[NG] virtual_trader のインポートに失敗しました: {exc}")
        print("     このスクリプトをプロジェクトルート (gmo-btc-ws) に置いているか確認してください。")
        return

    print("\n--- API呼び出し中 ---")
    try:
        state = fetch_real_account_state()
    except Exception as exc:
        print("\n[NG] API呼び出しでエラーが発生しました:")
        print(f"     {type(exc).__name__}: {exc}")
        print("\n考えられる原因:")
        print("  - APIキー/シークレットが無効、または権限不足")
        print("  - GMOコイン側のメンテナンス時間帯 (05:55-06:30 JST / 土曜09:00-11:00 JST)")
        print("  - ネットワーク接続不可")
        return

    print("\n[OK] API呼び出し成功")
    print("-" * 60)
    print(f"jpy_balance       : {state['jpy_balance']}")
    print(f"position_size_btc : {state['position_size_btc']}  (正=LONG / 負=SHORT / 0=建玉なし)")
    print("-" * 60)

    print("\n--- 簡易チェック ---")
    if state["jpy_balance"] == 0.0:
        print("[OK] 残高0円が正しく取得できています(想定通り)")
    else:
        print(f"[注意] 残高が0円ではありません: {state['jpy_balance']} 円")

    if state["position_size_btc"] == 0.0:
        print("[OK] 建玉なしが正しく取得できています(想定通り)")
    else:
        print(f"[注意] 建玉が検出されました: {state['position_size_btc']} BTC")

    print("\n完了しました。上記の結果を貼り付けて共有してください。")


if __name__ == "__main__":
    main()

