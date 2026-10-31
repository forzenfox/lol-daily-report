"""OP.GG 数据源适配器：通过页面自身的 getGames Server Action 拉取对局。

设计要点：
- 只请求统计窗口内的数据：按配置时区（默认 Asia/Shanghai）的 00:00~24:00 归属。
- 以 `endedAt` 游标翻页，每页 20 场；一旦覆盖窗口起点立即停止。
- 不依赖 Cookie、浏览器或第三方 API。
"""

from __future__ import annotations

import gzip
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from rank import RankInfo, parse_opgg_rank

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
PAGE_SIZE = 20

REQUIRED_PATHS: dict[str, tuple[str, ...]] = {
    "id": ("id",),
    "created_at": ("created_at",),
    "game_length": ("game_length",),
    "game_result": ("game_result",),
    "queue_type": ("game_type", "game_type"),
    "champion": ("champion", "key"),
    "kills": ("stats", "kda", "kill"),
    "deaths": ("stats", "kda", "death"),
    "assists": ("stats", "kda", "assist"),
    "cs": ("stats", "cs", "totalCs"),
}


class ProviderError(RuntimeError):
    """数据源请求或协议错误。"""


class SchemaChangedError(ProviderError):
    """响应结构不符合预期（疑似页面改版）。"""


class HttpClient(Protocol):
    """HTTP 依赖注入接口，便于离线测试。"""

    def get(self, url: str) -> tuple[int, str]: ...

    def post(
        self, url: str, body: list[dict[str, Any]], headers: dict[str, str]
    ) -> tuple[int, str, str]: ...


@dataclass(frozen=True)
class ResolvedAccount:
    page_url: str
    puuid: str


@dataclass(frozen=True)
class ParticipantPerformance:
    """单局某名参战者的完整表现（用于对位对比）。"""

    summoner_name: str
    champion_key: str
    champion_name: str
    position: str | None
    team_key: str
    kills: int
    deaths: int
    assists: int
    cs: int
    gold_earned: int
    damage_to_champions: int
    damage_taken: int
    vision_score: int
    team_kills: int
    op_score: float | None
    win: bool
    team_damage_to_champions: int = 0
    team_damage_taken: int = 0


@dataclass(frozen=True)
class NormalizedMatch:
    match_id: str
    start_time: datetime
    duration_seconds: int
    win: bool
    champion_key: str
    champion_name: str
    kills: int
    deaths: int
    assists: int
    cs: int
    position: str | None
    queue_type: str
    queue_label: str
    game_map: str | None
    kda_ratio: float | None
    cs_per_min: float | None
    op_score: float | None
    kill_participation: int | None
    source: str
    fetched_at: datetime
    op_score_rank: int | None = None
    own_performance: ParticipantPerformance | None = None
    lane_opponent: ParticipantPerformance | None = None


@dataclass
class DayCollection:
    account: str
    window_start: datetime
    window_end: datetime
    matches: list[NormalizedMatch]
    complete: bool
    pages: int
    oldest_seen: datetime | None
    warnings: list[str] = field(default_factory=list)
    page_url: str = ""
    rank: RankInfo | None = None


def _nested(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = data
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _parse_flight_data(body: str) -> list[dict[str, Any]] | None:
    for line in body.splitlines():
        match = re.match(r"^[0-9a-z]+:(.*)$", line)
        if not match:
            continue
        payload = match.group(1)
        if '"data"' not in payload:
            continue
        try:
            data = json.loads(payload)["data"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if isinstance(data, list):
            return data
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """手动处理 307/308，确保 POST 方法与 body 不丢失。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibHttpClient:
    """标准库实现，带有限重试与 307 跟随。"""

    def __init__(self, timeout: float = 30.0, retries: int = 2) -> None:
        self.timeout = timeout
        self.retries = retries

    @staticmethod
    def _read(resp: http.client.HTTPResponse) -> bytes:
        try:
            raw = resp.read()
        except http.client.IncompleteRead as exc:
            raw = exc.partial
        if resp.headers.get("Content-Encoding") == "gzip" and raw:
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        return raw

    def _request(self, req: urllib.request.Request) -> tuple[int, str, str]:
        opener = urllib.request.build_opener(_NoRedirect)
        current = req
        for _ in range(4):
            try:
                with opener.open(current, timeout=self.timeout) as resp:
                    body = self._read(resp).decode("utf-8", "replace")
                    return resp.status, body, current.full_url
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 307, 308):
                    location = exc.headers.get("Location")
                    if not location:
                        raise
                    new_url = urllib.parse.urljoin(current.full_url, location)
                    current = urllib.request.Request(
                        new_url,
                        data=current.data,
                        method=current.get_method(),
                        headers=dict(current.header_items()),
                    )
                    continue
                raise
        raise ProviderError("重定向次数过多")

    def get(self, url: str) -> tuple[int, str]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "identity",
            },
        )
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                status, body, _ = self._request(request)
                return status, body
            except Exception as exc:  # noqa: BLE001 - 网络抖动重试
                last_error = exc
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        raise ProviderError(f"GET 失败: {last_error}") from last_error

    def post(
        self, url: str, body: list[dict[str, Any]], headers: dict[str, str]
    ) -> tuple[int, str, str]:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "identity",
                **headers,
            },
        )
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._request(request)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 429):
                    raise ProviderError(f"访问受限: HTTP {exc.code}") from exc
                last_error = exc
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            if attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))
        raise ProviderError(f"POST 失败: {last_error}") from last_error


class OpggProvider:
    """OP.GG 玩家页数据源。"""

    def __init__(
        self,
        http: HttpClient | None = None,
        *,
        timezone: str = "Asia/Shanghai",
        locale: str = "en",
        timeout: float = 30.0,
        max_pages: int = 10,
        action_id: str | None = None,
        champion_names: Mapping[str, str] | None = None,
    ) -> None:
        self.http: HttpClient = http or UrllibHttpClient(timeout=timeout)
        self.tz = ZoneInfo(timezone)
        self.locale = locale
        self.max_pages = max_pages
        self.action_id = action_id or os.environ.get("OPGG_GET_GAMES_ACTION_ID") or None
        self.champion_names = {key.lower(): value for key, value in (champion_names or {}).items()}

    # ------------------------------------------------------------------ 账号
    def resolve_account(self, riot_id: str, region: str = "kr") -> ResolvedAccount:
        account, _ = self._resolve(riot_id, region)
        return account

    def _resolve(self, riot_id: str, region: str) -> tuple[ResolvedAccount, str]:
        page_url = self._page_url(riot_id, region)
        status, html = self.http.get(page_url)
        if status != 200:
            raise ProviderError(f"页面返回 HTTP {status}")
        puuid = self._find_puuid(html)
        if not puuid:
            raise ProviderError("页面中未找到 puuid（页面结构可能已变化）")
        return ResolvedAccount(page_url=page_url, puuid=puuid), html

    def _page_url(self, riot_id: str, region: str) -> str:
        name, _, tag = riot_id.partition("#")
        slug = f"{urllib.parse.quote(name, safe='')}-{urllib.parse.quote(tag, safe='')}"
        return f"https://op.gg/{self.locale}/lol/summoners/{region}/{slug}"

    @staticmethod
    def _find_puuid(html: str) -> str | None:
        match = re.search(r'puuid\\?"?\s*:\s*\\?"?([A-Za-z0-9_\-]{40,120})', html)
        return match.group(1) if match else None

    # ------------------------------------------------------------------ 动作
    def _discover_action_id(self, html: str) -> str:
        if self.action_id:
            return self.action_id
        chunks = re.findall(r'https://[^"\']+/_next/static/chunks/[^"\']+\.js', html)
        for url in dict.fromkeys(chunks):
            status, script = self.http.get(url)
            if status != 200 or "getGames" not in script:
                continue
            position = script.find("getGames")
            window = script[max(0, position - 600) : position + 50]
            match = re.search(r'"([0-9a-f]{40,42})"', window)
            if match:
                self.action_id = match.group(1)
                return self.action_id
        raise ProviderError("未能发现 getGames 动作 ID")

    def _post_headers(self, page_url: str, action_id: str) -> dict[str, str]:
        return {
            "Accept": "text/x-component",
            "Content-Type": "text/plain;charset=UTF-8",
            "Accept-Language": "en-US,en;q=0.9",
            "next-action": action_id,
            "Origin": "https://op.gg",
            "Referer": page_url,
        }

    # ------------------------------------------------------------------ 采集
    def collect_day(
        self,
        riot_id: str,
        day: date | None = None,
        *,
        region: str = "kr",
    ) -> DayCollection:
        """采集指定日期（默认配置时区的今天）的对局。"""
        day = day or datetime.now(self.tz).date()
        window_start = datetime.combine(day, dt_time.min, tzinfo=self.tz)
        window_end = window_start + timedelta(days=1)

        account, html = self._resolve(riot_id, region)
        rank = parse_opgg_rank(html)
        action_id = self._discover_action_id(html)

        matches: list[NormalizedMatch] = []
        pages = 0
        cursor = ""
        oldest_seen: datetime | None = None
        exhausted = False
        hit_page_cap = False
        fetched_at = datetime.now(UTC)

        while pages < self.max_pages:
            body = [
                {
                    "locale": self.locale,
                    "region": region,
                    "puuid": account.puuid,
                    "gameType": "TOTAL",
                    "endedAt": cursor,
                    "champion": "",
                }
            ]
            status, text, _ = self.http.post(
                account.page_url, body, self._post_headers(account.page_url, action_id)
            )
            if status != 200:
                raise ProviderError(f"getGames 返回 HTTP {status}")
            batch = _parse_flight_data(text)
            if batch is None:
                raise SchemaChangedError("getGames 响应无法解析（疑似结构变更）")
            pages += 1

            for game in batch:
                match = self._to_match(game, fetched_at)
                if oldest_seen is None or match.start_time < oldest_seen:
                    oldest_seen = match.start_time
                if window_start <= match.start_time < window_end:
                    matches.append(match)

            if len(batch) < PAGE_SIZE:
                exhausted = True
                break
            if oldest_seen is not None and oldest_seen < window_start:
                break
            cursor = batch[-1]["created_at"]
        else:
            hit_page_cap = True

        warnings: list[str] = []
        complete = exhausted or (oldest_seen is not None and oldest_seen < window_start)
        if hit_page_cap and not complete:
            complete = False
            warnings.append("coverage_may_be_truncated")

        matches.sort(key=lambda item: item.start_time, reverse=True)
        return DayCollection(
            account=riot_id,
            window_start=window_start,
            window_end=window_end,
            matches=matches,
            complete=complete,
            pages=pages,
            oldest_seen=oldest_seen,
            warnings=warnings,
            page_url=account.page_url,
            rank=rank,
        )

    def _to_match(self, game: dict[str, Any], fetched_at: datetime) -> NormalizedMatch:
        missing = [label for label, path in REQUIRED_PATHS.items() if _nested(game, path) is None]
        if missing:
            raise SchemaChangedError(f"对局缺少必需字段: {', '.join(missing)}")
        if game["game_result"] not in ("WIN", "LOSE"):
            raise SchemaChangedError(f"未知胜负字段: {game['game_result']!r}")

        queue = game.get("game_type") or {}
        champion = game.get("champion") or {}
        stats = game.get("stats") or {}
        kda = stats.get("kda") or {}
        cs = stats.get("cs") or {}

        start_time = datetime.fromisoformat(game["created_at"]).astimezone(self.tz)
        champion_key = champion.get("key", "")
        champion_name = self.champion_names.get(champion_key.lower(), champion.get("name", ""))
        own_performance, lane_opponent = self._parse_lane_data(game)
        return NormalizedMatch(
            match_id=game["id"],
            start_time=start_time,
            duration_seconds=int(game["game_length"]),
            win=game["game_result"] == "WIN",
            champion_key=champion_key,
            champion_name=champion_name,
            kills=int(kda["kill"]),
            deaths=int(kda["death"]),
            assists=int(kda["assist"]),
            cs=int(cs["totalCs"]),
            position=game.get("position"),
            queue_type=queue.get("game_type", ""),
            queue_label=queue.get("game_translate", ""),
            game_map=game.get("game_map"),
            kda_ratio=kda.get("kda"),
            cs_per_min=cs.get("csPerMin"),
            op_score=stats.get("op_score"),
            kill_participation=stats.get("killParticipation"),
            source="opgg-getgames",
            fetched_at=fetched_at,
            op_score_rank=_valid_rank(stats.get("op_score_rank")),
            own_performance=own_performance,
            lane_opponent=lane_opponent,
        )

    def _parse_lane_data(
        self, game: dict[str, Any]
    ) -> tuple[ParticipantPerformance | None, ParticipantPerformance | None]:
        """从 5v5 参战者中定位本人与同位置对位；数据不全时返回 (None, None)。"""
        blue = game.get("team_blue") or []
        red = game.get("team_red") or []
        if len(blue) != 5 or len(red) != 5:
            return None, None

        team_key = str(game.get("summoner_team") or "").upper()
        teams = {"BLUE": blue, "RED": red}
        if team_key not in teams:
            return None, None
        own_team = teams[team_key]
        other_team = red if team_key == "BLUE" else blue

        participant_id = game.get("participant_id")
        own = next(
            (item for item in own_team if item.get("participant_id") == participant_id), None
        )
        if own is None:
            return None, None

        own_team_kills, own_team_damage, own_team_taken = _team_totals(own_team)
        own_performance = _to_performance(own, own_team_kills, own_team_damage, own_team_taken)
        position = own.get("position")
        if not position:
            return own_performance, None

        candidates = [item for item in other_team if item.get("position") == position]
        if len(candidates) != 1:
            return own_performance, None
        rival_kills, rival_damage, rival_taken = _team_totals(other_team)
        return own_performance, _to_performance(
            candidates[0], rival_kills, rival_damage, rival_taken
        )


def _team_totals(participants: list[dict[str, Any]]) -> tuple[int, int, int]:
    """返回队伍总击杀、总对英雄伤害、总承伤，用于占比类指标。"""
    kills = damage = damage_taken = 0
    for item in participants:
        stats = item.get("stats") or {}
        kills += int(stats.get("kill") or 0)
        damage += int(stats.get("total_damage_dealt_to_champions") or 0)
        damage_taken += int(stats.get("total_damage_taken") or 0)
    return kills, damage, damage_taken


def _valid_rank(value: Any) -> int | None:
    """竞技场等模式会返回 0，只有 1~10 才是有效的队内 OP 评分排名。"""
    if isinstance(value, int) and 1 <= value <= 10:
        return value
    return None


def _to_performance(
    participant: dict[str, Any],
    team_kills: int,
    team_damage_to_champions: int,
    team_damage_taken: int,
) -> ParticipantPerformance:
    stats = participant.get("stats") or {}
    champion = participant.get("champion") or {}
    summoner = participant.get("summoner") or {}
    champion_name = str(champion.get("name") or "")
    game_name = str(summoner.get("game_name") or summoner.get("name") or "")
    tagline = str(summoner.get("tagline") or "")
    summoner_name = f"{game_name}#{tagline}" if tagline else game_name
    return ParticipantPerformance(
        summoner_name=summoner_name,
        champion_key="".join(char for char in champion_name.lower() if char.isalnum()),
        champion_name=champion_name,
        position=participant.get("position"),
        team_key=str(participant.get("team_key") or ""),
        kills=int(stats.get("kill") or 0),
        deaths=int(stats.get("death") or 0),
        assists=int(stats.get("assist") or 0),
        cs=int(stats.get("minion_kill") or 0) + int(stats.get("neutral_minion_kill") or 0),
        gold_earned=int(stats.get("gold_earned") or 0),
        damage_to_champions=int(stats.get("total_damage_dealt_to_champions") or 0),
        damage_taken=int(stats.get("total_damage_taken") or 0),
        vision_score=int(stats.get("vision_score") or 0),
        team_kills=team_kills,
        op_score=stats.get("op_score"),
        win=stats.get("result") == "WIN",
        team_damage_to_champions=team_damage_to_champions,
        team_damage_taken=team_damage_taken,
    )
