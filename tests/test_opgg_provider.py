"""OpggProvider 的契约测试（TDD：先于实现编写）。"""

from __future__ import annotations

import json
from datetime import date

import pytest

from providers.opgg import (
    NormalizedMatch,
    OpggProvider,
    ProviderError,
    SchemaChangedError,
)

PUUID = "nbJBkHxHe6KE_siQSiE7O5upB47jv4vch4oR12t6IQiu-leFagxavuWZVXlcGJBfu9tEWv7wTYvWmg"
ACTION_ID = "409a2b9ca50d15e50a4dace93552e3a40113dc2753"
CHUNK_URL = "https://c-lol-web.op.gg/app-router/releases/production-422-1-abc/_next/static/chunks/1534-e6a932b1a102c8ea.js"
PAGE_URL_CHUNK = CHUNK_URL


def page_html(puuid: str = PUUID) -> str:
    """模拟服务端 HTML：包含 chunk 引用与转义后的 puuid。"""
    return (
        "<html><head>"
        f'<script src="{CHUNK_URL}"></script>'
        "</head><body><script>self.__next_f.push([1,"
        f'\\"puuid\\":\\"{puuid}\\"'
        "])</script></body></html>"
    )


def chunk_js(action_id: str = ACTION_ID, name: str = "getGames") -> str:
    return (
        "s.d(t,{Z:()=>a})},48940:(e,t,s)=>{var d=s(30926);"
        f'let c=(0,d.createServerReference)("{action_id}",d.callServer,void 0,'
        f'd.findSourceMapURL,"{name}");'
    )


def build_game(match_id: str, created_at: str, **overrides) -> dict:
    game = {
        "id": match_id,
        "created_at": created_at,
        "game_length": 1698,
        "game_result": "WIN",
        "game_type": {"game_type": "SOLORANKED", "game_translate": "Ranked Solo/Duo"},
        "game_map": "SUMMONERS_RIFT",
        "champion": {"key": "yorick", "name": "Yorick"},
        "position": "TOP",
        "role": "FIGHTER",
        "stats": {
            "kda": {"kill": 6, "death": 4, "assist": 1, "kda": 1.75},
            "cs": {"totalCs": 279, "csPerMin": 9.9},
            "op_score": 4.8,
            "killParticipation": 21,
            "lane_score": 60,
        },
    }
    game.update(overrides)
    return game


class FakeHttp:
    """可注入的 HTTP 替身：页面、chunk、动作批次分别可控。"""

    def __init__(
        self,
        html: str | None = None,
        chunks: dict[str, str] | None = None,
        batches: list[list[dict]] | None = None,
        page_status: int = 200,
    ) -> None:
        self.html = html if html is not None else page_html()
        self.chunks = chunks if chunks is not None else {CHUNK_URL: chunk_js()}
        self.batches = list(batches or [])
        self.page_status = page_status
        self.posts: list[dict] = []

    def get(self, url: str) -> tuple[int, str]:
        if "_next/static/chunks/" in url:
            return 200, self.chunks.get(url, "")
        return self.page_status, self.html

    def post(self, url: str, body: list[dict], headers: dict) -> tuple[int, str, str]:
        self.posts.append({"url": url, "body": body, "headers": headers})
        batch = self.batches.pop(0) if self.batches else []
        flight = '0:{"a":"$@1"}\n1:' + json.dumps({"data": batch}, ensure_ascii=False)
        return 200, flight, url


def make_provider(fake: FakeHttp, **kwargs) -> OpggProvider:
    params = {"http": fake, "timezone": "Asia/Shanghai", "max_pages": 10}
    params.update(kwargs)
    return OpggProvider(**params)


def test_filters_by_shanghai_day_boundaries() -> None:
    # KST 与本项目上海时区相差 1 小时，跨零点时必须按上海日期归属。
    batch = [
        build_game("m_d", "2026-10-21T01:00:00+09:00"),  # 上海 10-21 00:00 → 排除
        build_game("m_a", "2026-10-21T00:30:00+09:00"),  # 上海 10-20 23:30 → 保留
        build_game("m_c", "2026-10-20T23:30:00+09:00"),  # 上海 10-20 22:30 → 保留
        build_game("m_b", "2026-10-20T00:30:00+09:00"),  # 上海 10-19 23:30 → 排除
    ]
    fake = FakeHttp(batches=[batch])
    provider = make_provider(fake)

    result = provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20))

    assert [m.match_id for m in result.matches] == ["m_a", "m_c"]
    assert all(m.start_time.date() == date(2026, 10, 20) for m in result.matches)
    assert str(result.matches[0].start_time.tzinfo) == "Asia/Shanghai"


def test_stops_paging_once_window_is_covered() -> None:
    newer = [
        build_game(
            f"m_new_{i:02d}", f"2026-10-20T{23 - i // 2:02d}:{30 if i % 2 else 0:02d}:00+09:00"
        )
        for i in range(20)
    ]
    older = [
        build_game(f"m_old_{i:02d}", f"2026-10-19T{18 - i % 12:02d}:00:00+09:00") for i in range(20)
    ]
    fake = FakeHttp(batches=[newer, older])
    provider = make_provider(fake)

    result = provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20))

    assert result.pages == 2
    assert result.complete is True
    assert len(fake.posts) == 2
    assert result.oldest_seen < result.window_start


def test_short_batch_marks_complete_and_stops() -> None:
    batch = [
        build_game("m_1", "2026-10-20T22:00:00+09:00"),
        build_game("m_2", "2026-10-20T20:00:00+09:00"),
    ]
    fake = FakeHttp(batches=[batch])
    provider = make_provider(fake)

    result = provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20))

    assert result.pages == 1
    assert result.complete is True
    assert len(result.matches) == 2


def test_max_pages_marks_incomplete() -> None:
    batch = [build_game(f"m_{i:02d}", "2026-10-20T23:00:00+09:00") for i in range(20)]
    fake = FakeHttp(batches=[batch, batch])
    provider = make_provider(fake, max_pages=1)

    result = provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20))

    assert result.pages == 1
    assert result.complete is False
    assert "coverage_may_be_truncated" in result.warnings


def test_discovers_action_id_from_chunks() -> None:
    fake = FakeHttp(batches=[[build_game("m_1", "2026-10-20T22:00:00+09:00")]])
    provider = OpggProvider(http=fake, timezone="Asia/Shanghai")

    provider.collect_day("斗鱼丶凯利#2862", date(2026, 10, 20))

    assert fake.posts[0]["headers"]["next-action"] == ACTION_ID


def test_env_action_id_skips_chunk_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPGG_GET_GAMES_ACTION_ID", ACTION_ID)
    fake = FakeHttp(batches=[[build_game("m_1", "2026-10-20T22:00:00+09:00")]])
    provider = OpggProvider(http=fake, timezone="Asia/Shanghai")

    provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20))

    assert fake.posts[0]["headers"]["next-action"] == ACTION_ID


def test_sends_puuid_region_and_total_game_type() -> None:
    fake = FakeHttp(batches=[[build_game("m_1", "2026-10-20T22:00:00+09:00")]])
    provider = make_provider(fake)

    provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20), region="kr")

    body = fake.posts[0]["body"][0]
    assert body["puuid"] == PUUID
    assert body["region"] == "kr"
    assert body["gameType"] == "TOTAL"
    assert body["endedAt"] == ""
    assert body["locale"] == "en"


def test_maps_normalized_fields() -> None:
    fake = FakeHttp(batches=[[build_game("m_1", "2026-10-20T22:00:00+09:00")]])
    provider = make_provider(fake)

    match = provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20)).matches[0]

    assert isinstance(match, NormalizedMatch)
    assert match.match_id == "m_1"
    assert match.duration_seconds == 1698
    assert match.win is True
    assert match.champion_key == "yorick"
    assert match.champion_name == "Yorick"
    assert (match.kills, match.deaths, match.assists) == (6, 4, 1)
    assert match.cs == 279
    assert match.position == "TOP"
    assert match.queue_type == "SOLORANKED"
    assert match.source == "opgg-getgames"
    assert match.fetched_at is not None


def test_raises_schema_error_when_required_field_missing() -> None:
    broken = build_game("m_1", "2026-10-20T22:00:00+09:00")
    del broken["stats"]["kda"]
    fake = FakeHttp(batches=[[broken]])
    provider = make_provider(fake)

    with pytest.raises(SchemaChangedError):
        provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20))


def test_unknown_game_result_raises_schema_error() -> None:
    bad = build_game("m_1", "2026-10-20T22:00:00+09:00", game_result="REMAKE")
    fake = FakeHttp(batches=[[bad]])
    provider = make_provider(fake)

    with pytest.raises(SchemaChangedError):
        provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20))


def test_raises_provider_error_on_non_200_page() -> None:
    fake = FakeHttp(page_status=403, batches=[])
    provider = make_provider(fake)

    with pytest.raises(ProviderError):
        provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20))


def test_champion_names_can_be_localized() -> None:
    fake = FakeHttp(batches=[[build_game("m_1", "2026-10-20T22:00:00+09:00")]])
    provider = OpggProvider(
        http=fake, timezone="Asia/Shanghai", champion_names={"yorick": "牧魂人"}
    )

    result = provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20))

    assert result.matches[0].champion_key == "yorick"
    assert result.matches[0].champion_name == "牧魂人"


def test_champion_name_falls_back_when_mapping_missing() -> None:
    fake = FakeHttp(batches=[[build_game("m_1", "2026-10-20T22:00:00+09:00")]])
    provider = OpggProvider(
        http=fake, timezone="Asia/Shanghai", champion_names={"gnar": "迷失之牙"}
    )

    result = provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20))

    assert result.matches[0].champion_name == "Yorick"


def test_collection_exposes_page_url() -> None:
    fake = FakeHttp(batches=[[build_game("m_1", "2026-10-20T22:00:00+09:00")]])
    provider = make_provider(fake)

    result = provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20))

    assert result.page_url == (
        "https://op.gg/en/lol/summoners/kr/"
        "%E6%96%97%E9%B1%BC%E4%B8%B6%E8%83%A1%E5%87%AF%E5%88%A9-2862"
    )


def test_uses_encoded_riot_id_in_page_url() -> None:
    fake = FakeHttp(batches=[[build_game("m_1", "2026-10-20T22:00:00+09:00")]])
    provider = make_provider(fake)

    provider.collect_day("斗鱼丶胡凯利#2862", date(2026, 10, 20))

    assert fake.posts[0]["url"].endswith(
        "/en/lol/summoners/kr/%E6%96%97%E9%B1%BC%E4%B8%B6%E8%83%A1%E5%87%AF%E5%88%A9-2862"
    )
