"""
websocket_manager.py
--------------------
GMOコイン Public WebSocket から板情報を常時受信し、
最新の OrderbookSnapshot を別スレッドで保持し続けるクラス。

加えて約定通知 (trades) を購読し、直近の market_snapshot 区間分を
メモリ上に蓄積する（板情報処理とは別経路）。

Streamlit や他のメインスレッドから .latest_snapshot を参照するだけで
最新の板情報を取得できる。
"""
import json
import time
import threading
import hmac
import hashlib
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

import websocket

from strategy_logic import OrderbookSnapshot
from virtual_trader import _resolve_gmo_api_credentials

WS_ENDPOINT = "wss://api.coin.z.com/ws/public/v1"

SUBSCRIBE_MSG = json.dumps({
    "command": "subscribe",
    "channel": "orderbooks",
    "symbol": "BTC",
})

SUBSCRIBE_TRADES_MSG = json.dumps({
    "command": "subscribe",
    "channel": "trades",
    "symbol": "BTC",
})

# 再接続の待機時間（秒）。失敗のたびに指数バックオフで延ばし、上限で頭打ちにする。
_RECONNECT_BASE_SEC: float = 3.0
_RECONNECT_MAX_SEC:  float = 60.0
PRIVATE_WS_ENDPOINT_BASE = "wss://api.coin.z.com/ws/private/v1"
PRIVATE_API_ENDPOINT = "https://api.coin.z.com/private"
_TOKEN_EXTEND_INTERVAL_SEC = 45 * 60


def _sum_orderbook_sizes(levels: List[dict], max_levels: int = 5) -> float:
    """板の浅い方から max_levels 階層分の size を合計する。"""
    total = 0.0
    for level in levels[:max_levels]:
        try:
            total += float(level.get("size") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def _compute_depth_stats(
    bids: List[dict],
    asks: List[dict],
    max_levels: int = 5,
) -> Dict[str, Optional[float]]:
    """5階層分の買い/売りサイズ合計と厚み比率を返す。"""
    bid_depth5_size = _sum_orderbook_sizes(bids, max_levels)
    ask_depth5_size = _sum_orderbook_sizes(asks, max_levels)
    denom = bid_depth5_size + ask_depth5_size
    depth_imbalance: Optional[float] = (
        bid_depth5_size / denom if denom > 0 else None
    )
    return {
        "bid_depth5_size": bid_depth5_size,
        "ask_depth5_size": ask_depth5_size,
        "depth_imbalance": depth_imbalance,
    }


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
        self._depth_stats: Optional[Dict[str, Optional[float]]] = None
        self._lock              = threading.Lock()
        self._ws_app:           Optional[websocket.WebSocketApp] = None
        self._thread:           Optional[threading.Thread] = None
        self._stop_event        = threading.Event()
        self._on_snapshot_cb    = on_snapshot_callback  # リアルタイム売買コールバック
        self._on_status_cb      = on_exchange_status_callback
        self._maintenance_alert_active = False
        # trades 用（板情報の _lock / _snapshot とは独立）
        self._trade_lock = threading.Lock()
        self._trade_buffer: List[Dict[str, Any]] = []

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

    @property
    def latest_depth_stats(self) -> Optional[Dict[str, Optional[float]]]:
        """スレッドセーフに最新の5階層板厚み集計を返す"""
        with self._lock:
            return self._depth_stats

    def consume_trade_window_stats(self) -> Dict[str, Any]:
        """
        直近区間に蓄積した約定を集計して返し、バッファをクリアする。
        market_snapshot 書き込み直後に呼ぶ想定。
        """
        with self._trade_lock:
            trades = self._trade_buffer
            self._trade_buffer = []

        trade_count = len(trades)
        buy_volume = 0.0
        sell_volume = 0.0
        for trade in trades:
            side = str(trade.get("side") or "").upper()
            size = float(trade.get("size") or 0.0)
            if side == "BUY":
                buy_volume += size
            elif side == "SELL":
                sell_volume += size
        return {
            "trade_count": trade_count,
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
        }

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
        print("[WebSocketManager] 接続しました。板情報・約定通知を購読開始...")
        ws.send(SUBSCRIBE_MSG)
        # 同一接続で連続 subscribe すると ERR-5003 になることがあるため間隔を空ける
        time.sleep(2.0)
        ws.send(SUBSCRIBE_TRADES_MSG)

    def _on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError as exc:
            print(f"[WebSocketManager] orderbook JSON parse error: {exc}")
            return
        if str(data.get("channel") or "") == "trades":
            self._on_trades_message(data)
            return

        asks = data.get("asks", [])
        bids = data.get("bids", [])
        if not asks or not bids:
            return

        if self._maintenance_alert_active:
            self._maintenance_alert_active = False
            self._notify_status("NORMAL_RESPONSE", "orderbook response recovered")

        try:
            snap = OrderbookSnapshot(
                best_bid_price = float(bids[0]["price"]),
                best_bid_size  = float(bids[0]["size"]),
                best_ask_price = float(asks[0]["price"]),
                best_ask_size  = float(asks[0]["size"]),
            )
            depth_stats = _compute_depth_stats(bids, asks, 5)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            print(f"[WebSocketManager] orderbook parse error: {exc}")
            return
        with self._lock:
            self._snapshot = snap
            self._depth_stats = depth_stats

        # 売買評価をミリ秒単位でリアルタイム実行（Streamlit の描画サイクルと独立）
        if self._on_snapshot_cb is not None:
            try:
                self._on_snapshot_cb(snap)
            except Exception as exc:
                print(f"[WebSocketManager] コールバック例外: {exc}")

    def _on_trades_message(self, data: Dict[str, Any]) -> None:
        """約定通知チャンネル専用。板情報コールバックとは独立。"""
        try:
            side = str(data.get("side") or "").upper()
            if side not in {"BUY", "SELL"}:
                return
            price = float(data.get("price") or 0.0)
            size = float(data.get("size") or 0.0)
            if size <= 0.0:
                return
            timestamp = str(data.get("timestamp") or "")
        except (TypeError, ValueError) as exc:
            print(f"[WebSocketManager] trades parse error: {exc}")
            return

        trade = {
            "price": price,
            "size": size,
            "side": side,
            "timestamp": timestamp,
        }
        with self._trade_lock:
            self._trade_buffer.append(trade)

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        print(f"[WebSocketManager] エラー発生: {error}")
        self._handle_possible_maintenance_error(error)
        # 古い板情報でゾンビ動作しないようスナップショットをクリア
        with self._lock:
            self._snapshot = None
            self._depth_stats = None

    def _on_close(
        self,
        ws:                websocket.WebSocketApp,
        close_status_code: Optional[int],
        close_msg:         Optional[str],
    ) -> None:
        # 切断中に古い板情報が残らないようクリア
        with self._lock:
            self._snapshot = None
            self._depth_stats = None

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


class PrivateWebSocketManager:
    """
    GMOコイン Private WebSocket の executionEvents / orderEvents を購読する。

    - POST /private/v1/ws-auth でトークンを取得
    - wss://api.coin.z.com/ws/private/v1/<token> に接続
    - 45分ごとに PUT /private/v1/ws-auth でトークンを延長
    - 切断時は指数バックオフで再接続
    - stop() 時に DELETE /private/v1/ws-auth でトークンを削除
    """

    def __init__(
        self,
        on_execution_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_order_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._ws_app: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._token_lock = threading.Lock()
        self._token: Optional[str] = None

        self._renew_stop_event = threading.Event()
        self._renew_thread: Optional[threading.Thread] = None
        # 再接続のたびに増やす。古い renew ループが clear 後に誤継続しないようにする。
        self._renew_generation = 0

        self._on_execution_cb = on_execution_callback
        self._on_order_cb = on_order_callback

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._stop_token_renewer()
        if self._ws_app:
            self._ws_app.close()
        self._delete_token_safely()

    def _run_loop(self) -> None:
        attempt = 0
        try:
            while not self._stop_event.is_set():
                if attempt > 0:
                    wait = min(_RECONNECT_BASE_SEC * (2 ** (attempt - 1)), _RECONNECT_MAX_SEC)
                    print(f"[PrivateWebSocketManager] {wait:.0f} 秒後に再接続します... (試行 #{attempt})")
                    if self._stop_event.wait(timeout=wait):
                        break

                try:
                    token = self._get_or_create_token()
                    self._connect_once(token)
                except Exception as exc:
                    print(f"[PrivateWebSocketManager] 予期しない例外: {exc}")

                attempt += 1
        finally:
            self._stop_token_renewer()
            print("[PrivateWebSocketManager] 再接続ループを終了しました。")

    def _connect_once(self, token: str) -> None:
        endpoint = f"{PRIVATE_WS_ENDPOINT_BASE}/{token}"
        self._ws_app = websocket.WebSocketApp(
            endpoint,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        try:
            self._ws_app.run_forever()
        finally:
            # on_close が呼ばれない例外経路でも古いタイマーを残さない
            self._stop_token_renewer()
            self._ws_app = None

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        self._start_token_renewer()
        print("[PrivateWebSocketManager] 接続しました。executionEvents/orderEvents を購読開始...")
        ws.send(json.dumps({"command": "subscribe", "channel": "executionEvents"}))
        ws.send(json.dumps({"command": "subscribe", "channel": "orderEvents"}))

    def _on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            print(f"[PrivateWebSocketManager] JSON parse error: {message}")
            return

        channel = str(data.get("channel", ""))
        if channel == "executionEvents":
            print("[PrivateWebSocketManager] executionEvents 受信")
            if self._on_execution_cb is not None:
                try:
                    self._on_execution_cb(data)
                except Exception as exc:
                    print(f"[PrivateWebSocketManager] execution callback error: {exc}")
            return
        if channel == "orderEvents":
            print("[PrivateWebSocketManager] orderEvents 受信")
            if self._on_order_cb is not None:
                try:
                    self._on_order_cb(data)
                except Exception as exc:
                    print(f"[PrivateWebSocketManager] order callback error: {exc}")
            return

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        print(f"[PrivateWebSocketManager] エラー発生: {error}")
        self._stop_token_renewer()
        self._invalidate_token_if_needed(error)

    def _on_close(
        self,
        ws: websocket.WebSocketApp,
        close_status_code: Optional[int],
        close_msg: Optional[str],
    ) -> None:
        self._stop_token_renewer()
        reason = close_msg or close_status_code
        self._invalidate_token_if_needed(reason)
        if self._stop_event.is_set():
            print(f"[PrivateWebSocketManager] 正常切断 (code={close_status_code})")
        else:
            print(
                f"[PrivateWebSocketManager] unexpected disconnect (code={close_status_code})"
                " -> reconnect pending"
            )

    def _get_or_create_token(self) -> str:
        with self._token_lock:
            if self._token:
                return self._token
        token = self._create_token()
        with self._token_lock:
            self._token = token
            return token

    def _create_token(self) -> str:
        payload = self._private_request("POST", "/v1/ws-auth", {})
        token = str(payload.get("data", "")).strip()
        if not token:
            raise RuntimeError("ws-auth token の取得に失敗しました。")
        print("[PrivateWebSocketManager] ws-auth token を取得しました。")
        return token

    def _extend_token(self, expected_token: str) -> None:
        with self._token_lock:
            token = self._token
        if not token:
            raise RuntimeError("延長対象トークンがありません。")
        if token != expected_token:
            raise RuntimeError("延長対象トークンが現行トークンと一致しません。")
        self._private_request("PUT", "/v1/ws-auth", {"token": token})
        print("[PrivateWebSocketManager] ws-auth token を延長しました。")

    def _delete_token_safely(self) -> None:
        with self._token_lock:
            token = self._token
            self._token = None
        if not token:
            return
        try:
            self._private_request("DELETE", "/v1/ws-auth", {"token": token})
            print("[PrivateWebSocketManager] ws-auth token を削除しました。")
        except Exception as exc:
            print(f"[PrivateWebSocketManager] ws-auth token 削除失敗: {exc}")

    def _start_token_renewer(self) -> None:
        self._stop_token_renewer()
        with self._token_lock:
            bound_token = self._token
        if not bound_token:
            return
        self._renew_stop_event.clear()
        self._renew_generation += 1
        generation = self._renew_generation
        self._renew_thread = threading.Thread(
            target=self._renew_loop,
            args=(generation, bound_token),
            daemon=True,
        )
        self._renew_thread.start()

    def _stop_token_renewer(self) -> None:
        self._renew_generation += 1
        self._renew_stop_event.set()
        thread = self._renew_thread
        self._renew_thread = None
        if thread is not None and thread.is_alive():
            # urllib timeout(10s) 中でも確実に待つ
            thread.join(timeout=12.0)
            if thread.is_alive():
                print("[PrivateWebSocketManager] token renewer join timed out")

    def _renew_loop(self, generation: int, bound_token: str) -> None:
        while (
            not self._renew_stop_event.is_set()
            and not self._stop_event.is_set()
            and generation == self._renew_generation
        ):
            if self._renew_stop_event.wait(timeout=_TOKEN_EXTEND_INTERVAL_SEC):
                break
            if generation != self._renew_generation or self._stop_event.is_set():
                break
            with self._token_lock:
                if self._token != bound_token:
                    break
            try:
                self._extend_token(bound_token)
            except Exception as exc:
                print(f"[PrivateWebSocketManager] token 延長失敗: {exc} -> 新規取得で再接続します。")
                with self._token_lock:
                    if self._token == bound_token:
                        self._token = None
                if self._ws_app is not None:
                    self._ws_app.close()
                break

    @staticmethod
    def _is_token_error(error: object) -> bool:
        text = str(error).lower()
        keywords = (
            "401",
            "403",
            "unauthorized",
            "forbidden",
            "invalid token",
            "token",
            "auth",
            "1008",
            "認証",
        )
        return any(k in text for k in keywords)

    def _invalidate_token_if_needed(self, error: object) -> None:
        if not self._is_token_error(error):
            return
        with self._token_lock:
            self._token = None
        print(f"[PrivateWebSocketManager] token を無効化して再取得します。 detail={error}")

    def _private_request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        api_key, api_secret = _resolve_gmo_api_credentials("trade")

        payload_obj = body or {}
        payload = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False)
        timestamp = str(int(time.time() * 1000))

        if method == "POST":
            text = timestamp + method + path + payload
        else:
            text = timestamp + method + path
        sign = hmac.new(api_secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()

        request = urllib.request.Request(
            PRIVATE_API_ENDPOINT + path,
            data=payload.encode("utf-8"),
            headers={
                "API-KEY": api_key,
                "API-TIMESTAMP": timestamp,
                "API-SIGN": sign,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as res:
                raw = res.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} HTTPError {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"{method} {path} request failed: {exc}") from exc

        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{method} {path} JSON decode failed: {raw}") from exc

        status = doc.get("status")
        if status != 0:
            messages = doc.get("messages")
            raise RuntimeError(f"{method} {path} failed: status={status} messages={messages}")
        return doc
