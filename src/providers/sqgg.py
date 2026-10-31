"""SQGG 国服数据源适配器：登录 → 每日签到 → 战绩查询（纯 HTTP）。

设计要点：
- 访问权限来自签到，窗口约 1 小时；过期后数据接口返回 403，当日不可恢复。
- 会话为单会话：任何一次登录或用户信息刷新都会使旧令牌立即失效。
- 采集按 `startIndex` 翻页，只取单双排（queueId=420），覆盖统计窗口即停止。
- 不依赖浏览器；所有外部协议差异收敛在本模块内。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from providers.opgg import DayCollection, NormalizedMatch, ParticipantPerformance
from rank import parse_sqgg_rank

BASE_URL = "https://sqgg.cc"
PAGE_SIZE = 20
SOLO_QUEUE_ID = 420
SOLO_QUEUE_LABEL = "单双排"
SOLO_QUEUE_TYPE = "SOLORANKED"
SUMMONERS_RIFT_MAP_ID = 11
SUMMONERS_RIFT = "SUMMONERS_RIFT"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

SUMMONER_BY_NAME_PATH = "/sqgg-api/sgp-service/summoner-by-name"
LOL_MATCH_PATH = "/sqgg-api/sgp-service/lol-match"
RANK_PATH = "/sqgg-api/sgp-service/rank"

REQUIRED_JSON_KEYS = (
    "gameId",
    "gameStartTimestamp",
    "gameDuration",
    "queueId",
    "participants",
)
REQUIRED_PARTICIPANT_KEYS = ("puuid", "championName", "kills", "deaths", "assists", "teamId", "win")


class SqggError(RuntimeError):
    """SQGG 数据源基础异常。"""


class SqggRequestError(SqggError):
    """业务失败或网络错误（窗口内可重试）。"""


class SqggAuthError(SqggError):
    """令牌失效：invalid token 或账号在其它设备登录。"""


class SqggAccessExpired(SqggError):
    """签到窗口已结束或余量不足。"""


class SqggAccountNotFound(SqggError):
    """召唤师名称/大区无法解析。"""


class SqggSchemaChanged(SqggError):
    """响应结构或 Server Action 映射变化。"""


# --------------------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class ActionIds:
    """当前构建的 Server Action ID。"""

    login: str
    checkin: str
    userinfo: str


@dataclass(frozen=True)
class AccessWindow:
    """一次有效签到对应的查询窗口。"""

    token: str
    start_time: datetime
    end_time: datetime
    rewards_daily_time: datetime | None = None


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str


@dataclass(frozen=True)
class SqggSettings:
    """sqgg 相关的运行配置（凭据只存环境变量名）。"""

    email_env: str = "SQGG_EMAIL"
    password_env: str = "SQGG_PASSWORD"
    token_env: str = "SQGG_TOKEN"
    count_per_page: int = PAGE_SIZE
    max_pages: int = 5
    timeout: float = 30.0
    retries: int = 2
    reserve_seconds: int = 300
    token_cache: str = ".cache/sqgg-session.json"
    action_cache: str = ".cache/sqgg_actions.json"
    environment_cache: str = ".cache/sqgg_environments.json"


# --------------------------------------------------------------------------- 协议


class SqggHttp(Protocol):
    """HTTP 依赖注入接口，便于离线测试。"""

    def action(self, action_id: str, args: list[Any], *, page: str = "/") -> dict[str, Any]: ...

    def get_json(
        self,
        path: str,
        params: Mapping[str, str | int] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]: ...

    def get_text(self, url: str) -> str: ...


class TokenStore(Protocol):
    def load(self) -> AccessWindow | None: ...

    def save(self, window: AccessWindow) -> None: ...


# --------------------------------------------------------------------------- 工具函数


def parse_iso(value: str) -> datetime:
    """解析站点返回的 ISO 时间（含 Z 后缀）。"""
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_action_body(body: str) -> dict[str, Any]:
    """从 RSC flight 文本中提取第一行业务 JSON。"""
    for line in body.splitlines():
        match = re.match(r"^([0-9a-z]+):(.*)$", line.strip())
        if not match:
            continue
        if match.group(1) != "1":
            continue
        try:
            payload = json.loads(match.group(2))
        except json.JSONDecodeError as exc:
            raise SqggSchemaChanged(f"动作响应无法解析: {exc}") from exc
        if not isinstance(payload, dict):
            raise SqggSchemaChanged("动作响应不是对象")
        return payload
    raise SqggSchemaChanged("动作响应缺少业务数据行")


def parse_access_window(
    payload: Mapping[str, Any], *, token: str, timezone: ZoneInfo
) -> AccessWindow:
    """把登录/用户信息响应转换为本地时区的访问窗口。"""
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise SqggSchemaChanged("响应缺少 data")
    end_raw = data.get("end_time")
    if not isinstance(end_raw, str):
        raise SqggSchemaChanged("响应缺少 end_time")
    end_time = parse_iso(end_raw).astimezone(timezone)
    start_raw = data.get("start_time")
    start_time = (
        parse_iso(start_raw).astimezone(timezone)
        if isinstance(start_raw, str)
        else end_time - timedelta(hours=1)
    )
    rewards_raw = data.get("rewards_daily_time")
    rewards = parse_iso(rewards_raw).astimezone(timezone) if isinstance(rewards_raw, str) else None
    token_value = data.get("token") if isinstance(data.get("token"), str) else token
    return AccessWindow(
        token=str(token_value),
        start_time=start_time,
        end_time=end_time,
        rewards_daily_time=rewards,
    )


def build_action_headers(action_id: str, page: str = "/") -> dict[str, str]:
    referer = BASE_URL + page
    return {
        "accept": "text/x-component",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "text/plain;charset=UTF-8",
        "next-action": action_id,
        "origin": BASE_URL,
        "referer": referer,
        "user-agent": USER_AGENT,
    }


def build_json_headers(token: str | None = None, page: str = "/") -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "referer": BASE_URL + page,
        "user-agent": USER_AGENT,
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def _checked_in_today(window: AccessWindow, now: datetime) -> bool:
    rewards = window.rewards_daily_time
    return rewards is not None and rewards.astimezone(now.tzinfo).date() == now.date()


def _split_riot_id(riot_id: str) -> tuple[str, str]:
    name, _, tag = riot_id.rpartition("#")
    if not name or not tag:
        raise SqggRequestError(f"账号格式应为 名称#标签: {riot_id!r}")
    return name, tag


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


# --------------------------------------------------------------------------- HTTP


class UrllibSqggHttp:
    """标准库实现：nginx 要求 Referer；窗口内有限重试。"""

    def __init__(
        self,
        timeout: float = 30.0,
        retries: int = 2,
        deadline: Callable[[], datetime | None] | None = None,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.deadline = deadline

    def _check_deadline(self) -> None:
        if self.deadline is None:
            return
        limit = self.deadline()
        if limit is not None and datetime.now(limit.tzinfo) >= limit:
            raise SqggAccessExpired("访问窗口余量不足")

    def _raw(
        self,
        request: urllib.request.Request,
        *,
        retry_status: tuple[int, ...] = (500, 502, 503, 504),
    ) -> tuple[int, str]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._check_deadline()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.status, response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                exc.read()
                if exc.code == 401:
                    raise SqggAuthError(f"HTTP {exc.code}") from exc
                if exc.code in retry_status and attempt < self.retries:
                    last_error = SqggRequestError(f"HTTP {exc.code}")
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise SqggRequestError(f"HTTP {exc.code}") from exc
            except Exception as exc:  # noqa: BLE001 - 网络抖动重试
                last_error = exc
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        raise SqggRequestError(f"请求失败: {last_error}")

    def action(self, action_id: str, args: list[Any], *, page: str = "/") -> dict[str, Any]:
        payload = json.dumps(args, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            BASE_URL + page,
            data=payload,
            method="POST",
            headers=build_action_headers(action_id, page),
        )
        status, body = self._raw(request)
        if status != 200:
            raise SqggRequestError(f"动作请求 HTTP {status}")
        return parse_action_body(body)

    def get_json(
        self,
        path: str,
        params: Mapping[str, str | int] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(
                {key: value for key, value in params.items()}, quote_via=urllib.parse.quote
            )
        request = urllib.request.Request(
            BASE_URL + path + query, headers=build_json_headers(token, path)
        )
        status, body = self._raw(request)
        if status != 200:
            raise SqggRequestError(f"数据接口 HTTP {status}")
        stripped = body.lstrip()
        if stripped.startswith("<"):
            raise SqggRequestError("数据接口返回 HTML（可能被 WAF 拦截）")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SqggSchemaChanged(f"数据接口无法解析: {exc}") from exc
        if not isinstance(payload, dict):
            raise SqggSchemaChanged("数据接口响应不是对象")
        return payload

    def get_text(self, url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={"user-agent": USER_AGENT, "accept": "text/html,application/javascript"},
        )
        status, body = self._raw(request)
        if status != 200:
            raise SqggRequestError(f"GET {url} HTTP {status}")
        return body


# --------------------------------------------------------------------------- 动作 ID 与大区


def _parse_action_ids(script: str) -> ActionIds:
    marker = script.find("C4:function(){return")
    if marker < 0:
        raise SqggSchemaChanged("未能定位 Server Action 导出映射")
    start = script.rfind("a.d(t,{", 0, marker)
    if start < 0:
        raise SqggSchemaChanged("未能定位 Server Action 导出映射")
    block = script[start : marker + 2000]
    exports = dict(re.findall(r"(\w+):function\(\)\{return (\w+)\}", block))
    ids = dict(re.findall(r"(\w+)=\(0,s\.\$\)\(\"([0-9a-f]{40,42})\"\)", script))
    login = ids.get(exports.get("VZ", ""))
    checkin = ids.get(exports.get("C4", ""))
    userinfo = ids.get(exports.get("C0", ""))
    if not (login and checkin and userinfo):
        raise SqggSchemaChanged("未能解析登录/签到/续期动作 ID")
    return ActionIds(login=login, checkin=checkin, userinfo=userinfo)


class ActionIdResolver:
    """从 layout chunk 解析 Server Action ID，并按构建缓存。"""

    def __init__(
        self,
        http: SqggHttp,
        cache_path: str | Path | None = None,
        overrides: Mapping[str, str] | None = None,
    ) -> None:
        self.http = http
        self.cache_path = Path(cache_path) if cache_path else None
        self.overrides = dict(overrides or {})

    def resolve(self) -> ActionIds:
        html = self.http.get_text(BASE_URL + "/")
        match = re.search(r"/_next/static/chunks/app/layout-[0-9a-f]+\.js", html)
        if not match:
            raise SqggSchemaChanged("未能找到 layout chunk")
        chunk = match.group(0)
        ids = self._cached(chunk)
        if ids is None:
            ids = _parse_action_ids(self.http.get_text(BASE_URL + chunk))
            self._store(chunk, ids)
        merged = replace(
            ids,
            login=self.overrides.get("login", ids.login),
            checkin=self.overrides.get("checkin", ids.checkin),
            userinfo=self.overrides.get("userinfo", ids.userinfo),
        )
        return merged

    def _cached(self, chunk: str) -> ActionIds | None:
        if self.cache_path is None or not self.cache_path.exists():
            return None
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("chunk") != chunk or not isinstance(payload.get("actions"), dict):
            return None
        actions = payload["actions"]
        try:
            return ActionIds(
                login=actions["login"], checkin=actions["checkin"], userinfo=actions["userinfo"]
            )
        except KeyError:
            return None

    def _store(self, chunk: str, ids: ActionIds) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "chunk": chunk,
            "actions": {"login": ids.login, "checkin": ids.checkin, "userinfo": ids.userinfo},
        }
        self.cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


class EnvironmentResolver:
    """把中文大区名解析为站点环境代码（HN1/TJ100/…）。"""

    PATTERN = re.compile(r'label\\":\\"(.+?)\\",\\"value\\":\\"([A-Z0-9]+)\\"')
    FALLBACK = re.compile(r'"label":"(.+?)","value":"([A-Z0-9]+)"')

    def __init__(self, http: SqggHttp, cache_path: str | Path | None = None) -> None:
        self.http = http
        self.cache_path = Path(cache_path) if cache_path else None
        self._mapping: dict[str, str] | None = None

    def code_for(self, server: str) -> str:
        mapping = self._load_mapping()
        if server in mapping:
            return mapping[server]
        if server in set(mapping.values()):
            return server
        raise SqggRequestError(f"大区不存在: {server}")

    def _load_mapping(self) -> dict[str, str]:
        if self._mapping is not None:
            return self._mapping
        if self.cache_path is not None and self.cache_path.exists():
            try:
                cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and cached:
                    self._mapping = {str(k): str(v) for k, v in cached.items()}
                    return self._mapping
            except (OSError, json.JSONDecodeError):
                pass
        html = self.http.get_text(BASE_URL + "/")
        pairs = dict(self.PATTERN.findall(html)) or dict(self.FALLBACK.findall(html))
        if not pairs:
            raise SqggSchemaChanged("未能解析大区映射")
        self._mapping = pairs
        if self.cache_path is not None:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(pairs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        return self._mapping


# --------------------------------------------------------------------------- 会话


class FileTokenStore:
    """本地令牌缓存：原子写、仅本人可读。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> AccessWindow | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return AccessWindow(
                token=str(payload["token"]),
                start_time=parse_iso(payload["start_time"]),
                end_time=parse_iso(payload["end_time"]),
                rewards_daily_time=(
                    parse_iso(payload["rewards_daily_time"])
                    if payload.get("rewards_daily_time")
                    else None
                ),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save(self, window: AccessWindow) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "token": window.token,
            "start_time": window.start_time.isoformat(),
            "end_time": window.end_time.isoformat(),
            "rewards_daily_time": (
                window.rewards_daily_time.isoformat() if window.rewards_daily_time else None
            ),
        }
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


class SqggSession:
    """登录、签到与令牌轮换的唯一入口。"""

    def __init__(
        self,
        http: SqggHttp,
        credentials: Credentials,
        actions: ActionIds,
        store: TokenStore,
        timezone: str | ZoneInfo = "Asia/Shanghai",
        now: Callable[[], datetime] | None = None,
        boot_token: str | None = None,
    ) -> None:
        self.http = http
        self.credentials = credentials
        self.actions = actions
        self.store = store
        self.tz = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
        self._now = now or (lambda: datetime.now(self.tz))
        self.boot_token = boot_token

    def ensure_access(self) -> AccessWindow:
        now = self._now()
        window = self.store.load()
        if window is None and self.boot_token:
            window = AccessWindow(
                token=self.boot_token,
                start_time=now - timedelta(hours=1),
                end_time=now,
                rewards_daily_time=None,
            )
        if window is not None and now < window.end_time:
            return window
        if window is None:
            return self._after_login(self._login(), now)
        try:
            return self._checkin(window, now)
        except SqggAuthError:
            return self._after_login(self._login(), now)

    def _after_login(self, window: AccessWindow, now: datetime) -> AccessWindow:
        if _checked_in_today(window, now):
            if now < window.end_time:
                return window
            raise SqggAccessExpired("今日签到窗口已结束")
        return self._checkin(window, now)

    def _login(self) -> AccessWindow:
        payload = self.http.action(
            self.actions.login, [self.credentials.email, self.credentials.password]
        )
        if payload.get("status") != "Success":
            raise SqggRequestError(str(payload.get("message") or "登录失败"))
        window = parse_access_window(payload, token="", timezone=self.tz)
        self.store.save(window)
        return window

    def _checkin(self, window: AccessWindow, now: datetime) -> AccessWindow:
        payload = self.http.action(self.actions.checkin, [window.token])
        if payload.get("status") == "Success":
            data = payload.get("data")
            if not isinstance(data, Mapping) or not isinstance(data.get("end_time"), str):
                raise SqggSchemaChanged("签到响应缺少 end_time")
            end_time = parse_iso(str(data["end_time"])).astimezone(self.tz)
            updated = replace(window, end_time=end_time, rewards_daily_time=now)
            self.store.save(updated)
            if now >= end_time:
                raise SqggAccessExpired("今日签到窗口已结束")
            return updated

        code = payload.get("code")
        message = str(payload.get("message") or "")
        if code == 401:
            raise SqggAuthError(message or "invalid token")
        if message == "今日已签到":
            # 续期会签发新令牌，必须落盘后再使用。
            info = self.http.action(self.actions.userinfo, [window.token])
            if info.get("status") != "Success":
                raw_code = info.get("code")
                error = SqggAuthError if raw_code == 401 else SqggRequestError
                raise error(str(info.get("message") or "用户信息刷新失败"))
            refreshed = parse_access_window(info, token=window.token, timezone=self.tz)
            self.store.save(refreshed)
            if now >= refreshed.end_time:
                raise SqggAccessExpired("今日签到窗口已结束")
            return refreshed
        raise SqggRequestError(message or "签到失败")


# --------------------------------------------------------------------------- 采集器


class SqggProvider:
    """把 SQGG 对局转换为项目通用的 DayCollection。"""

    def __init__(
        self,
        session: SqggSession,
        http: SqggHttp,
        server: str,
        timezone: str | ZoneInfo = "Asia/Shanghai",
        *,
        environment: str | None = None,
        page_size: int = PAGE_SIZE,
        max_pages: int = 5,
        reserve_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.http = http
        self.server = server
        self.tz = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
        self.environment = environment
        self.page_size = page_size
        self.max_pages = max_pages
        self.reserve_seconds = reserve_seconds
        self._now = now or (lambda: datetime.now(self.tz))
        self.page_url = ""
        self._environment_resolver = EnvironmentResolver(http)

    def collect_day(
        self, riot_id: str, day: date | None = None, *, region: str = "cn"
    ) -> DayCollection:
        day = day or self._now().date()
        window_start = datetime.combine(day, dt_time.min, tzinfo=self.tz)
        window_end = window_start + timedelta(days=1)

        access = self.session.ensure_access()
        now = self._now()
        deadline = access.end_time - timedelta(seconds=self.reserve_seconds)
        if now >= deadline:
            raise SqggAccessExpired("访问窗口余量不足")

        game_name, tag_line = _split_riot_id(riot_id)
        environment = self.environment or self._environment_resolver.code_for(self.server)
        puuid = self._resolve_account(environment, game_name, tag_line)
        self.page_url = (
            f"{BASE_URL}/summoner/{urllib.parse.quote(self.server)}"
            f"?gameName={urllib.parse.quote(game_name)}"
            f"&tagLine={urllib.parse.quote(tag_line)}&puuid={puuid}"
        )

        rank = None
        rank_failed = False
        try:
            rank_payload = self.http.get_json(
                RANK_PATH,
                params={"environment": environment, "puuid": puuid},
                token=access.token,
            )
            _ensure_success(rank_payload)
            rank = parse_sqgg_rank(rank_payload)
        except SqggError:
            rank_failed = True

        matches: list[NormalizedMatch] = []
        warnings: list[str] = []
        pages = 0
        oldest_seen: datetime | None = None
        exhausted = partial_failed = hit_page_cap = timed_out = False
        start_index = 0
        fetched_at = datetime.now(UTC)

        while pages < self.max_pages:
            if self._now() >= deadline:
                warnings.append("time_budget_exhausted")
                timed_out = True
                break
            try:
                payload = self.http.get_json(
                    LOL_MATCH_PATH,
                    params={
                        "environment": environment,
                        "puuid": puuid,
                        "count": self.page_size,
                        "startIndex": start_index,
                        "tag": f"q_{SOLO_QUEUE_ID}",
                    },
                    token=access.token,
                )
            except SqggRequestError:
                if not matches:
                    raise
                warnings.append("partial_page_failure")
                partial_failed = True
                break
            _ensure_success(payload)
            data = payload.get("data")
            games = data.get("games") if isinstance(data, Mapping) else None
            if not isinstance(games, list):
                raise SqggSchemaChanged("对局响应缺少 games 列表")
            if not games:
                exhausted = True
                break
            pages += 1
            for game in games:
                info = game.get("json")
                if not isinstance(info, Mapping):
                    raise SqggSchemaChanged("对局缺少 json 字段")
                start_time = _start_time(info, self.tz)
                if oldest_seen is None or start_time < oldest_seen:
                    oldest_seen = start_time
                if int(info.get("queueId") or 0) != SOLO_QUEUE_ID:
                    continue
                if window_start <= start_time < window_end:
                    matches.append(self._to_match(game, puuid, fetched_at, start_time))
            if len(games) < self.page_size or (
                oldest_seen is not None and oldest_seen < window_start
            ):
                exhausted = True
                break
            start_index += len(games)
        else:
            hit_page_cap = True

        if hit_page_cap:
            warnings.append("coverage_may_be_truncated")
        if rank_failed:
            warnings.append("rank_unavailable")
        reached_end = exhausted or (oldest_seen is not None and oldest_seen < window_start)
        matches.sort(key=lambda item: item.start_time, reverse=True)
        return DayCollection(
            account=riot_id,
            window_start=window_start,
            window_end=window_end,
            matches=matches,
            complete=reached_end and not partial_failed and not timed_out,
            pages=pages,
            oldest_seen=oldest_seen,
            warnings=warnings,
            page_url=self.page_url,
            rank=rank,
        )

    def _resolve_account(self, environment: str, game_name: str, tag_line: str) -> str:
        payload = self.http.get_json(
            SUMMONER_BY_NAME_PATH,
            params={"environment": environment, "gameName": game_name, "tagLine": tag_line},
        )
        data = payload.get("data")
        if not isinstance(data, Mapping) or not data.get("puuid"):
            message = str(payload.get("message") or "未找到该召唤师")
            raise SqggAccountNotFound(message)
        return str(data["puuid"])

    def _to_match(
        self,
        game: Mapping[str, Any],
        puuid: str,
        fetched_at: datetime,
        start_time: datetime,
    ) -> NormalizedMatch:
        info = game.get("json")
        if not isinstance(info, Mapping):
            raise SqggSchemaChanged("对局缺少 json 字段")
        missing = [key for key in REQUIRED_JSON_KEYS if info.get(key) is None]
        if missing:
            raise SqggSchemaChanged(f"对局缺少必需字段: {', '.join(missing)}")

        participants = info["participants"]
        if not isinstance(participants, list) or not participants:
            raise SqggSchemaChanged("对局缺少参战者")
        own = next((item for item in participants if item.get("puuid") == puuid), None)
        if own is None:
            raise SqggSchemaChanged("未找到本人在该局的参战数据")
        missing_participant = [key for key in REQUIRED_PARTICIPANT_KEYS if own.get(key) is None]
        if missing_participant:
            raise SqggSchemaChanged(f"参战者缺少必需字段: {', '.join(missing_participant)}")

        horse = game.get("horse") if isinstance(game.get("horse"), Mapping) else {}
        team_id = int(own["teamId"])
        team = [item for item in participants if item.get("teamId") == team_id]
        enemy = [item for item in participants if item.get("teamId") != team_id]
        team_kills = sum(int(item.get("kills") or 0) for item in team)
        team_damage = sum(int(item.get("totalDamageDealtToChampions") or 0) for item in team)
        team_taken = sum(int(item.get("totalDamageTaken") or 0) for item in team)

        position = _position_of(own)
        lane_opponent = None
        if position:
            candidates = [item for item in enemy if _position_of(item) == position]
            if len(candidates) == 1:
                rival = candidates[0]
                rival_team = [
                    item for item in participants if item.get("teamId") == rival["teamId"]
                ]
                rival_kills = sum(int(item.get("kills") or 0) for item in rival_team)
                rival_damage = sum(
                    int(item.get("totalDamageDealtToChampions") or 0) for item in rival_team
                )
                rival_taken = sum(int(item.get("totalDamageTaken") or 0) for item in rival_team)
                lane_opponent = _to_performance(
                    rival, rival_kills, rival_damage, rival_taken, horse
                )

        own_performance = _to_performance(own, team_kills, team_damage, team_taken, horse)
        champion_name = str(own["championName"])
        kills = int(own["kills"])
        deaths = int(own["deaths"])
        assists = int(own["assists"])
        cs = int(own.get("totalMinionsKilled") or 0) + int(own.get("neutralMinionsKilled") or 0)
        duration = int(info["gameDuration"])
        minutes = duration / 60 if duration else 0
        own_score = _safe_float((horse.get(puuid) or {}).get("score"))
        metadata = game.get("metadata") if isinstance(game.get("metadata"), Mapping) else {}
        match_id = metadata.get("match_id") or f"{info.get('platformId', 'HN1')}_{info['gameId']}"

        return NormalizedMatch(
            match_id=str(match_id),
            start_time=start_time,
            duration_seconds=duration,
            win=bool(own["win"]),
            champion_key=_champion_key(champion_name),
            champion_name=champion_name,
            kills=kills,
            deaths=deaths,
            assists=assists,
            cs=cs,
            position=position,
            queue_type=SOLO_QUEUE_TYPE,
            queue_label=SOLO_QUEUE_LABEL,
            game_map=SUMMONERS_RIFT
            if int(info.get("mapId") or 0) == SUMMONERS_RIFT_MAP_ID
            else None,
            kda_ratio=round((kills + assists) / max(deaths, 1), 2),
            cs_per_min=round(cs / minutes, 2) if minutes else None,
            op_score=own_score,
            kill_participation=(
                round((kills + assists) / team_kills * 100) if team_kills else None
            ),
            source="sqgg",
            fetched_at=fetched_at,
            op_score_rank=_op_score_rank(horse, puuid),
            own_performance=own_performance,
            lane_opponent=lane_opponent,
        )


def _start_time(info: Mapping[str, Any], tz: ZoneInfo) -> datetime:
    """把毫秒时间戳换算为本地时区；缺失时视为结构变更。"""
    raw = info.get("gameStartTimestamp")
    if raw is None:
        raise SqggSchemaChanged("对局缺少 gameStartTimestamp")
    return datetime.fromtimestamp(int(raw) / 1000, tz)


def _ensure_success(payload: Mapping[str, Any]) -> None:
    if payload.get("status") == "Success":
        return
    code = payload.get("code")
    message = str(payload.get("message") or "接口失败")
    if code == 401:
        raise SqggAuthError(message)
    if code == 403:
        raise SqggAccessExpired(message)
    raise SqggRequestError(message)


def _position_of(participant: Mapping[str, Any]) -> str | None:
    value = participant.get("teamPosition") or participant.get("individualPosition")
    if not isinstance(value, str) or not value or value == "Invalid":
        return None
    return value


def _champion_key(champion_name: str) -> str:
    return "".join(char for char in champion_name.lower() if char.isalnum())


def _op_score_rank(horse: Mapping[str, Any], puuid: str) -> int | None:
    own = _safe_float((horse.get(puuid) or {}).get("score"))
    if own is None:
        return None
    scores = [
        score
        for key, value in horse.items()
        if key != "__all__"
        and isinstance(value, Mapping)
        and (score := _safe_float(value.get("score")))
    ]
    if not scores:
        return None
    rank = 1 + sum(1 for score in scores if score > own)
    return rank if 1 <= rank <= 10 else None


def _to_performance(
    participant: Mapping[str, Any],
    team_kills: int,
    team_damage_to_champions: int,
    team_damage_taken: int,
    horse: Mapping[str, Any],
) -> ParticipantPerformance:
    game_name = str(participant.get("riotIdGameName") or participant.get("summonerName") or "")
    tag_line = str(participant.get("riotIdTagline") or "")
    summoner_name = f"{game_name}#{tag_line}" if tag_line else game_name
    champion_name = str(participant.get("championName") or "")
    team_id = participant.get("teamId")
    return ParticipantPerformance(
        summoner_name=summoner_name,
        champion_key=_champion_key(champion_name),
        champion_name=champion_name,
        position=_position_of(participant),
        team_key="BLUE" if team_id == 100 else "RED",
        kills=int(participant.get("kills") or 0),
        deaths=int(participant.get("deaths") or 0),
        assists=int(participant.get("assists") or 0),
        cs=int(participant.get("totalMinionsKilled") or 0)
        + int(participant.get("neutralMinionsKilled") or 0),
        gold_earned=int(participant.get("goldEarned") or 0),
        damage_to_champions=int(participant.get("totalDamageDealtToChampions") or 0),
        damage_taken=int(participant.get("totalDamageTaken") or 0),
        vision_score=int(participant.get("visionScore") or 0),
        team_kills=team_kills,
        op_score=_safe_float((horse.get(participant.get("puuid")) or {}).get("score")),
        win=bool(participant.get("win")),
        team_damage_to_champions=team_damage_to_champions,
        team_damage_taken=team_damage_taken,
    )


@dataclass(frozen=True)
class SqggRuntime:
    """一次运行内共享的 HTTP 与会话（多个账号共用同一签到窗口）。"""

    http: SqggHttp
    session: SqggSession


def build_runtime(settings: SqggSettings, timezone: str) -> SqggRuntime:
    """装配 HTTP、动作 ID 与凭据，返回可复用的运行时。"""
    http = UrllibSqggHttp(timeout=settings.timeout, retries=settings.retries)
    actions = _resolve_actions(http, settings)
    email = os.environ.get(settings.email_env)
    password = os.environ.get(settings.password_env)
    if not email or not password:
        raise SqggRequestError(f"缺少环境变量 {settings.email_env}/{settings.password_env}")
    session = SqggSession(
        http=http,
        credentials=Credentials(email, password),
        actions=actions,
        store=FileTokenStore(settings.token_cache),
        timezone=timezone,
        boot_token=os.environ.get(settings.token_env) or None,
    )
    return SqggRuntime(http=http, session=session)


def build_provider(
    settings: SqggSettings,
    server: str,
    timezone: str,
    environment: str | None = None,
    runtime: SqggRuntime | None = None,
) -> SqggProvider:
    """按配置装配 HTTP、动作 ID、会话与采集器。"""
    active = runtime or build_runtime(settings, timezone)
    return SqggProvider(
        session=active.session,
        http=active.http,
        server=server,
        timezone=timezone,
        environment=environment,
        page_size=settings.count_per_page,
        max_pages=settings.max_pages,
        reserve_seconds=settings.reserve_seconds,
    )


def _resolve_actions(http: SqggHttp, settings: SqggSettings) -> ActionIds:
    """优先使用环境变量固定的动作 ID，其次自动发现。"""
    raw = os.environ.get("SQGG_ACTION_IDS")
    if raw:
        try:
            payload = json.loads(raw)
            return ActionIds(
                login=str(payload["login"]),
                checkin=str(payload["checkin"]),
                userinfo=str(payload["userinfo"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise SqggSchemaChanged("SQGG_ACTION_IDS 解析失败") from exc
    overrides = {
        key: value
        for key, value in {
            "login": os.environ.get("SQGG_LOGIN_ACTION", ""),
            "checkin": os.environ.get("SQGG_CHECKIN_ACTION", ""),
            "userinfo": os.environ.get("SQGG_USERINFO_ACTION", ""),
        }.items()
        if value
    }
    return ActionIdResolver(http, cache_path=settings.action_cache, overrides=overrides).resolve()
