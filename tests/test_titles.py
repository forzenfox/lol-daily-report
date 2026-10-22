"""称号规则引擎的契约测试（TDD：先于实现编写）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from providers.opgg import NormalizedMatch
from stats import compute_daily_stats
from titles import Title, TitleRules, evaluate_titles

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
    index: int = 0,
) -> NormalizedMatch:
    return NormalizedMatch(
        match_id=match_id,
        start_time=datetime(2026, 10, 20, 12, 0, tzinfo=SHANGHAI) + timedelta(minutes=30 * index),
        duration_seconds=1800,
        win=win,
        champion_key=champion_key,
        champion_name=champion_name,
        kills=kills,
        deaths=deaths,
        assists=assists,
        cs=200,
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


def evaluate(matches: list[NormalizedMatch], rules: TitleRules | None = None) -> list[Title]:
    stats = compute_daily_stats(matches, minimum_games=1)
    return evaluate_titles(stats, matches, rules)


def keys(titles: list[Title]) -> list[str]:
    return [title.key for title in titles]


def test_empty_day_has_no_titles() -> None:
    assert evaluate([]) == []


def test_insufficient_sample_never_triggers_titles() -> None:
    matches = [make_match("m1", win=True), make_match("m2", win=True)]

    assert evaluate(matches) == []


def test_climbing_triggers_at_threshold() -> None:
    matches = [
        make_match("m1", win=True),
        make_match("m2", win=True),
        make_match("m3", win=True),
        make_match("m4", win=False),
        make_match("m5", win=False),
    ]

    titles = evaluate(matches, TitleRules(climbing_win_rate=60.0))

    assert "climbing" in keys(titles)
    climbing = next(title for title in titles if title.key == "climbing")
    assert isinstance(climbing, Title)
    assert "3 胜 2 负" in climbing.reason
    assert "60.0%" in climbing.reason


def test_climbing_does_not_trigger_below_threshold() -> None:
    matches = [
        make_match("m1", win=True),
        make_match("m2", win=False),
        make_match("m3", win=False),
    ]

    assert "climbing" not in keys(evaluate(matches))


def test_philanthropist_triggers_on_high_average_deaths() -> None:
    matches = [
        make_match("m1", deaths=7),
        make_match("m2", deaths=6),
        make_match("m3", deaths=5),
    ]

    titles = evaluate(matches, TitleRules(philanthropist_avg_deaths=6.0))

    assert "philanthropist" in keys(titles)
    assert "6.0" in next(title for title in titles if title.key == "philanthropist").reason


def test_rollercoaster_requires_alternating_results() -> None:
    alternating = [make_match(f"m{i}", win=bool(i % 2 == 0), index=i) for i in range(6)]
    streaky = [make_match(f"s{i}", win=i < 3, index=i) for i in range(6)]

    assert "rollercoaster" in keys(evaluate(alternating))
    assert "rollercoaster" not in keys(evaluate(streaky))


def test_workhorse_uses_actual_game_count_only() -> None:
    ten = [make_match(f"m{i}", index=i) for i in range(10)]
    nine = [make_match(f"n{i}", index=i) for i in range(9)]

    assert "workhorse" in keys(evaluate(ten))
    assert "workhorse" not in keys(evaluate(nine))


def test_specialist_requires_multiple_games_on_same_champion() -> None:
    three_on_yorick = [
        make_match("m1", champion_key="yorick", champion_name="Yorick", win=True),
        make_match("m2", champion_key="yorick", champion_name="Yorick", win=True),
        make_match("m3", champion_key="yorick", champion_name="Yorick", win=False),
    ]
    split = [
        make_match("a1", champion_key="yorick", champion_name="Yorick"),
        make_match("a2", champion_key="gnar", champion_name="Gnar"),
        make_match("a3", champion_key="sion", champion_name="Sion"),
    ]

    assert "specialist" in keys(evaluate(three_on_yorick))
    assert "specialist" not in keys(evaluate(split))
    specialist = next(title for title in evaluate(three_on_yorick) if title.key == "specialist")
    assert "Yorick" in specialist.name or "Yorick" in specialist.reason
    assert "3 场" in specialist.reason


def test_titles_are_ordered_and_have_evidence_based_reasons() -> None:
    matches = [
        make_match(f"m{i}", win=i < 7, index=i, champion_key=f"champ{i}", champion_name=f"Champ{i}")
        for i in range(10)
    ]

    titles = evaluate(matches)

    assert keys(titles) == ["climbing", "workhorse"]
    assert all(title.reason for title in titles)
