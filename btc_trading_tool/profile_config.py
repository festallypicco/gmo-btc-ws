"""
profile_config.py
-----------------
時間帯プロファイルの純粋ロジック。
外部 I/O や副作用を持たない。

注意:
- 日付またぎプロファイル（例: 22:00-06:00）はサポートしない。
- 必要な場合は 22:00-24:00 と 00:00-06:00 に分割して定義する。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from strategy_logic import StrategyConfig


@dataclass(frozen=True)
class ProfileDefinition:
    name: str
    start_minute: int
    end_minute: int
    config: StrategyConfig


def parse_hhmm_to_minute(s: str) -> int:
    """
    "HH:MM" を 00:00 からの分数へ変換する。
    - 00:00 -> 0
    - 12:34 -> 754
    - 24:00 -> 1440
    """
    if not isinstance(s, str):
        raise ValueError(f"時刻文字列ではありません: {s!r}")

    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"時刻形式が不正です（HH:MM）: {s!r}")

    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"時刻に数値以外が含まれています: {s!r}") from exc

    if hour == 24 and minute == 0:
        return 1440
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"時刻の範囲が不正です: {s!r}")

    return hour * 60 + minute


def validate_profiles(profiles: List[ProfileDefinition]) -> Optional[str]:
    if not profiles:
        return "profiles が空です"

    names = [p.name for p in profiles]
    if len(names) != len(set(names)):
        return "profile 名が重複しています"

    for p in profiles:
        if p.start_minute >= p.end_minute:
            return (
                f"profile '{p.name}' の時間範囲が不正です: "
                f"start={p.start_minute}, end={p.end_minute}"
            )

    sorted_profiles = sorted(profiles, key=lambda x: x.start_minute)

    if sorted_profiles[0].start_minute != 0:
        return (
            f"先頭 profile '{sorted_profiles[0].name}' の開始時刻は 00:00（0分）である必要があります"
        )

    if sorted_profiles[-1].end_minute != 1440:
        return (
            f"末尾 profile '{sorted_profiles[-1].name}' の終了時刻は 24:00（1440分）である必要があります"
        )

    for i in range(len(sorted_profiles) - 1):
        left = sorted_profiles[i]
        right = sorted_profiles[i + 1]
        if left.end_minute != right.start_minute:
            return (
                "profile の連続性が不正です: "
                f"'{left.name}' end={left.end_minute}, "
                f"'{right.name}' start={right.start_minute}"
            )

    return None


def get_active_profile(profiles: List[ProfileDefinition], now_minute: int) -> ProfileDefinition:
    for p in profiles:
        if p.start_minute <= now_minute < p.end_minute:
            return p
    raise ValueError(f"now_minute={now_minute} に対応する profile がありません")
