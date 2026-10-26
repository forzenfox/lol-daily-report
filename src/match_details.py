"""对局明细与对位雷达的统计口径（纯函数，便于复算与测试）。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from providers.opgg import NormalizedMatch, ParticipantPerformance

RADAR_LABELS: tuple[str, ...] = ("分均补刀", "KDA", "伤转", "参团率", "承伤占比", "伤害占比")

# 六维参考上限（达到该值即落在雷达外圈），用于跨英雄、跨位置可比。
RADAR_REFERENCES: tuple[float, ...] = (10.0, 12.0, 200.0, 80.0, 50.0, 30.0)

POSITION_LABELS: dict[str, str] = {
    "TOP": "上单",
    "JUNGLE": "打野",
    "MID": "中单",
    "ADC": "下路",
    "SUPPORT": "辅助",
}


@dataclass(frozen=True)
class MatchDetail:
    """单局明细：原始指标 + 归一化后的双序列雷达值。"""

    match: NormalizedMatch
    opponent: ParticipantPerformance | None
    player_metrics: tuple[float, ...] = ()
    opponent_metrics: tuple[float, ...] = ()
    player_radar: tuple[float, ...] = ()
    opponent_radar: tuple[float, ...] = ()


def match_metrics(
    performance: ParticipantPerformance | None, duration_seconds: int
) -> tuple[float, ...]:
    """计算六维指标；缺少参战者数据时返回空序列。

    顺序与 RADAR_LABELS 一致：分均补刀、KDA、伤转、参团率、承伤占比、伤害占比。
    """
    if performance is None:
        return ()
    minutes = max(duration_seconds / 60, 1.0)
    kills_assists = performance.kills + performance.assists
    return (
        performance.cs / minutes,
        kills_assists / max(performance.deaths, 1),
        performance.damage_to_champions / max(performance.gold_earned, 1) * 100,
        kills_assists / max(performance.team_kills, 1) * 100,
        performance.damage_taken / max(performance.team_damage_taken, 1) * 100,
        performance.damage_to_champions / max(performance.team_damage_to_champions, 1) * 100,
    )


def opponent_metrics(match: NormalizedMatch) -> tuple[float, ...]:
    return match_metrics(match.lane_opponent, match.duration_seconds)


def normalize_pair(
    player: Sequence[float], opponent: Sequence[float]
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """按六维参考上限归一化到 0～1，超出参考值按外圈截断。"""
    player_values = tuple(
        _normalize(value, reference) for value, reference in zip(player, RADAR_REFERENCES)
    )
    opponent_values = tuple(
        _normalize(value, reference) for value, reference in zip(opponent, RADAR_REFERENCES)
    )
    return player_values, opponent_values


def _normalize(value: float, reference: float) -> float:
    if reference <= 0 or value <= 0:
        return 0.0
    return min(value / reference, 1.0)


def build_match_detail(match: NormalizedMatch) -> MatchDetail:
    """构建单局明细；任一方缺少参战者数据时不生成雷达序列。"""
    own = match_metrics(match.own_performance, match.duration_seconds)
    rival = opponent_metrics(match)
    if not own or not rival:
        return MatchDetail(
            match=match,
            opponent=match.lane_opponent,
            player_metrics=own,
            opponent_metrics=rival,
        )
    player_radar, opponent_radar = normalize_pair(own, rival)
    return MatchDetail(
        match=match,
        opponent=match.lane_opponent,
        player_metrics=own,
        opponent_metrics=rival,
        player_radar=player_radar,
        opponent_radar=opponent_radar,
    )


def compute_match_details(matches: Iterable[NormalizedMatch]) -> list[MatchDetail]:
    """按输入顺序生成全部对局明细；同一 match_id 只保留首次出现。"""
    details: list[MatchDetail] = []
    seen: set[str] = set()
    for match in matches:
        if match.match_id in seen:
            continue
        seen.add(match.match_id)
        details.append(build_match_detail(match))
    return details


def position_label(position: str | None) -> str:
    if not position:
        return "—"
    return POSITION_LABELS.get(position.upper(), position)
