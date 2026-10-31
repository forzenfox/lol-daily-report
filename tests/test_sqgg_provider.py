"""SqggProvider / SqggSession 契约测试（TDD：先于实现编写，全部离线）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from providers.opgg import DayCollection
from providers.sqgg import (
    AccessWindow,
    ActionIdResolver,
    ActionIds,
    Credentials,
    EnvironmentResolver,
    SqggAccessExpired,
    SqggAccountNotFound,
    SqggProvider,
    SqggRequestError,
    SqggSchemaChanged,
    SqggSession,
    build_action_headers,
    build_json_headers,
    parse_access_window,
    parse_action_body,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 30, 6, 0, tzinfo=SHANGHAI)

PUUID = "4e009383-921f-54b2-82ec-5cddeaf0ec34"
SERVER = "艾欧尼亚"
ENVIRONMENT = "HN1"
TOKEN = "token-v1"
NEW_TOKEN = "token-v2"
ACTION_IDS = ActionIds(login="login-id", checkin="checkin-id", userinfo="userinfo-id")


def rsc(*payloads: dict) -> str:
    lines = ['0:["$@1",["_QQxpUYei0oawWN9Rx2He",null]]']
    lines.append("1:" + json_dumps(payloads[0]))
    return "\n".join(lines)


def json_dumps(value: dict) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def user_payload(
    *,
    token: str = TOKEN,
    end_time: str = "2026-10-30T00:00:00.000Z",
    start_time: str = "2026-10-29T23:00:00.000Z",
    rewards_daily_time: str | None = "2026-10-29T21:50:46.219Z",
    status: str = "Success",
    message: str = "登录成功",
) -> dict:
    data = {
        "email": "a***@qq.com",
        "status": 1,
        "token": token,
        "start_time": start_time,
        "end_time": end_time,
    }
    if rewards_daily_time is not None:
        data["rewards_daily_time"] = rewards_daily_time
    return {"status": status, "message": message, "data": data}


class FakeHttp:
    """可编排响应与请求记录的 HTTP 替身。"""

    def __init__(self) -> None:
        self.action_queue: dict[str, list[object]] = {}
        self.json_queue: dict[str, list[object]] = {}
        self.texts: dict[str, str] = {}
        self.calls: list[tuple] = []

    def queue_action(self, action_id: str, *responses: object) -> None:
        self.action_queue.setdefault(action_id, []).extend(responses)

    def queue_json(self, path: str, *responses: object) -> None:
        self.json_queue.setdefault(path, []).extend(responses)

    @staticmethod
    def _take(queue: list[object]) -> object:
        assert queue, "没有可用的预置响应"
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    def action(self, action_id: str, args: list[object], *, page: str = "/") -> dict:
        self.calls.append(("action", action_id, list(args)))
        return self._take(self.action_queue[action_id])

    def get_json(self, path: str, params=None, token: str | None = None) -> dict:
        self.calls.append(("json", path, dict(params or {}), token))
        return self._take(self.json_queue[path])

    def get_text(self, url: str) -> str:
        self.calls.append(("text", url))
        return self.texts[url]


class MemoryStore:
    def __init__(self, value: AccessWindow | None = None) -> None:
        self.value = value
        self.saves: list[AccessWindow] = []

    def load(self) -> AccessWindow | None:
        return self.value

    def save(self, window: AccessWindow) -> None:
        self.value = window
        self.saves.append(window)


class FakeSession:
    def __init__(self, window: AccessWindow) -> None:
        self.window = window
        self.calls = 0

    def ensure_access(self) -> AccessWindow:
        self.calls += 1
        return self.window


def make_window(
    *,
    token: str = TOKEN,
    end_time: datetime | None = None,
    rewards: datetime | None = NOW,
) -> AccessWindow:
    return AccessWindow(
        token=token,
        start_time=end_time - timedelta(hours=1) if end_time else NOW,
        end_time=end_time or NOW + timedelta(hours=1),
        rewards_daily_time=rewards,
    )


def make_session(
    http: FakeHttp,
    store: MemoryStore,
    *,
    now: datetime = NOW,
    credentials: Credentials | None = None,
) -> SqggSession:
    return SqggSession(
        http=http,
        credentials=credentials or Credentials("a@b.c", "pwd"),
        actions=ACTION_IDS,
        store=store,
        timezone="Asia/Shanghai",
        now=lambda: now,
    )


# --------------------------------------------------------------------------- 会话


def test_cached_window_skips_all_requests() -> None:
    http = FakeHttp()
    store = MemoryStore(make_window(end_time=NOW + timedelta(minutes=30)))

    window = make_session(http, store).ensure_access()

    assert window.token == TOKEN
    assert http.calls == []


def test_login_then_checkin_when_not_checked_in_today() -> None:
    http = FakeHttp()
    http.queue_action(
        ACTION_IDS.login,
        user_payload(
            rewards_daily_time="2026-10-28T21:50:46.219Z", end_time="2026-10-29T22:50:46.000Z"
        ),
    )
    http.queue_action(
        ACTION_IDS.checkin,
        {
            "status": "Success",
            "message": "签到成功",
            "data": {"end_time": "2026-10-30T00:00:00.000Z"},
        },
    )
    store = MemoryStore()

    window = make_session(http, store).ensure_access()

    assert [call[0] for call in http.calls] == ["action", "action"]
    assert http.calls[0][1] == ACTION_IDS.login
    assert http.calls[1][1] == ACTION_IDS.checkin
    assert window.end_time == datetime(2026, 10, 30, 8, 0, tzinfo=SHANGHAI)
    assert store.value is not None and store.value.end_time == window.end_time


def test_login_today_already_checked_in_skips_checkin() -> None:
    http = FakeHttp()
    http.queue_action(
        ACTION_IDS.login,
        user_payload(
            rewards_daily_time="2026-10-29T21:50:46.219Z",
            end_time="2026-10-30T00:00:00.000Z",
        ),
    )
    store = MemoryStore()

    window = make_session(http, store).ensure_access()

    assert len(http.calls) == 1
    assert window.end_time == datetime(2026, 10, 30, 8, 0, tzinfo=SHANGHAI)


def test_checkin_success_updates_expired_window() -> None:
    http = FakeHttp()
    http.queue_action(
        ACTION_IDS.checkin,
        {
            "status": "Success",
            "message": "签到成功",
            "data": {"end_time": "2026-10-30T00:00:00.000Z"},
        },
    )
    store = MemoryStore(make_window(end_time=NOW - timedelta(minutes=5)))

    window = make_session(http, store).ensure_access()

    assert window.token == TOKEN
    assert window.end_time == datetime(2026, 10, 30, 8, 0, tzinfo=SHANGHAI)
    assert store.saves[-1].end_time == window.end_time


def test_already_checked_in_rotates_token_via_userinfo() -> None:
    http = FakeHttp()
    http.queue_action(ACTION_IDS.checkin, {"status": "Fail", "message": "今日已签到", "data": None})
    http.queue_action(
        ACTION_IDS.userinfo,
        user_payload(token=NEW_TOKEN, end_time="2026-10-30T00:00:00.000Z"),
    )
    store = MemoryStore(make_window(token=TOKEN, end_time=NOW - timedelta(minutes=5)))

    window = make_session(http, store).ensure_access()

    assert window.token == NEW_TOKEN
    assert store.value is not None and store.value.token == NEW_TOKEN
    assert [call[1] for call in http.calls] == [ACTION_IDS.checkin, ACTION_IDS.userinfo]


def test_expired_after_already_checked_in_raises() -> None:
    http = FakeHttp()
    http.queue_action(ACTION_IDS.checkin, {"status": "Fail", "message": "今日已签到", "data": None})
    http.queue_action(
        ACTION_IDS.userinfo,
        user_payload(token=NEW_TOKEN, end_time="2026-10-29T22:00:00.000Z"),
    )
    store = MemoryStore(make_window(end_time=NOW - timedelta(minutes=5)))

    with pytest.raises(SqggAccessExpired, match="窗口"):
        make_session(http, store).ensure_access()


def test_auth_error_relogs_in_once() -> None:
    http = FakeHttp()
    http.queue_action(
        ACTION_IDS.checkin,
        {"status": "Fail", "message": "invalid token", "data": None, "code": 401},
    )
    http.queue_action(
        ACTION_IDS.login,
        user_payload(
            rewards_daily_time="2026-10-30T01:00:00.000Z", end_time="2026-10-30T02:00:00.000Z"
        ),
    )
    store = MemoryStore(make_window(end_time=NOW - timedelta(minutes=5)))

    window = make_session(http, store).ensure_access()

    assert window.token == TOKEN
    assert [call[1] for call in http.calls] == [ACTION_IDS.checkin, ACTION_IDS.login]


def test_login_failure_raises_request_error() -> None:
    http = FakeHttp()
    http.queue_action(
        ACTION_IDS.login,
        {"status": "Fail", "message": "邮箱或密码错误", "data": None},
    )

    with pytest.raises(SqggRequestError, match="邮箱或密码错误"):
        make_session(http, MemoryStore()).ensure_access()


def test_short_window_rejected_by_reserve() -> None:
    http = FakeHttp()
    store = MemoryStore(make_window(end_time=NOW + timedelta(minutes=2)))
    session = make_session(http, store)

    provider = SqggProvider(
        session=session, http=http, server=SERVER, environment=ENVIRONMENT, now=lambda: NOW
    )

    with pytest.raises(SqggAccessExpired, match="余量不足"):
        provider.collect_day("热心市民张合#65360", date(2026, 10, 30))


# --------------------------------------------------------------------------- 解析


def test_parse_action_body_extracts_first_payload() -> None:
    body = rsc({"status": "Success", "message": "ok", "data": {"token": TOKEN}})

    assert parse_action_body(body)["data"]["token"] == TOKEN


def test_parse_access_window_converts_to_local_time() -> None:
    window = parse_access_window(
        user_payload(end_time="2026-10-30T00:00:00.000Z"), token=TOKEN, timezone=SHANGHAI
    )

    assert window.end_time == datetime(2026, 10, 30, 8, 0, tzinfo=SHANGHAI)
    assert window.rewards_daily_time == datetime(2026, 10, 30, 5, 50, 46, 219000, tzinfo=SHANGHAI)


def test_headers_include_referer_and_action_id() -> None:
    action_headers = build_action_headers("id-1")
    json_headers = build_json_headers(TOKEN)

    assert action_headers["next-action"] == "id-1"
    assert action_headers["referer"].startswith("https://sqgg.cc/")
    assert json_headers["referer"].startswith("https://sqgg.cc/")
    assert json_headers["authorization"] == f"Bearer {TOKEN}"


def test_action_id_resolver_parses_layout_chunk() -> None:
    http = FakeHttp()
    http.texts["https://sqgg.cc/"] = (
        '<script src="/_next/static/chunks/app/layout-1a66c18b3af3ac2a.js"></script>'
    )
    http.texts["https://sqgg.cc/_next/static/chunks/app/layout-1a66c18b3af3ac2a.js"] = (
        "a.d(t,{default:function(){return W}});"
        "a.d(t,{BK:function(){return v},C0:function(){return u},C4:function(){return d},"
        "VZ:function(){return n},vl:function(){return c}});"
        'var n=(0,s.$)("bc7bfa13b6c407306dbb20eed4c1b296cd454b00"),'
        'd=(0,s.$)("ca18bf21f3544b0d8dd37c2d7c3cf8ffca1c01c5"),'
        'u=(0,s.$)("e47f3b49dd5957b577d85abb533382e09fe6ee24"),'
        'c=(0,s.$)("79a0cbd7a61fb6d5ae5e187360b5e4bb45733d12");'
    )

    ids = ActionIdResolver(http).resolve()

    assert ids.login == "bc7bfa13b6c407306dbb20eed4c1b296cd454b00"
    assert ids.checkin == "ca18bf21f3544b0d8dd37c2d7c3cf8ffca1c01c5"
    assert ids.userinfo == "e47f3b49dd5957b577d85abb533382e09fe6ee24"


def test_environment_resolver_reads_home_payload() -> None:
    http = FakeHttp()
    http.texts["https://sqgg.cc/"] = (
        'environments\\":[{\\"label\\":\\"艾欧尼亚\\",\\"value\\":\\"HN1\\",\\"id\\":\\"x\\"},'
        '{\\"label\\":\\"黑色玫瑰\\",\\"value\\":\\"HN10\\",\\"id\\":\\"y\\"}]'
    )

    resolver = EnvironmentResolver(http)

    assert resolver.code_for(SERVER) == "HN1"
    assert resolver.code_for("黑色玫瑰") == "HN10"
    with pytest.raises(SqggRequestError, match="大区"):
        resolver.code_for("不存在的大区")


# --------------------------------------------------------------------------- 采集


def participant(
    puuid: str,
    team_id: int,
    position: str,
    champion: str,
    *,
    kills: int = 3,
    deaths: int = 3,
    assists: int = 5,
    cs: int = 180,
    neutral: int = 0,
    win: bool = True,
) -> dict:
    return {
        "puuid": puuid,
        "riotIdGameName": f"玩家{puuid[:4]}",
        "riotIdTagline": "1234",
        "summonerName": f"玩家{puuid[:4]}",
        "championId": 1,
        "championName": champion,
        "teamId": team_id,
        "teamPosition": position,
        "individualPosition": position,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "totalMinionsKilled": cs,
        "neutralMinionsKilled": neutral,
        "goldEarned": 9000,
        "totalDamageDealtToChampions": 15000,
        "totalDamageTaken": 12000,
        "visionScore": 20,
        "win": win,
        "summonerLevel": 500,
        "profileIcon": 1,
    }


def riot_game(
    game_id: int,
    start_ms: int,
    *,
    queue_id: int = 420,
    map_id: int = 11,
    duration: int = 1800,
    own_score: float = 10.5,
    own_win: bool = True,
) -> dict:
    positions = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    participants = []
    horse = {}
    for index, position in enumerate(positions):
        blue_puuid = PUUID if position == "TOP" else f"blue-{index}"
        red_puuid = f"red-{index}"
        participants.append(
            participant(
                blue_puuid,
                100,
                position,
                "Mordekaiser" if index == 0 else f"Blue{index}",
                win=own_win,
            )
        )
        participants.append(participant(red_puuid, 200, position, f"Red{index}", win=not own_win))
        horse[blue_puuid] = {"score": own_score if index == 0 else 7.0 - index, "honor": None}
        horse[red_puuid] = {"score": 6.0 - index, "honor": None}
    return {
        "metadata": {
            "product": "lol",
            "tags": ["ranked", f"q_{queue_id}"],
            "participants": [item["puuid"] for item in participants],
            "timestamp": str(start_ms),
            "info_type": "summary",
            "match_id": f"HN1_{game_id}",
        },
        "json": {
            "gameId": game_id,
            "gameStartTimestamp": start_ms,
            "gameEndTimestamp": start_ms + duration * 1000,
            "gameDuration": duration,
            "queueId": queue_id,
            "mapId": map_id,
            "gameMode": "CLASSIC",
            "gameType": "MATCHED_GAME",
            "platformId": "HN1",
            "participants": participants,
            "teams": [
                {"teamId": 100, "win": own_win},
                {"teamId": 200, "win": not own_win},
            ],
        },
        "horse": horse,
    }


def start_ms(day: int, hour: int, minute: int = 0) -> int:
    moment = datetime(2026, 10, day, hour, minute, tzinfo=SHANGHAI)
    return int(moment.timestamp() * 1000)


def make_provider(http: FakeHttp, session: FakeSession, **kwargs) -> SqggProvider:
    return SqggProvider(
        session=session,
        http=http,
        server=SERVER,
        environment=ENVIRONMENT,
        timezone="Asia/Shanghai",
        now=lambda: NOW,
        **kwargs,
    )


RANK_PATH = "/sqgg-api/sgp-service/rank"


def queue_rank(http: FakeHttp, tier: str = "MASTER", division: str = "I", lp: int = 277) -> None:
    http.queue_json(
        RANK_PATH,
        {
            "status": "Success",
            "data": {
                "queues": [
                    {
                        "queueType": "RANKED_SOLO_5x5",
                        "tier": tier,
                        "rank": division,
                        "leaguePoints": lp,
                        "wins": 218,
                        "losses": 206,
                    }
                ]
            },
        },
    )


def queue_account(http: FakeHttp, puuid: str = PUUID, *, with_rank: bool = True) -> None:
    http.queue_json(
        "/sqgg-api/sgp-service/summoner-by-name",
        {"status": "Success", "message": None, "data": {"puuid": puuid}},
    )
    if with_rank:
        queue_rank(http)


def queue_games(http: FakeHttp, *pages: list[dict]) -> None:
    for page in pages:
        http.queue_json(
            "/sqgg-api/sgp-service/lol-match",
            {"status": "Success", "data": {"games": page}},
        )


def test_collect_day_paginates_and_filters_queue() -> None:
    http = FakeHttp()
    queue_account(http)
    page_one = [riot_game(1, start_ms(30, 5)), riot_game(2, start_ms(30, 3), queue_id=870)]
    page_two = [riot_game(3, start_ms(29, 22)), riot_game(4, start_ms(29, 20))]
    queue_games(http, page_one, page_two)
    session = FakeSession(make_window(end_time=NOW + timedelta(minutes=50)))

    collection = make_provider(http, session, page_size=2, max_pages=3).collect_day(
        "热心市民张合#65360", date(2026, 10, 30)
    )

    assert collection.complete is True
    assert collection.pages == 2
    assert [match.match_id for match in collection.matches] == ["HN1_1"]
    assert collection.matches[0].start_time == datetime(2026, 10, 30, 5, 0, tzinfo=SHANGHAI)


def test_collect_day_stops_when_page_reaches_older_than_window() -> None:
    http = FakeHttp()
    queue_account(http)
    queue_games(
        http,
        [riot_game(1, start_ms(30, 1))],
        [riot_game(2, start_ms(29, 23))],
    )
    session = FakeSession(make_window(end_time=NOW + timedelta(minutes=50)))

    collection = make_provider(http, session, page_size=1, max_pages=5).collect_day(
        "热心市民张合#65360", date(2026, 10, 30)
    )

    assert collection.pages == 2
    assert collection.complete is True
    assert len(collection.matches) == 1


def test_partial_failure_marks_incomplete_when_data_exists() -> None:
    http = FakeHttp()
    queue_account(http)
    http.queue_json(
        "/sqgg-api/sgp-service/lol-match",
        {"status": "Success", "data": {"games": [riot_game(1, start_ms(30, 4))]}},
        SqggRequestError("boom"),
    )
    session = FakeSession(make_window(end_time=NOW + timedelta(minutes=50)))

    collection = make_provider(http, session, page_size=1, max_pages=5).collect_day(
        "热心市民张合#65360", date(2026, 10, 30)
    )

    assert collection.complete is False
    assert "partial_page_failure" in collection.warnings
    assert len(collection.matches) == 1


def test_first_page_failure_raises() -> None:
    http = FakeHttp()
    queue_account(http)
    http.queue_json("/sqgg-api/sgp-service/lol-match", SqggRequestError("boom"))
    session = FakeSession(make_window(end_time=NOW + timedelta(minutes=50)))

    with pytest.raises(SqggRequestError):
        make_provider(http, session).collect_day("热心市民张合#65360", date(2026, 10, 30))


def test_account_not_found_raises() -> None:
    http = FakeHttp()
    http.queue_json(
        "/sqgg-api/sgp-service/summoner-by-name",
        {"status": "Success", "message": "该id不存在", "data": None},
    )
    session = FakeSession(make_window(end_time=NOW + timedelta(minutes=50)))

    with pytest.raises(SqggAccountNotFound, match="该id不存在"):
        make_provider(http, session).collect_day("热心市民张合#65360", date(2026, 10, 30))


def test_normalize_own_and_lane_opponent_and_rank() -> None:
    http = FakeHttp()
    queue_account(http)
    queue_games(http, [riot_game(1, start_ms(30, 5), own_score=10.5)])
    session = FakeSession(make_window(end_time=NOW + timedelta(minutes=50)))

    match = (
        make_provider(http, session)
        .collect_day("热心市民张合#65360", date(2026, 10, 30))
        .matches[0]
    )

    assert match.kills == 3
    assert match.cs == 180
    assert match.champion_key == "mordekaiser"
    assert match.queue_type == "SOLORANKED"
    assert match.queue_label == "单双排"
    assert match.game_map == "SUMMONERS_RIFT"
    assert match.op_score == 10.5
    assert match.op_score_rank == 1
    assert match.own_performance is not None
    assert match.own_performance.team_key == "BLUE"
    assert match.own_performance.team_kills == 15
    assert match.lane_opponent is not None
    assert match.lane_opponent.champion_name == "Red0"
    assert match.kill_participation == 53


def test_collect_day_includes_current_rank() -> None:
    http = FakeHttp()
    queue_account(http)
    queue_games(http, [riot_game(1, start_ms(30, 5))])
    session = FakeSession(make_window(end_time=NOW + timedelta(minutes=50)))

    collection = make_provider(http, session).collect_day("热心市民张合#65360", date(2026, 10, 30))

    assert collection.rank is not None
    assert collection.rank.display() == "大师 277 胜点"
    assert collection.rank.wins == 218


def test_rank_failure_does_not_block_collection() -> None:
    http = FakeHttp()
    queue_account(http, with_rank=False)
    http.queue_json(RANK_PATH, SqggRequestError("rank boom"))
    queue_games(http, [riot_game(1, start_ms(30, 5))])
    session = FakeSession(make_window(end_time=NOW + timedelta(minutes=50)))

    collection = make_provider(http, session).collect_day("热心市民张合#65360", date(2026, 10, 30))

    assert collection.rank is None
    assert "rank_unavailable" in collection.warnings
    assert len(collection.matches) == 1


def test_schema_changed_when_participant_missing() -> None:
    http = FakeHttp()
    queue_account(http)
    game = riot_game(1, start_ms(30, 5))
    game["json"]["participants"] = game["json"]["participants"][1:]
    queue_games(http, [game])
    session = FakeSession(make_window(end_time=NOW + timedelta(minutes=50)))

    with pytest.raises(SqggSchemaChanged):
        make_provider(http, session).collect_day("热心市民张合#65360", date(2026, 10, 30))


def test_collect_day_returns_day_collection_type() -> None:
    http = FakeHttp()
    queue_account(http)
    queue_games(http, [])
    session = FakeSession(make_window(end_time=NOW + timedelta(minutes=50)))

    collection = make_provider(http, session).collect_day("热心市民张合#65360", date(2026, 10, 30))

    assert isinstance(collection, DayCollection)
    assert collection.matches == []
    assert collection.page_url.startswith("https://sqgg.cc/summoner/")


def test_collect_day_supports_historical_date() -> None:
    """站点不限制查询时间范围：传入历史日期即可补跑，只按目标日窗口归属。"""
    http = FakeHttp()
    queue_account(http)
    queue_games(
        http,
        [riot_game(1, start_ms(29, 20)), riot_game(2, start_ms(28, 22))],
        [riot_game(3, start_ms(28, 10)), riot_game(4, start_ms(27, 23))],
    )
    session = FakeSession(make_window(end_time=NOW + timedelta(minutes=50)))

    collection = make_provider(http, session, page_size=2, max_pages=4).collect_day(
        "热心市民张合#65360", date(2026, 10, 28)
    )

    assert [match.match_id for match in collection.matches] == ["HN1_2", "HN1_3"]
    assert collection.complete is True
    assert collection.window_start == datetime(2026, 10, 28, 0, 0, tzinfo=SHANGHAI)
