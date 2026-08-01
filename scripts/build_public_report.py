"""
Public daily report generator and X (Twitter) auto-poster (Phase 1: text only).

This module is intentionally INDEPENDENT from the internal report code
(build_daily_report.py) and the AI pipeline. It never imports internal
aggregation logic, so no non-public judgement criteria or tuning parameters
can leak into the published text.

It pulls only the specific numbers it needs from public-safe data sources:
  - runtime/live_state.db          : cumulative_pnl / win / loss counts
  - log/realtime_trading_log_*.csv : per trading-day settlement / entry counts
  - log/daily_history.jsonl        : target-day realized_pnl (and equity curve for drawdown)
  - runtime/manual_stop_reason.json: circuit breaker trigger flag (bool only)

Only the whitelisted metrics below are ever written to the report text or
the log. All intermediate values are repacked into an allow-listed dict
(collect_public_metrics) before being formatted.

Privacy note (do NOT reverse this):
  Never allow-list a pair of "absolute yen change" and "percent of total assets"
  (e.g. daily_pnl_jpy + daily_change_pct). Together they reveal absolute equity.
  The same rationale removed cumulative_return_pct earlier.

Task Scheduler registration (Windows), run AFTER the internal report (05:55):

  schtasks /Create /TN "BTC_Public_Report_Post" /SC DAILY /ST 06:10 ^
    /TR "powershell.exe -ExecutionPolicy Bypass -File \"C:\\Users\\tai_m\\Cursor\\Projects\\gmo-btc-ws\\scripts\\run_public_report.ps1\"" ^
    /RL LIMITED /F

Usage:
  python build_public_report.py --dry-run     # print text only, do not post
  python build_public_report.py               # collect + post to X
  python build_public_report.py --examples     # print 3 sample texts for review
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from telegram_notifier import send_telegram_message

ROOT_DIR = Path(__file__).resolve().parent.parent
LIVE_STATE_DB_PATH = ROOT_DIR / "runtime" / "live_state.db"
MANUAL_STOP_REASON_PATH = ROOT_DIR / "runtime" / "manual_stop_reason.json"
RUNTIME_DIR = ROOT_DIR / "runtime"
LOG_DIR = ROOT_DIR / "log"
DAILY_HISTORY_PATH = LOG_DIR / "daily_history.jsonl"
ENV_PATH = ROOT_DIR / ".env"

# 運用開始時点の資産（reset_trading_state.py / trading_engine.py と同じ既定値）。
INITIAL_JPY = 50_000.0
TRADING_DAY_ROLLOVER_HOUR = 6

SETTLEMENT_REASONS: Set[str] = {
    "TAKE_PROFIT",
    "STOP_LOSS",
    "FORCE_CLOSE_MAINTENANCE",
}
ENTRY_REASON = "ENTRY"

# これ以外のキーはレポート文字列・ログに一切出さない（厳守）。
# 注意: 「絶対額の変化」(例: daily_pnl_jpy) と「総資産に対する比率」
# (例: daily_change_pct) を同時に許可リストへ追加しないこと。
# 組み合わせると総資産の絶対額を逆算でき、累積収益率%を廃した方針に反する。
ALLOWED_KEYS: Set[str] = {
    "cumulative_pnl_jpy",
    "daily_pnl_jpy",
    "trade_count",
    "win_rate_cumulative_pct",
    "win_rate_daily_pct",
    "circuit_breaker_triggered",
    "uptime_days",
    "entry_count",
    "max_drawdown_pct",
}

X_MAX_TWEET_LEN = 280
POST_MAX_RETRIES = 2
POST_RETRY_INTERVAL_SEC = 30

LOGGER = logging.getLogger("public_report")


def _setup_logging(run_day: Optional[date] = None) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    day = run_day or date.today()
    log_path = LOG_DIR / f"public_report_{day.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [public_report] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    return log_path


# --------------------------------------------------------------------------- #
#  Trading-day helpers (local reimplementation; no internal imports)          #
# --------------------------------------------------------------------------- #
def _trading_day_label(dt: datetime) -> date:
    """06:00 起点の取引日ラベル。06:00 より前は前暦日に属する。"""
    d = dt.date()
    if dt.hour < TRADING_DAY_ROLLOVER_HOUR:
        d = d - timedelta(days=1)
    return d


def target_completed_trading_day(now: Optional[datetime] = None) -> str:
    """
    直近で完了した取引日（06:00 に締まったサイクル）の日付ラベルを返す。
    06:10 実行を想定: 実行時点の取引日ラベルの1日前が完了済みサイクル。
    """
    now = now or datetime.now()
    completed = _trading_day_label(now) - timedelta(days=1)
    return completed.isoformat()


def _parse_timestamp(raw: str) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
#  Individual data-source readers                                             #
# --------------------------------------------------------------------------- #
def _csv_paths_for_trading_day(target_date: str, log_dir: Path) -> List[Path]:
    day = date.fromisoformat(target_date)
    next_day = day + timedelta(days=1)
    return [
        log_dir / f"realtime_trading_log_{day.isoformat()}.csv",
        log_dir / f"realtime_trading_log_{next_day.isoformat()}.csv",
    ]


def count_daily_trades(
    target_date: str,
    log_dir: Path = LOG_DIR,
) -> Tuple[int, int, int]:
    """
    対象取引日 [target 06:00, 翌 06:00) の (決済件数, 勝ち件数, エントリー件数)。
    CSV から独立集計する（内部レポートの集計関数は使わない）。
    """
    day = date.fromisoformat(target_date)
    window_start = datetime(day.year, day.month, day.day, TRADING_DAY_ROLLOVER_HOUR, 0, 0)
    window_end = window_start + timedelta(days=1)

    settlements = 0
    wins = 0
    entries = 0
    for csv_path in _csv_paths_for_trading_day(target_date, log_dir):
        if not csv_path.exists():
            continue
        with csv_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = _parse_timestamp(str(row.get("timestamp") or ""))
                if ts is None or not (window_start <= ts < window_end):
                    continue
                reason = str(row.get("reason") or "").strip()
                if reason == ENTRY_REASON:
                    entries += 1
                elif reason in SETTLEMENT_REASONS:
                    settlements += 1
                    try:
                        pnl = float(row.get("pnl") or 0.0)
                    except (TypeError, ValueError):
                        pnl = 0.0
                    if pnl > 0:
                        wins += 1
    return settlements, wins, entries


def load_cumulative_counters(
    db_path: Path = LIVE_STATE_DB_PATH,
) -> Tuple[Optional[float], int, int]:
    """live_state.db から生涯累積の (cumulative_pnl, win_count, loss_count) を返す。"""
    if not db_path.exists():
        return None, 0, 0
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT cumulative_pnl, win_count, loss_count FROM live_state WHERE id = 1"
            ).fetchone()
    except Exception as exc:
        LOGGER.warning("live_state.db read failed: %s", exc)
        return None, 0, 0
    if row is None:
        return None, 0, 0
    pnl = row["cumulative_pnl"]
    return (
        float(pnl) if pnl is not None else None,
        int(row["win_count"] or 0),
        int(row["loss_count"] or 0),
    )


def load_daily_pnl_from_history(
    target_date: str,
    history_path: Path = DAILY_HISTORY_PATH,
) -> float:
    """
    daily_history.jsonl の対象取引日 realized_pnl を返す。
    05:55 内部レポートが書き込んだスナップショット（06:00 リセット後も残る）。
    該当行が無い場合は RuntimeError（投稿中断・Telegram 通知）。
    """
    rows = _load_history_rows(history_path, target_date=target_date)
    for row in reversed(rows):
        if str(row.get("trading_day") or "") != target_date:
            continue
        raw = row.get("realized_pnl")
        if raw is None:
            break
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"daily_history realized_pnl invalid for {target_date}: {raw!r}"
            ) from exc
    raise RuntimeError(
        f"daily_history.jsonl has no realized_pnl for trading_day={target_date}"
    )


def _load_history_rows(
    history_path: Path = DAILY_HISTORY_PATH,
    target_date: Optional[str] = None,
) -> List[Dict[str, object]]:
    """daily_history.jsonl を取引日昇順で読み込む（target_date 以前のみ）。"""
    if not history_path.exists():
        return []
    rows: List[Dict[str, object]] = []
    try:
        for line in history_path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            day = str(obj.get("trading_day") or "").strip()
            if not day:
                continue
            if target_date is not None and day > target_date:
                continue
            rows.append(obj)
    except Exception as exc:
        LOGGER.warning("daily_history.jsonl read failed: %s", exc)
        return []
    rows.sort(key=lambda r: str(r.get("trading_day") or ""))
    return rows


def operating_days(
    history_path: Path = DAILY_HISTORY_PATH,
    target_date: Optional[str] = None,
) -> int:
    """稼働継続日数 = daily_history.jsonl に記録された取引日数（target 以前）。"""
    rows = _load_history_rows(history_path, target_date=target_date)
    distinct = {str(r.get("trading_day")) for r in rows}
    if not distinct:
        # 履歴が無い（初日等）でも、完了した target 日が最低1日は存在する。
        return 1
    return len(distinct)


def max_drawdown_pct(
    history_path: Path = DAILY_HISTORY_PATH,
    target_date: Optional[str] = None,
    initial_jpy: float = INITIAL_JPY,
) -> float:
    """
    運用開始からの最大ドローダウン（%）。
    equity = initial_jpy + 日次 realized_pnl の累積、で作った資産曲線から算出。
    """
    rows = _load_history_rows(history_path, target_date=target_date)
    equity = float(initial_jpy)
    peak = equity
    max_dd = 0.0
    for r in rows:
        try:
            equity += float(r.get("realized_pnl") or 0.0)
        except (TypeError, ValueError):
            continue
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd * 100.0


def circuit_breaker_triggered(
    target_date: str,
    reason_path: Path = MANUAL_STOP_REASON_PATH,
) -> bool:
    """
    対象取引日 [target 06:00, 翌 06:00) にサーキットブレーカーが発動したか（真偽値のみ）。
    発動理由・しきい値等の詳細は一切読み取らない。
    """
    if not reason_path.exists():
        return False
    try:
        obj = json.loads(reason_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    ts = _parse_timestamp(str(obj.get("triggered_at") or ""))
    if ts is None:
        return False
    return _trading_day_label(ts).isoformat() == target_date


# --------------------------------------------------------------------------- #
#  Metric assembly (repack into allow-listed dict)                            #
# --------------------------------------------------------------------------- #
def collect_public_metrics(
    now: Optional[datetime] = None,
    db_path: Path = LIVE_STATE_DB_PATH,
    log_dir: Path = LOG_DIR,
    history_path: Path = DAILY_HISTORY_PATH,
    reason_path: Path = MANUAL_STOP_REASON_PATH,
    initial_jpy: float = INITIAL_JPY,
) -> Tuple[str, Dict[str, object]]:
    """
    公開可能な数値だけを集めて (target_date, public_dict) を返す。
    非公開値は本関数内の一時変数に留め、返す public_dict には ALLOWED_KEYS のみを詰める。
    """
    target_date = target_completed_trading_day(now)

    settlements, daily_wins, entries = count_daily_trades(target_date, log_dir=log_dir)
    cumulative_pnl, win_count, loss_count = load_cumulative_counters(db_path=db_path)

    if cumulative_pnl is None:
        # フォールバック: 履歴の realized_pnl 累積から復元。
        rows = _load_history_rows(history_path, target_date=target_date)
        cumulative_pnl = sum(float(r.get("realized_pnl") or 0.0) for r in rows)

    # 当日損益は 05:55 スナップショット（history）を唯一のソースとする。
    daily_pnl = load_daily_pnl_from_history(target_date, history_path=history_path)

    cumulative_total = win_count + loss_count
    win_rate_cumulative = (win_count / cumulative_total * 100.0) if cumulative_total else 0.0
    win_rate_daily = (daily_wins / settlements * 100.0) if settlements else 0.0

    # ---- 出力直前に許可リストのキーだけで詰め替える（厳守事項） ----
    public: Dict[str, object] = {
        "cumulative_pnl_jpy": round(float(cumulative_pnl), 0),
        "daily_pnl_jpy": round(float(daily_pnl), 0),
        "trade_count": int(settlements),
        "win_rate_cumulative_pct": round(win_rate_cumulative, 1),
        "win_rate_daily_pct": round(win_rate_daily, 1),
        "circuit_breaker_triggered": bool(
            circuit_breaker_triggered(target_date, reason_path=reason_path)
        ),
        "uptime_days": int(
            operating_days(history_path, target_date=target_date)
        ),
        "entry_count": int(entries),
        "max_drawdown_pct": round(
            max_drawdown_pct(history_path, target_date=target_date, initial_jpy=initial_jpy),
            2,
        ),
    }
    assert_only_allowed_keys(public)
    return target_date, public


def assert_only_allowed_keys(public: Dict[str, object]) -> None:
    extra = set(public.keys()) - ALLOWED_KEYS
    if extra:
        raise ValueError(f"public report contains disallowed keys: {sorted(extra)}")


def _format_jpy_signed(value: float) -> str:
    rounded = int(round(value))
    sign = "+" if rounded >= 0 else "-"
    return f"{sign}{abs(rounded):,}円"


def format_report_text(target_date: str, public: Dict[str, object]) -> str:
    """許可リスト dict のみからレポート本文を生成する。"""
    assert_only_allowed_keys(public)
    cb = "発動あり" if public["circuit_breaker_triggered"] else "発動なし"
    lines = [
        f"BTC自動売買 日次レポート ({target_date})",
        f"累積損益: {_format_jpy_signed(float(public['cumulative_pnl_jpy']))}",
        f"当日損益: {_format_jpy_signed(float(public['daily_pnl_jpy']))}",
        f"最大ドローダウン: {float(public['max_drawdown_pct']):.2f}%",
        f"稼働継続日数: {int(public['uptime_days'])}日",
        f"当日決済件数: {int(public['trade_count'])}件",
        f"当日エントリー回数: {int(public['entry_count'])}回",
        f"累積勝率: {float(public['win_rate_cumulative_pct']):.1f}%",
        f"当日勝率: {float(public['win_rate_daily_pct']):.1f}%",
        f"サーキットブレーカー: {cb}",
    ]
    return "\n".join(lines)


def build_example_reports() -> List[Tuple[str, str]]:
    """レビュー用のサンプル出力を3パターン返す（通常時 / CB発動時 / 初日）。"""
    normal = {
        "cumulative_pnl_jpy": 18420.0,
        "daily_pnl_jpy": -1200.0,
        "trade_count": 63,
        "win_rate_cumulative_pct": 58.8,
        "win_rate_daily_pct": 54.0,
        "circuit_breaker_triggered": False,
        "uptime_days": 30,
        "entry_count": 104,
        "max_drawdown_pct": 4.2,
    }
    circuit = {
        "cumulative_pnl_jpy": -8750.0,
        "daily_pnl_jpy": -3200.0,
        "trade_count": 41,
        "win_rate_cumulative_pct": 42.1,
        "win_rate_daily_pct": 38.2,
        "circuit_breaker_triggered": True,
        "uptime_days": 12,
        "entry_count": 55,
        "max_drawdown_pct": 12.90,
    }
    first_day = {
        "cumulative_pnl_jpy": 60.0,
        "daily_pnl_jpy": 60.0,
        "trade_count": 8,
        "win_rate_cumulative_pct": 62.5,
        "win_rate_daily_pct": 62.5,
        "circuit_breaker_triggered": False,
        "uptime_days": 1,
        "entry_count": 10,
        "max_drawdown_pct": 0.35,
    }
    return [
        ("通常時", format_report_text("2026-07-21", normal)),
        ("サーキットブレーカー発動時", format_report_text("2026-07-21", circuit)),
        ("初日", format_report_text("2026-07-21", first_day)),
    ]


# --------------------------------------------------------------------------- #
#  X (Twitter) posting                                                        #
# --------------------------------------------------------------------------- #
def _load_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    try:
        raw_text = path.read_text(encoding="utf-8")
    except Exception:
        return values
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            values[key] = value
    return values


def get_x_credentials() -> Optional[Dict[str, str]]:
    """
    X API OAuth 1.0a User Context の認証情報を取得する。
    GMO_API_KEY 等と同様、環境変数を優先し .env をフォールバックとする。
    未設定の項目が1つでもあれば None を返す（呼び出し側で安全終了する）。
    """
    file_env = _load_env_file(ENV_PATH)
    keys = [
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
    ]
    creds: Dict[str, str] = {}
    for name in keys:
        value = (os.environ.get(name) or file_env.get(name, "")).strip()
        if not value:
            return None
        creds[name] = value
    return creds


def post_to_x(
    text: str,
    dry_run: bool = False,
    max_retries: int = POST_MAX_RETRIES,
    retry_interval_sec: int = POST_RETRY_INTERVAL_SEC,
) -> bool:
    """
    X へ1件投稿する。成功時 True。
    - dry_run: API を呼ばず内容をログ出力のみ。
    - 従量課金前提のため呼び出しは最小限。成功レスポンス確認後は再送しない。
    - 送信例外時のみ最大 max_retries までリトライ（多重投稿防止）。
    """
    if dry_run:
        LOGGER.info("DRY-RUN mode. The following text would be posted to X:\n%s", text)
        return True

    creds = get_x_credentials()
    if creds is None:
        LOGGER.error(
            "X API credentials are not configured. "
            "Set X_API_KEY/X_API_SECRET/X_ACCESS_TOKEN/X_ACCESS_TOKEN_SECRET in .env."
        )
        return False

    try:
        import tweepy  # type: ignore
    except ImportError:
        LOGGER.error("tweepy is not installed. Run: pip install tweepy")
        return False

    client = tweepy.Client(
        consumer_key=creds["X_API_KEY"],
        consumer_secret=creds["X_API_SECRET"],
        access_token=creds["X_ACCESS_TOKEN"],
        access_token_secret=creds["X_ACCESS_TOKEN_SECRET"],
    )

    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            response = client.create_tweet(text=text)
            tweet_id = None
            data = getattr(response, "data", None)
            if isinstance(data, dict):
                tweet_id = data.get("id")
            if tweet_id:
                LOGGER.info("X post succeeded. tweet_id=%s", tweet_id)
                return True
            # 成功レスポンスが確認できない場合は再送しない（多重投稿防止）。
            LOGGER.error("X post response missing tweet id; not resending.")
            return False
        except Exception as exc:  # network / auth / rate limit
            last_error = f"{type(exc).__name__}: {exc}"
            LOGGER.warning(
                "X post attempt %d/%d failed: %s",
                attempt + 1,
                max_retries + 1,
                last_error,
            )
            if attempt < max_retries:
                time.sleep(retry_interval_sec)

    LOGGER.error("X post failed after %d attempts: %s", max_retries + 1, last_error)
    return False


def _build_failure_alert(target_date: str, detail: str) -> str:
    return (
        "[ALERT] X public report post failed\n"
        f"target_date={target_date}\n"
        f"detail={detail}"
    )


def posted_marker_path(
    trading_day: str,
    runtime_dir: Optional[Path] = None,
) -> Path:
    base = RUNTIME_DIR if runtime_dir is None else runtime_dir
    return base / f"public_report_posted_{trading_day}.flag"


def already_posted(
    trading_day: str,
    runtime_dir: Optional[Path] = None,
) -> bool:
    """同一取引日の投稿済みマーカーが存在すれば True。"""
    return posted_marker_path(trading_day, runtime_dir=runtime_dir).exists()


def mark_posted(
    trading_day: str,
    runtime_dir: Optional[Path] = None,
) -> None:
    """投稿成功後にマーカーファイルを作成する（重複投稿防止）。"""
    path = posted_marker_path(trading_day, runtime_dir=runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        datetime.now().isoformat(timespec="seconds"),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
#  Entry point                                                                #
# --------------------------------------------------------------------------- #
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Public daily report -> X poster")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect and print the report text without posting to X",
    )
    parser.add_argument(
        "--examples",
        action="store_true",
        help="print 3 sample report texts for review and exit",
    )
    args = parser.parse_args(argv)

    _setup_logging()

    if args.examples:
        for label, text in build_example_reports():
            LOGGER.info("[example:%s]\n%s", label, text)
        return 0

    try:
        target_date, public = collect_public_metrics()
        text = format_report_text(target_date, public)
    except Exception as exc:
        LOGGER.exception("Failed to collect public metrics: %s", exc)
        try:
            send_telegram_message(_build_failure_alert("unknown", str(exc)))
        except Exception:
            pass
        return 1

    LOGGER.info("Public report built:\n%s", text)
    if len(text) > X_MAX_TWEET_LEN:
        LOGGER.warning(
            "Report text length %d exceeds %d; it may be rejected by X.",
            len(text),
            X_MAX_TWEET_LEN,
        )

    if not args.dry_run and already_posted(target_date):
        LOGGER.info(
            "Skip X post: already posted for trading_day=%s (marker exists).",
            target_date,
        )
        return 0

    posted = post_to_x(text, dry_run=args.dry_run)
    if posted:
        if not args.dry_run:
            try:
                mark_posted(target_date)
                LOGGER.info(
                    "Posted marker written: %s",
                    posted_marker_path(target_date),
                )
            except Exception as mark_exc:
                LOGGER.warning("Failed to write posted marker: %s", mark_exc)
        LOGGER.info("Public report run finished successfully (dry_run=%s).", args.dry_run)
        return 0

    LOGGER.error("Public report post failed.")
    try:
        send_telegram_message(
            _build_failure_alert(target_date, "X post failed; see public_report log")
        )
    except Exception as exc:
        LOGGER.warning("Telegram alert failed: %s", exc)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
