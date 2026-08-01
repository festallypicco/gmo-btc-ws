"""
tests/test_build_public_report.py

scripts/build_public_report.py の公開レポート集計・許可リスト・整形・ドライランを検証する。
特に「許可リスト以外のキー/非公開キーワードが出力に混入しないこと」を重点的に確認する。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import build_public_report as bpr  # noqa: E402


# --------------------------------------------------------------------------- #
#  Fixtures / helpers                                                         #
# --------------------------------------------------------------------------- #
def _write_live_state(
    db_path: Path,
    cumulative_pnl: float,
    win_count: int,
    loss_count: int,
    daily_realized_pnl: float = 0.0,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cumulative_pnl REAL,
                win_count INTEGER,
                loss_count INTEGER,
                daily_realized_pnl REAL
            )
            """
        )
        conn.execute("DELETE FROM live_state WHERE id = 1")
        conn.execute(
            """
            INSERT INTO live_state (
                id, cumulative_pnl, win_count, loss_count, daily_realized_pnl
            ) VALUES (1, ?, ?, ?, ?)
            """,
            (cumulative_pnl, win_count, loss_count, daily_realized_pnl),
        )
        conn.commit()


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "timestamp,trade_id,side,order_type,reason,price,size,fee,pnl\n"
    lines = [header]
    for row in rows:
        lines.append(
            "{timestamp},t1,SELL,MAKER,{reason},10000000,0.01,0,{pnl}\n".format(
                timestamp=row["timestamp"],
                reason=row["reason"],
                pnl=row.get("pnl", "0"),
            )
        )
    path.write_text("".join(lines), encoding="utf-8")


def _write_history(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
#  Trading-day helpers                                                        #
# --------------------------------------------------------------------------- #
def test_target_completed_trading_day_after_rollover() -> None:
    # 06:10 実行 -> 完了済みサイクルは前日
    assert bpr.target_completed_trading_day(datetime(2026, 7, 22, 6, 10, 0)) == "2026-07-21"
    # 05:59 は取引日ラベルが前日なので完了済みは前々日
    assert bpr.target_completed_trading_day(datetime(2026, 7, 22, 5, 59, 0)) == "2026-07-20"


# --------------------------------------------------------------------------- #
#  CSV aggregation                                                            #
# --------------------------------------------------------------------------- #
def test_count_daily_trades_window_and_reasons(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    _write_csv(
        log_dir / "realtime_trading_log_2026-07-21.csv",
        [
            {"timestamp": "2026-07-21 05:59:59", "reason": "ENTRY"},          # 範囲外
            {"timestamp": "2026-07-21 06:00:00", "reason": "ENTRY"},          # 対象
            {"timestamp": "2026-07-21 10:00:00", "reason": "TAKE_PROFIT", "pnl": "100"},
            {"timestamp": "2026-07-21 11:00:00", "reason": "STOP_LOSS", "pnl": "-50"},
            {"timestamp": "2026-07-21 12:00:00", "reason": "CANCEL_ORDER"},   # 決済でもエントリーでもない
        ],
    )
    _write_csv(
        log_dir / "realtime_trading_log_2026-07-22.csv",
        [
            {"timestamp": "2026-07-22 05:00:00", "reason": "ENTRY"},          # 対象（翌日06:00前）
            {"timestamp": "2026-07-22 06:00:00", "reason": "ENTRY"},          # 範囲外
        ],
    )
    settlements, wins, entries = bpr.count_daily_trades("2026-07-21", log_dir=log_dir)
    assert settlements == 2
    assert wins == 1
    assert entries == 2


# --------------------------------------------------------------------------- #
#  History-based metrics                                                       #
# --------------------------------------------------------------------------- #
def test_max_drawdown_and_operating_days(tmp_path: Path) -> None:
    history = tmp_path / "log" / "daily_history.jsonl"
    _write_history(
        history,
        [
            {"trading_day": "2026-07-20", "realized_pnl": 1000.0},
            {"trading_day": "2026-07-21", "realized_pnl": -2000.0},
            {"trading_day": "2026-07-22", "realized_pnl": 500.0},  # target より後
        ],
    )
    # target=07-21 まで: equity 50000 -> 51000(peak) -> 49000, dd=(51000-49000)/51000
    dd = bpr.max_drawdown_pct(history, target_date="2026-07-21", initial_jpy=50_000.0)
    assert dd == pytest.approx((2000.0 / 51000.0) * 100.0, abs=1e-6)
    assert bpr.operating_days(history, target_date="2026-07-21") == 2


def test_operating_days_defaults_to_one_when_missing(tmp_path: Path) -> None:
    missing = tmp_path / "log" / "daily_history.jsonl"
    assert bpr.operating_days(missing, target_date="2026-07-21") == 1
    assert bpr.max_drawdown_pct(missing, target_date="2026-07-21") == 0.0


# --------------------------------------------------------------------------- #
#  Circuit breaker                                                            #
# --------------------------------------------------------------------------- #
def test_circuit_breaker_triggered_only_bool(tmp_path: Path) -> None:
    reason_path = tmp_path / "runtime" / "manual_stop_reason.json"
    reason_path.parent.mkdir(parents=True, exist_ok=True)
    reason_path.write_text(
        json.dumps(
            {
                "reason": "daily_loss_limit",
                "details": {"limit_jpy": 5000, "daily_loss_limit_pct": 0.1},
                "triggered_at": "2026-07-21T14:00:00",
            }
        ),
        encoding="utf-8",
    )
    assert bpr.circuit_breaker_triggered("2026-07-21", reason_path=reason_path) is True
    assert bpr.circuit_breaker_triggered("2026-07-20", reason_path=reason_path) is False


def test_circuit_breaker_missing_file_is_false(tmp_path: Path) -> None:
    assert bpr.circuit_breaker_triggered(
        "2026-07-21", reason_path=tmp_path / "none.json"
    ) is False


# --------------------------------------------------------------------------- #
#  Allow-list enforcement                                                     #
# --------------------------------------------------------------------------- #
def test_collect_public_metrics_only_allowed_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime" / "live_state.db"
    log_dir = tmp_path / "log"
    history = log_dir / "daily_history.jsonl"
    reason_path = tmp_path / "runtime" / "manual_stop_reason.json"

    # live_state は 06:00 リセット後（daily_realized_pnl=0）でも history を優先する
    _write_live_state(
        db_path,
        cumulative_pnl=1500.0,
        win_count=40,
        loss_count=20,
        daily_realized_pnl=0.0,
    )
    _write_csv(
        log_dir / "realtime_trading_log_2026-07-21.csv",
        [
            {"timestamp": "2026-07-21 10:00:00", "reason": "ENTRY"},
            {"timestamp": "2026-07-21 10:05:00", "reason": "TAKE_PROFIT", "pnl": "100"},
            {"timestamp": "2026-07-21 11:00:00", "reason": "STOP_LOSS", "pnl": "-50"},
        ],
    )
    _write_history(
        history,
        [
            {
                "trading_day": "2026-07-20",
                "realized_pnl": 100.0,
                "total_assets_eod": 50_000.0,
            },
            {"trading_day": "2026-07-21", "realized_pnl": -1200.0},
        ],
    )

    target, public = bpr.collect_public_metrics(
        now=datetime(2026, 7, 22, 6, 10, 0),
        db_path=db_path,
        log_dir=log_dir,
        history_path=history,
        reason_path=reason_path,
    )
    assert target == "2026-07-21"
    assert set(public.keys()) == bpr.ALLOWED_KEYS
    assert public["cumulative_pnl_jpy"] == 1500.0
    assert public["daily_pnl_jpy"] == -1200.0
    assert public["trade_count"] == 2
    assert public["win_rate_cumulative_pct"] == pytest.approx(66.7)
    assert public["win_rate_daily_pct"] == pytest.approx(50.0)
    assert "daily_change_pct" not in public


def test_daily_pnl_from_history_even_when_live_state_reset(tmp_path: Path) -> None:
    """06:00 リセット後でも daily_history の realized_pnl を当日損益に使う。"""
    db_path = tmp_path / "runtime" / "live_state.db"
    log_dir = tmp_path / "log"
    history = log_dir / "daily_history.jsonl"
    reason_path = tmp_path / "runtime" / "manual_stop_reason.json"

    _write_live_state(
        db_path,
        cumulative_pnl=-30.0,
        win_count=10,
        loss_count=8,
        daily_realized_pnl=0.0,  # リセット後
    )
    _write_history(
        history,
        [{"trading_day": "2026-07-21", "realized_pnl": -73.88281159998473}],
    )

    _target, public = bpr.collect_public_metrics(
        now=datetime(2026, 7, 22, 6, 10, 0),
        db_path=db_path,
        log_dir=log_dir,
        history_path=history,
        reason_path=reason_path,
    )
    assert public["daily_pnl_jpy"] == -74.0  # round(-73.88)
    assert "daily_change_pct" not in public


def test_missing_daily_history_raises(tmp_path: Path) -> None:
    """対象日の history 行が無い場合は RuntimeError（投稿中断）。"""
    history = tmp_path / "log" / "daily_history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no realized_pnl"):
        bpr.load_daily_pnl_from_history("2026-07-21", history_path=history)

    db_path = tmp_path / "runtime" / "live_state.db"
    _write_live_state(db_path, cumulative_pnl=0.0, win_count=0, loss_count=0)
    with pytest.raises(RuntimeError, match="no realized_pnl"):
        bpr.collect_public_metrics(
            now=datetime(2026, 7, 22, 6, 10, 0),
            db_path=db_path,
            log_dir=tmp_path / "log",
            history_path=history,
            reason_path=tmp_path / "runtime" / "none.json",
        )


def test_missing_daily_history_aborts_main_with_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """history 欠落時、main は投稿せず Telegram 通知して非ゼロ終了する。"""
    alerts: list[str] = []

    def fake_collect(*_args, **_kwargs):
        raise RuntimeError(
            "daily_history.jsonl has no realized_pnl for trading_day=2026-07-21"
        )

    def fake_send(text: str) -> bool:
        alerts.append(text)
        return True

    monkeypatch.setattr(bpr, "collect_public_metrics", fake_collect)
    monkeypatch.setattr(bpr, "send_telegram_message", fake_send)

    exit_code = bpr.main([])
    assert exit_code == 1
    assert len(alerts) == 1
    assert "no realized_pnl" in alerts[0]


def test_assert_only_allowed_keys_rejects_extra() -> None:
    with pytest.raises(ValueError):
        bpr.assert_only_allowed_keys({"cumulative_pnl_jpy": 1.0, "maker_price_offset_jpy": 5})


def test_allowed_keys_exclude_equity_revealing_change_pct() -> None:
    """絶対額変化と総資産比率の組み合わせを許可しない（再発防止）。"""
    assert "daily_change_pct" not in bpr.ALLOWED_KEYS
    assert "cumulative_return_pct" not in bpr.ALLOWED_KEYS


def _sample_public(**overrides: object) -> dict:
    base = {
        "cumulative_pnl_jpy": 12345.0,
        "daily_pnl_jpy": -1200.0,
        "trade_count": 63,
        "win_rate_cumulative_pct": 58.8,
        "win_rate_daily_pct": 54.0,
        "circuit_breaker_triggered": True,
        "uptime_days": 30,
        "entry_count": 70,
        "max_drawdown_pct": 4.18,
    }
    base.update(overrides)
    return base


def test_report_text_has_no_non_public_keywords() -> None:
    public = _sample_public()
    text = bpr.format_report_text("2026-07-21", public)

    forbidden = [
        "imbalance",
        "offset",
        "cooldown",
        "threshold",
        "profile",
        "config",
        "spread",
        "maker",
        "taker",
        "size",
        "limit_jpy",
        "loss_limit",
        "stop_loss",
        "take_profit",
        "cumulative_return",
        "当日増減率",
    ]
    lowered = text.lower()
    for word in forbidden:
        assert word not in lowered, f"non-public keyword leaked: {word}"

    assert "累積損益: +12,345円" in text
    assert "当日損益: -1,200円" in text
    assert "累積勝率: 58.8%" in text
    assert "当日勝率: 54.0%" in text
    assert "発動あり" in text


def test_example_reports_are_three_and_clean() -> None:
    examples = bpr.build_example_reports()
    assert len(examples) == 3
    labels = {label for label, _text in examples}
    assert labels == {"通常時", "サーキットブレーカー発動時", "初日"}
    by_label = dict(examples)
    assert "当日増減率" not in by_label["通常時"]
    assert "累積勝率" in by_label["通常時"]
    assert "当日勝率" in by_label["通常時"]
    assert "発動あり" in by_label["サーキットブレーカー発動時"]
    for _label, text in examples:
        assert "config" not in text.lower()
        assert "offset" not in text.lower()
        assert "累積収益率" not in text
        assert "当日増減率" not in text


# --------------------------------------------------------------------------- #
#  X posting                                                                  #
# --------------------------------------------------------------------------- #
def test_post_to_x_dry_run_returns_true_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]:
        monkeypatch.delenv(name, raising=False)
    assert bpr.post_to_x("hello", dry_run=True) is True


def test_post_to_x_missing_credentials_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bpr, "ENV_PATH", tmp_path / "nonexistent.env")
    assert bpr.post_to_x("hello", dry_run=False) is False


def test_get_x_credentials_reads_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bpr, "ENV_PATH", tmp_path / "nonexistent.env")
    monkeypatch.setenv("X_API_KEY", "k")
    monkeypatch.setenv("X_API_SECRET", "s")
    monkeypatch.setenv("X_ACCESS_TOKEN", "t")
    monkeypatch.setenv("X_ACCESS_TOKEN_SECRET", "ts")
    creds = bpr.get_x_credentials()
    assert creds == {
        "X_API_KEY": "k",
        "X_API_SECRET": "s",
        "X_ACCESS_TOKEN": "t",
        "X_ACCESS_TOKEN_SECRET": "ts",
    }


# --------------------------------------------------------------------------- #
#  Duplicate post guard                                                       #
# --------------------------------------------------------------------------- #
def test_mark_and_already_posted(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    assert bpr.already_posted("2026-07-21", runtime_dir=runtime) is False
    bpr.mark_posted("2026-07-21", runtime_dir=runtime)
    assert bpr.already_posted("2026-07-21", runtime_dir=runtime) is True
    marker = bpr.posted_marker_path("2026-07-21", runtime_dir=runtime)
    assert marker.exists()
    assert marker.name == "public_report_posted_2026-07-21.flag"


def _allowed_public_stub() -> dict:
    return {
        "cumulative_pnl_jpy": 1.0,
        "daily_pnl_jpy": 1.0,
        "trade_count": 0,
        "win_rate_cumulative_pct": 0.0,
        "win_rate_daily_pct": 0.0,
        "circuit_breaker_triggered": False,
        "uptime_days": 1,
        "entry_count": 0,
        "max_drawdown_pct": 0.0,
    }


def test_main_skips_when_already_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    bpr.mark_posted("2026-07-21", runtime_dir=runtime)
    post_calls: list[object] = []

    monkeypatch.setattr(bpr, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(
        bpr,
        "collect_public_metrics",
        lambda: ("2026-07-21", _allowed_public_stub()),
    )
    monkeypatch.setattr(
        bpr,
        "post_to_x",
        lambda text, dry_run=False: post_calls.append(text) or True,
    )

    assert bpr.main([]) == 0
    assert post_calls == []


def test_main_writes_marker_after_successful_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(bpr, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(
        bpr,
        "collect_public_metrics",
        lambda: ("2026-07-21", _allowed_public_stub()),
    )
    monkeypatch.setattr(bpr, "post_to_x", lambda text, dry_run=False: True)

    assert bpr.main([]) == 0
    assert bpr.already_posted("2026-07-21", runtime_dir=runtime) is True


def test_main_dry_run_does_not_write_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(bpr, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(
        bpr,
        "collect_public_metrics",
        lambda: ("2026-07-21", _allowed_public_stub()),
    )
    monkeypatch.setattr(bpr, "post_to_x", lambda text, dry_run=False: True)

    assert bpr.main(["--dry-run"]) == 0
    assert bpr.already_posted("2026-07-21", runtime_dir=runtime) is False
