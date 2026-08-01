"""
一度きり: 再起動時ポジション未復元バグで欠損した live_state.db の現金を補正する。

会計恒等式:
  FLAT/SHORT: jpy_balance = initial_jpy + cumulative_pnl
  LONG (virtual): jpy_balance = initial_jpy + cumulative_pnl - entry_price * size
  LONG (real):    jpy_balance = initial_jpy + cumulative_pnl
                  （想定元本は差し引かない。3.4.18 の real 会計に合わせる）

trading_mode の解決順:
  1. live_state.db の trading_mode 列
  2. config/config.json の trading_mode
  3. いずれも無い/不正なら virtual

使い方:
  # ドライラン（変更なし）
  python scripts/repair_live_state_equity.py

  # 適用（エンジン停止後に実行すること）
  python scripts/repair_live_state_equity.py --apply

  # real mode かつ LONG 保有中に --apply する場合は追加確認が必要
  python scripts/repair_live_state_equity.py --apply --confirm-real
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
LIVE_STATE_DB_PATH = ROOT_DIR / "runtime" / "live_state.db"
PID_PATH = ROOT_DIR / "runtime" / "trading_engine.pid"
CONFIG_PATH = ROOT_DIR / "config" / "config.json"
INITIAL_JPY = 50_000.0


def _normalize_side(side: Optional[str]) -> Optional[str]:
    if side is None:
        return None
    normalized = str(side).strip().upper()
    if normalized in {"", "NONE", "FLAT", "NULL"}:
        return None
    if normalized in {"LONG", "SHORT"}:
        return normalized
    return None


def _normalize_trading_mode(trading_mode: Optional[str]) -> Optional[str]:
    if trading_mode is None:
        return None
    mode = str(trading_mode).strip().lower()
    if mode in {"", "none", "null"}:
        return None
    if mode in {"virtual", "real"}:
        return mode
    return None


def _load_trading_mode_from_config(config_path: Path) -> Optional[str]:
    if not config_path.exists():
        return None
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return _normalize_trading_mode(payload.get("trading_mode"))


def resolve_trading_mode(
    row: Dict[str, Any],
    *,
    config_path: Path = CONFIG_PATH,
) -> Tuple[str, str]:
    """
    trading_mode と読み取り元ラベルを返す。
    優先: live_state.db -> config.json -> virtual(default)
    """
    from_db = _normalize_trading_mode(row.get("trading_mode"))
    if from_db is not None:
        return from_db, "live_state.db"
    from_config = _load_trading_mode_from_config(config_path)
    if from_config is not None:
        return from_config, str(config_path)
    return "virtual", "default"


def _expected_jpy(
    *,
    initial_jpy: float,
    cumulative_pnl: float,
    position_side: Optional[str],
    position_entry_price: float,
    position_size: float,
    trading_mode: str = "virtual",
) -> float:
    flat = float(initial_jpy) + float(cumulative_pnl)
    side = _normalize_side(position_side)
    mode = _normalize_trading_mode(trading_mode) or "virtual"
    if (
        side == "LONG"
        and position_entry_price > 0
        and position_size > 0
        and mode != "real"
    ):
        return flat - (position_entry_price * position_size)
    return flat


def _engine_running() -> bool:
    if not PID_PATH.exists():
        return False
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import os

        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _load_row(db_path: Path) -> Dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"live_state.db not found: {db_path}")
    with sqlite3.connect(db_path, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM live_state WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError("live_state row(id=1) is missing")
    return dict(row)


def _apply_jpy(db_path: Path, new_jpy: float) -> None:
    with sqlite3.connect(db_path, timeout=5) as conn:
        conn.execute(
            """
            UPDATE live_state
            SET jpy_balance = ?, updated_at = ?
            WHERE id = 1
            """,
            (float(new_jpy), datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()


def repair(
    *,
    db_path: Path = LIVE_STATE_DB_PATH,
    initial_jpy: float = INITIAL_JPY,
    apply: bool = False,
    confirm_real: bool = False,
    config_path: Path = CONFIG_PATH,
) -> Dict[str, Any]:
    row = _load_row(db_path)
    side = _normalize_side(row.get("position_side"))
    entry = float(row.get("position_entry_price") or 0.0)
    size = float(row.get("position_size") or 0.0)
    cumulative = float(row.get("cumulative_pnl") or 0.0)
    before = float(row.get("jpy_balance") or 0.0)
    trading_mode, trading_mode_source = resolve_trading_mode(
        row, config_path=config_path
    )
    formula = (
        "real (LONG does not subtract notional)"
        if trading_mode == "real"
        else "virtual (LONG subtracts notional)"
    )
    expected = _expected_jpy(
        initial_jpy=initial_jpy,
        cumulative_pnl=cumulative,
        position_side=side,
        position_entry_price=entry,
        position_size=size,
        trading_mode=trading_mode,
    )
    delta = expected - before
    real_long = trading_mode == "real" and side == "LONG" and entry > 0 and size > 0
    result: Dict[str, Any] = {
        "db_path": str(db_path),
        "position_side": side,
        "position_entry_price": entry,
        "position_size": size,
        "cumulative_pnl": cumulative,
        "initial_jpy": initial_jpy,
        "trading_mode": trading_mode,
        "trading_mode_source": trading_mode_source,
        "formula": formula,
        "jpy_before": before,
        "jpy_after": expected,
        "delta_jpy": delta,
        "applied": False,
        "backup_path": None,
    }

    if real_long:
        print(
            "[WARN] trading_mode=real with open LONG position."
            " Expected cash does NOT subtract notional (3.4.18)."
            " Use --confirm-real with --apply to proceed."
        )

    if abs(delta) < 0.01:
        print("[OK] no repair needed: jpy already matches accounting identity")
        print(
            f"  trading_mode={trading_mode} source={trading_mode_source}"
            f" formula={formula}"
        )
        print(
            f"  jpy={before:,.4f} expected={expected:,.4f}"
            f" side={side} cumulative_pnl={cumulative:,.4f}"
        )
        return result

    print("[INFO] live_state equity repair plan")
    print(
        f"  trading_mode={trading_mode} source={trading_mode_source}"
        f" formula={formula}"
    )
    print(f"  position_side={side} entry={entry:,.0f} size={size:.6f}")
    print(f"  cumulative_pnl={cumulative:,.4f}")
    print(f"  jpy_before={before:,.4f}")
    print(f"  jpy_after ={expected:,.4f}")
    print(f"  delta     ={delta:+,.4f}")

    if not apply:
        print("[DRY-RUN] no changes written. Re-run with --apply after stopping the engine.")
        return result

    if real_long and not confirm_real:
        raise RuntimeError(
            "refusing --apply for real mode with open LONG without --confirm-real"
        )

    if _engine_running():
        raise RuntimeError(
            "trading_engine appears to be running (pid file alive). "
            "Stop the engine before --apply, otherwise it will overwrite the repair."
        )

    backup_dir = (
        ROOT_DIR
        / "log"
        / f"repair_backup_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / "live_state.db.before_repair"
    shutil.copy2(db_path, backup_path)
    _apply_jpy(db_path, expected)
    result["applied"] = True
    result["backup_path"] = str(backup_path)
    print(f"[OK] applied. backup={backup_path}")
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Repair live_state.db equity deficit")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write corrected jpy_balance (default: dry-run)",
    )
    parser.add_argument(
        "--confirm-real",
        action="store_true",
        help=(
            "Required with --apply when trading_mode=real and an open LONG exists"
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=LIVE_STATE_DB_PATH,
        help="Path to live_state.db",
    )
    parser.add_argument(
        "--initial-jpy",
        type=float,
        default=INITIAL_JPY,
        help="Initial virtual capital (default: 50000)",
    )
    args = parser.parse_args(argv)
    try:
        repair(
            db_path=args.db,
            initial_jpy=args.initial_jpy,
            apply=args.apply,
            confirm_real=args.confirm_real,
        )
    except Exception as exc:
        print(f"[ERROR] repair failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
