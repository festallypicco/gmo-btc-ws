"""
equity_guard.py
---------------
口座 equity（GMO actualProfitLoss の実測値）基準の止血ルール
（equity ガード）の状態管理。

- 状態は runtime/equity_guard.json に原子的（tmp + rename）に保存する
- 起点（baseline）は最初の equity 観測で1回だけ取得して固定する。
  起点を自動で付け替えることはしない
- equity <= stop_equity（= baseline - drawdown）で停止し、
  新規エントリー遮断に使う。自動では再開しない（再開は clear() の
  明示操作のみ。起点の付け替えは rebaseline() の明示操作のみ）
- 状態ファイルが無い / 壊れている場合は発動しない
  （データなしでは止めない）。壊れたファイルは上書きしない
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

try:
    import fcntl
except ImportError:  # fcntl not available: degrade to in-process lock only
    fcntl = None  # type: ignore[assignment]

def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


class EquityGuard:
    """equity ガードの状態ファイル読み書きと停止判定を担う。"""

    def __init__(
        self,
        state_path: Path,
        drawdown_jpy: float = 5000.0,
    ) -> None:
        self._state_path = Path(state_path)
        self._drawdown_jpy = float(drawdown_jpy)
        self._lock = threading.Lock()
        self._cached_state: Optional[Dict[str, Any]] = None
        self._cache_key: Optional[Tuple[bool, int]] = None

    # ------------------------------------------------------------------ #
    #  読み書き（内部）                                                     #
    # ------------------------------------------------------------------ #

    @contextlib.contextmanager
    def _file_lock(self) -> Iterator[None]:
        """状態ファイルと同ディレクトリの .lock への flock（プロセス間ロック）。

        CLI（別プロセス）の clear / rebaseline が、エンジン側の評価の
        読み替え〜書き戻しの間に挟まって巻き戻るのを防ぐ。
        fcntl が無い、またはロック取得に失敗した場合はロックなしで
        続行する（従来動作への degrade。ガード自体は止めない）。
        """
        if fcntl is None:
            yield
            return
        handle = None
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self._state_path.parent / (
                self._state_path.name + ".lock"
            )
            handle = open(lock_path, "a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            if handle is not None:
                handle.close()
            handle = None
        try:
            yield
        finally:
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                handle.close()

    @contextlib.contextmanager
    def _locked_section(self) -> Iterator[None]:
        """プロセス内ロック＋プロセス間ロックの臨界区間（読み替え〜書き戻し用）。"""
        with self._lock:
            with self._file_lock():
                yield

    def _read_file(self) -> Tuple[str, Optional[Dict[str, Any]]]:
        """(status, state) を返す。status は ok / absent / invalid。"""
        if not self._state_path.exists():
            return "absent", None
        try:
            obj = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return "invalid", None
        if not isinstance(obj, dict):
            return "invalid", None
        if not self._is_schema_valid(obj):
            return "invalid", None
        return "ok", obj

    def _save_state(self, state: Dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_path.parent / (self._state_path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, self._state_path)
        self._cached_state = state
        try:
            self._cache_key = (True, self._state_path.stat().st_mtime_ns)
        except OSError:
            self._cache_key = None

    def _current_state(self) -> Optional[Dict[str, Any]]:
        """mtime が変わっていれば再読込するキャッシュ付き読み取り。"""
        try:
            key: Tuple[bool, int] = (True, self._state_path.stat().st_mtime_ns)
        except OSError:
            key = (False, 0)
        if self._cache_key == key:
            return self._cached_state
        status, state = self._read_file()
        self._cached_state = state if status == "ok" else None
        self._cache_key = key
        return self._cached_state

    # ------------------------------------------------------------------ #
    #  公開 API                                                            #
    # ------------------------------------------------------------------ #

    def state_snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._current_state()
            return dict(state) if state is not None else None

    def is_stopped(self) -> bool:
        with self._lock:
            state = self._current_state()
            return state is not None and state.get("state") == "stopped"

    def ensure_baseline(
        self,
        equity_jpy: float,
        *,
        position_open: bool,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """起点が未取得なら現在 equity で取得する。取得済みなら変更しない。"""
        equity_value = self._coerce_equity(equity_jpy)
        if equity_value is None:
            print(
                "[WARN] [EquityGuard] equity is missing or invalid; "
                "baseline capture skipped"
            )
            return None
        equity_jpy = equity_value
        with self._locked_section():
            status, state = self._read_file()
            if status == "ok":
                self._cached_state = state
                return state
            if status == "invalid":
                print(
                    "[WARN] [EquityGuard] state file is invalid; "
                    f"baseline capture skipped: {self._state_path}"
                )
                return None
            state = self._new_baseline_state(
                equity_jpy, position_open=position_open, now=now, event="armed"
            )
            self._save_state(state)
            return state

    def evaluate(
        self,
        equity_jpy: float,
        *,
        position_open: bool,
        now: Optional[datetime] = None,
    ) -> bool:
        """
        実測 equity で判定する。今回の評価で新たに停止したら True。
        起点が未取得なら、この equity で起点を取得する（停止判定は次回から）。
        """
        equity_value = self._coerce_equity(equity_jpy)
        if equity_value is None:
            print(
                "[WARN] [EquityGuard] equity is missing or invalid; "
                "evaluation skipped"
            )
            return False
        equity_jpy = equity_value
        with self._locked_section():
            status, state = self._read_file()
            if status == "invalid":
                print(
                    "[WARN] [EquityGuard] state file is invalid; "
                    f"evaluation skipped: {self._state_path}"
                )
                return False
            checked_at = _now_iso(now)
            if status == "absent" or state is None:
                state = self._new_baseline_state(
                    equity_jpy, position_open=position_open, now=now, event="armed"
                )
                self._save_state(state)
                return False

            state["last_equity"] = float(equity_jpy)
            state["last_checked_at"] = checked_at
            newly_stopped = False
            stop_equity = state.get("stop_equity")
            if (
                state.get("state") == "armed"
                and isinstance(stop_equity, (int, float))
                and float(equity_jpy) <= float(stop_equity)
            ):
                state["state"] = "stopped"
                state["stopped_at"] = checked_at
                self._append_history(
                    state,
                    {
                        "event": "stopped",
                        "at": checked_at,
                        "equity": float(equity_jpy),
                    },
                )
                newly_stopped = True
            self._save_state(state)
            return newly_stopped

    def clear(self, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """手動再開: 停止中なら armed に戻す。起点は変更しない。"""
        with self._locked_section():
            status, state = self._read_file()
            if status != "ok" or state is None:
                return state
            if state.get("state") != "stopped":
                self._cached_state = state
                return state
            state["state"] = "armed"
            state["stopped_at"] = None
            self._append_history(
                state,
                {
                    "event": "cleared",
                    "at": _now_iso(now),
                    "equity": state.get("last_equity"),
                },
            )
            self._save_state(state)
            return state

    def rebaseline(
        self,
        equity_jpy: float,
        *,
        position_open: bool,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """手動の起点付け替え: 現在 equity を新しい起点とし、armed に戻す。"""
        equity_value = self._coerce_equity(equity_jpy)
        if equity_value is None:
            print(
                "[WARN] [EquityGuard] equity is missing or invalid; "
                "rebaseline skipped"
            )
            return None
        equity_jpy = equity_value
        with self._locked_section():
            status, _state = self._read_file()
            if status == "invalid":
                print(
                    "[WARN] [EquityGuard] state file is invalid; "
                    f"rebaseline skipped: {self._state_path}"
                )
                return None
            event = "rebaselined" if status == "ok" else "armed"
            state = self._new_baseline_state(
                equity_jpy, position_open=position_open, now=now, event=event
            )
            if status == "ok" and isinstance(_state, dict):
                history = _state.get("history")
                if isinstance(history, list):
                    state["history"] = history + state["history"]
            self._save_state(state)
            return state

    # ------------------------------------------------------------------ #
    #  内部                                                                #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_schema_valid(state: Dict[str, Any]) -> bool:
        """状態の必須キーと型を検証する。

        state は armed / stopped のいずれかで、起点（baseline_equity）と
        停止ライン（stop_equity）は数値でなければならない。{} のような
        中身のないファイルを「読めるが無力」なまま放置しないための検証。
        不全な状態は invalid として扱い、警告を出して発動も上書きもしない。
        """
        if state.get("state") not in ("armed", "stopped"):
            return False
        for key in ("baseline_equity", "stop_equity"):
            value = state.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
        return True

    @staticmethod
    def _coerce_equity(equity_jpy: Any) -> Optional[float]:
        """ガード判定に使える equity へ変換する。

        欠落・None・0以下・非数値・NaN は「取得失敗」と同じ扱いとし、
        None を返す（起点も取らない・発動もしない）。
        """
        try:
            value = float(equity_jpy)
        except (TypeError, ValueError):
            return None
        if not value > 0.0:
            return None
        return value

    @staticmethod
    def _append_history(state: Dict[str, Any], entry: Dict[str, Any]) -> None:
        history = state.get("history")
        if not isinstance(history, list):
            history = []
            state["history"] = history
        history.append(entry)

    def _new_baseline_state(
        self,
        equity_jpy: float,
        *,
        position_open: bool,
        now: Optional[datetime],
        event: str,
    ) -> Dict[str, Any]:
        captured_at = _now_iso(now)
        baseline = float(equity_jpy)
        return {
            "baseline_equity": baseline,
            "baseline_captured_at": captured_at,
            "baseline_position_open": bool(position_open),
            "stop_equity": baseline - self._drawdown_jpy,
            "drawdown_jpy": self._drawdown_jpy,
            "state": "armed",
            "stopped_at": None,
            "last_equity": baseline,
            "last_checked_at": captured_at,
            "history": [
                {"event": event, "at": captured_at, "equity": baseline}
            ],
        }


def _default_state_path() -> Path:
    return Path(__file__).resolve().parent.parent / "runtime" / "equity_guard.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="equity ガード（止血ルール）の手動操作"
    )
    parser.add_argument(
        "--state-path",
        default=str(_default_state_path()),
        help="状態ファイルのパス（既定: runtime/equity_guard.json）",
    )
    parser.add_argument(
        "--drawdown-jpy",
        type=float,
        default=5000.0,
        help="停止幅（円）。rebaseline 時の停止ライン計算に使う",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="現在の状態を表示する")
    sub.add_parser("clear", help="停止を手動で解除する（起点は変更しない）")
    sub.add_parser("rebaseline", help="現在の口座 equity で起点を付け替える")
    args = parser.parse_args()

    guard = EquityGuard(
        Path(args.state_path), drawdown_jpy=float(args.drawdown_jpy)
    )

    if args.command == "status":
        state = guard.state_snapshot()
        if state is None:
            print("equity guard: no state (not activated)")
            return 0
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    if args.command == "clear":
        state = guard.clear()
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    if args.command == "rebaseline":
        from virtual_trader import fetch_real_account_state

        real_state = fetch_real_account_state()
        equity = float(real_state["equity_jpy"])
        position_open = float(real_state.get("position_size_btc", 0.0)) != 0.0
        state = guard.rebaseline(equity, position_open=position_open)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
