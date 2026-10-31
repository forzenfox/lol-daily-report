"""段位解析与展示规则测试。"""

from __future__ import annotations

from rank import RankInfo, parse_opgg_rank, parse_sqgg_rank


def sqgg_payload(tier: str, rank: str, lp: int, queue: str = "RANKED_SOLO_5x5") -> dict:
    return {
        "status": "Success",
        "data": {
            "queues": [
                {
                    "queueType": "RANKED_TFT",
                    "tier": "DIAMOND",
                    "rank": "IV",
                    "leaguePoints": 0,
                },
                {
                    "queueType": queue,
                    "tier": tier,
                    "rank": rank,
                    "leaguePoints": lp,
                    "wins": 218,
                    "losses": 206,
                },
            ]
        },
    }


def test_sqgg_master_shows_league_points() -> None:
    rank = parse_sqgg_rank(sqgg_payload("MASTER", "I", 277))

    assert rank is not None
    assert rank.tier == "MASTER"
    assert rank.division is None
    assert rank.league_points == 277
    assert rank.display() == "大师 277 胜点"
    assert rank.wins == 218
    assert rank.losses == 206


def test_sqgg_diamond_shows_division_only() -> None:
    rank = parse_sqgg_rank(sqgg_payload("DIAMOND", "II", 75))

    assert rank is not None
    assert rank.division == "2"
    assert rank.display() == "钻石 2"


def test_sqgg_returns_none_without_solo_queue() -> None:
    payload = sqgg_payload("MASTER", "I", 277, queue="RANKED_FLEX_SR")

    assert parse_sqgg_rank(payload) is None


def test_sqgg_returns_none_for_invalid_payload() -> None:
    assert parse_sqgg_rank({}) is None
    assert parse_sqgg_rank({"data": {"queues": "bad"}}) is None


def test_opgg_parses_rank_from_json_ld_description() -> None:
    html = (
        '<meta name="description" content="斗鱼丶胡凯利#2862 is a League of Legends summoner '
        "on the KR server. 斗鱼丶胡凯利#2862's current SOLORANKED rank is master Division 1 466 LP "
        'with 587 wins, 571 losses, and a 51% win rate."/>'
    )

    rank = parse_opgg_rank(html)

    assert rank is not None
    assert rank.tier == "MASTER"
    assert rank.division is None
    assert rank.league_points == 466
    assert rank.display() == "大师 466 胜点"


def test_opgg_parses_division_for_lower_tier() -> None:
    html = (
        'content="current SOLORANKED rank is diamond Division 2 75 LP '
        'with 120 wins, 110 losses, and a 52% win rate."'
    )

    rank = parse_opgg_rank(html)

    assert rank is not None
    assert rank.division == "2"
    assert rank.display() == "钻石 2"


def test_opgg_falls_back_to_header_dom() -> None:
    html = (
        '<div class="flex flex-col gap-0.5"><strong class="text-xl">宗师</strong>'
        '<span class="text-xs text-gray-500">812<!-- --> LP</span></div>'
    )

    rank = parse_opgg_rank(html)

    assert rank is not None
    assert rank.tier == "GRANDMASTER"
    assert rank.display() == "宗师 812 胜点"


def test_opgg_returns_none_when_unranked() -> None:
    assert parse_opgg_rank("<html>no rank here</html>") is None


def test_rank_label_covers_all_tiers() -> None:
    expected = {
        "IRON": "黑铁",
        "BRONZE": "青铜",
        "SILVER": "白银",
        "GOLD": "黄金",
        "PLATINUM": "铂金",
        "EMERALD": "翡翠",
        "DIAMOND": "钻石",
        "MASTER": "大师",
        "GRANDMASTER": "宗师",
        "CHALLENGER": "王者",
    }

    assert {tier: RankInfo(tier).label for tier in expected} == expected
