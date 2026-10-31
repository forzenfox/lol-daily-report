"""战报图片渲染：Pillow 绘制深色电竞风竖版战报（不依赖生成式模型）。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from match_details import RADAR_LABELS, MatchDetail
from report import ReportContext
from stats import DailyStats
from titles import Title

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_FONT = REPO_ROOT / "assets" / "fonts" / "NotoSansCJKsc-Bold.otf"

# 仓库内字体优先：本地（Windows）与 CI（Linux）使用同一字体，保证渲染结果一致。
DEFAULT_FONT_CANDIDATES: tuple[str, ...] = (
    str(REPO_FONT),
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

COLOR_BACKGROUND = "#0B1020"
COLOR_CARD = "#151C33"
COLOR_ACCENT = "#C8A24A"
COLOR_TEXT = "#E6EDF3"
COLOR_MUTED = "#8B949E"
COLOR_WIN = "#3FB950"
COLOR_LOSS = "#F85149"

# 对局明细 / 对位雷达的版式常量（均为 1080x1920 基准下的逻辑尺寸，渲染时乘 scale）。
_DETAIL_SECTION_GAP = 40
_DETAIL_TITLE_HEIGHT = 58
_DETAIL_TABLE_ROW_HEIGHT = 42
_DETAIL_TABLE_HEADER_FONT = 20
_DETAIL_TABLE_FONT = 22
_DETAIL_TABLE_COLUMNS: tuple[tuple[str, float], ...] = (
    ("时间", 0.11),
    ("英雄", 0.16),
    ("结果", 0.07),
    ("K/D/A", 0.14),
    ("补刀", 0.09),
    ("排名", 0.12),
    ("对位", 0.31),
)
_DETAIL_RADAR_GAP = 36
_DETAILS_FOOTER_RESERVE = 120

# 对位雷达卡片：参照 OP.GG 对位图（左数值 + 中雷达 + 右数值）。
_RADAR_CARD_HEIGHT = 360
_RADAR_CARD_GAP = 18
_RADAR_CARD_PADDING = 22
_RADAR_CARD_TITLE_HEIGHT = 30
_RADAR_CARD_RADIUS = 100
_RADAR_AXIS_LABEL_MARGIN = 26
_RADAR_AXIS_LABEL_HALF_HEIGHT = 12
_RADAR_SIDE_WIDTH = 230
_RADAR_SIDE_ROW_HEIGHT = 32
_RADAR_SIDE_ORDER: tuple[int, ...] = (0, 5, 4, 3, 2, 1)
_RADAR_SIDE_LABELS: tuple[str, ...] = (
    "分均补刀/min",
    "伤害占比%",
    "承伤占比%",
    "参团率%",
    "伤转%",
    "KDA",
)

COLOR_LANE_SELF = "#FF5C5C"
COLOR_LANE_RIVAL = "#32C8E0"


class ImageRenderError(RuntimeError):
    """图片渲染失败（如字体缺失）。"""


@dataclass(frozen=True)
class ImageOptions:
    width: int = 1080
    height: int = 1920
    background: str = COLOR_BACKGROUND
    accent: str = COLOR_ACCENT
    font_paths: tuple[str, ...] = DEFAULT_FONT_CANDIDATES
    title: str = "鱼人韩服单双排日报"


def find_font(size: int, paths: Sequence[str] | None = None) -> ImageFont.FreeTypeFont:
    """按候选路径加载字体；全部失败时抛出 ImageRenderError。"""
    candidates = tuple(paths) if paths is not None else DEFAULT_FONT_CANDIDATES
    for path in candidates:
        if not Path(path).exists():
            continue
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    raise ImageRenderError(
        "未找到可用字体，请在系统中安装中文字体（如 Noto Sans CJK），"
        f"或通过 ImageOptions.font_paths 指定。已尝试: {list(candidates)}"
    )


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _fit_font(
    text: str,
    max_width: float,
    size: int,
    options: ImageOptions,
    draw: ImageDraw.ImageDraw,
) -> ImageFont.FreeTypeFont:
    current = size
    while current > 12:
        font = find_font(current, options.font_paths)
        if draw.textlength(text, font=font) <= max_width:
            return font
        current -= 2
    return find_font(current, options.font_paths)


def wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    draw: ImageDraw.ImageDraw,
) -> list[str]:
    """按空白分词换行，避免把 "71.4%" 这类数值拆开；超长词退化为逐字换行。"""
    tokens = re.findall(r"\S+\s*", text)
    if not tokens:
        return []

    lines: list[str] = []
    current = ""
    for token in tokens:
        candidate = current + token
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)

    result: list[str] = []
    for line in lines:
        if draw.textlength(line, font=font) <= max_width:
            result.append(line)
            continue
        buffer = ""
        for char in line:
            if buffer and draw.textlength(buffer + char, font=font) > max_width:
                result.append(buffer)
                buffer = char
            else:
                buffer += char
        if buffer:
            result.append(buffer)
    return result


def _avatar_image(path: Path, size: int) -> Image.Image | None:
    """加载并裁成圆形头像；文件缺失或损坏时返回 None。"""
    try:
        with Image.open(path) as source:
            avatar = source.convert("RGB").resize((size, size), Image.LANCZOS)
    except (OSError, ValueError):
        return None
    return avatar


def _circle_mask(size: int) -> Image.Image:
    scale = 4
    mask = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * scale - 1, size * scale - 1), fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def format_champion_kda(kda: float) -> str:
    return f"KDA {kda:.2f}"


def format_report_title(title: str, day: date) -> str:
    return f"{title} · {day.isoformat()}"


def format_report_subtitle(context: ReportContext) -> str:
    """副标题：账号 · 当前段位（可选） · 时区。"""
    parts = [context.account]
    if context.rank is not None:
        parts.append(context.rank.display())
    parts.append(context.timezone_label)
    return " · ".join(parts)


def _draw_radar(
    draw: ImageDraw.ImageDraw,
    center: tuple[float, float],
    radius: float,
    labels: Sequence[str],
    values: Sequence[float],
    options: ImageOptions,
    scale: float,
) -> None:
    count = len(labels)
    angles = [2 * math.pi * index / count - math.pi / 2 for index in range(count)]
    grid_color = "#2A3450"

    for ring in range(1, 5):
        ring_radius = radius * ring / 4
        points = [
            (
                center[0] + ring_radius * math.cos(angle),
                center[1] + ring_radius * math.sin(angle),
            )
            for angle in angles
        ]
        draw.polygon(points, outline=grid_color)

    for angle in angles:
        draw.line(
            [center, (center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle))],
            fill=grid_color,
            width=max(1, int(2 * scale)),
        )

    data_points = [
        (
            center[0] + radius * _clamp(value) * math.cos(angle),
            center[1] + radius * _clamp(value) * math.sin(angle),
        )
        for value, angle in zip(values, angles)
    ]
    accent = _hex_to_rgb(options.accent)
    card = _hex_to_rgb(COLOR_CARD)
    blended = tuple(int(accent[index] * 0.35 + card[index] * 0.65) for index in range(3))
    draw.polygon(data_points, fill=blended, outline=accent)

    label_font = find_font(int(20 * scale), options.font_paths)
    for label, angle in zip(labels, angles):
        label_x = center[0] + (radius + 26 * scale) * math.cos(angle)
        label_y = center[1] + (radius + 26 * scale) * math.sin(angle)
        draw.text(
            (label_x, label_y),
            label,
            font=label_font,
            fill=COLOR_MUTED,
            anchor="mm",
        )


@dataclass(frozen=True)
class _DetailsLayout:
    """对局明细与对位雷达的纵向排版结果（单位：像素）。"""

    title_top: float
    table_top: float
    table_bottom: float
    radar_title_top: float
    grid_top: float
    grid_bottom: float
    card_width: float
    card_height: float


def _plan_details_layout(
    content_top: float,
    details: Sequence[MatchDetail],
    inner_width: float,
    scale: float,
) -> _DetailsLayout:
    """按章节顺序推算明细表格与雷达卡片的位置，用于动态计算画布高度。"""
    title_top = content_top + _DETAIL_SECTION_GAP * scale
    table_top = title_top + _DETAIL_TITLE_HEIGHT * scale
    table_bottom = table_top + _DETAIL_TABLE_ROW_HEIGHT * scale * (len(details) + 1)
    radar_title_top = table_bottom + _DETAIL_RADAR_GAP * scale
    grid_top = radar_title_top + _DETAIL_TITLE_HEIGHT * scale
    card_height = _RADAR_CARD_HEIGHT * scale

    radar_count = sum(1 for detail in details if detail.player_radar)
    if radar_count:
        grid_bottom = (
            grid_top + radar_count * card_height + (radar_count - 1) * _RADAR_CARD_GAP * scale
        )
    else:
        grid_bottom = grid_top + 34 * scale

    return _DetailsLayout(
        title_top=title_top,
        table_top=table_top,
        table_bottom=table_bottom,
        radar_title_top=radar_title_top,
        grid_top=grid_top,
        grid_bottom=grid_bottom,
        card_width=inner_width,
        card_height=card_height,
    )


def _draw_text_fitted(
    draw: ImageDraw.ImageDraw,
    position: tuple[float, float],
    text: str,
    max_width: float,
    size: int,
    options: ImageOptions,
    fill: str,
    anchor: str = "lm",
) -> None:
    """按列宽缩放字号后绘制单行文本，避免表格与雷达标题溢出。"""
    font = find_font(size, options.font_paths)
    if draw.textlength(text, font=font) > max_width:
        font = _fit_font(text, max_width, size, options, draw)
    draw.text(position, text, font=font, fill=fill, anchor=anchor)


def _radar_points(
    center: tuple[float, float],
    radius: float,
    values: Sequence[float],
    angles: Sequence[float],
) -> list[tuple[float, float]]:
    return [
        (
            center[0] + radius * _clamp(value) * math.cos(angle),
            center[1] + radius * _clamp(value) * math.sin(angle),
        )
        for value, angle in zip(values, angles)
    ]


def _draw_dual_radar(
    draw: ImageDraw.ImageDraw,
    center: tuple[float, float],
    radius: float,
    labels: Sequence[str],
    series_a: Sequence[float],
    series_b: Sequence[float],
    options: ImageOptions,
    scale: float,
) -> None:
    """绘制账号与对位两套多边形的双雷达（参照图：红=账号、青=对位）。"""
    count = len(labels)
    angles = [2 * math.pi * index / count - math.pi / 2 for index in range(count)]
    grid_color = "#2A3450"

    for ring in range(1, 5):
        ring_radius = radius * ring / 4
        draw.polygon(
            [
                (
                    center[0] + ring_radius * math.cos(angle),
                    center[1] + ring_radius * math.sin(angle),
                )
                for angle in angles
            ],
            outline=grid_color,
        )

    for angle in angles:
        draw.line(
            [center, (center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle))],
            fill=grid_color,
            width=max(1, int(2 * scale)),
        )

    self_color = _hex_to_rgb(COLOR_LANE_SELF)
    rival_color = _hex_to_rgb(COLOR_LANE_RIVAL)
    card = _hex_to_rgb(COLOR_CARD)
    series = ((series_a, self_color, 0.18), (series_b, rival_color, 0.12))
    polygons = [_radar_points(center, radius, values, angles) for values, _c, _f in series]

    # 先画两套填充（与卡片色混合），再分别描边，保证两条轮廓都清晰。
    for points, (_values, color, strength) in zip(polygons, series):
        blended = tuple(
            int(color[channel] * strength + card[channel] * (1 - strength)) for channel in range(3)
        )
        draw.polygon(points, fill=blended)
    for points, (_values, color, _strength) in zip(polygons, series):
        draw.line(points + [points[0]], fill=color, width=max(1, int(2 * scale)))

    label_font = find_font(int(18 * scale), options.font_paths)
    for label, angle in zip(labels, angles):
        label_x = center[0] + (radius + _RADAR_AXIS_LABEL_MARGIN * scale) * math.cos(angle)
        label_y = center[1] + (radius + _RADAR_AXIS_LABEL_MARGIN * scale) * math.sin(angle)
        draw.text((label_x, label_y), label, font=label_font, fill=COLOR_MUTED, anchor="mm")


def _draw_detail_table(
    draw: ImageDraw.ImageDraw,
    details: Sequence[MatchDetail],
    layout: _DetailsLayout,
    margin: float,
    inner_width: float,
    options: ImageOptions,
    scale: float,
) -> None:
    row_height = _DETAIL_TABLE_ROW_HEIGHT * scale
    header_font = find_font(int(_DETAIL_TABLE_HEADER_FONT * scale), options.font_paths)
    row_size = int(_DETAIL_TABLE_FONT * scale)

    columns: list[tuple[str, float, float]] = []
    cursor_x = margin
    for label, fraction in _DETAIL_TABLE_COLUMNS:
        width = inner_width * fraction
        columns.append((label, cursor_x, width))
        cursor_x += width

    header_y = layout.table_top + row_height / 2
    for label, left, _width in columns:
        draw.text((left, header_y), label, font=header_font, fill=COLOR_MUTED, anchor="lm")
    draw.line(
        [
            (margin, layout.table_top + row_height),
            (margin + inner_width, layout.table_top + row_height),
        ],
        fill="#2A3450",
        width=max(1, int(scale)),
    )

    for index, detail in enumerate(details, start=1):
        match = detail.match
        opponent = detail.opponent
        row_y = layout.table_top + row_height * index + row_height / 2
        opponent_text = "—"
        if opponent is not None:
            opponent_text = (
                f"{opponent.champion_name} {opponent.kills}/{opponent.deaths}/{opponent.assists}"
            )
        values = (
            match.start_time.strftime("%H:%M"),
            match.champion_name,
            "胜" if match.win else "负",
            f"{match.kills}/{match.deaths}/{match.assists}",
            str(match.cs),
            f"{match.op_score_rank}/10" if match.op_score_rank is not None else "—",
            opponent_text,
        )
        for (label, left, width), value in zip(columns, values):
            fill = COLOR_WIN if label == "结果" and match.win else COLOR_TEXT
            if label == "结果" and not match.win:
                fill = COLOR_LOSS
            _draw_text_fitted(
                draw,
                (left, row_y),
                value,
                width - 8 * scale,
                row_size,
                options,
                fill,
            )


def _format_radar_value(label_index: int, value: float) -> str:
    # 百分比维度与参照图一致：数值不带 %，由左侧/右侧标签标注单位。
    if label_index == 1:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _draw_radar_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[float, float, float, float],
    index: int,
    detail: MatchDetail,
    options: ImageOptions,
    scale: float,
) -> None:
    """单场对位卡片：左侧账号数值、中间双雷达、右侧对位数值。"""
    left, top, right, bottom = box
    draw.rounded_rectangle(box, radius=16 * scale, fill=COLOR_CARD)
    padding = _RADAR_CARD_PADDING * scale

    opponent = detail.opponent
    opponent_name = opponent.champion_name if opponent is not None else "—"
    title = (
        f"#{index} {detail.match.start_time.strftime('%H:%M')} · "
        f"{detail.match.champion_name} vs {opponent_name}"
    )
    _draw_text_fitted(
        draw,
        (left + padding, top + padding),
        title,
        right - left - 2 * padding,
        int(22 * scale),
        options,
        COLOR_TEXT,
        anchor="la",
    )

    content_top = top + padding + _RADAR_CARD_TITLE_HEIGHT * scale + 6 * scale
    content_bottom = bottom - padding
    center = ((left + right) / 2, (content_top + content_bottom) / 2)
    _draw_dual_radar(
        draw,
        center,
        _RADAR_CARD_RADIUS * scale,
        RADAR_LABELS,
        detail.player_radar,
        detail.opponent_radar,
        options,
        scale,
    )

    rows = len(_RADAR_SIDE_ORDER)
    row_height = _RADAR_SIDE_ROW_HEIGHT * scale
    start_y = center[1] - (rows - 1) * row_height / 2
    value_font = find_font(int(22 * scale), options.font_paths)
    label_font = find_font(int(15 * scale), options.font_paths)
    left_origin = left + padding
    own_value_right = left_origin + 92 * scale
    own_label_left = left_origin + 102 * scale
    rival_label_left = right - padding - _RADAR_SIDE_WIDTH * scale
    rival_value_right = right - padding

    for row, label_index in enumerate(_RADAR_SIDE_ORDER):
        y = start_y + row * row_height
        label = _RADAR_SIDE_LABELS[row]
        own_value = _format_radar_value(label_index, detail.player_metrics[label_index])
        rival_value = _format_radar_value(label_index, detail.opponent_metrics[label_index])
        draw.text(
            (own_value_right, y),
            own_value,
            font=value_font,
            fill=COLOR_LANE_SELF,
            anchor="rm",
        )
        draw.text((own_label_left, y), label, font=label_font, fill=COLOR_MUTED, anchor="lm")
        draw.text((rival_label_left, y), label, font=label_font, fill=COLOR_MUTED, anchor="lm")
        draw.text(
            (rival_value_right, y),
            rival_value,
            font=value_font,
            fill=COLOR_LANE_RIVAL,
            anchor="rm",
        )


def _draw_radar_section(
    draw: ImageDraw.ImageDraw,
    details: Sequence[MatchDetail],
    layout: _DetailsLayout,
    margin: float,
    inner_width: float,
    options: ImageOptions,
    scale: float,
) -> None:
    section_font = find_font(int(34 * scale), options.font_paths)
    draw.text(
        (margin, layout.radar_title_top),
        "对位雷达",
        font=section_font,
        fill=COLOR_TEXT,
    )

    legend_font = find_font(int(20 * scale), options.font_paths)
    legend_y = layout.radar_title_top + 8 * scale
    account_text = "■ 账号（左）"
    rival_text = "■ 对位（右）"
    account_width = draw.textlength(account_text, font=legend_font)
    rival_width = draw.textlength(rival_text, font=legend_font)
    legend_gap = 20 * scale
    legend_left = margin + inner_width - account_width - legend_gap - rival_width
    draw.text(
        (legend_left, legend_y),
        account_text,
        font=legend_font,
        fill=COLOR_LANE_SELF,
    )
    draw.text(
        (legend_left + account_width + legend_gap, legend_y),
        rival_text,
        font=legend_font,
        fill=COLOR_LANE_RIVAL,
    )

    items = [
        (index, detail) for index, detail in enumerate(details, start=1) if detail.player_radar
    ]
    if not items:
        empty_font = find_font(int(24 * scale), options.font_paths)
        draw.text(
            (margin, layout.grid_top + 6 * scale),
            "当日无对位数据",
            font=empty_font,
            fill=COLOR_MUTED,
        )
        return

    for position, (index, detail) in enumerate(items):
        top = layout.grid_top + position * (layout.card_height + _RADAR_CARD_GAP * scale)
        _draw_radar_card(
            draw,
            (
                margin,
                top,
                margin + layout.card_width,
                top + layout.card_height,
            ),
            index,
            detail,
            options,
            scale,
        )


def _draw_metric_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[float, float, float, float],
    label: str,
    value: str,
    options: ImageOptions,
    scale: float,
) -> None:
    draw.rounded_rectangle(box, radius=18 * scale, fill=COLOR_CARD)
    label_font = find_font(int(22 * scale), options.font_paths)
    value_font = find_font(int(40 * scale), options.font_paths)
    left, top, right, bottom = box
    draw.text(
        ((left + right) / 2, top + 18 * scale),
        label,
        font=label_font,
        fill=COLOR_MUTED,
        anchor="ma",
    )
    draw.text(
        ((left + right) / 2, bottom - 20 * scale),
        value,
        font=value_font,
        fill=COLOR_TEXT,
        anchor="md",
    )


def render_report_image(
    stats: DailyStats,
    context: ReportContext,
    titles: Sequence[Title] = (),
    options: ImageOptions | None = None,
    avatars: Mapping[str, Path] | None = None,
    details: Sequence[MatchDetail] = (),
) -> Image.Image:
    """渲染 1080x1920 竖版战报图；有对局明细时按内容动态加高画布。"""
    active = options or ImageOptions()
    scale = min(active.width / 1080, active.height / 1920)

    margin = 48 * scale
    inner_width = active.width - 2 * margin

    record_top = 220 * scale
    record_height = 280 * scale
    if stats.total_games:
        metric_top = record_top + record_height + 32 * scale
        metric_height = 150 * scale
        radar_top = metric_top + metric_height + 40 * scale
        radar_radius = 190 * scale
        champion_top = radar_top + 2 * radar_radius + 70 * scale
        content_bottom = champion_top + 58 * scale + 52 * scale * len(stats.champions[:5])
    else:
        content_bottom = record_top + record_height

    layout = _plan_details_layout(content_bottom, details, inner_width, scale) if details else None
    height = active.height
    if layout is not None:
        required = layout.grid_bottom + _DETAILS_FOOTER_RESERVE * scale
        height = max(height, math.ceil(required))

    image = Image.new("RGB", (active.width, height), _hex_to_rgb(active.background))
    draw = ImageDraw.Draw(image)
    accent = _hex_to_rgb(active.accent)

    header = format_report_title(active.title, context.day)
    title_font = _fit_font(header, inner_width - 40 * scale, int(52 * scale), active, draw)
    draw.text((margin, 64 * scale), header, font=title_font, fill=COLOR_TEXT)
    subtitle = format_report_subtitle(context)
    sub_font = _fit_font(subtitle, inner_width, int(26 * scale), active, draw)
    draw.text(
        (margin, 132 * scale),
        subtitle,
        font=sub_font,
        fill=COLOR_MUTED,
    )
    draw.line(
        [(margin, 186 * scale), (margin + inner_width, 186 * scale)],
        fill="#2A3450",
        width=max(1, int(2 * scale)),
    )

    draw.rounded_rectangle(
        (margin, record_top, margin + inner_width, record_top + record_height),
        radius=24 * scale,
        fill=COLOR_CARD,
    )

    if stats.total_games == 0:
        empty_font = find_font(int(64 * scale), active.font_paths)
        draw.text(
            (active.width / 2, record_top + record_height / 2),
            "当天无对局",
            font=empty_font,
            fill=COLOR_MUTED,
            anchor="mm",
        )
    else:
        record_font = find_font(int(72 * scale), active.font_paths)
        rate_font = find_font(int(96 * scale), active.font_paths)
        draw.text(
            (margin + 48 * scale, record_top + 70 * scale),
            f"{stats.wins} 胜 {stats.losses} 负",
            font=record_font,
            fill=COLOR_TEXT,
        )
        draw.text(
            (margin + 48 * scale, record_top + 176 * scale),
            f"总场次 {stats.total_games}",
            font=sub_font,
            fill=COLOR_MUTED,
        )
        draw.text(
            (margin + inner_width - 48 * scale, record_top + 64 * scale),
            f"{stats.win_rate:.1f}%",
            font=rate_font,
            fill=accent,
            anchor="ra",
        )
        draw.text(
            (margin + inner_width - 48 * scale, record_top + 180 * scale),
            "胜率",
            font=sub_font,
            fill=COLOR_MUTED,
            anchor="ra",
        )

    if stats.total_games:
        gap = 18 * scale
        card_width = (inner_width - gap * 3) / 4
        values = [
            ("KDA", f"{stats.kda:.2f}"),
            ("场均 K/D/A", f"{stats.avg_kills:.1f}/{stats.avg_deaths:.1f}/{stats.avg_assists:.1f}"),
            (
                "场均时长",
                f"{stats.avg_duration_seconds // 60}:{stats.avg_duration_seconds % 60:02d}",
            ),
            ("场均补刀", f"{stats.avg_cs:.1f}" if stats.avg_cs is not None else "—"),
        ]
        for index, (label, value) in enumerate(values):
            left = margin + index * (card_width + gap)
            _draw_metric_card(
                draw,
                (left, metric_top, left + card_width, metric_top + metric_height),
                label,
                value,
                active,
                scale,
            )

        radar_center = (margin + 260 * scale, radar_top + radar_radius + 20 * scale)
        radar_labels = ["胜率", "KDA", "击杀", "助攻", "补刀"]
        radar_values = [
            (stats.win_rate or 0) / 100,
            (stats.kda or 0) / 5,
            (stats.avg_kills or 0) / 10,
            (stats.avg_assists or 0) / 15,
            (stats.avg_cs or 0) / 300,
        ]
        _draw_radar(draw, radar_center, radar_radius, radar_labels, radar_values, active, scale)

        section_font = find_font(int(34 * scale), active.font_paths)
        right_x = margin + 540 * scale
        draw.text((right_x, radar_top + 10 * scale), "趣味称号", font=section_font, fill=COLOR_TEXT)
        title_font_small = find_font(int(24 * scale), active.font_paths)
        cursor_y = radar_top + 66 * scale
        if titles:
            for title in titles:
                lines = wrap_text(
                    f"{title.name}：{title.reason}",
                    title_font_small,
                    inner_width - 560 * scale,
                    draw,
                )
                for line in lines:
                    draw.text((right_x, cursor_y), line, font=title_font_small, fill=accent)
                    cursor_y += 38 * scale
                cursor_y += 10 * scale
        else:
            draw.text(
                (right_x, cursor_y),
                "当日未触发称号",
                font=title_font_small,
                fill=COLOR_MUTED,
            )

        draw.text((margin, champion_top), "英雄表现", font=section_font, fill=COLOR_TEXT)
        row_font = find_font(int(26 * scale), active.font_paths)
        avatar_size = int(40 * scale)
        cursor_y = champion_top + 58 * scale
        for champion in stats.champions[:5]:
            avatar_y = cursor_y - 2 * scale
            avatar_path = avatars.get(champion.champion_key) if avatars else None
            avatar = _avatar_image(avatar_path, avatar_size) if avatar_path else None
            if avatar is not None:
                image.paste(
                    avatar,
                    (int(margin), int(avatar_y)),
                    _circle_mask(avatar_size),
                )
            else:
                draw.ellipse(
                    (margin, avatar_y, margin + avatar_size, avatar_y + avatar_size),
                    fill="#242C45",
                    outline=accent,
                )
                initial_font = find_font(int(18 * scale), active.font_paths)
                draw.text(
                    (margin + avatar_size / 2, avatar_y + avatar_size / 2),
                    champion.champion_name[:1] or "?",
                    font=initial_font,
                    fill=COLOR_TEXT,
                    anchor="mm",
                )
            draw.text(
                (margin + 52 * scale, cursor_y + 4 * scale),
                champion.champion_name,
                font=row_font,
                fill=COLOR_TEXT,
            )
            draw.text(
                (margin + 300 * scale, cursor_y + 4 * scale),
                format_champion_kda(champion.kda),
                font=row_font,
                fill=accent,
            )
            rate_color = COLOR_WIN if champion.win_rate >= 50 else COLOR_LOSS
            draw.text(
                (margin + 700 * scale, cursor_y),
                f"{champion.games} 场 · {champion.win_rate:.0f}%",
                font=row_font,
                fill=rate_color,
            )
            cursor_y += 52 * scale

    if layout is not None:
        section_font = find_font(int(34 * scale), active.font_paths)
        draw.text((margin, layout.title_top), "对局明细", font=section_font, fill=COLOR_TEXT)
        _draw_detail_table(draw, details, layout, margin, inner_width, active, scale)
        _draw_radar_section(draw, details, layout, margin, inner_width, active, scale)

    footer_top = height - 96 * scale
    footer_font = find_font(int(20 * scale), active.font_paths)
    footer = f"数据来源：{context.source_label}"
    if context.fetched_at is not None:
        footer += f" · 采集时间 {context.fetched_at.strftime('%Y-%m-%d %H:%M')}"
    if not context.complete:
        footer += " · 数据可能不完整"
    if stats.total_games and not stats.sample_sufficient:
        footer += f" · 样本不足（{stats.total_games}/{stats.minimum_games} 场）"
    draw.text((margin, footer_top), footer, font=footer_font, fill=COLOR_MUTED)

    return image


def save_report_image(
    path: str | Path,
    stats: DailyStats,
    context: ReportContext,
    titles: Sequence[Title] = (),
    options: ImageOptions | None = None,
    avatars: Mapping[str, Path] | None = None,
    details: Sequence[MatchDetail] = (),
) -> Path:
    """渲染并保存 PNG；目录不存在时自动创建。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    image = render_report_image(stats, context, titles, options, avatars, details)
    image.save(target, format="PNG")
    return target
