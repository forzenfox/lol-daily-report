"""称号规则引擎的契约测试（TDD：先于实现编写，口径见 PRD 8.2/8.3）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from providers.opgg import NormalizedMatch, ParticipantPerformance
from stats import compute_daily_stats
from titles import Title, TitleRules, evaluate_titles

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 0.75 KDA 与 4.00 KDA，用于构造明确低于/高于对位的胜场。
LOW_KDA = {"kills": 1, "deaths": 4, "assists": 2}
HIGH_KDA = {"kills": 5, "deaths": 2, "assists": 3}


def make_performance(
    *,
    kills: int = 5,
    deaths: int = 2,
    assists: int = 3,
    team_key: str = "RED",
    position: str = "TOP",
) -> ParticipantPerformance:
    return ParticipantPerformance(
        summoner_name="Opponent#KR1",
        champion_key="darius",
        champion_name="Darius",
        position=position,
        team_key=team_key,
        kills=kills,
        deaths=deaths,
        assists=assists,
        cs=184,
        gold_earned=8000,
        damage_to_champions=12000,
        damage_taken=10000,
        vision_score=30,
        team_kills=10,
        team_damage_to_champions=80000,
        team_damage_taken=70000,
        op_score=4.1,
        win=False,
    )


def make_match(
    match_id: str,
    *,
    win: bool = True,
    kills: int = 5,
    deaths: int = 2,
    assists: int = 3,
    opponent: ParticipantPerformance | None = None,
    index: int = 0,
    queue_type: str = "SOLORANKED",
    game_map: str | None = "SUMMONERS_RIFT",
) -> NormalizedMatch:
    return NormalizedMatch(
        match_id=match_id,
        start_time=datetime(2026, 10, 20, 12, 0, tzinfo=SHANGHAI) + timedelta(minutes=30 * index),
        duration_seconds=1800,
        win=win,
        champion_key="yorick",
        champion_name="Yorick",
        kills=kills,
        deaths=deaths,
        assists=assists,
        cs=200,
        position="TOP",
        queue_type=queue_type,
        queue_label="Ranked Solo/Duo",
        game_map=game_map,
        kda_ratio=None,
        cs_per_min=None,
        op_score=None,
        kill_participation=None,
        source="opgg-getgames",
        fetched_at=datetime.now(UTC),
        lane_opponent=opponent,
    )


def lower_lane_win(match_id: str, *, index: int = 0) -> NormalizedMatch:
    return make_match(
        match_id,
        win=True,
        opponent=make_performance(**HIGH_KDA),
        index=index,
        **LOW_KDA,
    )


def higher_lane_win(match_id: str, *, index: int = 0) -> NormalizedMatch:
    return make_match(
        match_id,
        win=True,
        opponent=make_performance(**LOW_KDA),
        index=index,
        **HIGH_KDA,
    )


def tied_lane_win(match_id: str, *, index: int = 0) -> NormalizedMatch:
    return make_match(
        match_id,
        win=True,
        kills=2,
        deaths=2,
        assists=2,
        opponent=make_performance(kills=1, deaths=2, assists=3),
        index=index,
    )


def evaluate(matches: list[NormalizedMatch], rules: TitleRules | None = None) -> list[Title]:
    stats = compute_daily_stats(matches, minimum_games=1)
    return evaluate_titles(stats, matches, rules)


def keys(titles: list[Title]) -> list[str]:
    return [title.key for title in titles]


def test_empty_day_has_no_titles() -> None:
    assert evaluate([]) == []


def test_insufficient_sample_never_triggers_titles() -> None:
    matches = [make_match("m1"), make_match("m2")]

    assert evaluate(matches) == []


def test_negative_kda_climber_triggers_when_most_wins_lose_lane_kda() -> None:
    matches = [
        lower_lane_win("m1", index=0),
        lower_lane_win("m2", index=1),
        lower_lane_win("m3", index=2),
        higher_lane_win("m4", index=3),
        make_match("m5", index=4),
        make_match("m6", win=False, index=5),
        make_match("m7", win=False, index=6),
    ]

    titles = evaluate(matches)

    assert keys(titles) == ["negative_kda_climber"]
    title = titles[0]
    assert title.name == "负战绩上分第一人"
    assert "5 胜 2 负" in title.reason
    assert "71.4%" in title.reason
    assert "对位可比胜场 4 场" in title.reason
    assert "3 场 KDA 低于对位" in title.reason
    assert "75.0%" in title.reason


def test_carry_god_triggers_when_most_wins_win_lane_kda() -> None:
    matches = [
        higher_lane_win("m1", index=0),
        higher_lane_win("m2", index=1),
        higher_lane_win("m3", index=2),
        lower_lane_win("m4", index=3),
        make_match("m5", index=4),
        make_match("m6", win=False, index=5),
        make_match("m7", win=False, index=6),
    ]

    titles = evaluate(matches)

    assert keys(titles) == ["carry_god"]
    title = titles[0]
    assert title.name == "战神"
    assert "3 场 KDA 高于对位" in title.reason
    assert "75.0%" in title.reason


def test_valley_terror_triggers_below_fifty_percent_win_rate() -> None:
    matches = [
        higher_lane_win("m1", index=0),
        higher_lane_win("m2", index=1),
        make_match("m3", win=False, index=2),
        make_match("m4", win=False, index=3),
        make_match("m5", win=False, index=4),
    ]

    titles = evaluate(matches)

    assert keys(titles) == ["valley_terror"]
    assert titles[0].name == "峡谷鬼见愁"
    assert "2 胜 3 负" in titles[0].reason
    assert "40.0%" in titles[0].reason


def test_exact_fifty_percent_win_rate_has_no_title() -> None:
    matches = [
        lower_lane_win("m1", index=0),
        lower_lane_win("m2", index=1),
        lower_lane_win("m3", index=2),
        make_match("m4", win=False, index=3),
        make_match("m5", win=False, index=4),
        make_match("m6", win=False, index=5),
    ]

    assert evaluate(matches) == []


def test_tied_kda_stays_in_denominator() -> None:
    matches = [
        lower_lane_win("m1", index=0),
        tied_lane_win("m2", index=1),
        make_match("m3", win=False, index=2),
    ]

    assert evaluate(matches) == []


def test_ratio_exactly_fifty_percent_does_not_trigger() -> None:
    matches = [
        lower_lane_win("m1", index=0),
        higher_lane_win("m2", index=1),
        make_match("m3", win=False, index=2),
    ]

    assert evaluate(matches) == []


def test_missing_lane_opponent_is_excluded_from_denominator() -> None:
    matches = [
        lower_lane_win("m1", index=0),
        make_match("m2", index=1),
        make_match("m3", index=2),
    ]

    titles = evaluate(matches)

    assert keys(titles) == ["negative_kda_climber"]
    assert "对位可比胜场 1 场" in titles[0].reason
    assert "100.0%" in titles[0].reason


def test_no_comparable_wins_never_triggers_lane_title() -> None:
    matches = [
        make_match("m1", index=0),
        make_match("m2", index=1),
        make_match("m3", index=2),
    ]

    assert evaluate(matches) == []


def test_duplicate_match_ids_do_not_inflate_lane_sample() -> None:
    duplicate = lower_lane_win("m1", index=0)
    matches = [
        duplicate,
        duplicate,
        tied_lane_win("m2", index=1),
        make_match("m3", win=False, index=2),
    ]

    assert evaluate(matches) == []


def test_old_titles_are_removed() -> None:
    matches = [make_match(f"m{i}", index=i) for i in range(10)]

    assert evaluate(matches) == []


def test_minimum_games_is_configurable() -> None:
    matches = [lower_lane_win("m1", index=0), make_match("m2", index=1), make_match("m3", index=2)]

    assert evaluate(matches, TitleRules(minimum_games=4)) == []
