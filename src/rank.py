"""段位模型与数据源解析（国服 SQGG / 韩服 OP.GG）。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

TIER_LABELS: dict[str, str] = {
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

ADVANCED_TIERS = frozenset({"MASTER", "GRANDMASTER", "CHALLENGER"})

_DIVISION_NUMBERS = {"I": "1", "II": "2", "III": "3", "IV": "4"}
_LABEL_TO_TIER = {label: tier for tier, label in TIER_LABELS.items()}

_OPGG_DESCRIPTION = re.compile(
    r"current\s+SOLORANKED\s+rank\s+is\s+(\w+)\s+Division\s+(\d+)\s+([\d,]+)\s+LP\s+"
    r"with\s+([\d,]+)\s+wins,\s*([\d,]+)\s+losses",
    re.IGNORECASE,
)
_OPGG_HEADER = re.compile(
    r'<strong class="text-xl">([^<]+)</strong>.{0,200}?'
    r"<span[^>]*>\s*([\d,]+)<!-- -->\s*LP</span>",
    re.DOTALL,
)


@dataclass(frozen=True)
class RankInfo:
    """单双排当前段位；大师及以上不显示小段位。"""

    tier: str
    division: str | None = None
    league_points: int | None = None
    wins: int | None = None
    losses: int | None = None

    @property
    def label(self) -> str:
        return TIER_LABELS.get(self.tier, self.tier)

    def display(self) -> str:
        """大师/宗师/王者展示胜点，其余段位展示小段位。"""
        if self.tier in ADVANCED_TIERS:
            if self.league_points is None:
                return self.label
            return f"{self.label} {self.league_points} 胜点"
        if self.division:
            return f"{self.label} {self.division}"
        return self.label


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.replace(",", ""))
        except ValueError:
            return None
    return None


def _normalize_division(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value) if 1 <= value <= 4 else None
    text = str(value or "").strip().upper()
    if text in _DIVISION_NUMBERS:
        return _DIVISION_NUMBERS[text]
    if text in {"1", "2", "3", "4"}:
        return text
    return None


def parse_sqgg_rank(payload: Mapping[str, Any]) -> RankInfo | None:
    """解析 SQGG rank 接口的单双排队列。"""
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    queues = data.get("queues")
    if not isinstance(queues, list):
        return None
    for item in queues:
        if not isinstance(item, Mapping) or item.get("queueType") != "RANKED_SOLO_5x5":
            continue
        tier = str(item.get("tier") or "").upper()
        if tier not in TIER_LABELS:
            return None
        division = None if tier in ADVANCED_TIERS else _normalize_division(item.get("rank"))
        return RankInfo(
            tier=tier,
            division=division,
            league_points=_to_int(item.get("leaguePoints")),
            wins=_to_int(item.get("wins")),
            losses=_to_int(item.get("losses")),
        )
    return None


def parse_opgg_rank(html: str) -> RankInfo | None:
    """从 OP.GG 召唤师页解析当前单双排段位。

    优先读取 JSON-LD 描述（含 SOLORANKED 当前段位），
    其次回退到页面头部的段位/胜点展示。
    """
    match = _OPGG_DESCRIPTION.search(html)
    if match:
        tier = match.group(1).upper()
        if tier in TIER_LABELS:
            division = None if tier in ADVANCED_TIERS else _normalize_division(match.group(2))
            return RankInfo(
                tier=tier,
                division=division,
                league_points=_to_int(match.group(3)),
                wins=_to_int(match.group(4)),
                losses=_to_int(match.group(5)),
            )

    match = _OPGG_HEADER.search(html)
    if match:
        parsed = _parse_chinese_label(match.group(1).strip())
        if parsed is not None:
            tier, division = parsed
            return RankInfo(
                tier=tier,
                division=None if tier in ADVANCED_TIERS else division,
                league_points=_to_int(match.group(2)),
            )
    return None


def _parse_chinese_label(text: str) -> tuple[str, str | None] | None:
    """把「大师」/「钻石 2」这类页面文案转换为段位与小段位。"""
    match = re.fullmatch(r"(黑铁|青铜|白银|黄金|铂金|翡翠|钻石|大师|宗师|王者)\s*(\d)?", text)
    if not match:
        return None
    return _LABEL_TO_TIER[match.group(1)], match.group(2)
