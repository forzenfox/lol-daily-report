"""趣味称号规则引擎：只由可复核的数据触发，规则与文案分离。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from providers.opgg import NormalizedMatch
from stats import DailyStats, unique_matches


@dataclass(frozen=True)
class TitleRules:
    """称号阈值；样本门槛与日报配置保持一致。"""

    minimum_games: int = 3


@dataclass(frozen=True)
class Title:
    key: str
    name: str
    reason: str


def _kda(kills: int, deaths: int, assists: int) -> float:
    """未取整 KDA，仅用于对位比较。"""
    return (kills + assists) / max(deaths, 1)


def _lane_kda(match: NormalizedMatch) -> tuple[float, float] | None:
    """返回本人与对位的未取整 KDA；无对位时返回 None。"""
    opponent = match.lane_opponent
    if opponent is None:
        return None
    return (
        _kda(match.kills, match.deaths, match.assists),
        _kda(opponent.kills, opponent.deaths, opponent.assists),
    )


def _lane_comparison(matches: Sequence[NormalizedMatch]) -> tuple[int, int, int]:
    """返回可比胜场数、KDA 低于对位局数、KDA 高于对位局数。"""
    comparable = lower = higher = 0
    for match in unique_matches(matches):
        if not match.win:
            continue
        pair = _lane_kda(match)
        if pair is None:
            continue
        comparable += 1
        own, rival = pair
        if own < rival:
            lower += 1
        elif own > rival:
            higher += 1
    return comparable, lower, higher


def _win_rate(stats: DailyStats) -> float:
    return stats.win_rate if stats.win_rate is not None else 0.0


def evaluate_titles(
    stats: DailyStats,
    matches: Sequence[NormalizedMatch],
    rules: TitleRules | None = None,
) -> list[Title]:
    """按 PRD 8.2 返回互斥称号；调用方需传入日报口径（仅 5v5 排位）对局。"""
    active = rules or TitleRules()
    if stats.total_games < active.minimum_games:
        return []

    record = f"{stats.wins} 胜 {stats.losses} 负（{_win_rate(stats):.1f}%）"

    if stats.wins < stats.losses:
        return [Title(key="valley_terror", name="峡谷鬼见愁", reason=record)]

    if stats.wins == stats.losses:
        return []

    comparable, lower, higher = _lane_comparison(matches)
    if comparable == 0:
        return []

    if lower * 2 > comparable:
        return [
            Title(
                key="negative_kda_climber",
                name="负战绩上分第一人",
                reason=f"{record}，对位可比胜场 {comparable} 场，"
                f"其中 {lower} 场 KDA 低于对位（{lower / comparable * 100:.1f}%）",
            )
        ]

    if higher * 2 > comparable:
        return [
            Title(
                key="carry_god",
                name="战神C",
                reason=f"{record}，对位可比胜场 {comparable} 场，"
                f"其中 {higher} 场 KDA 高于对位（{higher / comparable * 100:.1f}%）",
            )
        ]

    return []
