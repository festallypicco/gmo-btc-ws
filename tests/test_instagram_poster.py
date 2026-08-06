"""
tests/test_instagram_poster.py

Instagram Reels 動画自動投稿の認証分岐・dry-run・二重投稿防止・
公開用ファイルの後始末・配信非干渉を検証する。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import build_sns_report as sns  # noqa: E402
import instagram_poster as ig  # noqa: E402
import threads_poster as tp  # noqa: E402


def _make_video(tmp_path: Path, name: str = "reel.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x00\x00fake-mp4")
    return path


def test_missing_credentials_are_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # credentials=None だけでは実行環境の実 .env 資格情報にフォールバックしてしまい
    # （post_instagram_reel 内部で get_instagram_credentials() が呼ばれるため）、
    # INSTAGRAM_ACCESS_TOKEN 等が設定された環境では意図せず本番APIを呼んでしまう。
    # 「未設定環境」を確実に再現するため、資格情報取得自体を無条件にNoneへ固定する。
    monkeypatch.setattr(ig, "get_instagram_credentials", lambda *a, **k: None)

    def boom(*_a, **_k):
        raise AssertionError("HTTP must not be called when credentials are missing")

    monkeypatch.setattr(ig, "_http_post_form", boom)
    monkeypatch.setattr(ig, "_http_get_json", boom)

    video = _make_video(tmp_path)
    result = ig.post_instagram_reel(
        video,
        "caption",
        "2026-08-06",
        dry_run=False,
        runtime_dir=tmp_path,
        credentials=None,
    )
    assert result.status == "auth_error"
    assert result.is_auth_error
    assert "INSTAGRAM_ACCESS_TOKEN" in result.detail
    # 認証エラー時点でファイルコピーは発生しない
    assert not (tmp_path / "public_media").exists()


def test_dry_run_does_not_call_http_or_copy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a, **_k):
        raise AssertionError("HTTP must not be called in dry-run")

    monkeypatch.setattr(ig, "_http_post_form", boom)
    video = _make_video(tmp_path)
    public_media_dir = tmp_path / "public_media"
    creds = ig.InstagramCredentials(access_token="tok", user_id="uid")

    result = ig.post_instagram_reel(
        video,
        "caption body",
        "2026-08-06",
        dry_run=True,
        runtime_dir=tmp_path,
        public_media_dir=public_media_dir,
        credentials=creds,
    )
    assert result.status == "dry_run"
    assert result.dry_run_payload is not None
    assert result.dry_run_payload["create_fields"]["caption"] == "caption body"
    assert result.dry_run_payload["create_fields"]["media_type"] == "REELS"
    assert "access_token" not in str(result.dry_run_payload).lower().replace(
        "access_token", ""
    )
    assert not ig.already_posted_instagram("2026-08-06", runtime_dir=tmp_path)
    # dry-runではファイルコピーもディレクトリ作成も行わない
    assert not public_media_dir.exists()


def test_already_posted_skips(tmp_path: Path) -> None:
    ig.mark_instagram_posted("2026-08-06", runtime_dir=tmp_path, media_id="1")
    video = _make_video(tmp_path)
    result = ig.post_instagram_reel(
        video,
        "caption",
        "2026-08-06",
        dry_run=False,
        runtime_dir=tmp_path,
        credentials=ig.InstagramCredentials("tok", "uid"),
    )
    assert result.status == "skipped_already_posted"


def test_success_marks_flag_builds_url_and_removes_public_media_file(
    tmp_path: Path,
) -> None:
    video = _make_video(tmp_path)
    public_media_dir = tmp_path / "public_media"
    calls: List[Tuple[str, Dict[str, str]]] = []

    def fake_post(url: str, fields: Dict[str, str], **_k):
        calls.append((url, dict(fields)))
        if url.endswith("/media"):
            return 200, {"id": "container1"}, '{"id":"container1"}'
        if url.endswith("/media_publish"):
            return 200, {"id": "media99"}, '{"id":"media99"}'
        return 500, {}, ""

    def fake_get(url: str, **_k):
        assert "status_code" in url
        return 200, {"status_code": "FINISHED"}, '{"status_code":"FINISHED"}'

    def fake_permalink(media_id: str, access_token: str, **_k) -> str:
        assert media_id == "media99"
        return "https://www.instagram.com/reel/media99/"

    result = ig.post_instagram_reel(
        video,
        "hello instagram",
        "2026-08-06",
        dry_run=False,
        runtime_dir=tmp_path,
        public_media_dir=public_media_dir,
        credentials=ig.InstagramCredentials("tok", "uid123"),
        http_post_fn=fake_post,
        http_get_fn=fake_get,
        http_get_permalink_fn=fake_permalink,
        token="a" * 40,
    )

    assert result.status == "success"
    assert result.media_id == "media99"
    assert result.post_url == "https://www.instagram.com/reel/media99/"
    assert ig.already_posted_instagram("2026-08-06", runtime_dir=tmp_path)
    marker = ig.instagram_posted_marker_path("2026-08-06", runtime_dir=tmp_path)
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["media_id"] == "media99"

    # create -> publish の2回POSTされ、video_urlはnginxホワイトリスト形式
    assert len(calls) == 2
    assert calls[0][1]["media_type"] == "REELS"
    assert calls[0][1]["video_url"] == "https://m7x2kq9.festallypicco.com/" + "a" * 40 + ".mp4"
    assert calls[0][1]["caption"] == "hello instagram"

    # 投稿成功時は公開用ファイルをその場で削除する
    assert not (public_media_dir / (("a" * 40) + ".mp4")).exists()


def test_public_media_token_matches_nginx_whitelist_pattern(tmp_path: Path) -> None:
    """トークン+拡張子が nginx のホワイトリスト正規表現に合致すること。"""
    pattern = re.compile(r"^[A-Za-z0-9]{32,}\.[A-Za-z0-9]{1,8}$")
    for _ in range(20):
        token = ig.generate_public_media_token()
        filename = f"{token}.mp4"
        assert pattern.match(filename), filename
        assert re.fullmatch(r"[0-9a-f]+", token)
        assert len(token) >= 32


def test_http_401_is_auth_error(tmp_path: Path) -> None:
    video = _make_video(tmp_path)

    def fake_post(url: str, fields: Dict[str, str], **_k):
        return (
            401,
            {"error": {"code": 190, "message": "Invalid OAuth", "type": "OAuthException"}},
            "",
        )

    result = ig.post_instagram_reel(
        video,
        "caption",
        "2026-08-06",
        dry_run=False,
        runtime_dir=tmp_path,
        public_media_dir=tmp_path / "public_media",
        credentials=ig.InstagramCredentials("bad", "uid"),
        http_post_fn=fake_post,
    )
    assert result.status == "auth_error"
    assert not ig.already_posted_instagram("2026-08-06", runtime_dir=tmp_path)


def test_http_500_is_api_error(tmp_path: Path) -> None:
    video = _make_video(tmp_path)

    def fake_post(url: str, fields: Dict[str, str], **_k):
        return 500, {"error": {"code": 1, "message": "temporary", "type": "Exception"}}, ""

    result = ig.post_instagram_reel(
        video,
        "caption",
        "2026-08-06",
        dry_run=False,
        runtime_dir=tmp_path,
        public_media_dir=tmp_path / "public_media",
        credentials=ig.InstagramCredentials("tok", "uid"),
        http_post_fn=fake_post,
    )
    assert result.status == "api_error"


def test_poll_timeout_is_api_error(tmp_path: Path) -> None:
    video = _make_video(tmp_path)
    sleeps: List[float] = []

    def fake_post(url: str, fields: Dict[str, str], **_k):
        return 200, {"id": "container1"}, ""

    def fake_get(url: str, **_k):
        return 200, {"status_code": "IN_PROGRESS"}, ""

    result = ig.post_instagram_reel(
        video,
        "caption",
        "2026-08-06",
        dry_run=False,
        runtime_dir=tmp_path,
        public_media_dir=tmp_path / "public_media",
        credentials=ig.InstagramCredentials("tok", "uid"),
        http_post_fn=fake_post,
        http_get_fn=fake_get,
        sleep_fn=lambda s: sleeps.append(s),
        poll_interval_sec=1,
        poll_max_attempts=3,
    )
    assert result.status == "api_error"
    assert "timed out" in result.detail
    assert len(sleeps) == 2  # 3回試行、最後は待たない
    # 失敗時は公開用ファイルを削除せず残す（2時間自動削除に任せる）
    files = list((tmp_path / "public_media").glob("*.mp4"))
    assert len(files) == 1


def test_poll_error_status_is_api_error(tmp_path: Path) -> None:
    video = _make_video(tmp_path)

    def fake_post(url: str, fields: Dict[str, str], **_k):
        return 200, {"id": "container1"}, ""

    def fake_get(url: str, **_k):
        return 200, {"status_code": "ERROR"}, ""

    result = ig.post_instagram_reel(
        video,
        "caption",
        "2026-08-06",
        dry_run=False,
        runtime_dir=tmp_path,
        public_media_dir=tmp_path / "public_media",
        credentials=ig.InstagramCredentials("tok", "uid"),
        http_post_fn=fake_post,
        http_get_fn=fake_get,
        sleep_fn=lambda _s: None,
    )
    assert result.status == "api_error"
    assert "ERROR" in result.detail


def test_publish_step_failure_leaves_file_for_cleanup(tmp_path: Path) -> None:
    video = _make_video(tmp_path)
    public_media_dir = tmp_path / "public_media"

    def fake_post(url: str, fields: Dict[str, str], **_k):
        if url.endswith("/media"):
            return 200, {"id": "container1"}, ""
        return 500, {"error": {"code": 1, "message": "boom"}}, ""

    def fake_get(url: str, **_k):
        return 200, {"status_code": "FINISHED"}, ""

    result = ig.post_instagram_reel(
        video,
        "caption",
        "2026-08-06",
        dry_run=False,
        runtime_dir=tmp_path,
        public_media_dir=public_media_dir,
        credentials=ig.InstagramCredentials("tok", "uid"),
        http_post_fn=fake_post,
        http_get_fn=fake_get,
        sleep_fn=lambda _s: None,
        token="b" * 40,
    )
    assert result.status == "api_error"
    assert not ig.already_posted_instagram("2026-08-06", runtime_dir=tmp_path)
    assert (public_media_dir / (("b" * 40) + ".mp4")).exists()


def test_video_file_missing_is_file_error(tmp_path: Path) -> None:
    result = ig.post_instagram_reel(
        tmp_path / "does_not_exist.mp4",
        "caption",
        "2026-08-06",
        dry_run=False,
        runtime_dir=tmp_path,
        public_media_dir=tmp_path / "public_media",
        credentials=ig.InstagramCredentials("tok", "uid"),
    )
    assert result.status == "file_error"
    assert result.is_file_error
    assert not result.is_api_error
    assert "video file not found" in result.detail


def test_format_failure_alert_distinguishes_file_error(tmp_path: Path) -> None:
    file_error_result = ig.InstagramPostResult(
        status="file_error", detail="video file not found: x.mp4"
    )
    auth_error_result = ig.InstagramPostResult(status="auth_error", detail="missing token")
    api_error_result = ig.InstagramPostResult(status="api_error", detail="http=500")

    file_alert = ig.format_failure_alert(file_error_result, trading_day="2026-08-06")
    auth_alert = ig.format_failure_alert(auth_error_result, trading_day="2026-08-06")
    api_alert = ig.format_failure_alert(api_error_result, trading_day="2026-08-06")

    assert "ファイルエラー" in file_alert
    assert "認証エラー" not in file_alert
    assert "APIエラー" not in file_alert
    assert "video file not found" in file_alert

    assert "認証エラー" in auth_alert
    assert "APIエラー" in api_alert


# ---------------------------------------------------------------------------
# deliver_sns_report との統合: Threads / Instagram 自動投稿の組み合わせ
# ---------------------------------------------------------------------------


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


def _stub_video_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Instagram自動投稿が実際に呼ばれるよう、動画生成を成功させておく。"""
    video_path = tmp_path / "reel.mp4"
    video_path.write_bytes(b"\x00\x00fake-mp4")
    monkeypatch.setattr(
        sns,
        "try_generate_reel_video",
        lambda *a, **k: (video_path, None),
    )
    monkeypatch.setattr(
        sns,
        "reel_output_path",
        lambda target_date, dry_run=False: video_path,
    )
    return video_path


def _threads_result(status: str) -> tp.ThreadsPostResult:
    return tp.ThreadsPostResult(
        status=status,
        detail=status,
        post_id="t1" if status == "success" else "",
        post_url="https://www.threads.net/@bot/post/t1" if status == "success" else "",
    )


def _instagram_result(status: str) -> ig.InstagramPostResult:
    return ig.InstagramPostResult(
        status=status,
        detail=status,
        media_id="ig1" if status == "success" else "",
        post_url="https://www.instagram.com/reel/ig1/" if status == "success" else "",
    )


@pytest.mark.parametrize(
    "threads_status,instagram_status,expected_rc",
    [
        ("success", "auth_error", 2),  # Threads成功 / Instagram失敗(認証)
        ("success", "file_error", 2),  # Threads成功 / Instagram失敗(ファイル)
        ("api_error", "success", 2),  # Threads失敗 / Instagram成功
        ("auth_error", "api_error", 2),  # 両方失敗
        ("success", "success", 0),  # 両方成功
        ("dry_run", "dry_run", 0),
    ],
)
def test_deliver_exit_code_for_threads_instagram_combinations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    threads_status: str,
    instagram_status: str,
    expected_rc: int,
) -> None:
    """Telegram配信自体は成功する前提で、Threads/Instagram自動投稿の組み合わせごとに終了コードを検証する。"""
    _stub_video_generation(monkeypatch, tmp_path)
    alerts: List[str] = []

    rc = sns.deliver_sns_report(
        "2026-08-06",
        _minimal_public(),
        "threads body",
        "ig body",
        dry_run=False,
        send_video_fn=lambda *_a, **_k: True,
        send_text_fn=lambda _t: True,
        alert_fn=lambda msg: alerts.append(msg),
        threads_post_fn=lambda *a, **k: _threads_result(threads_status),
        instagram_post_fn=lambda *a, **k: _instagram_result(instagram_status),
    )
    assert rc == expected_rc

    threads_failed = threads_status in ("auth_error", "api_error")
    instagram_failed = instagram_status in ("auth_error", "api_error", "file_error")
    if threads_failed:
        assert any("Threads自動投稿失敗" in a for a in alerts)
    if instagram_failed:
        assert any("Instagram Reels自動投稿失敗" in a for a in alerts)
    if not threads_failed and not instagram_failed:
        assert alerts == []


def test_deliver_telegram_failure_takes_priority_over_auto_post_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Telegram配信自体が失敗した場合は、Threads/Instagramの成否に関わらず常に1。"""
    _stub_video_generation(monkeypatch, tmp_path)

    rc = sns.deliver_sns_report(
        "2026-08-06",
        _minimal_public(),
        "threads body",
        "ig body",
        dry_run=False,
        send_video_fn=lambda *_a, **_k: False,
        send_text_fn=lambda _t: False,
        alert_fn=lambda _m: None,
        threads_post_fn=lambda *a, **k: _threads_result("success"),
        instagram_post_fn=lambda *a, **k: _instagram_result("success"),
    )
    assert rc == 1


def test_deliver_appends_instagram_url_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_video_generation(monkeypatch, tmp_path)
    videos: List[Tuple[Any, str]] = []

    rc = sns.deliver_sns_report(
        "2026-08-06",
        _minimal_public(),
        "threads body",
        "ig body",
        dry_run=False,
        send_video_fn=lambda path, caption="": videos.append((path, caption))
        or True,
        send_text_fn=lambda _t: True,
        alert_fn=lambda _m: None,
        threads_post_fn=lambda *a, **k: _threads_result("success"),
        instagram_post_fn=lambda *a, **k: _instagram_result("success"),
    )
    assert rc == 0
    # 動画+キャプションはsend_video経由でTelegramへ送られる（実IG投稿とは別チャンネル）
    ig_msgs = [caption for _path, caption in videos if "[Instagram用キャプション]" in caption]
    assert ig_msgs
    assert "[Instagram自動投稿済み]" in ig_msgs[0]
    assert "https://www.instagram.com/reel/ig1/" in ig_msgs[0]


def test_deliver_skips_instagram_auto_post_when_video_generation_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """動画生成が失敗した場合、Instagram自動投稿はskipped_no_videoとなり失敗扱いしない。"""
    monkeypatch.setattr(
        sns,
        "try_generate_reel_video",
        lambda *a, **k: (None, "boom"),
    )
    monkeypatch.setattr(
        sns,
        "reel_output_path",
        lambda target_date, dry_run=False: tmp_path / "x.mp4",
    )
    calls: List[Any] = []

    rc = sns.deliver_sns_report(
        "2026-08-06",
        _minimal_public(),
        "threads body",
        "ig body",
        dry_run=False,
        send_video_fn=lambda *_a, **_k: True,
        send_text_fn=lambda _t: True,
        alert_fn=lambda _m: None,
        threads_post_fn=lambda *a, **k: _threads_result("success"),
        instagram_post_fn=lambda *a, **k: calls.append(1) or _instagram_result("success"),
    )
    assert rc == 0
    assert calls == []  # post_fn は呼ばれない（動画が無いため）
