"""战报文案渲染与文件输出（Markdown）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from match_details import MatchDetail, position_label
from stats import DailyStats
from titles import Title

WARNING_LABELS = {
    "coverage_may_be_truncated": "翻页达到上限，数据可能不完整",
    "single_day_density_high": "当日对局密度异常，建议人工复核",
}


@dataclass(frozen=True)
class ReportContext:
    """渲染所需的采集上下文，与数据源解耦。"""

    account: str
    day: date
    window_start: datetime
    window_end: datetime
    complete: bool
    pages: int = 0
    warnings: tuple[str, ...] = ()
    source_label: str = "OP.GG"
    source_url: str = ""
    fetched_at: datetime | None = None
    timezone_label: str = "Asia/Shanghai"


def _fmt_duration(seconds: int) -> str:
    minutes, remainder = divmod(max(seconds, 0), 60)
    return f"{minutes} 分 {remainder} 秒"


def _fmt_window(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def _format_notes(
    stats: DailyStats, context: ReportContext, *, has_details: bool = False
) -> list[str]:
    notes: list[str] = []
    if stats.total_games and not stats.sample_sufficient:
        notes.append(
            f"- 样本不足：当日 {stats.total_games} 场，少于 {stats.minimum_games} 场，"
            "统计与评价仅供参考。"
        )
    if not context.complete:
        notes.append("- 数据可能不完整：未能确认覆盖当日完整窗口。")
    for warning in context.warnings:
        label = WARNING_LABELS.get(warning, warning)
        notes.append(f"- 采集告警：{label}。")
    if context.source_url:
        notes.append(f"- 数据来源链接：{context.source_url}")
    if has_details:
        notes.append("- 对位按同位置、双方队伍匹配；非 5v5 或位置缺失时展示为 —。")
    notes.append(f"- 数据来源：{context.source_label}，采集后经人工审核再发布。")
    return notes


def _fmt_cs_per_min(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "—"


def _fmt_lane_opponent(detail: MatchDetail) -> str:
    opponent = detail.opponent
    if opponent is None:
        return "—"
    return (
        f"{opponent.champion_name} {opponent.kills}/{opponent.deaths}/{opponent.assists}"
        f" · {opponent.cs} 补刀"
    )


def _format_details(details: Sequence[MatchDetail]) -> list[str]:
    lines: list[str] = [
        "## 对局明细",
        "",
        "| # | 时间 | 英雄 | 位置 | 结果 | K/D/A | 补刀 | 分均补刀 | OP排名 | 对位 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, detail in enumerate(details, start=1):
        match = detail.match
        rank = f"{match.op_score_rank}/10" if match.op_score_rank is not None else "—"
        lines.append(
            f"| {index} | {match.start_time.strftime('%H:%M')} | {match.champion_name} | "
            f"{position_label(match.position)} | {'胜' if match.win else '负'} | "
            f"{match.kills}/{match.deaths}/{match.assists} | {match.cs} | "
            f"{_fmt_cs_per_min(match.cs_per_min)} | {rank} | {_fmt_lane_opponent(detail)} |"
        )
    lines.append("")
    return lines


def render_markdown(
    stats: DailyStats,
    context: ReportContext,
    titles: list[Title] | tuple[Title, ...] = (),
    details: Sequence[MatchDetail] = (),
) -> str:
    """渲染 Markdown 战报；无对局时明确说明，不输出误导性指标。"""
    window_label = (
        f"{_fmt_window(context.window_start)} ~ {_fmt_window(context.window_end)}"
        f"（{context.timezone_label}）"
    )
    lines: list[str] = [
        f"# {context.account} 战报 · {context.day.isoformat()}",
        "",
        f"- 账号：{context.account}",
        f"- 统计窗口：{window_label}",
        f"- 数据来源：{context.source_label}",
    ]
    if context.fetched_at is not None:
        lines.append(f"- 采集时间：{context.fetched_at.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"- 数据完整性：{'完整' if context.complete else '可能不完整'}")
    lines.append("")

    if stats.total_games == 0:
        lines.extend(["## 今日战绩", "", "当天无对局。", ""])
    else:
        lines.extend(
            [
                "## 今日战绩",
                "",
                f"- 总场次：{stats.total_games}",
                f"- 胜负：{stats.wins} 胜 {stats.losses} 负",
                f"- 胜率：{stats.win_rate:.1f}%",
                f"- KDA（(击杀+助攻)/死亡）：{stats.kda:.2f}",
                f"- 场均：{stats.avg_kills:.1f} / {stats.avg_deaths:.1f} / {stats.avg_assists:.1f}",
                f"- 场均时长：{_fmt_duration(stats.avg_duration_seconds)}",
            ]
        )
        if stats.avg_cs is not None:
            cs_minute = f"{stats.cs_per_min:.1f}" if stats.cs_per_min is not None else "—"
            lines.append(f"- 场均补刀：{stats.avg_cs:.1f}（{cs_minute}/分钟）")
        lines.append("")

    if details:
        lines.extend(_format_details(details))

    if stats.champions:
        lines.extend(
            [
                "## 英雄表现",
                "",
                "| 英雄 | 场次 | 胜率 | KDA | 场均 K/D/A |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for champion in stats.champions:
            lines.append(
                f"| {champion.champion_name} | {champion.games} | "
                f"{champion.win_rate:.1f}% | {champion.kda:.2f} | "
                f"{champion.avg_kills:.1f} / {champion.avg_deaths:.1f} / "
                f"{champion.avg_assists:.1f} |"
            )
        lines.append("")

    if titles:
        lines.extend(["## 趣味称号", ""])
        for title in titles:
            lines.append(f"- **{title.name}**：{title.reason}")
        lines.append("")

    lines.extend(["## 数据说明", ""])
    lines.extend(_format_notes(stats, context, has_details=bool(details)))
    lines.append("")
    return "\n".join(lines)


def write_report(path: str | Path, content: str) -> Path:
    """以 UTF-8 + LF 写文件，保证 Windows 开发与 Linux CI 产物一致。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")
    return target
