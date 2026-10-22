"""report.render_markdown / write_report 的契约测试（TDD：先于实现编写）。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from providers.opgg import NormalizedMatch
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
        cs_per_min=None,
        op_score=None,
        kill_participation=None,
        source="opgg-getgames",
        fetched_at=datetime.now(UTC),
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
    titles = [Title(key="climbing", name="上分密码", reason="3 场 3 胜 0 负，胜率 100.0%")]

    text = render_markdown(stats, make_context(), titles=titles)

    assert "## 趣味称号" in text
    assert "上分密码" in text
    assert "100.0%" in text


def test_write_report_uses_utf8_and_lf(tmp_path) -> None:
    content = "# 斗鱼战报\n\n- 胜率：66.7%\n"

    path = write_report(tmp_path / "nested" / "report.md", content)

    assert path.exists()
    raw = path.read_bytes()
    assert "斗鱼战报".encode() in raw
    assert b"\r\n" not in raw
