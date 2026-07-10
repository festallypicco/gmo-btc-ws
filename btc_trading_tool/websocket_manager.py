"""
websocket_manager.py
--------------------
GMOコイン Public WebSocket から板情報を常時受信し、
最新の OrderbookSnapshot を別スレッドで保持し続けるクラス。

Streamlit や他のメインスレッドから .latest_snapshot を参照するだけで
最新の板情報を取得できる。
"""
import json
import time
import threading
from typing import Callable, Optional

import websocket

from strategy_logic import OrderbookSnapshot

WS_ENDPOINT = "wss://api.coin.z.com/ws/public/v1"

SUBSCRIBE_MSG = json.dumps({
    "command": "subscribe",
    "channel": "orderbooks",
    "symbol": "BTC",
})

# 再接続の待機時間（秒）。失敗のたびに指数バックオフで延ばし、上限で頭打ちにする。
_RECONNECT_BASE_SEC: float = 3.0
_RECONNECT_MAX_SEC:  float = 60.0


class WebSocketManager:
    """
    別スレッドで WebSocket を常時接続し、
    最新の板スナップショットをスレッドセーフに保持する。

    切断・エラー発生時は指数バックオフ付きで自動再接続を無限に繰り返す。
    stop() を呼ぶと再接続ループを終了する。

    on_snapshot_callback: 新しい板情報を受信した瞬間（ミリ秒単位）に
    呼び出すコールバック。VirtualTrader.on_orderbook_update を渡すことで、
    Streamlit の描画サイクルと独立したリアルタイム売買評価が実現する。
    """

    def __init__(
        self,
        on_snapshot_callback: Optional[Callable[[OrderbookSnapshot], None]] = None,
        on_exchange_status_callback: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._snapshot: Optional[OrderbookSnapshot] = None
        self._lock              = threading.Lock()
        self._ws_app:           Optional[websocket.WebSocketApp] = None
        self._thread:           Optional[threading.Thread] = None
        self._stop_event        = threading.Event()
        self._on_snapshot_cb    = on_snapshot_callback  # リアルタイム売買コールバック
        self._on_status_cb      = on_exchange_status_callback
        self._maintenance_alert_active = False

    # ------------------------------------------------------------------ #
    #  Public                                                              #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """バックグラウンドスレッドで WebSocket 接続を開始する（冪等）"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """再接続ループを止め、現在の接続を閉じる"""
        self._stop_event.set()
        if self._ws_app:
            self._ws_app.close()

    @property
    def latest_snapshot(self) -> Optional[OrderbookSnapshot]:
        """スレッドセーフに最新スナップショットを返す"""
        with self._lock:
            return self._snapshot

    # ------------------------------------------------------------------ #
    #  再接続ループ（内部）                                                 #
    # ------------------------------------------------------------------ #

    def _run_loop(self) -> None:
        """
        切断・例外が発生しても指数バックオフで再接続し続けるループ。
        stop() が呼ばれると安全に終了する。
        """
        attempt = 0
        while not self._stop_event.is_set():
            if attempt > 0:
                wait = min(_RECONNECT_BASE_SEC * (2 ** (attempt - 1)), _RECONNECT_MAX_SEC)
                print(f"[WebSocketManager] {wait:.0f} 秒後に再接続します... (試行 #{attempt})")
                # stop_event が先に立ったら待機を中断して終了
                if self._stop_event.wait(timeout=wait):
                    break

            try:
                self._connect_once()
            except Exception as exc:
                print(f"[WebSocketManager] 予期しない例外: {exc}")

            attempt += 1

        print("[WebSocketManager] 再接続ループを終了しました。")

    def _connect_once(self) -> None:
        """WebSocketApp を 1 セッション分だけ実行する"""
        self._ws_app = websocket.WebSocketApp(
            WS_ENDPOINT,
            on_open    = self._on_open,
            on_message = self._on_message,
            on_error   = self._on_error,
            on_close   = self._on_close,
        )
        # run_forever はセッションが終わると（切断・エラー後に）返ってくる
        self._ws_app.run_forever()

    # ------------------------------------------------------------------ #
    #  WebSocket callbacks（内部）                                         #
    # ------------------------------------------------------------------ #

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        print("[WebSocketManager] 接続しました。板情報を購読開始...")
        ws.send(SUBSCRIBE_MSG)

    def _on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        data = json.loads(message)
        asks = data.get("asks", [])
        bids = data.get("bids", [])
        if not asks or not bids:
            return

        if self._maintenance_alert_active:
            self._maintenance_alert_active = False
            self._notify_status("NORMAL_RESPONSE", "orderbook response recovered")

        snap = OrderbookSnapshot(
            best_bid_price = float(bids[0]["price"]),
            best_bid_size  = float(bids[0]["size"]),
            best_ask_price = float(asks[0]["price"]),
            best_ask_size  = float(asks[0]["size"]),
        )
        with self._lock:
            self._snapshot = snap

        # 売買評価をミリ秒単位でリアルタイム実行（Streamlit の描画サイクルと独立）
        if self._on_snapshot_cb is not None:
            try:
                self._on_snapshot_cb(snap)
            except Exception as exc:
                print(f"[WebSocketManager] コールバック例外: {exc}")

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        print(f"[WebSocketManager] エラー発生: {error}")
        self._handle_possible_maintenance_error(error)
        # 古い板情報でゾンビ動作しないようスナップショットをクリア
        with self._lock:
            self._snapshot = None

    def _on_close(
        self,
        ws:                websocket.WebSocketApp,
        close_status_code: Optional[int],
        close_msg:         Optional[str],
    ) -> None:
        # 切断中に古い板情報が残らないようクリア
        with self._lock:
            self._snapshot = None

        self._handle_possible_maintenance_error(close_msg or close_status_code)

        if self._stop_event.is_set():
            print(f"[WebSocketManager] 正常切断 (code={close_status_code})")
        else:
            print(f"[WebSocketManager] unexpected disconnect (code={close_status_code}) -> reconnect pending")

    def _notify_status(self, status: str, detail: str) -> None:
        if self._on_status_cb is None:
            return
        try:
            self._on_status_cb(status, detail)
        except Exception as exc:
            print(f"[WebSocketManager] ステータスコールバック例外: {exc}")

    @staticmethod
    def _is_maintenance_error(error: object) -> bool:
        text = str(error).lower()
        keywords = (
            "503",
            "service unavailable",
            "maintenance",
            "temporarily unavailable",
            "メンテ",
        )
        return any(k in text for k in keywords)

    def _handle_possible_maintenance_error(self, error: object) -> None:
        if not self._is_maintenance_error(error):
            return
        if self._maintenance_alert_active:
            return
        self._maintenance_alert_active = True
        detail = str(error)
        print(f"[WARNING] [WebSocketManager] メンテナンス/503系エラーを検知: {detail}")
        self._notify_status("MAINTENANCE_DETECTED", detail)
