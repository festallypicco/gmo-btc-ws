"""
tests/test_refresh_sns_tokens.py

Threads/Instagram トークン自動更新の成功時書き換え・片側独立性・失敗時の
値保持・失敗アラート・check-only時の非破壊性を検証する。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import refresh_sns_tokens as rst  # noqa: E402


def _write_env(path: Path, lines: List[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _boom(*_a, **_k):
    raise AssertionError("this function must not be called in this scenario")


# ---------------------------------------------------------------------------
# update_env_value / _update_env_value_in_file（アトミック書き換え）
# ---------------------------------------------------------------------------


def test_update_env_value_rewrites_only_target_key(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    _write_env(
        env_path,
        [
            "GMO_API_KEY_TRADE=abc",
            "THREADS_ACCESS_TOKEN=old-threads-token",
            "INSTAGRAM_ACCESS_TOKEN=old-instagram-token",
            "TELEGRAM_BOT_TOKEN=xyz",
        ],
    )

    updated = rst.update_env_value(
        "THREADS_ACCESS_TOKEN", "new-threads-token", env_candidates=(env_path,)
    )

    assert updated == [env_path]
    content = env_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert "THREADS_ACCESS_TOKEN=new-threads-token" in lines
    # 他の行は一切変更されない
    assert "GMO_API_KEY_TRADE=abc" in lines
    assert "INSTAGRAM_ACCESS_TOKEN=old-instagram-token" in lines
    assert "TELEGRAM_BOT_TOKEN=xyz" in lines
    assert content.endswith("\n")


def test_update_env_value_skips_files_without_key(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    other_path = tmp_path / "scripts.env"
    _write_env(env_path, ["THREADS_ACCESS_TOKEN=old"])
    _write_env(other_path, ["TELEGRAM_BOT_TOKEN=xyz"])  # THREADS_ACCESS_TOKEN無し

    updated = rst.update_env_value(
        "THREADS_ACCESS_TOKEN", "new", env_candidates=(env_path, other_path)
    )

    assert updated == [env_path]
    assert "THREADS_ACCESS_TOKEN=new" in env_path.read_text(encoding="utf-8")
    # キーが無いファイルは変更されない
    assert other_path.read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=xyz\n"


def test_update_env_value_missing_file_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.env"
    updated = rst.update_env_value("THREADS_ACCESS_TOKEN", "new", env_candidates=(missing,))
    assert updated == []


# ---------------------------------------------------------------------------
# _refresh_token / refresh_threads_token / refresh_instagram_token
# ---------------------------------------------------------------------------


def test_refresh_threads_token_success() -> None:
    def fake_get(url: str, **_k):
        assert "th_refresh_token" in url
        return 200, {"access_token": "new-tok", "expires_in": 5184000}, ""

    result = rst.refresh_threads_token("old-tok", http_get_fn=fake_get)
    assert result.ok
    assert result.platform == "threads"
    assert result.new_access_token == "new-tok"
    assert result.expires_in_sec == 5184000


def test_refresh_instagram_token_success() -> None:
    def fake_get(url: str, **_k):
        assert "ig_refresh_token" in url
        assert url.startswith("https://graph.instagram.com/refresh_access_token")
        return 200, {"access_token": "new-ig-tok", "expires_in": 5183944}, ""

    result = rst.refresh_instagram_token("old-tok", http_get_fn=fake_get)
    assert result.ok
    assert result.platform == "instagram"
    assert result.new_access_token == "new-ig-tok"
    assert result.expires_in_sec == 5183944


def test_refresh_token_missing_current_token_is_error() -> None:
    result = rst.refresh_threads_token("", http_get_fn=_boom)
    assert result.status == "error"
    assert "not configured" in result.detail


def test_refresh_token_http_error() -> None:
    def fake_get(url: str, **_k):
        return 400, {"error": {"code": 190, "message": "invalid token"}}, ""

    result = rst.refresh_instagram_token("old-tok", http_get_fn=fake_get)
    assert result.status == "error"
    assert not result.ok
    assert "invalid token" in result.detail


def test_refresh_token_malformed_response_is_error() -> None:
    def fake_get(url: str, **_k):
        return 200, {"unexpected": "shape"}, '{"unexpected":"shape"}'

    result = rst.refresh_threads_token("old-tok", http_get_fn=fake_get)
    assert result.status == "error"


# ---------------------------------------------------------------------------
# run_refresh: 成功時書き換え・片側独立性・失敗時の値保持・アラート
# ---------------------------------------------------------------------------


def test_run_refresh_success_rewrites_env(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    _write_env(
        env_path,
        [
            "THREADS_ACCESS_TOKEN=old-threads",
            "INSTAGRAM_ACCESS_TOKEN=old-instagram",
            "OTHER_KEY=untouched",
        ],
    )
    alerts: List[str] = []

    def fake_threads(current_token: str) -> rst.RefreshResult:
        assert current_token == "old-threads"
        return rst.RefreshResult(
            platform="threads", status="success",
            new_access_token="new-threads", expires_in_sec=5184000,
        )

    def fake_instagram(current_token: str) -> rst.RefreshResult:
        assert current_token == "old-instagram"
        return rst.RefreshResult(
            platform="instagram", status="success",
            new_access_token="new-instagram", expires_in_sec=5183944,
        )

    results = rst.run_refresh(
        env={"THREADS_ACCESS_TOKEN": "old-threads", "INSTAGRAM_ACCESS_TOKEN": "old-instagram"},
        env_candidates=(env_path,),
        refresh_threads_fn=fake_threads,
        refresh_instagram_fn=fake_instagram,
        alert_fn=lambda msg: alerts.append(msg),
    )

    assert results["threads"].ok
    assert results["instagram"].ok
    content = env_path.read_text(encoding="utf-8")
    assert "THREADS_ACCESS_TOKEN=new-threads" in content
    assert "INSTAGRAM_ACCESS_TOKEN=new-instagram" in content
    assert "OTHER_KEY=untouched" in content
    assert len(alerts) == 2
    assert any("Threads" in a and "成功" in a for a in alerts)
    assert any("Instagram" in a and "成功" in a for a in alerts)
    assert all("[ALERT]" not in a for a in alerts)


def test_run_refresh_one_platform_failure_does_not_block_the_other(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    _write_env(
        env_path,
        [
            "THREADS_ACCESS_TOKEN=old-threads",
            "INSTAGRAM_ACCESS_TOKEN=old-instagram",
        ],
    )
    alerts: List[str] = []

    def fake_threads(current_token: str) -> rst.RefreshResult:
        raise RuntimeError("boom: threads API is down")

    def fake_instagram(current_token: str) -> rst.RefreshResult:
        return rst.RefreshResult(
            platform="instagram", status="success",
            new_access_token="new-instagram", expires_in_sec=5183944,
        )

    results = rst.run_refresh(
        env={"THREADS_ACCESS_TOKEN": "old-threads", "INSTAGRAM_ACCESS_TOKEN": "old-instagram"},
        env_candidates=(env_path,),
        refresh_threads_fn=fake_threads,
        refresh_instagram_fn=fake_instagram,
        alert_fn=lambda msg: alerts.append(msg),
    )

    # threadsは例外→api_error相当のerrorだが、instagramは独立して成功する
    assert results["threads"].status == "error"
    assert results["instagram"].ok

    content = env_path.read_text(encoding="utf-8")
    # 失敗したthreadsの値は維持
    assert "THREADS_ACCESS_TOKEN=old-threads" in content
    # 成功したinstagramの値は更新される
    assert "INSTAGRAM_ACCESS_TOKEN=new-instagram" in content

    assert len(alerts) == 2
    failure_alerts = [a for a in alerts if "[ALERT]" in a]
    success_alerts = [a for a in alerts if "成功" in a]
    assert len(failure_alerts) == 1
    assert "Threads" in failure_alerts[0]
    assert len(success_alerts) == 1
    assert "Instagram" in success_alerts[0]


def test_run_refresh_failure_keeps_env_value_unchanged(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    _write_env(
        env_path,
        [
            "THREADS_ACCESS_TOKEN=old-threads",
            "INSTAGRAM_ACCESS_TOKEN=old-instagram",
        ],
    )
    original_content = env_path.read_text(encoding="utf-8")

    def fake_error(current_token: str) -> rst.RefreshResult:
        return rst.RefreshResult(
            platform="threads", status="error", detail="http=400 code=190"
        )

    rst.run_refresh(
        env={"THREADS_ACCESS_TOKEN": "old-threads", "INSTAGRAM_ACCESS_TOKEN": "old-instagram"},
        env_candidates=(env_path,),
        refresh_threads_fn=fake_error,
        refresh_instagram_fn=fake_error,
        alert_fn=lambda _m: None,
    )

    assert env_path.read_text(encoding="utf-8") == original_content


def test_run_refresh_failure_sends_alert_with_detail(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    _write_env(env_path, ["THREADS_ACCESS_TOKEN=old-threads", "INSTAGRAM_ACCESS_TOKEN=old-instagram"])
    alerts: List[str] = []

    def fake_error(current_token: str) -> rst.RefreshResult:
        return rst.RefreshResult(
            platform="threads", status="error", detail="http=401 code=190 message='Invalid OAuth'"
        )

    rst.run_refresh(
        env={"THREADS_ACCESS_TOKEN": "old-threads", "INSTAGRAM_ACCESS_TOKEN": "old-instagram"},
        env_candidates=(env_path,),
        refresh_threads_fn=fake_error,
        refresh_instagram_fn=fake_error,
        alert_fn=lambda msg: alerts.append(msg),
    )

    assert len(alerts) == 2
    assert all("[ALERT]" in a for a in alerts)
    assert all("変更していません" in a for a in alerts)
    assert any("Invalid OAuth" in a for a in alerts)


# ---------------------------------------------------------------------------
# --check-only: リフレッシュAPIを一切呼ばない
# ---------------------------------------------------------------------------


def test_check_token_status_reports_debug_token_result() -> None:
    def fake_get(url: str, **_k):
        assert "debug_token" in url
        return 200, {"data": {"is_valid": True, "expires_at": 1999999999}}, ""

    text = rst.check_token_status("threads", "some-token", http_get_fn=fake_get)
    assert "is_valid=True" in text


def test_check_token_status_unset_token() -> None:
    text = rst.check_token_status("instagram", "", http_get_fn=_boom)
    assert text == "未設定"


def test_check_token_status_falls_back_when_debug_unavailable() -> None:
    def fake_get(url: str, **_k):
        return 400, {"error": {"code": 100, "message": "no app token"}}, ""

    text = rst.check_token_status("threads", "tok", http_get_fn=fake_get)
    assert "確認不可" in text


def test_run_check_only_never_calls_refresh_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # check-only経路がリフレッシュ関数を呼んでいないことの回帰ガード
    monkeypatch.setattr(rst, "refresh_threads_token", _boom)
    monkeypatch.setattr(rst, "refresh_instagram_token", _boom)

    calls: List[Tuple[str, str]] = []

    def fake_check(platform: str, token: str) -> str:
        calls.append((platform, token))
        return "is_valid=True expires_at=2099-01-01T00:00:00"

    alerts: List[str] = []
    results = rst.run_check_only(
        env={"THREADS_ACCESS_TOKEN": "tok-a", "INSTAGRAM_ACCESS_TOKEN": "tok-b"},
        check_fn=fake_check,
        alert_fn=lambda msg: alerts.append(msg),
    )

    assert set(calls) == {("threads", "tok-a"), ("instagram", "tok-b")}
    assert results["threads"] == "is_valid=True expires_at=2099-01-01T00:00:00"
    assert len(alerts) == 1
    assert "check-only" in alerts[0]
    assert "Threads" in alerts[0]
    assert "Instagram" in alerts[0]


def test_check_only_does_not_write_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = tmp_path / ".env"
    _write_env(env_path, ["THREADS_ACCESS_TOKEN=old-threads", "INSTAGRAM_ACCESS_TOKEN=old-instagram"])
    original_content = env_path.read_text(encoding="utf-8")

    monkeypatch.setattr(rst, "update_env_value", _boom)

    rst.run_check_only(
        env={"THREADS_ACCESS_TOKEN": "old-threads", "INSTAGRAM_ACCESS_TOKEN": "old-instagram"},
        check_fn=lambda platform, token: "is_valid=True expires_at=unknown",
        alert_fn=lambda _m: None,
    )

    assert env_path.read_text(encoding="utf-8") == original_content


# ---------------------------------------------------------------------------
# main(): CLIディスパッチ（--check-only有無で呼び分ける）
# ---------------------------------------------------------------------------


def test_main_check_only_dispatches_to_run_check_only_not_run_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[str] = []
    monkeypatch.setattr(
        rst, "run_check_only", lambda **_k: calls.append("check_only") or {}
    )
    monkeypatch.setattr(rst, "run_refresh", lambda **_k: _boom())

    rc = rst.main(["--check-only"])

    assert rc == 0
    assert calls == ["check_only"]


def test_main_default_dispatches_to_run_refresh_not_check_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[str] = []
    monkeypatch.setattr(rst, "run_check_only", lambda **_k: _boom())
    monkeypatch.setattr(
        rst,
        "run_refresh",
        lambda **_k: calls.append("refresh")
        or {
            "threads": rst.RefreshResult(platform="threads", status="success"),
            "instagram": rst.RefreshResult(platform="instagram", status="success"),
        },
    )

    rc = rst.main([])

    assert rc == 0
    assert calls == ["refresh"]


def test_main_returns_1_when_any_platform_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rst,
        "run_refresh",
        lambda **_k: {
            "threads": rst.RefreshResult(platform="threads", status="error", detail="x"),
            "instagram": rst.RefreshResult(platform="instagram", status="success"),
        },
    )

    rc = rst.main([])

    assert rc == 1
