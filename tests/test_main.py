"""main 编排器的契约测试（TDD：先于实现编写，全部离线）。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from champions import ChampionCatalog, ChampionInfo, RefreshResult
from main import (
    AccountConfig,
    AccountResult,
    AppConfig,
    RunResult,
    _ensure_catalog,
    load_config,
    main,
    run,
)
from providers.opgg import (
    DayCollection,
    NormalizedMatch,
    ParticipantPerformance,
    ProviderError,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_performance(
    champion_name: str = "Darius", *, team_key: str = "RED", win: bool = False
) -> ParticipantPerformance:
    return ParticipantPerformance(
        summoner_name="Opponent#KR1",
        champion_key=champion_name.lower(),
        champion_name=champion_name,
        position="TOP",
        team_key=team_key,
        kills=2,
        deaths=5,
        assists=3,
        cs=184,
        gold_earned=8000,
        damage_to_champions=12000,
        damage_taken=10000,
        vision_score=30,
        team_kills=10,
        team_damage_to_champions=80000,
        team_damage_taken=70000,
        op_score=4.1,
        win=win,
    )


def make_match(
    match_id: str,
    *,
    win: bool = True,
    index: int = 0,
    own: ParticipantPerformance | None = None,
    opponent: ParticipantPerformance | None = None,
) -> NormalizedMatch:
    return NormalizedMatch(
        match_id=match_id,
        start_time=datetime(2026, 10, 20, 20, 0, tzinfo=SHANGHAI) + timedelta(minutes=30 * index),
        duration_seconds=1800,
        win=win,
        champion_key="yorick",
        champion_name="Yorick",
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
        op_score_rank=6,
        own_performance=own,
        lane_opponent=opponent,
    )


class FakeRenderer:
    """替身渲染器：只写一个占位文件，避免测试依赖字体与图片下载。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.details: list = []

    def render(self, path, stats, context, titles, avatars=None, details=()) -> Path:
        if self.error:
            raise self.error
        self.details = list(details)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-png")
        return target


class FakeProvider:
    def __init__(
        self, collection: DayCollection | None = None, error: Exception | None = None
    ) -> None:
        self.collection = collection
        self.error = error

    def collect_day(self, riot_id: str, day: date | None = None, *, region: str = "kr"):
        if self.error:
            raise self.error
        assert self.collection is not None
        return self.collection


def make_collection(account: str = "斗鱼丶胡凯利#2862") -> DayCollection:
    matches = [
        make_match(
            "m1",
            win=True,
            own=make_performance("Yorick", team_key="BLUE", win=True),
            opponent=make_performance("Darius"),
        ),
        make_match("m2", win=True, index=1),
        make_match("m3", win=False, index=2),
    ]
    return DayCollection(
        account=account,
        window_start=datetime(2026, 10, 20, 0, 0, tzinfo=SHANGHAI),
        window_end=datetime(2026, 10, 21, 0, 0, tzinfo=SHANGHAI),
        matches=matches,
        complete=True,
        pages=1,
        oldest_seen=datetime(2026, 10, 20, 19, 0, tzinfo=SHANGHAI),
        page_url="https://op.gg/en/lol/summoners/kr/example-2862",
    )


def make_empty_collection(account: str = "斗鱼丶胡凯利#2862") -> DayCollection:
    return DayCollection(
        account=account,
        window_start=datetime(2026, 10, 20, 0, 0, tzinfo=SHANGHAI),
        window_end=datetime(2026, 10, 21, 0, 0, tzinfo=SHANGHAI),
        matches=[],
        complete=True,
        pages=1,
        oldest_seen=None,
        page_url="https://op.gg/en/lol/summoners/kr/example-2862",
    )


def test_empty_day_skips_outputs_and_marks_skipped(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    config = AppConfig(
        accounts=(AccountConfig(display_name="斗鱼丶胡凯利", riot_id="斗鱼丶胡凯利#2862"),)
    )

    result = run(
        config,
        date(2026, 10, 20),
        provider_factory=lambda _account: FakeProvider(make_empty_collection()),
        output_dir=tmp_path,
        renderer=renderer,
    )

    account_result = result.accounts[0]
    assert account_result.ok is True
    assert account_result.skipped is True
    assert account_result.matches == 0
    assert account_result.report_path is None
    assert account_result.json_path is None
    assert account_result.image_path is None
    assert renderer.details == []
    assert not (tmp_path / "2026-10-20").exists()
    assert result.failed is False


def test_main_logs_skip_for_empty_day(tmp_path: Path, monkeypatch, capsys) -> None:
    import main as main_module

    account = AccountConfig(display_name="斗鱼丶胡凯利", riot_id="斗鱼丶胡凯利#2862")
    config = AppConfig(accounts=(account,))
    monkeypatch.setattr(main_module, "load_config", lambda _path: config)
    monkeypatch.setattr(
        main_module,
        "run",
        lambda *_args, **_kwargs: RunResult(
            day=date(2026, 10, 20),
            accounts=[AccountResult(account=account.riot_id, ok=True, skipped=True)],
        ),
    )

    code = main(["--date", "2026-10-20"])

    captured = capsys.readouterr()
    assert code == 0
    assert "[SKIP]" in captured.out
    assert "无对局" in captured.out


def test_load_config_parses_accounts_and_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """accounts:
  - display_name: 胡凯利
    riot_id: 斗鱼丶胡凯利#2862
    region: kr
  - display_name: 停用账号
    riot_id: someone#KR1
    enabled: false
report:
  output_dir: custom-output
  minimum_games_for_judgement: 2
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert isinstance(config, AppConfig)
    assert config.timezone == "Asia/Shanghai"
    assert config.output_dir == "custom-output"
    assert config.minimum_games_for_judgement == 2
    assert len(config.accounts) == 2
    assert config.accounts[0].enabled is True
    assert config.accounts[1].enabled is False


def test_config_defaults_when_optional_fields_missing(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "accounts:\n  - display_name: 胡凯利\n    riot_id: 斗鱼丶胡凯利#2862\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.output_dir == "output"
    assert config.minimum_games_for_judgement == 3
    assert config.accounts[0].region == "kr"
    assert config.accounts[0].enabled is True


def test_run_generates_markdown_and_json(tmp_path: Path) -> None:
    config = AppConfig(
        accounts=(AccountConfig(display_name="斗鱼丶胡凯利", riot_id="斗鱼丶胡凯利#2862"),)
    )

    result = run(
        config,
        date(2026, 10, 20),
        provider_factory=lambda _account: FakeProvider(make_collection()),
        output_dir=tmp_path,
        renderer=FakeRenderer(),
    )

    account_result = result.accounts[0]
    assert account_result.ok is True
    assert Path(account_result.report_path or "").name == "斗鱼丶胡凯利.md"
    assert Path(account_result.json_path or "").name == "斗鱼丶胡凯利.json"
    assert account_result.image_path is not None
    assert Path(account_result.image_path).name == "斗鱼丶胡凯利.png"
    markdown = Path(account_result.report_path or "").read_text(encoding="utf-8")
    assert "总场次：3" in markdown
    assert "上分密码" in markdown

    payload = json.loads(Path(account_result.json_path or "").read_text(encoding="utf-8"))
    assert payload["account"] == "斗鱼丶胡凯利#2862"
    assert payload["day"] == "2026-10-20"
    assert payload["stats"]["total_games"] == 3
    assert payload["stats"]["win_rate"] == 66.7
    assert [title["key"] for title in payload["titles"]] == ["climbing", "specialist"]
    assert payload["matches"][0]["match_id"] == "m1"
    assert payload["matches"][0]["start_time"] == "2026-10-20T20:00:00+08:00"
    assert payload["source"]["url"] == "https://op.gg/en/lol/summoners/kr/example-2862"
    assert payload["complete"] is True


def test_disabled_accounts_are_skipped(tmp_path: Path) -> None:
    config = AppConfig(
        accounts=(
            AccountConfig(display_name="胡凯利", riot_id="斗鱼丶胡凯利#2862"),
            AccountConfig(display_name="停用", riot_id="someone#KR1", enabled=False),
        )
    )

    result = run(
        config,
        date(2026, 10, 20),
        provider_factory=lambda _account: FakeProvider(make_collection()),
        output_dir=tmp_path,
        renderer=FakeRenderer(),
    )

    assert len(result.accounts) == 1
    assert result.accounts[0].account == "斗鱼丶胡凯利#2862"


def test_one_account_failure_does_not_block_others(tmp_path: Path) -> None:
    failing = AccountConfig(display_name="失败账号", riot_id="bad#KR1")
    working = AccountConfig(display_name="胡凯利", riot_id="斗鱼丶胡凯利#2862")
    providers = {
        "bad#KR1": FakeProvider(error=ProviderError("boom")),
        "斗鱼丶胡凯利#2862": FakeProvider(make_collection()),
    }
    config = AppConfig(accounts=(failing, working))

    result = run(
        config,
        date(2026, 10, 20),
        provider_factory=lambda account: providers[account.riot_id],
        output_dir=tmp_path,
        renderer=FakeRenderer(),
    )

    assert [item.ok for item in result.accounts] == [False, True]
    assert "boom" in (result.accounts[0].error or "")
    assert result.failed is True


def test_run_uses_provider_factory_per_account(tmp_path: Path) -> None:
    seen: list[str] = []

    def factory(account: AccountConfig):
        seen.append(account.riot_id)
        return FakeProvider(make_collection(account.riot_id))

    config = AppConfig(
        accounts=(
            AccountConfig(display_name="A", riot_id="a#KR1"),
            AccountConfig(display_name="B", riot_id="b#KR1"),
        )
    )

    result = run(
        config,
        date(2026, 10, 20),
        provider_factory=factory,
        output_dir=tmp_path,
        renderer=FakeRenderer(),
    )

    assert seen == ["a#KR1", "b#KR1"]
    assert [item.ok for item in result.accounts] == [True, True]


def test_image_filename_is_unique_for_multiple_accounts(tmp_path: Path) -> None:
    config = AppConfig(
        accounts=(
            AccountConfig(display_name="A", riot_id="a#KR1"),
            AccountConfig(display_name="B", riot_id="b#KR1"),
        )
    )

    result = run(
        config,
        date(2026, 10, 20),
        provider_factory=lambda account: FakeProvider(make_collection(account.riot_id)),
        output_dir=tmp_path,
        renderer=FakeRenderer(),
    )

    names = [Path(item.image_path or "").name for item in result.accounts]
    assert names == ["A.png", "B.png"]
    assert len(set(names)) == 2


def test_image_failure_does_not_block_reports(tmp_path: Path) -> None:
    config = AppConfig(
        accounts=(AccountConfig(display_name="胡凯利", riot_id="斗鱼丶胡凯利#2862"),)
    )

    result = run(
        config,
        date(2026, 10, 20),
        provider_factory=lambda _account: FakeProvider(make_collection()),
        output_dir=tmp_path,
        renderer=FakeRenderer(error=RuntimeError("image boom")),
    )

    account_result = result.accounts[0]
    assert account_result.ok is True
    assert account_result.image_path is None
    assert "image boom" in (account_result.image_error or "")
    assert Path(account_result.report_path or "").exists()
    assert Path(account_result.json_path or "").exists()


def test_config_parses_render_image_flag(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """accounts:
  - display_name: 胡凯利
    riot_id: 斗鱼丶胡凯利#2862
report:
  render_image: false
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.render_image is False
    assert AppConfig().render_image is True


def test_run_without_renderer_skips_image(tmp_path: Path) -> None:
    config = AppConfig(
        accounts=(AccountConfig(display_name="胡凯利", riot_id="斗鱼丶胡凯利#2862"),),
        render_image=False,
    )

    result = run(
        config,
        date(2026, 10, 20),
        provider_factory=lambda _account: FakeProvider(make_collection()),
        output_dir=tmp_path,
    )

    assert result.accounts[0].ok is True
    assert result.accounts[0].image_path is None


def test_ensure_catalog_refreshes_missing_avatar(tmp_path: Path, monkeypatch) -> None:
    import main as main_module

    avatars = tmp_path / "avatars"
    avatars.mkdir()
    monkeypatch.setattr(main_module, "DATA_AVATAR_DIR", avatars)
    monkeypatch.setattr(main_module, "CHAMPION_REFRESH_MARKER", tmp_path / "marker.json")

    old = ChampionCatalog(
        version="1",
        champions={
            "yorick": ChampionInfo("yorick", "Yorick", "旧名", "", (), "https://x/Yorick.png")
        },
    )
    new = ChampionCatalog(
        version="2",
        champions={
            "yorick": ChampionInfo("yorick", "Yorick", "牧魂人", "", (), "https://x/Yorick.png")
        },
    )
    monkeypatch.setattr(
        main_module,
        "refresh_assets",
        lambda *args, **kwargs: RefreshResult(catalog=new, version="2"),
    )
    collection = make_collection()
    names = {"yorick": "旧名"}

    result = _ensure_catalog(AppConfig(), old, collection, None, names)

    assert result is new
    assert names["yorick"] == "牧魂人"
    assert collection.matches[0].champion_name == "牧魂人"


def test_ensure_catalog_localizes_lane_opponent_without_refresh(
    tmp_path: Path, monkeypatch
) -> None:
    import main as main_module

    avatars = tmp_path / "avatars"
    avatars.mkdir()
    (avatars / "Yorick.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(main_module, "DATA_AVATAR_DIR", avatars)
    monkeypatch.setattr(main_module, "CHAMPION_REFRESH_MARKER", tmp_path / "marker.json")
    monkeypatch.setattr(
        main_module,
        "refresh_assets",
        lambda *args, **kwargs: pytest.fail("头像齐全时不应触发刷新"),
    )

    catalog = ChampionCatalog(
        version="1",
        champions={
            "yorick": ChampionInfo("yorick", "Yorick", "牧魂人", "", (), "https://x/Yorick.png"),
            "darius": ChampionInfo("darius", "Darius", "诺手", "", (), "https://x/Darius.png"),
        },
    )
    collection = make_collection()

    refreshed = _ensure_catalog(AppConfig(), catalog, collection, None, {})

    assert refreshed is catalog
    assert collection.matches[0].champion_name == "牧魂人"
    assert collection.matches[0].lane_opponent is not None
    assert collection.matches[0].lane_opponent.champion_name == "诺手"


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.yaml")


def test_run_renders_match_details_in_markdown_and_json(tmp_path: Path) -> None:
    config = AppConfig(
        accounts=(AccountConfig(display_name="斗鱼丶胡凯利", riot_id="斗鱼丶胡凯利#2862"),)
    )

    result = run(
        config,
        date(2026, 10, 20),
        provider_factory=lambda _account: FakeProvider(make_collection()),
        output_dir=tmp_path,
        renderer=FakeRenderer(),
    )

    markdown = Path(result.accounts[0].report_path or "").read_text(encoding="utf-8")
    assert "## 对局明细" in markdown
    assert "| 1 | 20:00 | 牧魂人 | 上单 | 胜 | 6/4/1 | 279 | 9.9 | 6/10 |" in markdown
    assert "诺克萨斯之手 2/5/3 · 184 补刀" in markdown

    payload = json.loads(Path(result.accounts[0].json_path or "").read_text(encoding="utf-8"))
    first, second = payload["matches"][0], payload["matches"][1]
    assert first["op_score_rank"] == 6
    assert first["lane_opponent"]["champion_name"] == "诺克萨斯之手"
    assert first["lane_opponent"]["summoner_name"] == "Opponent#KR1"
    assert first["lane_opponent"]["kills"] == 2
    assert first["lane_opponent"]["cs"] == 184
    assert second["lane_opponent"] is None


def test_renderer_receives_match_details(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    config = AppConfig(
        accounts=(AccountConfig(display_name="斗鱼丶胡凯利", riot_id="斗鱼丶胡凯利#2862"),)
    )

    run(
        config,
        date(2026, 10, 20),
        provider_factory=lambda _account: FakeProvider(make_collection()),
        output_dir=tmp_path,
        renderer=renderer,
    )

    assert len(renderer.details) == 3
    assert renderer.details[0].opponent.champion_name == "诺克萨斯之手"
    assert renderer.details[1].opponent is None


def test_ensure_catalog_localizes_lane_opponent_name(tmp_path: Path, monkeypatch) -> None:
    import main as main_module

    avatars = tmp_path / "avatars"
    avatars.mkdir()
    monkeypatch.setattr(main_module, "DATA_AVATAR_DIR", avatars)
    monkeypatch.setattr(main_module, "CHAMPION_REFRESH_MARKER", tmp_path / "marker.json")

    old = ChampionCatalog(
        version="1",
        champions={
            "yorick": ChampionInfo("yorick", "Yorick", "牧魂人", "", (), "https://x/Yorick.png"),
            "darius": ChampionInfo("darius", "Darius", "Darius", "", (), "https://x/Darius.png"),
        },
    )
    new = ChampionCatalog(
        version="2",
        champions={
            "yorick": ChampionInfo("yorick", "Yorick", "牧魂人", "", (), "https://x/Yorick.png"),
            "darius": ChampionInfo("darius", "Darius", "诺手", "", (), "https://x/Darius.png"),
        },
    )
    monkeypatch.setattr(
        main_module,
        "refresh_assets",
        lambda *args, **kwargs: RefreshResult(catalog=new, version="2"),
    )
    collection = make_collection()
    names = {"yorick": "牧魂人", "darius": "Darius"}

    _ensure_catalog(AppConfig(), old, collection, None, names)

    assert collection.matches[0].lane_opponent is not None
    assert collection.matches[0].lane_opponent.champion_name == "诺手"
