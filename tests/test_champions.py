"""英雄中文名目录的契约测试（TDD：先于实现编写）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from champions import (
    ChampionCatalog,
    download_all_avatars,
    download_avatar,
    ensure_assets,
    load_catalog,
    needs_refresh,
    refresh_assets,
)

FIXTURE = {
    "version": "16.21.1",
    "champions": {
        "yorick": {
            "id": "Yorick",
            "name": "牧魂人",
            "title": "掘墓人",
            "tags": ["FIGHTER"],
            "image_url": "https://ddragon.leagueoflegends.com/cdn/16.21.1/img/champion/Yorick.png",
        },
        "drmundo": {
            "id": "DrMundo",
            "name": "祖安狂人",
            "title": "蒙多医生",
            "tags": ["TANK"],
            "image_url": "https://ddragon.leagueoflegends.com/cdn/16.21.1/img/champion/DrMundo.png",
        },
    },
}

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


def write_catalog(tmp_path: Path) -> Path:
    path = tmp_path / "champions.json"
    path.write_text(json.dumps(FIXTURE, ensure_ascii=False), encoding="utf-8")
    return path


def test_name_lookup_is_case_and_punctuation_insensitive(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))

    assert catalog.name_for("yorick") == "牧魂人"
    assert catalog.name_for("YORICK") == "牧魂人"
    assert catalog.name_for("DrMundo") == "祖安狂人"


def test_unknown_champion_falls_back(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))

    assert catalog.name_for("unknown", fallback="Unknown") == "Unknown"
    assert catalog.name_for("unknown") == "unknown"


def test_avatar_url_uses_data_dragon_id(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))

    url = catalog.avatar_url("yorick")

    assert url is not None
    assert url.endswith("/img/champion/Yorick.png")
    assert catalog.avatar_url("missing") is None


def test_download_avatar_caches_file(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))
    calls: list[str] = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return PNG_BYTES

    first = download_avatar(catalog, "yorick", tmp_path / "avatars", fetcher=fetcher)
    second = download_avatar(catalog, "yorick", tmp_path / "avatars", fetcher=fetcher)

    assert first is not None and first.exists()
    assert first.read_bytes() == PNG_BYTES
    assert second == first
    assert len(calls) == 1


def test_download_avatar_returns_none_on_failure(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))

    def failing(url: str) -> bytes:
        raise OSError("network down")

    assert download_avatar(catalog, "yorick", tmp_path / "avatars", fetcher=failing) is None
    assert download_avatar(catalog, "missing", tmp_path / "avatars", fetcher=failing) is None


def test_download_avatar_prefers_local_resource(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))
    local_dir = tmp_path / "data-avatars"
    local_dir.mkdir()
    (local_dir / "Yorick.png").write_bytes(PNG_BYTES)

    def fetcher(url: str) -> bytes:
        raise AssertionError("本地已有头像时不应发起网络请求")

    path = download_avatar(
        catalog, "yorick", tmp_path / "cache", fetcher=fetcher, local_dir=local_dir
    )

    assert path == local_dir / "Yorick.png"


def test_download_all_avatars_reports_counts(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))
    calls: list[str] = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return PNG_BYTES

    first = download_all_avatars(catalog, tmp_path / "all", fetcher=fetcher)
    second = download_all_avatars(catalog, tmp_path / "all", fetcher=fetcher)

    assert first == (2, 0, 0)
    assert second == (0, 2, 0)
    assert len(calls) == 2
    assert (tmp_path / "all" / "Yorick.png").exists()
    assert (tmp_path / "all" / "DrMundo.png").exists()


def test_download_all_counts_failures(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))

    def failing(url: str) -> bytes:
        raise OSError("network down")

    assert download_all_avatars(catalog, tmp_path / "all", fetcher=failing) == (0, 0, 2)


def test_needs_refresh_detects_missing_avatar_or_unknown_key(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))
    avatars = tmp_path / "avatars"
    avatars.mkdir()
    (avatars / "Yorick.png").write_bytes(PNG_BYTES)

    assert needs_refresh(catalog, ["yorick"], avatars) is False
    assert needs_refresh(catalog, ["yorick", "drmundo"], avatars) is True
    assert needs_refresh(catalog, ["unknown"], avatars) is True


def test_needs_refresh_detects_corrupt_avatar(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))
    avatars = tmp_path / "avatars"
    avatars.mkdir()
    (avatars / "Yorick.png").write_bytes(b"x")

    assert needs_refresh(catalog, ["yorick"], avatars) is True


def test_download_all_avatars_replaces_corrupt_file(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))
    directory = tmp_path / "all"
    directory.mkdir()
    (directory / "Yorick.png").write_bytes(b"x")

    downloaded, skipped, failed = download_all_avatars(
        catalog, directory, fetcher=lambda url: PNG_BYTES
    )

    assert (downloaded, skipped, failed) == (2, 0, 0)
    assert (directory / "Yorick.png").read_bytes() == PNG_BYTES


def test_download_avatar_ignores_corrupt_local_file(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "Yorick.png").write_bytes(b"x")

    path = download_avatar(
        catalog,
        "yorick",
        tmp_path / "cache",
        fetcher=lambda url: PNG_BYTES,
        local_dir=local_dir,
    )

    assert path == tmp_path / "cache" / "Yorick.png"
    assert path.read_bytes() == PNG_BYTES


def test_ensure_assets_triggers_refresh_when_missing(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))
    refreshed = ChampionCatalog(
        version="16.99.1", champions={entry.key: entry for entry in catalog}
    )
    calls: list[int] = []

    def refresher() -> ChampionCatalog:
        calls.append(1)
        return refreshed

    result = ensure_assets(
        catalog,
        ["yorick", "unknown"],
        avatars_dir=tmp_path / "avatars",
        marker_path=tmp_path / "marker.json",
        refresher=refresher,
        now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC),
    )

    assert calls == [1]
    assert result is refreshed


def test_ensure_assets_respects_cooldown(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))
    marker = tmp_path / "marker.json"
    marker.write_text(json.dumps({"refreshed_at": "2026-10-22T11:00:00+00:00"}), encoding="utf-8")
    calls: list[int] = []

    def refresher() -> ChampionCatalog:
        calls.append(1)
        return catalog

    result = ensure_assets(
        catalog,
        ["unknown"],
        avatars_dir=tmp_path / "avatars",
        marker_path=marker,
        refresher=refresher,
        now=datetime(2026, 10, 22, 12, 0, tzinfo=UTC),
        cooldown=timedelta(hours=6),
    )

    assert calls == []
    assert result is catalog


def test_refresh_assets_writes_catalog_and_avatars(tmp_path: Path) -> None:
    ddragon = {
        "data": {
            "Yorick": {"name": "牧魂人", "title": "掘墓人", "tags": ["FIGHTER"]},
            "DrMundo": {"name": "祖安狂人", "title": "蒙多医生", "tags": ["TANK"]},
        }
    }
    calls: list[str] = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        if url.endswith("versions.json"):
            return b'["16.99.1"]'
        if url.endswith("champion.json"):
            return json.dumps(ddragon, ensure_ascii=False).encode("utf-8")
        return PNG_BYTES

    catalog_path = tmp_path / "data" / "champions.json"
    result = refresh_assets(
        catalog_path,
        tmp_path / "avatars",
        fetcher=fetcher,
        version=None,
    )

    assert result.version == "16.99.1"
    assert result.catalog.name_for("yorick") == "牧魂人"
    assert result.downloaded == 2
    assert catalog_path.exists()
    assert (tmp_path / "avatars" / "Yorick.png").exists()
    assert (tmp_path / "avatars" / "DrMundo.png").exists()


def test_load_catalog_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_catalog(tmp_path / "missing.json")


def test_catalog_is_iterable_and_reports_version(tmp_path: Path) -> None:
    catalog = load_catalog(write_catalog(tmp_path))

    assert isinstance(catalog, ChampionCatalog)
    assert catalog.version == "16.21.1"
    assert len(catalog) == 2
