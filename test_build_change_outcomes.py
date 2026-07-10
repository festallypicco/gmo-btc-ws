from __future__ import annotations

from datetime import datetime

from ai_review.build_change_outcomes import _build_evaluation, _merge_outcomes


def _change(
    change_id: str,
    ts: str,
    profile: str,
    version: str,
    previous_version: str,
) -> dict:
    return {
        "change_id": change_id,
        "timestamp": ts,
        "change_ts": datetime.fromisoformat(ts),
        "profile_name": profile,
        "version": version,
        "previous_version": previous_version,
        "change_type": "changed",
        "changed_fields": {"take_profit_pct": {"old": 0.0015, "new": 0.0018}},
        "reason": "test",
    }


def test_after_window_still_uses_config_version_match() -> None:
    change_ts = datetime(2026, 7, 10, 3, 30, 0)
    trade_rows = [
        {
            "timestamp": datetime(2026, 7, 10, 4, 0, 0),
            "date": datetime(2026, 7, 10).date(),
            "config_version": "v1",
            "profile_name": "night",
            "reason": "STOP_LOSS",
            "pnl": -100.0,
        },
        {
            "timestamp": datetime(2026, 7, 10, 5, 0, 0),
            "date": datetime(2026, 7, 10).date(),
            "config_version": "v2",
            "profile_name": "night",
            "reason": "TAKE_PROFIT",
            "pnl": 200.0,
        },
    ]
    market_rows = [
        {"timestamp": datetime(2026, 7, 10, 0, 0, 0), "mid_price": 10_000_000.0},
        {"timestamp": datetime(2026, 7, 10, 6, 0, 0), "mid_price": 10_100_000.0},
    ]

    evaluation = _build_evaluation(
        trade_rows=trade_rows,
        market_rows=market_rows,
        profile_name="night",
        previous_version="v1",
        version="v2",
        change_ts=change_ts,
        end_ts=datetime(2026, 7, 17, 3, 30, 0),
        requested_days=7,
        label="provisional",
        profile_effective_start_ts=None,
    )

    assert evaluation["before"]["trade_count"] == 0
    assert evaluation["after"]["trade_count"] == 1
    assert evaluation["after"]["total_pnl"] == 200.0


def test_trigger_conditions_for_provisional_and_final() -> None:
    changes = [
        _change("c1", "2026-07-01T03:30:00", "night", "v2", "v1"),
        _change("c2", "2026-07-10T03:30:00", "night", "v3", "v2"),
        _change("c3", "2026-07-01T03:30:00", "daytime", "v11", "v10"),
    ]
    merged = _merge_outcomes(
        existing_rows=[],
        profile_changes=changes,
        trade_rows=[],
        market_rows=[],
        now_dt=datetime(2026, 8, 2, 0, 0, 0),
    )
    by_id = {r["change_id"]: r for r in merged}

    assert by_id["c1"]["provisional_evaluation"] is not None
    assert by_id["c1"]["final_evaluation"] is not None  # 次変更(c2)で最終評価

    assert by_id["c2"]["provisional_evaluation"] is not None  # 7日経過
    assert by_id["c2"].get("final_evaluation") is None  # 30日未満かつ次変更なし

    assert by_id["c3"]["final_evaluation"] is not None  # 30日経過で最終評価


def test_insufficient_data_records_insufficient_confidence() -> None:
    changes = [_change("c1", "2026-07-01T03:30:00", "night", "v2", "v1")]
    merged = _merge_outcomes(
        existing_rows=[],
        profile_changes=changes,
        trade_rows=[],
        market_rows=[],
        now_dt=datetime(2026, 7, 9, 0, 0, 0),
    )
    row = merged[0]
    assert row["provisional_evaluation"] is not None
    assert row["provisional_evaluation"]["after"]["trade_count"] == 0
    assert row["provisional_evaluation"]["confidence"] == "insufficient"


def test_before_window_spans_versions_until_previous_same_profile_change() -> None:
    """
    対象プロファイル未変更の間に他プロファイルだけ変更され、
    config_version が複数回変わっても before は時刻範囲で拾えることを確認。
    """
    changes = [
        _change("n0", "2026-07-01T03:30:00", "night", "n-v1", "n-v0"),
        _change("d1", "2026-07-03T03:30:00", "daytime", "d-v1", "d-v0"),
        _change("d2", "2026-07-05T03:30:00", "daytime", "d-v2", "d-v1"),
        _change("n1", "2026-07-10T03:30:00", "night", "n-v2", "n-v1"),
    ]
    trade_rows = [
        {
            "timestamp": datetime(2026, 7, 2, 0, 0, 0),
            "date": datetime(2026, 7, 2).date(),
            "config_version": "n-v1",
            "profile_name": "night",
            "reason": "STOP_LOSS",
            "pnl": -10.0,
        },
        {
            "timestamp": datetime(2026, 7, 4, 0, 0, 0),
            "date": datetime(2026, 7, 4).date(),
            "config_version": "d-v1",  # 他プロファイル変更で更新された version
            "profile_name": "night",
            "reason": "TAKE_PROFIT",
            "pnl": 20.0,
        },
        {
            "timestamp": datetime(2026, 7, 6, 0, 0, 0),
            "date": datetime(2026, 7, 6).date(),
            "config_version": "d-v2",  # 他プロファイル変更で更新された version
            "profile_name": "night",
            "reason": "TAKE_PROFIT",
            "pnl": 30.0,
        },
        {
            "timestamp": datetime(2026, 7, 11, 0, 0, 0),
            "date": datetime(2026, 7, 11).date(),
            "config_version": "n-v2",
            "profile_name": "night",
            "reason": "TAKE_PROFIT",
            "pnl": 40.0,
        },
    ]
    merged = _merge_outcomes(
        existing_rows=[],
        profile_changes=changes,
        trade_rows=trade_rows,
        market_rows=[],
        now_dt=datetime(2026, 7, 18, 0, 0, 0),
    )
    by_id = {r["change_id"]: r for r in merged}
    row = by_id["n1"]
    before = row["provisional_evaluation"]["before"]
    after = row["provisional_evaluation"]["after"]
    assert before["trade_count"] == 2
    assert before["total_pnl"] == 50.0
    assert after["trade_count"] == 1
    assert after["total_pnl"] == 40.0


def test_first_profile_change_has_no_effective_start_limit() -> None:
    changes = [_change("n1", "2026-07-10T03:30:00", "night", "n-v2", "n-v1")]
    trade_rows = [
        {
                "timestamp": datetime(2026, 7, 3, 12, 0, 0),
            "date": datetime(2026, 7, 3).date(),
            "config_version": "legacy",
            "profile_name": "night",
            "reason": "STOP_LOSS",
            "pnl": -10.0,
        },
        {
            "timestamp": datetime(2026, 7, 9, 0, 0, 0),
            "date": datetime(2026, 7, 9).date(),
            "config_version": "legacy2",
            "profile_name": "night",
            "reason": "TAKE_PROFIT",
            "pnl": 20.0,
        },
    ]
    merged = _merge_outcomes(
        existing_rows=[],
        profile_changes=changes,
        trade_rows=trade_rows,
        market_rows=[],
        now_dt=datetime(2026, 7, 20, 0, 0, 0),
    )
    row = merged[0]
    before = row["provisional_evaluation"]["before"]
    assert before["trade_count"] == 2
    assert before["total_pnl"] == 10.0
