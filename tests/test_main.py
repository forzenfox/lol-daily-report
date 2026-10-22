"""main 编排器的契约测试（TDD：先于实现编写，全部离线）。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from champions import ChampionCatalog, ChampionInfo, RefreshResult
from main import AccountConfig, AppConfig, _ensure_catalog, load_config, run
from providers.opgg import DayCollection, NormalizedMatch, ProviderError

SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_match(match_id: str, *, win: bool = True, index: int = 0) -> NormalizedMatch:
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
    )


class FakeRenderer:
    """替身渲染器：只写一个占位文件，避免测试依赖字体与图片下载。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def render(self, path, stats, context, titles, avatars=None) -> Path:
        if self.error:
            raise self.error
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
        make_match("m1", win=True),
        make_match("m2", win=True),
        make_match("m3", win=False),
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


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.yaml")
