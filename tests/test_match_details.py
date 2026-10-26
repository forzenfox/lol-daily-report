"""match_details 统计模块的契约测试（TDD：先于实现编写）。"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from match_details import (
    RADAR_LABELS,
    RADAR_REFERENCES,
    build_match_detail,
    compute_match_details,
    match_metrics,
    normalize_pair,
    opponent_metrics,
)
from providers.opgg import NormalizedMatch, ParticipantPerformance

SHANGHAI = ZoneInfo("Asia/Shanghai")


def performance(
    *,
    champion_name: str = "Darius",
    kills: int = 2,
    deaths: int = 5,
    assists: int = 3,
    cs: int = 184,
    gold: int = 8000,
    damage: int = 12000,
    damage_taken: int = 10000,
    vision: int = 30,
    team_kills: int = 10,
    team_damage: int = 80000,
    team_damage_taken: int = 70000,
) -> ParticipantPerformance:
    return ParticipantPerformance(
        summoner_name="Opponent#KR1",
        champion_key=champion_name.lower(),
        champion_name=champion_name,
        position="TOP",
        team_key="RED",
        kills=kills,
        deaths=deaths,
        assists=assists,
        cs=cs,
        gold_earned=gold,
        damage_to_champions=damage,
        damage_taken=damage_taken,
        vision_score=vision,
        team_kills=team_kills,
        team_damage_to_champions=team_damage,
        team_damage_taken=team_damage_taken,
        op_score=4.1,
        win=False,
    )


def make_match(
    match_id: str = "m1",
    *,
    own: ParticipantPerformance | None = None,
    opponent: ParticipantPerformance | None = None,
) -> NormalizedMatch:
    return NormalizedMatch(
        match_id=match_id,
        start_time=datetime(2026, 10, 20, 20, 0, tzinfo=SHANGHAI),
        duration_seconds=1800,
        win=True,
        champion_key="yorick",
        champion_name="Yorick",
        kills=6,
        deaths=4,
        assists=1,
        cs=285,
        position="TOP",
        queue_type="SOLORANKED",
        queue_label="Ranked Solo/Duo",
        game_map="SUMMONERS_RIFT",
        kda_ratio=1.75,
        cs_per_min=9.9,
        op_score=4.8,
        kill_participation=50,
        source="opgg-getgames",
        fetched_at=datetime.now(UTC),
        op_score_rank=6,
        own_performance=own,
        lane_opponent=opponent,
    )


def own_performance() -> ParticipantPerformance:
    return ParticipantPerformance(
        summoner_name="斗鱼丶胡凯利#2862",
        champion_key="yorick",
        champion_name="Yorick",
        position="TOP",
        team_key="BLUE",
        kills=6,
        deaths=4,
        assists=1,
        cs=285,
        gold_earned=9000,
        damage_to_champions=15000,
        damage_taken=14000,
        vision_score=20,
        team_kills=14,
        team_damage_to_champions=60000,
        team_damage_taken=70000,
        op_score=4.8,
        win=True,
    )


def test_radar_labels_and_references_cover_six_dimensions() -> None:
    assert RADAR_LABELS == ("分均补刀", "KDA", "伤转", "参团率", "承伤占比", "伤害占比")
    assert len(RADAR_REFERENCES) == len(RADAR_LABELS)
    assert all(reference > 0 for reference in RADAR_REFERENCES)


def test_match_metrics_use_share_and_conversion_formulas() -> None:
    metrics = match_metrics(own_performance(), 1800)

    assert metrics == pytest.approx((9.5, 1.75, 15000 / 9000 * 100, 50.0, 20.0, 25.0))


def test_match_metrics_returns_empty_without_performance() -> None:
    assert match_metrics(None, 1800) == ()


def test_match_metrics_handles_zero_team_totals() -> None:
    zero_team = ParticipantPerformance(
        summoner_name="x",
        champion_key="x",
        champion_name="X",
        position="TOP",
        team_key="BLUE",
        kills=0,
        deaths=1,
        assists=0,
        cs=0,
        gold_earned=0,
        damage_to_champions=0,
        damage_taken=0,
        vision_score=0,
        team_kills=0,
        team_damage_to_champions=0,
        team_damage_taken=0,
        op_score=None,
        win=False,
    )

    metrics = match_metrics(zero_team, 1800)

    assert metrics == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_opponent_metrics_reads_lane_opponent() -> None:
    match = make_match(own=own_performance(), opponent=performance())

    assert opponent_metrics(match) == pytest.approx(
        (184 / 30, 1.0, 150.0, 50.0, 10000 / 70000 * 100, 15.0)
    )


def test_normalize_pair_uses_fixed_reference_per_dimension() -> None:
    player = (9.5, 1.75, 15000 / 9000 * 100, 50.0, 20.0, 25.0)
    opponent = (184 / 30, 1.0, 150.0, 50.0, 10000 / 70000 * 100, 15.0)

    normalized_player, normalized_opponent = normalize_pair(player, opponent)

    assert normalized_player == pytest.approx(
        (
            9.5 / 10,
            1.75 / 12,
            (15000 / 9000 * 100) / 200,
            50.0 / 80,
            20.0 / 50,
            25.0 / 30,
        )
    )
    assert normalized_opponent == pytest.approx(
        (
            (184 / 30) / 10,
            1.0 / 12,
            150.0 / 200,
            50.0 / 80,
            (10000 / 70000 * 100) / 50,
            15.0 / 30,
        )
    )
    assert all(0.0 <= value <= 1.0 for value in normalized_player + normalized_opponent)


def test_normalize_pair_clamps_values_above_reference() -> None:
    player, opponent = normalize_pair((20.0, 30.0), (0.0, 0.0))

    assert player == (1.0, 1.0)
    assert opponent == (0.0, 0.0)


def test_detail_without_opponent_has_empty_radar() -> None:
    detail = build_match_detail(make_match(own=own_performance()))

    assert detail.opponent is None
    assert detail.player_radar == ()
    assert detail.opponent_radar == ()


def test_detail_with_opponent_builds_two_radar_series() -> None:
    detail = build_match_detail(make_match(own=own_performance(), opponent=performance()))

    assert detail.opponent is not None
    assert len(detail.player_radar) == len(RADAR_LABELS)
    assert len(detail.opponent_radar) == len(RADAR_LABELS)
    assert len(detail.player_metrics) == len(RADAR_LABELS)


def test_compute_match_details_deduplicates_by_match_id() -> None:
    first = make_match("m1", own=own_performance(), opponent=performance())
    duplicate = make_match("m1", own=own_performance(), opponent=performance())
    second = make_match("m2", own=own_performance())

    details = compute_match_details([first, duplicate, second])

    assert [detail.match.match_id for detail in details] == ["m1", "m2"]
