"""
tests/test_threads_poster.py

Threads テキスト自動投稿の認証分岐・dry-run・二重投稿防止・配信非干渉を検証する。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import build_sns_report as sns  # noqa: E402
import threads_poster as tp  # noqa: E402


def test_missing_credentials_are_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # credentials=None だけでは実行環境の実 .env 資格情報にフォールバックしてしまい
    # （post_threads_text 内部で get_threads_credentials() が呼ばれるため）、実運用の
    # THREADS_ACCESS_TOKEN 等が設定された環境では意図せず本番APIを呼んでしまう。
    # 「未設定環境」を確実に再現するため、資格情報取得自体を無条件にNoneへ固定する。
    monkeypatch.setattr(tp, "get_threads_credentials", lambda *a, **k: None)

    def boom(*_a, **_k):
        raise AssertionError("HTTP must not be called when credentials are missing")

    monkeypatch.setattr(tp, "_http_post_form", boom)

    result = tp.post_threads_text(
        "hello",
        "2026-08-05",
        dry_run=False,
        runtime_dir=tmp_path,
        credentials=None,
    )
    assert result.status == "auth_error"
    assert result.is_auth_error
    assert "THREADS_ACCESS_TOKEN" in result.detail


def test_dry_run_does_not_call_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("HTTP must not be called in dry-run")

    monkeypatch.setattr(tp, "_http_post_form", boom)
    creds = tp.ThreadsCredentials(access_token="tok", user_id="uid", username="bot")
    result = tp.post_threads_text(
        "body text",
        "2026-08-05",
        dry_run=True,
        runtime_dir=tmp_path,
        credentials=creds,
    )
    assert result.status == "dry_run"
    assert result.dry_run_payload is not None
    assert result.dry_run_payload["create_fields"]["text"] == "body text"
    assert not tp.already_posted_threads("2026-08-05", runtime_dir=tmp_path)


def test_already_posted_skips(tmp_path: Path) -> None:
    tp.mark_threads_posted("2026-08-05", runtime_dir=tmp_path, post_id="1")
    result = tp.post_threads_text(
        "hello",
        "2026-08-05",
        dry_run=False,
        runtime_dir=tmp_path,
        credentials=tp.ThreadsCredentials("tok", "uid"),
    )
    assert result.status == "skipped_already_posted"


def test_success_marks_flag_and_builds_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: List[Tuple[str, Dict[str, str]]] = []

    def fake_post(url: str, fields: Dict[str, str], **_k):
        calls.append((url, dict(fields)))
        if url.endswith("/threads"):
            return 200, {"id": "container1"}, '{"id":"container1"}'
        if url.endswith("/threads_publish"):
            return 200, {"id": "media99"}, '{"id":"media99"}'
        return 500, {}, ""

    def fake_permalink(media_id: str, access_token: str) -> str:
        assert media_id == "media99"
        return "https://www.threads.net/@bot/post/media99"

    result = tp.post_threads_text(
        "hello threads",
        "2026-08-05",
        dry_run=False,
        runtime_dir=tmp_path,
        credentials=tp.ThreadsCredentials("tok", "uid123", username="bot"),
        http_post_fn=fake_post,
        http_get_permalink_fn=fake_permalink,
    )
    assert result.status == "success"
    assert result.post_id == "media99"
    assert result.post_url.endswith("/post/media99")
    assert tp.already_posted_threads("2026-08-05", runtime_dir=tmp_path)
    marker = tp.threads_posted_marker_path("2026-08-05", runtime_dir=tmp_path)
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["post_id"] == "media99"
    assert len(calls) == 2
    assert calls[0][1]["media_type"] == "TEXT"
    assert calls[0][1]["text"] == "hello threads"
    assert "access_token" not in str(result.dry_run_payload)


def test_http_401_is_auth_error(tmp_path: Path) -> None:
    def fake_post(url: str, fields: Dict[str, str], **_k):
        return 401, {"error": {"code": 190, "message": "Invalid OAuth", "type": "OAuthException"}}, ""

    result = tp.post_threads_text(
        "hello",
        "2026-08-05",
        dry_run=False,
        runtime_dir=tmp_path,
        credentials=tp.ThreadsCredentials("bad", "uid"),
        http_post_fn=fake_post,
    )
    assert result.status == "auth_error"
    assert not tp.already_posted_threads("2026-08-05", runtime_dir=tmp_path)


def test_http_500_is_api_error(tmp_path: Path) -> None:
    def fake_post(url: str, fields: Dict[str, str], **_k):
        return 500, {"error": {"code": 1, "message": "temporary", "type": "Exception"}}, ""

    result = tp.post_threads_text(
        "hello",
        "2026-08-05",
        dry_run=False,
        runtime_dir=tmp_path,
        credentials=tp.ThreadsCredentials("tok", "uid"),
        http_post_fn=fake_post,
    )
    assert result.status == "api_error"


def test_deliver_still_sends_telegram_when_threads_auth_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public = {
        "cumulative_pnl_jpy": 1.0,
        "daily_pnl_jpy": -2.0,
        "trade_count": 1,
        "win_rate_cumulative_pct": 50.0,
        "win_rate_daily_pct": 0.0,
        "circuit_breaker_triggered": False,
        "uptime_days": 1,
        "entry_count": 1,
        "max_drawdown_pct": 0.1,
    }
    alerts: List[str] = []
    sent: List[str] = []

    def fake_threads(*_a, **_k):
        return tp.ThreadsPostResult(status="auth_error", detail="missing token")

    monkeypatch.setattr(
        sns,
        "try_generate_reel_video",
        lambda *a, **k: (None, "no video"),
    )
    monkeypatch.setattr(
        sns,
        "reel_output_path",
        lambda target_date, dry_run=False: tmp_path / "x.mp4",
    )

    rc = sns.deliver_sns_report(
        "2026-08-05",
        public,
        "threads body",
        "ig body",
        dry_run=False,
        send_video_fn=lambda *_a, **_k: False,
        send_text_fn=lambda text: sent.append(text) or True,
        alert_fn=lambda msg: alerts.append(msg),
        threads_post_fn=fake_threads,
    )
    assert rc == 2
    assert any("認証エラー" in a for a in alerts)
    # Instagram fallback text + Threads telegram text
    assert len(sent) >= 2
    assert any("[Threads用テキスト]" in t for t in sent)
    # 失敗時は自動投稿済みラベルを付けない
    assert all("[Threads自動投稿済み]" not in t for t in sent)


def _minimal_public() -> Dict[str, object]:
    return {
        "cumulative_pnl_jpy": 1.0,
        "daily_pnl_jpy": -2.0,
        "trade_count": 1,
        "win_rate_cumulative_pct": 50.0,
        "win_rate_daily_pct": 0.0,
        "circuit_breaker_triggered": False,
        "uptime_days": 1,
        "entry_count": 1,
        "max_drawdown_pct": 0.1,
    }


def _stub_deliver_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        sns,
        "try_generate_reel_video",
        lambda *a, **k: (None, "no video"),
    )
    monkeypatch.setattr(
        sns,
        "reel_output_path",
        lambda target_date, dry_run=False: tmp_path / "x.mp4",
    )


@pytest.mark.parametrize(
    "threads_status,expected_rc",
    [
        ("auth_error", 2),
        ("api_error", 2),
        ("success", 0),
        ("dry_run", 0),
        ("skipped_already_posted", 0),
    ],
)
def test_deliver_exit_code_when_telegram_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    threads_status: str,
    expected_rc: int,
) -> None:
    """Telegram成功時: Threads失敗は2、success/dry_run/skipは0。"""
    _stub_deliver_paths(monkeypatch, tmp_path)

    def fake_threads(*_a, **_k):
        return tp.ThreadsPostResult(
            status=threads_status,
            detail=threads_status,
            post_id="x" if threads_status == "success" else "",
            post_url="https://example.test/p" if threads_status == "success" else "",
        )

    rc = sns.deliver_sns_report(
        "2026-08-05",
        _minimal_public(),
        "threads body",
        "ig body",
        dry_run=False,
        send_text_fn=lambda _t: True,
        alert_fn=lambda _m: None,
        threads_post_fn=fake_threads,
    )
    assert rc == expected_rc


@pytest.mark.parametrize("threads_status", ["success", "auth_error", "api_error"])
def test_deliver_exit_code_1_when_telegram_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    threads_status: str,
) -> None:
    """Telegram配信失敗時は Threads 成否に関わらず常に 1。"""
    _stub_deliver_paths(monkeypatch, tmp_path)

    def fake_threads(*_a, **_k):
        return tp.ThreadsPostResult(status=threads_status, detail=threads_status)

    rc = sns.deliver_sns_report(
        "2026-08-05",
        _minimal_public(),
        "threads body",
        "ig body",
        dry_run=False,
        send_text_fn=lambda _t: False,
        alert_fn=lambda _m: None,
        threads_post_fn=fake_threads,
    )
    assert rc == 1


def test_deliver_appends_url_on_threads_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public = {
        "cumulative_pnl_jpy": 1.0,
        "daily_pnl_jpy": -2.0,
        "trade_count": 1,
        "win_rate_cumulative_pct": 50.0,
        "win_rate_daily_pct": 0.0,
        "circuit_breaker_triggered": False,
        "uptime_days": 1,
        "entry_count": 1,
        "max_drawdown_pct": 0.1,
    }
    sent: List[str] = []

    def fake_threads(*_a, **_k):
        return tp.ThreadsPostResult(
            status="success",
            post_id="abc",
            post_url="https://www.threads.net/@bot/post/abc",
        )

    monkeypatch.setattr(
        sns,
        "try_generate_reel_video",
        lambda *a, **k: (None, "no video"),
    )
    monkeypatch.setattr(
        sns,
        "reel_output_path",
        lambda target_date, dry_run=False: tmp_path / "x.mp4",
    )

    rc = sns.deliver_sns_report(
        "2026-08-05",
        public,
        "threads body",
        "ig body",
        dry_run=False,
        send_text_fn=lambda text: sent.append(text) or True,
        alert_fn=lambda _m: None,
        threads_post_fn=fake_threads,
    )
    assert rc == 0
    threads_msgs = [t for t in sent if "[Threads用テキスト]" in t]
    assert threads_msgs
    assert "[Threads自動投稿済み]" in threads_msgs[0]
    assert "https://www.threads.net/@bot/post/abc" in threads_msgs[0]


def test_deliver_dry_run_sends_confirm_not_production_captions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public = {
        "cumulative_pnl_jpy": 1.0,
        "daily_pnl_jpy": -2.0,
        "trade_count": 1,
        "win_rate_cumulative_pct": 50.0,
        "win_rate_daily_pct": 0.0,
        "circuit_breaker_triggered": False,
        "uptime_days": 1,
        "entry_count": 1,
        "max_drawdown_pct": 0.1,
    }
    sent: List[str] = []

    def fake_threads(text, trading_day, dry_run=False, **_k):
        assert dry_run is True
        return tp.ThreadsPostResult(
            status="dry_run",
            dry_run_payload={
                "create_url": "https://graph.threads.net/v1.0/uid/threads",
                "publish_url": "https://graph.threads.net/v1.0/uid/threads_publish",
                "create_fields": {"text": text, "media_type": "TEXT"},
                "user_id_configured": True,
                "access_token_configured": True,
                "text_length": len(text),
                "text_max": 500,
            },
        )

    monkeypatch.setattr(
        sns,
        "try_generate_reel_video",
        lambda *a, **k: (tmp_path / "v.mp4", None),
    )
    (tmp_path / "v.mp4").write_bytes(b"x")
    monkeypatch.setattr(
        sns,
        "reel_output_path",
        lambda target_date, dry_run=False: tmp_path / "v.mp4",
    )

    rc = sns.deliver_sns_report(
        "2026-08-05",
        public,
        "threads body",
        "ig body",
        dry_run=True,
        send_text_fn=lambda text: sent.append(text) or True,
        send_video_fn=lambda *_a, **_k: sent.append("VIDEO") or True,
        alert_fn=lambda _m: None,
        threads_post_fn=fake_threads,
    )
    assert rc == 0
    assert sent
    assert all("VIDEO" not in s for s in sent)
    assert any("DRY-RUN" in s for s in sent)
    assert all("[Threads用テキスト]" not in s for s in sent)
