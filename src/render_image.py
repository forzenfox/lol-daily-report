"""战报图片渲染：Pillow 绘制深色电竞风竖版战报（不依赖生成式模型）。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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


class ImageRenderError(RuntimeError):
    """图片渲染失败（如字体缺失）。"""


@dataclass(frozen=True)
class ImageOptions:
    width: int = 1080
    height: int = 1920
    background: str = COLOR_BACKGROUND
    accent: str = COLOR_ACCENT
    font_paths: tuple[str, ...] = DEFAULT_FONT_CANDIDATES
    title: str = "鱼人韩服单排日报"


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
) -> Image.Image:
    """渲染 1080x1920 竖版战报图。"""
    active = options or ImageOptions()
    scale = min(active.width / 1080, active.height / 1920)
    image = Image.new("RGB", (active.width, active.height), _hex_to_rgb(active.background))
    draw = ImageDraw.Draw(image)
    accent = _hex_to_rgb(active.accent)

    margin = 48 * scale
    inner_width = active.width - 2 * margin

    header = format_report_title(active.title, context.day)
    title_font = _fit_font(header, inner_width - 40 * scale, int(52 * scale), active, draw)
    draw.text((margin, 64 * scale), header, font=title_font, fill=COLOR_TEXT)
    sub_font = find_font(int(26 * scale), active.font_paths)
    draw.text(
        (margin, 132 * scale),
        f"{context.account} · {context.timezone_label}",
        font=sub_font,
        fill=COLOR_MUTED,
    )
    draw.line(
        [(margin, 186 * scale), (margin + inner_width, 186 * scale)],
        fill="#2A3450",
        width=max(1, int(2 * scale)),
    )

    record_top = 220 * scale
    record_height = 280 * scale
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
        metric_top = record_top + record_height + 32 * scale
        metric_height = 150 * scale
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

        radar_top = metric_top + metric_height + 40 * scale
        radar_radius = 190 * scale
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

        champion_top = radar_top + 2 * radar_radius + 70 * scale
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

    footer_top = active.height - 96 * scale
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
) -> Path:
    """渲染并保存 PNG；目录不存在时自动创建。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    image = render_report_image(stats, context, titles, options, avatars)
    image.save(target, format="PNG")
    return target
