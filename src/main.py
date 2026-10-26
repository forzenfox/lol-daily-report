"""战报生成入口：加载配置、逐账号采集、计算统计、生成 Markdown 与 JSON。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import yaml

from champions import (
    ChampionCatalog,
    download_avatar,
    ensure_assets,
    load_catalog,
    refresh_assets,
)
from match_details import MatchDetail, compute_match_details
from providers.opgg import (
    DayCollection,
    NormalizedMatch,
    OpggProvider,
    ParticipantPerformance,
)
from render_image import ImageOptions, save_report_image
from report import ReportContext, render_markdown, write_report
from stats import DailyStats, compute_daily_stats
from titles import Title, evaluate_titles

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_CHAMPION_CATALOG = Path("data/champions-zh_CN.json")
DATA_AVATAR_DIR = Path("data/avatars")
AVATAR_CACHE_DIR = Path(".cache/avatars")
CHAMPION_REFRESH_MARKER = Path(".cache/champion-refresh.json")


class ProviderLike(Protocol):
    def collect_day(
        self, riot_id: str, day: date | None = None, *, region: str = "kr"
    ) -> DayCollection: ...


class RendererLike(Protocol):
    def render(
        self,
        path: str | Path,
        stats: DailyStats,
        context: ReportContext,
        titles: list[Title] | tuple[Title, ...],
        avatars: dict[str, Path] | None = None,
        details: Sequence[MatchDetail] = (),
    ) -> Path: ...


class OpggImageRenderer:
    """默认渲染器：下载英雄头像后交给 Pillow 渲染。"""

    def __init__(
        self,
        catalog: ChampionCatalog | None,
        cache_dir: Path = AVATAR_CACHE_DIR,
        options: ImageOptions | None = None,
        local_dir: Path = DATA_AVATAR_DIR,
    ) -> None:
        self.catalog = catalog
        self.cache_dir = cache_dir
        self.options = options
        self.local_dir = local_dir

    def render(
        self,
        path: str | Path,
        stats: DailyStats,
        context: ReportContext,
        titles: list[Title] | tuple[Title, ...],
        avatars: dict[str, Path] | None = None,
        details: Sequence[MatchDetail] = (),
    ) -> Path:
        avatar_map = dict(avatars or {})
        if self.catalog is not None:
            for champion in stats.champions[:5]:
                avatar = download_avatar(
                    self.catalog,
                    champion.champion_key,
                    self.cache_dir,
                    local_dir=self.local_dir,
                )
                if avatar is not None:
                    avatar_map[champion.champion_key] = avatar
        return save_report_image(
            path,
            stats,
            context,
            titles,
            options=self.options,
            avatars=avatar_map,
            details=details,
        )


@dataclass(frozen=True)
class AccountConfig:
    display_name: str
    riot_id: str
    region: str = "kr"
    enabled: bool = True


@dataclass(frozen=True)
class AppConfig:
    accounts: tuple[AccountConfig, ...] = ()
    timezone: str = "Asia/Shanghai"
    output_dir: str = "output"
    minimum_games_for_judgement: int = 3
    source_label: str = "OP.GG"
    render_image: bool = True
    auto_update_champions: bool = True


@dataclass
class AccountResult:
    account: str
    ok: bool
    matches: int = 0
    skipped: bool = False
    report_path: str | None = None
    json_path: str | None = None
    image_path: str | None = None
    image_error: str | None = None
    error: str | None = None


@dataclass
class RunResult:
    day: date
    accounts: list[AccountResult] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(not item.ok for item in self.accounts)


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """读取 YAML 配置；缺省字段使用项目默认值。"""
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise TypeError("配置文件根节点必须是对象")

    accounts: list[AccountConfig] = []
    for item in raw.get("accounts") or []:
        if not isinstance(item, dict) or not item.get("riot_id"):
            raise ValueError("每个账号都必须提供 riot_id")
        accounts.append(
            AccountConfig(
                display_name=str(item.get("display_name") or item["riot_id"]),
                riot_id=str(item["riot_id"]),
                region=str(item.get("region") or "kr"),
                enabled=bool(item.get("enabled", True)),
            )
        )

    report = raw.get("report") or {}
    return AppConfig(
        accounts=tuple(accounts),
        timezone=str(raw.get("timezone") or "Asia/Shanghai"),
        output_dir=str(report.get("output_dir") or "output"),
        minimum_games_for_judgement=int(report.get("minimum_games_for_judgement", 3)),
        source_label=str(report.get("source_label") or "OP.GG"),
        render_image=bool(report.get("render_image", True)),
        auto_update_champions=bool(report.get("auto_update_champions", True)),
    )


def _safe_filename(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|#]', "-", text).strip(" .-")
    return cleaned or "account"


def _performance_payload(performance: ParticipantPerformance | None) -> dict[str, Any] | None:
    if performance is None:
        return None
    return {
        "summoner_name": performance.summoner_name,
        "champion_key": performance.champion_key,
        "champion_name": performance.champion_name,
        "position": performance.position,
        "team_key": performance.team_key,
        "kills": performance.kills,
        "deaths": performance.deaths,
        "assists": performance.assists,
        "cs": performance.cs,
        "gold_earned": performance.gold_earned,
        "damage_to_champions": performance.damage_to_champions,
        "damage_taken": performance.damage_taken,
        "vision_score": performance.vision_score,
        "team_kills": performance.team_kills,
        "op_score": performance.op_score,
        "win": performance.win,
    }


def _match_payload(match: NormalizedMatch) -> dict[str, Any]:
    return {
        "match_id": match.match_id,
        "start_time": match.start_time.isoformat(),
        "duration_seconds": match.duration_seconds,
        "win": match.win,
        "champion_key": match.champion_key,
        "champion_name": match.champion_name,
        "kills": match.kills,
        "deaths": match.deaths,
        "assists": match.assists,
        "cs": match.cs,
        "position": match.position,
        "queue_type": match.queue_type,
        "queue_label": match.queue_label,
        "game_map": match.game_map,
        "kda_ratio": match.kda_ratio,
        "cs_per_min": match.cs_per_min,
        "op_score": match.op_score,
        "op_score_rank": match.op_score_rank,
        "kill_participation": match.kill_participation,
        "lane_opponent": _performance_payload(match.lane_opponent),
        "source": match.source,
        "fetched_at": match.fetched_at.isoformat(),
    }


def _stats_payload(stats: DailyStats) -> dict[str, Any]:
    return {
        "total_games": stats.total_games,
        "wins": stats.wins,
        "losses": stats.losses,
        "win_rate": stats.win_rate,
        "kda": stats.kda,
        "avg_kills": stats.avg_kills,
        "avg_deaths": stats.avg_deaths,
        "avg_assists": stats.avg_assists,
        "avg_duration_seconds": stats.avg_duration_seconds,
        "avg_cs": stats.avg_cs,
        "cs_per_min": stats.cs_per_min,
        "sample_sufficient": stats.sample_sufficient,
        "champions": [
            {
                "champion_key": champion.champion_key,
                "champion_name": champion.champion_name,
                "games": champion.games,
                "wins": champion.wins,
                "losses": champion.losses,
                "win_rate": champion.win_rate,
                "kda": champion.kda,
                "avg_kills": champion.avg_kills,
                "avg_deaths": champion.avg_deaths,
                "avg_assists": champion.avg_assists,
            }
            for champion in stats.champions
        ],
    }


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _build_payload(
    account: AccountConfig,
    collection: DayCollection,
    stats: DailyStats,
    titles: list[Any],
    config: AppConfig,
) -> dict[str, Any]:
    fetched_at = collection.matches[0].fetched_at.isoformat() if collection.matches else None
    return {
        "account": account.riot_id,
        "display_name": account.display_name,
        "region": account.region,
        "day": collection.window_start.date().isoformat(),
        "window": {
            "start": collection.window_start.isoformat(),
            "end": collection.window_end.isoformat(),
            "timezone": config.timezone,
        },
        "complete": collection.complete,
        "pages": collection.pages,
        "warnings": list(collection.warnings),
        "source": {
            "provider": "opgg",
            "collector": "getgames",
            "url": collection.page_url,
        },
        "fetched_at": fetched_at,
        "stats": _stats_payload(stats),
        "titles": [
            {"key": title.key, "name": title.name, "reason": title.reason} for title in titles
        ],
        "matches": [_match_payload(match) for match in collection.matches],
    }


def _load_catalog_safely(path: Path = DEFAULT_CHAMPION_CATALOG) -> ChampionCatalog | None:
    try:
        return load_catalog(path)
    except (OSError, ValueError, KeyError):
        return None


def _ensure_catalog(
    config: AppConfig,
    catalog: ChampionCatalog | None,
    collection: DayCollection,
    renderer: RendererLike | None,
    champion_names: dict[str, str],
) -> ChampionCatalog | None:
    """缺少英雄中文名或头像时自动刷新一次，并回填到对局与渲染器。"""
    needed = tuple(match.champion_key for match in collection.matches)
    refreshed = catalog
    if config.auto_update_champions:
        if catalog is None:
            try:
                refreshed = refresh_assets(DEFAULT_CHAMPION_CATALOG, DATA_AVATAR_DIR).catalog
            except Exception:  # noqa: BLE001 - 资源更新失败不阻断战报
                refreshed = None
        elif needed:
            refreshed = ensure_assets(
                catalog,
                needed,
                avatars_dir=DATA_AVATAR_DIR,
                marker_path=CHAMPION_REFRESH_MARKER,
                refresher=lambda: refresh_assets(DEFAULT_CHAMPION_CATALOG, DATA_AVATAR_DIR).catalog,
            )

    if refreshed is not None:
        # 无论是否发生刷新，都对本人与对位英雄名做一次本地化回填。
        champion_names.update({entry.key: entry.name for entry in refreshed})
        collection.matches = [
            replace(
                match,
                champion_name=refreshed.name_for(match.champion_key, match.champion_name),
                lane_opponent=_localize_performance(refreshed, match.lane_opponent),
                own_performance=_localize_performance(refreshed, match.own_performance),
            )
            for match in collection.matches
        ]
        if refreshed is not catalog and isinstance(renderer, OpggImageRenderer):
            renderer.catalog = refreshed
    return refreshed


def _localize_performance(
    catalog: ChampionCatalog, performance: ParticipantPerformance | None
) -> ParticipantPerformance | None:
    if performance is None:
        return None
    return replace(
        performance,
        champion_name=catalog.name_for(performance.champion_key, performance.champion_name),
    )


def run(
    config: AppConfig,
    day: date,
    provider_factory: Callable[[AccountConfig], ProviderLike] | None = None,
    output_dir: str | Path | None = None,
    renderer: RendererLike | None = None,
) -> RunResult:
    """逐个账号生成战报；单账号失败不影响其它账号，图片失败不阻断文案。"""
    catalog = _load_catalog_safely()
    champion_names = {entry.key: entry.name for entry in catalog} if catalog else {}
    factory = provider_factory or (
        lambda _account: OpggProvider(timezone=config.timezone, champion_names=champion_names)
    )
    active_renderer = renderer
    if active_renderer is None and config.render_image:
        active_renderer = OpggImageRenderer(catalog)

    target_dir = Path(output_dir or config.output_dir) / day.isoformat()
    result = RunResult(day=day)

    for account in config.accounts:
        if not account.enabled:
            continue
        try:
            provider = factory(account)
            collection = provider.collect_day(account.riot_id, day, region=account.region)
            if not collection.matches:
                # 当日无对局：不创建目录、不产出任何文件，仅记录跳过状态供日志输出。
                result.accounts.append(
                    AccountResult(account=account.riot_id, ok=True, skipped=True)
                )
                continue
            catalog = _ensure_catalog(config, catalog, collection, active_renderer, champion_names)
            stats = compute_daily_stats(
                collection.matches, minimum_games=config.minimum_games_for_judgement
            )
            titles = evaluate_titles(stats, collection.matches)
            details = compute_match_details(collection.matches)

            fetched_at = (
                collection.matches[0].fetched_at
                if collection.matches
                else datetime.now(ZoneInfo(config.timezone))
            )
            context = ReportContext(
                account=account.riot_id,
                day=day,
                window_start=collection.window_start,
                window_end=collection.window_end,
                complete=collection.complete,
                pages=collection.pages,
                warnings=tuple(collection.warnings),
                source_label=config.source_label,
                source_url=collection.page_url,
                fetched_at=fetched_at,
                timezone_label=config.timezone,
            )
            content = render_markdown(stats, context, titles, details=details)
            safe_name = _safe_filename(account.display_name)
            report_path = write_report(target_dir / f"{safe_name}.md", content)
            json_path = _write_json(
                target_dir / f"{safe_name}.json",
                _build_payload(account, collection, stats, titles, config),
            )

            image_path: str | None = None
            image_error: str | None = None
            if active_renderer is not None:
                image_name = f"{safe_name}.png"
                try:
                    image_path = str(
                        active_renderer.render(
                            target_dir / image_name,
                            stats,
                            context,
                            titles,
                            details=details,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - 图片失败不阻断文案产物
                    image_error = f"{type(exc).__name__}: {exc}"

            result.accounts.append(
                AccountResult(
                    account=account.riot_id,
                    ok=True,
                    matches=len(collection.matches),
                    report_path=str(report_path),
                    json_path=str(json_path),
                    image_path=image_path,
                    image_error=image_error,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单账号失败需记录并继续
            result.accounts.append(
                AccountResult(
                    account=account.riot_id,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 LOL 韩服主播每日战报")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    parser.add_argument("--date", default=None, help="统计日期（Asia/Shanghai），YYYY-MM-DD")
    parser.add_argument("--output-dir", default=None, help="覆盖配置中的输出目录")
    parser.add_argument("--no-image", action="store_true", help="跳过 PNG 生成")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        print(f"[ERROR] 读取配置失败: {exc}", file=sys.stderr)
        return 2
    if args.no_image:
        config = replace(config, render_image=False)

    timezone = ZoneInfo(config.timezone)
    day = date.fromisoformat(args.date) if args.date else datetime.now(timezone).date()
    result = run(config, day, output_dir=args.output_dir)

    for account in result.accounts:
        if account.skipped:
            print(f"[SKIP] {account.account}: 当日无对局，跳过产物生成")
        elif account.ok:
            suffix = ""
            if account.image_path:
                suffix = f" | 图片 {account.image_path}"
            elif account.image_error:
                suffix = f" | 图片失败: {account.image_error}"
            print(
                f"[OK] {account.account}: {account.matches} 场 -> {account.report_path}{suffix}",
            )
        else:
            print(f"[FAIL] {account.account}: {account.error}", file=sys.stderr)

    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
