"""render_image 的契约测试（TDD：先于实现编写）。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageDraw

from match_details import compute_match_details
from providers.opgg import NormalizedMatch, ParticipantPerformance
from rank import RankInfo
from render_image import (
    DEFAULT_FONT_CANDIDATES,
    ImageOptions,
    ImageRenderError,
    _format_radar_value,
    find_font,
    format_champion_kda,
    format_report_subtitle,
    format_report_title,
    render_report_image,
    save_report_image,
    wrap_text,
)
from report import ReportContext
from stats import compute_daily_stats
from titles import Title

SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_performance(
    *,
    champion_name: str = "Darius",
    kills: int = 2,
    deaths: int = 5,
    assists: int = 3,
    cs: int = 184,
    gold: int = 8000,
    damage: int = 12000,
    vision: int = 30,
    team_kills: int = 10,
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
        damage_taken=10000,
        vision_score=vision,
        team_kills=team_kills,
        team_damage_to_champions=80000,
        team_damage_taken=70000,
        op_score=4.1,
        win=False,
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


def make_match(
    match_id: str,
    *,
    win: bool = True,
    champion_key: str = "yorick",
    champion_name: str = "Yorick",
    index: int = 0,
    op_score_rank: int | None = None,
    own: ParticipantPerformance | None = None,
    opponent: ParticipantPerformance | None = None,
) -> NormalizedMatch:
    return NormalizedMatch(
        match_id=match_id,
        start_time=datetime(2026, 10, 20, 20, 0, tzinfo=SHANGHAI) + timedelta(minutes=30 * index),
        duration_seconds=1698,
        win=win,
        champion_key=champion_key,
        champion_name=champion_name,
        kills=6,
        deaths=4,
        assists=1,
        cs=279,
        position="TOP",
        queue_type="SOLORANKED",
        queue_label="Ranked Solo/Duo",
        game_map="SUMMONERS_RIFT",
        kda_ratio=1.75,
        cs_per_min=9.9,
        op_score=4.8,
        kill_participation=21,
        source="opgg-getgames",
        fetched_at=datetime.now(UTC),
        op_score_rank=op_score_rank,
        own_performance=own,
        lane_opponent=opponent,
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


def small_options(**overrides) -> ImageOptions:
    params = {"width": 540, "height": 960}
    params.update(overrides)
    return ImageOptions(**params)


def available_font() -> str | None:
    for candidate in DEFAULT_FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def test_repository_font_is_the_first_candidate() -> None:
    """仓库内 Noto 字体应作为首选，保证本地与 CI 渲染一致。"""
    assert "assets" in DEFAULT_FONT_CANDIDATES[0]
    assert Path(DEFAULT_FONT_CANDIDATES[0]).exists()


def test_find_font_prefers_repository_font() -> None:
    repo_font = DEFAULT_FONT_CANDIDATES[0]
    if not Path(repo_font).exists():
        pytest.skip("仓库字体未入库")

    font = find_font(24)
    assert Path(font.path).resolve() == Path(repo_font).resolve()


def test_find_font_raises_when_no_candidate_exists(tmp_path: Path) -> None:
    with pytest.raises(ImageRenderError):
        find_font(24, paths=[str(tmp_path / "missing.ttf")])


def test_find_font_loads_available_font() -> None:
    font_path = available_font()
    if font_path is None:
        pytest.skip("测试环境没有可用字体")

    font = find_font(24, paths=[font_path])

    assert font.size == 24


def test_render_returns_configured_size_and_mode() -> None:
    stats = compute_daily_stats([make_match("m1")], minimum_games=1)

    image = render_report_image(stats, make_context(), options=small_options())

    assert isinstance(image, Image.Image)
    assert image.size == (540, 960)
    assert image.mode == "RGB"


def test_render_is_not_blank() -> None:
    stats = compute_daily_stats([make_match("m1"), make_match("m2", win=False)], minimum_games=1)

    image = render_report_image(stats, make_context(), options=small_options())
    colors = image.getcolors(maxcolors=1_000_000)

    assert colors is not None
    assert len(colors) > 20


def test_render_with_no_games_does_not_crash() -> None:
    stats = compute_daily_stats([], minimum_games=3)

    image = render_report_image(stats, make_context(complete=False), options=small_options())

    assert image.size == (540, 960)


def test_render_handles_long_account_and_many_champions() -> None:
    matches = [
        make_match(
            f"m{i}",
            champion_key=f"champ{i}",
            champion_name=f"Champion{i}",
            win=i % 2 == 0,
        )
        for i in range(12)
    ]
    stats = compute_daily_stats(matches, minimum_games=1)

    image = render_report_image(
        stats,
        make_context(account="非常长的斗鱼主播账号名称测试#99999"),
        options=small_options(),
    )

    assert image.size == (540, 960)


def test_render_with_titles_does_not_crash() -> None:
    stats = compute_daily_stats([make_match("m1")], minimum_games=1)
    titles = [
        Title(
            key="carry_god",
            name="战神",
            reason="5 胜 2 负（71.4%），对位可比胜场 4 场，其中 3 场 KDA 高于对位（75.0%）",
        )
    ]

    image = render_report_image(stats, make_context(), titles=titles, options=small_options())

    assert image.size == (540, 960)


def test_save_report_image_writes_png(tmp_path: Path) -> None:
    stats = compute_daily_stats([make_match("m1")], minimum_games=1)

    path = save_report_image(
        tmp_path / "nested" / "report.png",
        stats,
        make_context(),
        options=small_options(),
    )

    assert path.exists()
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.size == (540, 960)


def test_render_uses_champion_avatar_when_provided(tmp_path: Path) -> None:
    avatar_path = tmp_path / "avatar.png"
    Image.new("RGB", (64, 64), (220, 40, 40)).save(avatar_path)
    stats = compute_daily_stats([make_match("m1")], minimum_games=1)

    plain = render_report_image(stats, make_context(), options=small_options())
    with_avatar = render_report_image(
        stats, make_context(), options=small_options(), avatars={"yorick": avatar_path}
    )

    assert plain.tobytes() != with_avatar.tobytes()


def test_missing_avatar_path_falls_back_without_crash(tmp_path: Path) -> None:
    stats = compute_daily_stats([make_match("m1")], minimum_games=1)

    image = render_report_image(
        stats,
        make_context(),
        options=small_options(),
        avatars={"yorick": tmp_path / "missing.png"},
    )

    assert image.size == (540, 960)


def test_format_champion_kda_uses_two_decimals() -> None:
    assert format_champion_kda(1.75) == "KDA 1.75"
    assert format_champion_kda(0.4) == "KDA 0.40"


def test_format_report_subtitle_includes_rank() -> None:
    context = make_context(rank=RankInfo(tier="MASTER", league_points=277))

    assert format_report_subtitle(context) == "斗鱼丶胡凯利#2862 · 大师 277 胜点 · Asia/Shanghai"


def test_format_report_subtitle_without_rank() -> None:
    assert format_report_subtitle(make_context()) == "斗鱼丶胡凯利#2862 · Asia/Shanghai"


def test_format_report_title_appends_day() -> None:
    assert (
        format_report_title("鱼人韩服单双排日报", date(2026, 10, 20))
        == "鱼人韩服单双排日报 · 2026-10-20"
    )


def test_wrap_text_keeps_percentage_token_intact() -> None:
    font_path = available_font()
    if font_path is None:
        pytest.skip("测试环境没有可用字体")
    font = find_font(24, [font_path])
    canvas = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(canvas)
    text = "战神：7 场 5 胜 2 负，胜率 71.4%"

    lines = wrap_text(text, font, 200, draw)

    assert "".join(lines) == text
    assert any(line.endswith("71.4%") for line in lines)
    assert all("%" not in line or line.endswith("71.4%") for line in lines)


def test_wrap_text_falls_back_to_character_wrap_for_long_words() -> None:
    font_path = available_font()
    if font_path is None:
        pytest.skip("测试环境没有可用字体")
    font = find_font(24, [font_path])
    canvas = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(canvas)
    text = "非常长的斗鱼主播账号名称测试"

    lines = wrap_text(text, font, 100, draw)

    assert "".join(lines) == text
    assert len(lines) > 1


def test_missing_font_raises_render_error(tmp_path: Path) -> None:
    stats = compute_daily_stats([make_match("m1")], minimum_games=1)
    options = small_options(font_paths=(str(tmp_path / "missing.ttf"),))

    with pytest.raises(ImageRenderError):
        render_report_image(stats, make_context(), options=options)


def test_render_with_details_grows_canvas_and_draws_below_base() -> None:
    matches = [
        make_match(
            "m1",
            own=own_performance(),
            opponent=make_performance(champion_name="Darius"),
            op_score_rank=6,
        ),
        make_match(
            "m2",
            win=False,
            index=1,
            own=own_performance(),
            opponent=make_performance(champion_name="Garen", kills=3, deaths=2),
            op_score_rank=8,
        ),
    ]
    details = compute_match_details(matches)
    stats = compute_daily_stats(matches, minimum_games=1)

    image = render_report_image(stats, make_context(), options=small_options(), details=details)

    assert image.width == 540
    assert image.height > 960
    below_base = image.crop((0, 960, image.width, image.height))
    colors = below_base.getcolors(maxcolors=1_000_000)
    assert colors is not None
    assert len(colors) > 1


def test_render_with_empty_details_keeps_configured_size() -> None:
    stats = compute_daily_stats([make_match("m1")], minimum_games=1)

    image = render_report_image(stats, make_context(), options=small_options(), details=())

    assert image.size == (540, 960)


def test_render_details_without_lane_opponent_does_not_crash() -> None:
    matches = [
        make_match(
            f"m{index}",
            champion_key=f"champ{index}",
            champion_name=f"Champion{index}",
            index=index,
            own=own_performance(),
        )
        for index in range(5)
    ]
    details = compute_match_details(matches)
    stats = compute_daily_stats(matches, minimum_games=1)

    image = render_report_image(stats, make_context(), options=small_options(), details=details)

    assert image.width == 540
    assert image.height > 960
    below_base = image.crop((0, 960, image.width, image.height))
    colors = below_base.getcolors(maxcolors=1_000_000)
    assert colors is not None
    assert len(colors) > 1


def test_save_report_image_writes_png_with_details(tmp_path: Path) -> None:
    matches = [
        make_match("m1", own=own_performance(), opponent=make_performance(), op_score_rank=6)
    ]
    details = compute_match_details(matches)
    stats = compute_daily_stats(matches, minimum_games=1)

    path = save_report_image(
        tmp_path / "details" / "report.png",
        stats,
        make_context(),
        options=small_options(),
        details=details,
    )

    assert path.exists()
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.width == 540
        assert image.height > 960


def test_format_radar_value_keeps_unit_on_label_only() -> None:
    # 参照图：数值只保留数字，% 或 /min 由左右两侧的标签承载。
    assert _format_radar_value(0, 8.27) == "8.27"
    assert _format_radar_value(1, 11.0) == "11.0"
    assert _format_radar_value(2, 156.59) == "156.59"
    assert _format_radar_value(5, 21.59) == "21.59"


def test_each_lane_radar_takes_its_own_full_width_row() -> None:
    first = make_match(
        "m1", own=own_performance(), opponent=make_performance(champion_name="Darius")
    )
    second = make_match(
        "m2",
        index=1,
        own=own_performance(),
        opponent=make_performance(champion_name="Garen"),
    )
    context = make_context()

    one = render_report_image(
        compute_daily_stats([first], minimum_games=1),
        context,
        options=small_options(),
        details=compute_match_details([first]),
    )
    two = render_report_image(
        compute_daily_stats([first, second], minimum_games=1),
        context,
        options=small_options(),
        details=compute_match_details([first, second]),
    )

    # 参照图的单场布局（左数值 + 中雷达 + 右数值）独占一行，第二场必须继续加高。
    assert two.height > one.height
