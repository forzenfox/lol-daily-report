"""每日对局统计（纯函数，便于复算与测试）。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from providers.opgg import NormalizedMatch


@dataclass(frozen=True)
class ChampionStats:
    champion_key: str
    champion_name: str
    games: int
    wins: int
    losses: int
    win_rate: float
    kda: float
    avg_kills: float
    avg_deaths: float
    avg_assists: float


@dataclass(frozen=True)
class DailyStats:
    total_games: int
    wins: int
    losses: int
    win_rate: float | None
    kda: float | None
    avg_kills: float | None
    avg_deaths: float | None
    avg_assists: float | None
    avg_duration_seconds: int
    avg_cs: float | None
    cs_per_min: float | None
    champions: list[ChampionStats]
    minimum_games: int
    sample_sufficient: bool


REPORT_QUEUE_TYPE = "SOLORANKED"
REPORT_GAME_MAP = "SUMMONERS_RIFT"


def _kda(kills: int, deaths: int, assists: int) -> float:
    return round((kills + assists) / max(deaths, 1), 2)


def unique_matches(matches: Iterable[NormalizedMatch]) -> list[NormalizedMatch]:
    """按 match_id 去重，保留首次出现。"""
    result: list[NormalizedMatch] = []
    seen: set[str] = set()
    for match in matches:
        if match.match_id in seen:
            continue
        seen.add(match.match_id)
        result.append(match)
    return result


def is_report_match(match: NormalizedMatch) -> bool:
    """日报口径：仅 5v5 排位（单双排）对局；缺少地图时按队列类型判定。"""
    return match.queue_type == REPORT_QUEUE_TYPE and match.game_map in (None, REPORT_GAME_MAP)


def select_report_matches(matches: Iterable[NormalizedMatch]) -> list[NormalizedMatch]:
    """按日报口径筛选并去重：只保留 5v5 排位对局。"""
    return unique_matches(match for match in matches if is_report_match(match))


def _champion_stats(matches: list[NormalizedMatch]) -> list[ChampionStats]:
    grouped: dict[str, list[NormalizedMatch]] = {}
    for match in matches:
        grouped.setdefault(match.champion_key, []).append(match)

    result: list[ChampionStats] = []
    for key, group in grouped.items():
        games = len(group)
        wins = sum(1 for item in group if item.win)
        kills = sum(item.kills for item in group)
        deaths = sum(item.deaths for item in group)
        assists = sum(item.assists for item in group)
        result.append(
            ChampionStats(
                champion_key=key,
                champion_name=group[0].champion_name,
                games=games,
                wins=wins,
                losses=games - wins,
                win_rate=round(wins / games * 100, 1),
                kda=_kda(kills, deaths, assists),
                avg_kills=round(kills / games, 1),
                avg_deaths=round(deaths / games, 1),
                avg_assists=round(assists / games, 1),
            )
        )
    result.sort(key=lambda item: (-item.games, -item.wins, item.champion_key))
    return result


def compute_daily_stats(matches: Iterable[NormalizedMatch], minimum_games: int = 3) -> DailyStats:
    """按 PRD 口径计算当日统计；同一 match_id 只统计一次。"""
    unique = unique_matches(matches)
    total = len(unique)
    if total == 0:
        return DailyStats(
            total_games=0,
            wins=0,
            losses=0,
            win_rate=None,
            kda=None,
            avg_kills=None,
            avg_deaths=None,
            avg_assists=None,
            avg_duration_seconds=0,
            avg_cs=None,
            cs_per_min=None,
            champions=[],
            minimum_games=minimum_games,
            sample_sufficient=False,
        )

    wins = sum(1 for match in unique if match.win)
    kills = sum(match.kills for match in unique)
    deaths = sum(match.deaths for match in unique)
    assists = sum(match.assists for match in unique)
    total_cs = sum(match.cs for match in unique)
    total_duration = sum(match.duration_seconds for match in unique)

    return DailyStats(
        total_games=total,
        wins=wins,
        losses=total - wins,
        win_rate=round(wins / total * 100, 1),
        kda=_kda(kills, deaths, assists),
        avg_kills=round(kills / total, 1),
        avg_deaths=round(deaths / total, 1),
        avg_assists=round(assists / total, 1),
        avg_duration_seconds=round(total_duration / total),
        avg_cs=round(total_cs / total, 1),
        cs_per_min=round(total_cs / (total_duration / 60), 1) if total_duration else None,
        champions=_champion_stats(unique),
        minimum_games=minimum_games,
        sample_sufficient=total >= minimum_games,
    )
