"""趣味称号规则引擎：只由可复核的数据触发，规则与文案分离。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from providers.opgg import NormalizedMatch
from stats import ChampionStats, DailyStats


@dataclass(frozen=True)
class TitleRules:
    """称号阈值；运营可按试运行结果调整。"""

    minimum_games: int = 3
    climbing_min_games: int = 3
    climbing_win_rate: float = 60.0
    workhorse_min_games: int = 10
    rollercoaster_min_games: int = 4
    rollercoaster_min_alternations: int = 3
    philanthropist_avg_deaths: float = 6.0
    specialist_min_games: int = 3
    specialist_win_rate: float = 60.0


@dataclass(frozen=True)
class Title:
    key: str
    name: str
    reason: str


def _alternations(matches: Sequence[NormalizedMatch]) -> int:
    ordered = sorted(matches, key=lambda match: match.start_time)
    results = [match.win for match in ordered]
    return sum(1 for left, right in pairwise(results) if left != right)


def _best_specialist(stats: DailyStats, rules: TitleRules) -> ChampionStats | None:
    candidates = [
        champion
        for champion in stats.champions
        if champion.games >= rules.specialist_min_games
        and champion.win_rate >= rules.specialist_win_rate
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda champion: (champion.games, champion.win_rate, champion.kda))


def evaluate_titles(
    stats: DailyStats,
    matches: Sequence[NormalizedMatch],
    rules: TitleRules | None = None,
) -> list[Title]:
    """按固定顺序返回触发的称号；样本不足时一律不触发。"""
    active = rules or TitleRules()
    if stats.total_games < active.minimum_games:
        return []

    titles: list[Title] = []

    if (
        stats.total_games >= active.climbing_min_games
        and stats.win_rate is not None
        and stats.win_rate >= active.climbing_win_rate
    ):
        titles.append(
            Title(
                key="climbing",
                name="上分密码",
                reason=f"{stats.total_games} 场 {stats.wins} 胜 {stats.losses} 负，"
                f"胜率 {stats.win_rate:.1f}%",
            )
        )

    if stats.total_games >= active.workhorse_min_games:
        titles.append(
            Title(
                key="workhorse",
                name="峡谷劳模",
                reason=f"当日 {stats.total_games} 场",
            )
        )

    alternations = _alternations(matches)
    if (
        stats.total_games >= active.rollercoaster_min_games
        and alternations >= active.rollercoaster_min_alternations
    ):
        titles.append(
            Title(
                key="rollercoaster",
                name="状态过山车",
                reason=f"{stats.total_games} 场中出现 {alternations} 次胜负交替",
            )
        )

    if stats.avg_deaths is not None and stats.avg_deaths >= active.philanthropist_avg_deaths:
        titles.append(
            Title(
                key="philanthropist",
                name="峡谷慈善家",
                reason=f"场均死亡 {stats.avg_deaths:.1f}"
                f"（阈值 {active.philanthropist_avg_deaths:.1f}）",
            )
        )

    specialist = _best_specialist(stats, active)
    if specialist is not None:
        titles.append(
            Title(
                key="specialist",
                name=f"英雄专精 · {specialist.champion_name}",
                reason=f"{specialist.champion_name} {specialist.games} 场，"
                f"胜率 {specialist.win_rate:.1f}%，KDA {specialist.kda:.2f}",
            )
        )

    return titles
