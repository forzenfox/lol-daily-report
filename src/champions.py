"""英雄中文名目录与头像下载（数据来自 Riot Data Dragon）。"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

USER_AGENT = "lol-daily-report/1.0"
VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CHAMPIONS_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/zh_CN/champion.json"
DEFAULT_REFRESH_COOLDOWN = timedelta(hours=6)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _is_valid_avatar(path: Path) -> bool:
    """只认完整 PNG 头，避免把 0 字节或截断文件当作可用头像。"""
    try:
        with path.open("rb") as handle:
            return handle.read(len(PNG_SIGNATURE)) == PNG_SIGNATURE
    except OSError:
        return False


@dataclass(frozen=True)
class ChampionInfo:
    key: str
    champion_id: str
    name: str
    title: str
    tags: tuple[str, ...]
    image_url: str


def normalize_key(value: str) -> str:
    """把 Yorick / Dr. Mundo / drmundo 统一成 drmundo 这类键。"""
    return "".join(char for char in value.lower() if char.isalnum())


class ChampionCatalog:
    """英雄键 → 中文名/头像地址的只读目录。"""

    def __init__(self, version: str, champions: dict[str, ChampionInfo]) -> None:
        self.version = version
        self._champions = champions

    def __len__(self) -> int:
        return len(self._champions)

    def __iter__(self) -> Iterator[ChampionInfo]:
        return iter(self._champions.values())

    def info_for(self, key: str) -> ChampionInfo | None:
        return self._champions.get(normalize_key(key))

    def name_for(self, key: str, fallback: str | None = None) -> str:
        info = self.info_for(key)
        if info is not None:
            return info.name
        return fallback if fallback is not None else key

    def avatar_url(self, key: str) -> str | None:
        info = self.info_for(key)
        return info.image_url if info else None


def load_catalog(path: str | Path) -> ChampionCatalog:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    champions = {
        key: ChampionInfo(
            key=key,
            champion_id=value["id"],
            name=value["name"],
            title=value.get("title", ""),
            tags=tuple(value.get("tags", ())),
            image_url=value["image_url"],
        )
        for key, value in payload["champions"].items()
    }
    return ChampionCatalog(version=payload.get("version", ""), champions=champions)


def _fetch_bytes(url: str, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def download_avatar(
    catalog: ChampionCatalog,
    key: str,
    cache_dir: str | Path,
    fetcher: Callable[[str], bytes] | None = None,
    timeout: float = 30.0,
    local_dir: str | Path | None = None,
) -> Path | None:
    """优先使用本地静态头像，其次缓存，最后下载；失败返回 None。"""
    info = catalog.info_for(key)
    if info is None:
        return None

    filename = f"{info.champion_id}.png"
    if local_dir is not None:
        local_path = Path(local_dir) / filename
        if local_path.exists() and _is_valid_avatar(local_path):
            return local_path

    target = Path(cache_dir) / filename
    if target.exists() and _is_valid_avatar(target):
        return target

    fetch = fetcher or (lambda url: _fetch_bytes(url, timeout=timeout))
    try:
        content = fetch(info.image_url)
    except Exception:  # noqa: BLE001 - 头像失败不应影响主流程
        return None
    if not content or not content.startswith(PNG_SIGNATURE):
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def download_all_avatars(
    catalog: ChampionCatalog,
    directory: str | Path,
    fetcher: Callable[[str], bytes] | None = None,
    timeout: float = 30.0,
) -> tuple[int, int, int]:
    """批量下载全部头像，返回 (下载数, 跳过数, 失败数)。"""
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    fetch = fetcher or (lambda url: _fetch_bytes(url, timeout=timeout))

    downloaded = skipped = failed = 0
    for info in catalog:
        target = target_dir / f"{info.champion_id}.png"
        if target.exists() and _is_valid_avatar(target):
            skipped += 1
            continue
        try:
            content = fetch(info.image_url)
        except Exception:  # noqa: BLE001 - 单张失败不影响批量任务
            failed += 1
            continue
        if not content or not content.startswith(PNG_SIGNATURE):
            failed += 1
            continue
        target.write_bytes(content)
        downloaded += 1
    return downloaded, skipped, failed


@dataclass(frozen=True)
class RefreshResult:
    catalog: ChampionCatalog
    version: str
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0


def refresh_assets(
    catalog_path: str | Path,
    avatars_dir: str | Path,
    fetcher: Callable[[str], bytes] | None = None,
    version: str | None = None,
    timeout: float = 30.0,
    download_avatars: bool = True,
) -> RefreshResult:
    """从 Data Dragon 拉取最新中文名与头像，写入本地资源。"""
    fetch = fetcher or (lambda url: _fetch_bytes(url, timeout=timeout))
    resolved_version = version or json.loads(fetch(VERSIONS_URL).decode("utf-8"))[0]
    payload = json.loads(fetch(CHAMPIONS_URL.format(version=resolved_version)).decode("utf-8"))

    champions: dict[str, dict] = {}
    for champion_id, item in payload["data"].items():
        champions[normalize_key(champion_id)] = {
            "id": champion_id,
            "name": item["name"],
            "title": item.get("title", ""),
            "tags": item.get("tags", []),
            "image_url": (
                f"https://ddragon.leagueoflegends.com/cdn/{resolved_version}"
                f"/img/champion/{champion_id}.png"
            ),
        }

    path = Path(catalog_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": resolved_version, "champions": champions}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    catalog = load_catalog(path)

    downloaded = skipped = failed = 0
    if download_avatars:
        downloaded, skipped, failed = download_all_avatars(catalog, avatars_dir, fetcher=fetch)
    return RefreshResult(
        catalog=catalog,
        version=resolved_version,
        downloaded=downloaded,
        skipped=skipped,
        failed=failed,
    )


def needs_refresh(
    catalog: ChampionCatalog, keys: list[str] | tuple[str, ...], avatars_dir: str | Path
) -> bool:
    """缺少中文名或本地头像时返回 True。"""
    avatar_dir = Path(avatars_dir)
    for key in keys:
        info = catalog.info_for(key)
        if info is None:
            return True
        avatar = avatar_dir / f"{info.champion_id}.png"
        if not avatar.exists() or not _is_valid_avatar(avatar):
            return True
    return False


def ensure_assets(
    catalog: ChampionCatalog,
    keys: list[str] | tuple[str, ...],
    *,
    avatars_dir: str | Path,
    marker_path: str | Path,
    refresher: Callable[[], ChampionCatalog],
    cooldown: timedelta = DEFAULT_REFRESH_COOLDOWN,
    now: datetime | None = None,
) -> ChampionCatalog:
    """检测到缺失时自动刷新一次；带冷却标记，避免频繁请求。"""
    if not keys or not needs_refresh(catalog, keys, avatars_dir):
        return catalog

    moment = now or datetime.now(UTC)
    marker = Path(marker_path)
    try:
        last = datetime.fromisoformat(
            json.loads(marker.read_text(encoding="utf-8"))["refreshed_at"]
        )
        if moment - last < cooldown:
            return catalog
    except (OSError, ValueError, KeyError, TypeError):
        pass

    try:
        refreshed = refresher()
    except Exception:  # noqa: BLE001 - 刷新失败不应阻断战报
        refreshed = catalog

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"refreshed_at": moment.isoformat()}), encoding="utf-8")
    except OSError:
        pass
    return refreshed
