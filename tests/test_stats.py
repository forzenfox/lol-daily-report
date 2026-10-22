"""stats.compute_daily_stats 的契约测试（TDD：先于实现编写）。"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from providers.opgg import NormalizedMatch
from stats import ChampionStats, DailyStats, compute_daily_stats

SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_match(
    match_id: str,
    *,
    win: bool = True,
    champion_key: str = "yorick",
    champion_name: str = "Yorick",
    kills: int = 5,
    deaths: int = 2,
    assists: int = 3,
    cs: int = 200,
    duration: int = 1800,
    start: datetime | None = None,
) -> NormalizedMatch:
    return NormalizedMatch(
        match_id=match_id,
        start_time=start or datetime(2026, 10, 20, 20, 0, tzinfo=SHANGHAI),
        duration_seconds=duration,
        win=win,
        champion_key=champion_key,
        champion_name=champion_name,
        kills=kills,
        deaths=deaths,
        assists=assists,
        cs=cs,
        position="TOP",
        queue_type="SOLORANKED",
        queue_label="Ranked Solo/Duo",
        game_map="SUMMONERS_RIFT",
        kda_ratio=None,
        cs_per_min=None,
        op_score=None,
        kill_participation=None,
        source="opgg-getgames",
        fetched_at=datetime.now(UTC),
    )


def test_empty_matches_return_zero_and_no_rate() -> None:
    stats = compute_daily_stats([], minimum_games=3)

    assert isinstance(stats, DailyStats)
    assert stats.total_games == 0
    assert stats.wins == 0
    assert stats.losses == 0
    assert stats.win_rate is None
    assert stats.kda is None
    assert stats.champions == []
    assert stats.sample_sufficient is False


def test_basic_aggregates_follow_prd_formula() -> None:
    matches = [
        make_match("m1", win=True, kills=6, deaths=4, assists=1, cs=279, duration=1698),
        make_match("m2", win=False, kills=11, deaths=10, assists=7, cs=294, duration=2491),
        make_match("m3", win=True, kills=1, deaths=2, assists=3, cs=126, duration=929),
    ]

    stats = compute_daily_stats(matches, minimum_games=3)

    assert stats.total_games == 3
    assert stats.wins == 2
    assert stats.losses == 1
    assert stats.win_rate == 66.7
    # KDA = (6+11+1 + 1+7+3) / max(4+10+2, 1) = 29 / 16 = 1.8125
    assert stats.kda == 1.81
    assert stats.avg_kills == 6.0
    assert stats.avg_deaths == 5.3
    assert stats.avg_assists == 3.7
    assert stats.avg_duration_seconds == 1706
    assert stats.avg_cs == 233.0
    # 总补刀 / 总分钟 = 699 / (5118 / 60) ≈ 8.2
    assert stats.cs_per_min == 8.2
    assert stats.sample_sufficient is True


def test_zero_deaths_uses_one_as_denominator() -> None:
    stats = compute_daily_stats([make_match("m1", kills=5, deaths=0, assists=10)], minimum_games=3)

    assert stats.kda == 15.0


def test_duplicate_match_ids_are_counted_once() -> None:
    duplicated = [make_match("m1", win=True), make_match("m1", win=False), make_match("m2")]

    stats = compute_daily_stats(duplicated, minimum_games=1)

    assert stats.total_games == 2
    assert stats.wins == 2


def test_champion_aggregation_and_ordering() -> None:
    matches = [
        make_match("m1", champion_key="yorick", champion_name="Yorick", win=True),
        make_match(
            "m2",
            champion_key="yorick",
            champion_name="Yorick",
            win=False,
            kills=1,
            deaths=5,
            assists=2,
        ),
        make_match(
            "m3", champion_key="gnar", champion_name="Gnar", win=True, kills=3, deaths=1, assists=6
        ),
    ]

    stats = compute_daily_stats(matches, minimum_games=1)
    by_key = {champ.champion_key: champ for champ in stats.champions}

    assert isinstance(stats.champions[0], ChampionStats)
    assert stats.champions[0].champion_key == "yorick"
    assert by_key["yorick"].games == 2
    assert by_key["yorick"].wins == 1
    assert by_key["yorick"].win_rate == 50.0
    assert by_key["yorick"].kda == 1.57
    assert by_key["gnar"].games == 1
    assert by_key["gnar"].win_rate == 100.0


def test_champion_tiebreak_is_language_independent() -> None:
    matches = [
        make_match("m1", champion_key="yorick", champion_name="牧魂人"),
        make_match("m2", champion_key="ambessa", champion_name="铁血狼母"),
        make_match("m3", champion_key="sion", champion_name="亡灵战神"),
    ]

    stats = compute_daily_stats(matches, minimum_games=1)

    assert [champion.champion_key for champion in stats.champions] == [
        "ambessa",
        "sion",
        "yorick",
    ]


def test_sample_sufficiency_uses_threshold() -> None:
    matches = [make_match("m1"), make_match("m2")]

    assert compute_daily_stats(matches, minimum_games=3).sample_sufficient is False
    assert compute_daily_stats(matches, minimum_games=2).sample_sufficient is True


def test_zero_duration_yields_no_cs_per_min() -> None:
    stats = compute_daily_stats([make_match("m1", duration=0, cs=100)], minimum_games=1)

    assert stats.cs_per_min is None
    assert stats.avg_duration_seconds == 0
