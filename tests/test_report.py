"""report.render_markdown / write_report 的契约测试（TDD：先于实现编写）。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from match_details import compute_match_details
from providers.opgg import NormalizedMatch, ParticipantPerformance
from rank import RankInfo
from report import ReportContext, render_markdown, write_report
from stats import compute_daily_stats
from titles import Title

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
    cs_per_min: float | None = None,
    op_score_rank: int | None = None,
    own: ParticipantPerformance | None = None,
    opponent: ParticipantPerformance | None = None,
) -> NormalizedMatch:
    return NormalizedMatch(
        match_id=match_id,
        start_time=datetime(2026, 10, 20, 20, 0, tzinfo=SHANGHAI),
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
        cs_per_min=cs_per_min,
        op_score=None,
        kill_participation=None,
        source="opgg-getgames",
        fetched_at=datetime.now(UTC),
        op_score_rank=op_score_rank,
        own_performance=own,
        lane_opponent=opponent,
    )


def make_performance(
    *,
    champion_name: str = "Darius",
    team_key: str = "RED",
    kills: int = 2,
    deaths: int = 5,
    assists: int = 3,
    cs: int = 184,
) -> ParticipantPerformance:
    return ParticipantPerformance(
        summoner_name="Opponent#KR1",
        champion_key=champion_name.lower(),
        champion_name=champion_name,
        position="TOP",
        team_key=team_key,
        kills=kills,
        deaths=deaths,
        assists=assists,
        cs=cs,
        gold_earned=8000,
        damage_to_champions=12000,
        damage_taken=10000,
        vision_score=30,
        team_kills=10,
        op_score=4.1,
        win=False,
    )


def make_context(**overrides) -> ReportContext:
    params = {
        "account": "斗鱼丶胡凯利#2862",
        "day": date(2026, 10, 20),
        "window_start": datetime(2026, 10, 20, 0, 0, tzinfo=SHANGHAI),
        "window_end": datetime(2026, 10, 21, 0, 0, tzinfo=SHANGHAI),
        "complete": True,
        "pages": 1,
        "warnings": (),
        "source_label": "OP.GG",
        "source_url": "https://op.gg/en/lol/summoners/kr/example",
        "fetched_at": datetime(2026, 10, 21, 9, 14, tzinfo=SHANGHAI),
        "timezone_label": "Asia/Shanghai",
    }
    params.update(overrides)
    return ReportContext(**params)


def test_includes_current_rank_when_provided() -> None:
    stats = compute_daily_stats([make_match("m1")], minimum_games=1)

    text = render_markdown(
        stats,
        make_context(rank=RankInfo(tier="MASTER", league_points=277)),
    )

    assert "当前段位：大师 277 胜点" in text


def test_includes_header_window_and_source() -> None:
    stats = compute_daily_stats([make_match("m1")], minimum_games=1)

    text = render_markdown(stats, make_context())

    assert "# 斗鱼丶胡凯利#2862 战报 · 2026-10-20" in text
    assert "2026-10-20 00:00 ~ 2026-10-21 00:00（Asia/Shanghai）" in text
    assert "数据来源：OP.GG" in text
    assert "https://op.gg/en/lol/summoners/kr/example" in text


def test_includes_core_metrics() -> None:
    stats = compute_daily_stats(
        [
            make_match("m1", win=True),
            make_match("m2", win=False),
            make_match("m3", win=True),
        ],
        minimum_games=1,
    )

    text = render_markdown(stats, make_context())

    assert "总场次：3" in text
    assert "2 胜 1 负" in text
    assert "胜率：66.7%" in text
    assert "KDA" in text and "4.00" in text


def test_lists_champion_rows() -> None:
    stats = compute_daily_stats(
        [
            make_match("m1", champion_key="yorick", champion_name="Yorick", win=True),
            make_match("m2", champion_key="yorick", champion_name="Yorick", win=False),
        ],
        minimum_games=1,
    )

    text = render_markdown(stats, make_context())

    assert "| 英雄 | 场次 | 胜率 | KDA | 场均 K/D/A |" in text
    assert "| Yorick | 2 | 50.0% |" in text


def test_no_games_message_without_misleading_win_rate() -> None:
    stats = compute_daily_stats([], minimum_games=3)

    text = render_markdown(stats, make_context())

    assert "当天无对局" in text
    assert "胜率：0.0%" not in text


def test_incomplete_coverage_warning() -> None:
    stats = compute_daily_stats([make_match("m1")], minimum_games=1)

    text = render_markdown(
        stats, make_context(complete=False, warnings=("coverage_may_be_truncated",))
    )

    assert "数据可能不完整" in text


def test_sample_insufficient_note() -> None:
    stats = compute_daily_stats(
        [make_match("m1"), make_match("m2")],
        minimum_games=3,
    )

    text = render_markdown(stats, make_context())

    assert "样本不足" in text


def test_renders_titles_with_reasons() -> None:
    stats = compute_daily_stats([make_match("m1"), make_match("m2"), make_match("m3")], 1)
    titles = [
        Title(
            key="carry_god",
            name="战神",
            reason="5 胜 2 负（71.4%），对位可比胜场 4 场，其中 3 场 KDA 高于对位（75.0%）",
        )
    ]

    text = render_markdown(stats, make_context(), titles=titles)

    assert "## 趣味称号" in text
    assert "战神" in text
    assert "75.0%" in text


def test_details_section_lists_row_values() -> None:
    own = make_performance(
        champion_name="Yorick", team_key="BLUE", kills=6, deaths=4, assists=1, cs=285
    )
    match = make_match(
        "m1",
        kills=6,
        deaths=4,
        assists=1,
        cs=285,
        cs_per_min=9.5,
        op_score_rank=6,
        own=own,
        opponent=make_performance(),
    )
    stats = compute_daily_stats([match], minimum_games=1)
    details = compute_match_details([match])

    text = render_markdown(stats, make_context(), details=details)

    assert "## 对局明细" in text
    assert "| # | 时间 | 英雄 | 位置 | 结果 | K/D/A | 补刀 | 分均补刀 | OP排名 | 对位 |" in text
    assert "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |" in text
    assert (
        "| 1 | 20:00 | Yorick | 上单 | 胜 | 6/4/1 | 285 | 9.5 | 6/10 | Darius 2/5/3 · 184 补刀 |"
        in text
    )


def test_details_section_between_daily_and_champion_sections() -> None:
    match = make_match("m1", own=make_performance(), opponent=make_performance())
    stats = compute_daily_stats([match], minimum_games=1)
    details = compute_match_details([match])

    text = render_markdown(stats, make_context(), details=details)

    assert text.index("## 今日战绩") < text.index("## 对局明细") < text.index("## 英雄表现")


def test_details_row_uses_dash_without_opponent_or_rank() -> None:
    match = make_match("m1", own=make_performance())
    stats = compute_daily_stats([match], minimum_games=1)
    details = compute_match_details([match])

    text = render_markdown(stats, make_context(), details=details)

    assert "| 1 | 20:00 | Yorick | 上单 | 胜 | 5/2/3 | 200 | — | — | — |" in text


def test_details_rows_numbered_and_show_loss() -> None:
    first = make_match("m1", win=True)
    second = make_match("m2", win=False, champion_name="Darius")
    stats = compute_daily_stats([first, second], minimum_games=1)
    details = compute_match_details([first, second])

    text = render_markdown(stats, make_context(), details=details)

    assert "| 1 | 20:00 | Yorick | 上单 | 胜 |" in text
    assert "| 2 | 20:00 | Darius | 上单 | 负 |" in text


def test_details_note_added_to_data_notes() -> None:
    match = make_match("m1", own=make_performance(), opponent=make_performance())
    stats = compute_daily_stats([match], minimum_games=1)
    details = compute_match_details([match])

    text = render_markdown(stats, make_context(), details=details)

    assert "## 数据说明" in text
    assert "对位按同位置、双方队伍匹配；非 5v5 或位置缺失时展示为 —。" in text


def test_empty_details_omit_details_section() -> None:
    stats = compute_daily_stats([make_match("m1")], minimum_games=1)

    text = render_markdown(stats, make_context())

    assert "## 对局明细" not in text
    assert "对位按同位置" not in text


def test_write_report_uses_utf8_and_lf(tmp_path) -> None:
    content = "# 斗鱼战报\n\n- 胜率：66.7%\n"

    path = write_report(tmp_path / "nested" / "report.md", content)

    assert path.exists()
    raw = path.read_bytes()
    assert "斗鱼战报".encode() in raw
    assert b"\r\n" not in raw
