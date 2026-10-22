#!/usr/bin/env python3
"""验证 OP.GG `getGames` Server Action 的数据获取可行性。

只依赖 Python 标准库，不需要浏览器、Cookie 或 API Key。

用法示例：
    .venv\\Scripts\\python.exe scripts\\verify_opgg_getgames.py \
        --riot-id "斗鱼丶胡凯利#2862" --pages 3

退出码：
    0 = 全部检查通过
    1 = 存在硬性失败
    2 = 部分检查失败（如无法交叉核对 JSON-LD）
"""

from __future__ import annotations

import argparse
import gzip
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_RIOT_ID = "斗鱼丶胡凯利#2862"
DEFAULT_REGION = "kr"
DEFAULT_LOCALE = "en"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
SHANGHAI_TZ = timezone(timedelta(hours=8))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用自动跳转，便于对 307 保留 POST 方法和 body 手动重发。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_body(resp: http.client.HTTPResponse) -> bytes:
    """读取响应体；RSC 为分块传输，连接提前关闭时保留已读部分。"""
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


def http_get(url: str, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, _read_body(resp).decode("utf-8", "replace")


def http_get_retry(url: str, timeout: float, attempts: int = 3) -> tuple[int, str]:
    """GET 带有限重试，覆盖代理/握手抖动。"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return http_get(url, timeout)
        except Exception as exc:  # noqa: BLE001 - 记录最后一次异常后重试
            last_error = exc
            if attempt < attempts:
                time.sleep(1.5 * attempt)
    assert last_error is not None
    raise last_error


def http_post_flight(
    url: str,
    body: list[dict[str, Any]],
    action_id: str,
    timeout: float,
    max_redirects: int = 3,
) -> tuple[int, str, str]:
    """发送 getGames 动作请求，手动处理 307/308（保留 POST 与 body）。"""
    opener = urllib.request.build_opener(NoRedirect)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/x-component",
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "next-action": action_id,
        "Origin": "https://op.gg",
        "Referer": url,
    }
    current = url
    for _ in range(max_redirects + 1):
        req = urllib.request.Request(current, data=payload, method="POST", headers=headers)
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, _read_body(resp).decode("utf-8", "replace"), current
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 307, 308):
                location = exc.headers.get("Location")
                if not location:
                    raise
                current = urllib.parse.urljoin(current, location)
                continue
            raise
    raise RuntimeError("too many redirects")


def parse_flight_data(body: str) -> list[dict[str, Any]] | None:
    """从 Next.js flight 文本中取出 data 数组。"""
    for line in body.splitlines():
        match = re.match(r"^[0-9a-z]+:(.*)$", line)
        if not match:
            continue
        payload = match.group(1)
        if '"data"' not in payload:
            continue
        try:
            data = json.loads(payload)["data"]
        except (json.JSONDecodeError, KeyError):
            continue
        if isinstance(data, list):
            return data
    return None


def find_puuid(html: str) -> str | None:
    match = re.search(r'puuid\\?"?\s*:\s*\\?"?([A-Za-z0-9_\-]{40,120})', html)
    return match.group(1) if match else None


def find_build_id(html: str) -> str | None:
    match = re.search(r"releases/([A-Za-z0-9._-]+)/", html)
    return match.group(1) if match else None


def extract_chunk_urls(html: str) -> list[str]:
    urls = re.findall(r'https://[^"\']+/_next/static/chunks/[^"\']+\.js', html)
    return list(dict.fromkeys(urls))


def discover_action_id(
    html: str, timeout: float, max_chunks: int
) -> tuple[str | None, int, str | None]:
    """扫描 JS chunk，定位 createServerReference("<id>", ..., "getGames")。"""
    chunks = extract_chunk_urls(html)[:max_chunks]
    for index, url in enumerate(chunks, start=1):
        try:
            _, script = http_get(url, timeout)
        except Exception:  # noqa: BLE001, S112 - 单个 chunk 失败不影响整体发现
            continue
        if "getGames" not in script:
            continue
        pos = script.find("getGames")
        window = script[max(0, pos - 600) : pos + 50]
        match = re.search(r'"([0-9a-f]{40,42})"', window)
        if match:
            return match.group(1), index, url
    return None, len(chunks), None


def parse_jsonld_games(html: str) -> list[dict[str, Any]]:
    """解析页面内嵌 JSON-LD 的对局（用于交叉核对）。"""
    blocks = re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    games: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("@type") == "ListItem" and isinstance(node.get("item"), dict):
            item = node["item"]
            props = {
                p.get("name"): p.get("value")
                for p in item.get("additionalProperty", [])
                if isinstance(p, dict)
            }
            if props.get("matchId"):
                games.append(
                    {
                        "id": props.get("matchId"),
                        "result": props.get("result"),
                        "kills": props.get("kills"),
                        "deaths": props.get("deaths"),
                        "assists": props.get("assists"),
                        "cs": props.get("cs"),
                        "start": item.get("startTime"),
                    }
                )
        for value in node.values():
            walk(value)

    for block in blocks:
        try:
            walk(json.loads(block))
        except json.JSONDecodeError:
            continue
    return games


def games_in_window(
    games: list[dict[str, Any]], day: str | None
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    if not day:
        return [], {}
    start = datetime.fromisoformat(day).replace(tzinfo=SHANGHAI_TZ)
    end = start + timedelta(days=1)
    picked = []
    for game in games:
        try:
            ts = datetime.fromisoformat(game["created_at"])
        except (KeyError, ValueError):
            continue
        if start <= ts < end:
            picked.append(game)
    return picked, {"start": start.isoformat(), "end": end.isoformat()}


@dataclass
class Report:
    checks: list[dict[str, Any]] = field(default_factory=list)
    batches: list[dict[str, Any]] = field(default_factory=list)
    games: list[dict[str, Any]] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "", hard: bool = True) -> None:
        self.checks.append({"name": name, "ok": ok, "detail": detail, "hard": hard})

    @property
    def hard_failed(self) -> bool:
        return any(not c["ok"] and c["hard"] for c in self.checks)

    @property
    def soft_failed(self) -> bool:
        return any(not c["ok"] and not c["hard"] for c in self.checks)


REQUIRED_PATHS = {
    "id": ("id",),
    "created_at": ("created_at",),
    "game_length": ("game_length",),
    "game_result": ("game_result",),
    "queue_type": ("game_type", "game_type"),
    "champion_key": ("champion", "key"),
    "kills": ("stats", "kda", "kill"),
    "deaths": ("stats", "kda", "death"),
    "assists": ("stats", "kda", "assist"),
    "cs": ("stats", "cs", "totalCs"),
    "position": ("position",),
}


def nested(game: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = game
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def run(args: argparse.Namespace) -> Report:
    report = Report()
    name, _, tag = args.riot_id.partition("#")
    slug = f"{urllib.parse.quote(name, safe='')}-{urllib.parse.quote(tag, safe='')}"
    page_url = f"https://op.gg/{args.locale}/lol/summoners/{args.region}/{slug}"

    started = time.time()
    status, html = http_get_retry(page_url, args.timeout)
    report.check("页面 GET 200", status == 200, f"status={status}, html={len(html)}B")

    puuid = find_puuid(html)
    report.check("页面包含 puuid", bool(puuid), puuid or "未找到")
    if not puuid:
        return report

    build_id = find_build_id(html)
    report.check("可识别构建号", bool(build_id), build_id or "未找到", hard=False)

    action_id = args.action_id or None
    scanned = 0
    if action_id:
        report.check("使用手工指定 action_id", True, action_id, hard=False)
    else:
        action_id, scanned, chunk_url = discover_action_id(html, args.timeout, args.max_chunks)
        detail = f"扫描 {scanned} 个 chunk" + (
            f"，命中 {chunk_url.split('/')[-1]}" if chunk_url else ""
        )
        report.check("发现 getGames action_id", bool(action_id), detail)
    if not action_id:
        return report

    games: list[dict[str, Any]] = []
    cursor = ""
    previous_oldest: str | None = None
    for page in range(1, args.pages + 1):
        body = [
            {
                "locale": args.locale,
                "region": args.region,
                "puuid": puuid,
                "gameType": args.game_type,
                "endedAt": cursor,
                "champion": "",
            }
        ]
        try:
            status, text, final_url = http_post_flight(page_url, body, action_id, args.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 401, 403):
                report.check(f"第 {page} 页请求成功", False, f"HTTP {exc.code}（不重试）")
                break
            time.sleep(2)
            try:
                status, text, final_url = http_post_flight(page_url, body, action_id, args.timeout)
            except Exception as retry_exc:  # noqa: BLE001 - 重试后仍失败则记录
                report.check(
                    f"第 {page} 页请求成功",
                    False,
                    f"{type(retry_exc).__name__}: {retry_exc}",
                )
                break
        except Exception:  # noqa: BLE001 - 网络抖动重试一次
            time.sleep(2)
            try:
                status, text, final_url = http_post_flight(page_url, body, action_id, args.timeout)
            except Exception as retry_exc:  # noqa: BLE001
                report.check(
                    f"第 {page} 页请求成功",
                    False,
                    f"{type(retry_exc).__name__}: {retry_exc}",
                )
                break
        batch = parse_flight_data(text)
        if not batch:
            report.check(
                f"第 {page} 页 flight 可解析",
                False,
                f"status={status}, body={text[:120]!r}",
            )
            break

        ordered = all(
            batch[i]["created_at"] > batch[i + 1]["created_at"] for i in range(len(batch) - 1)
        )
        report.check(
            f"第 {page} 页返回 20 场",
            len(batch) == 20,
            f"实际 {len(batch)}",
            hard=False,
        )
        report.check(
            f"第 {page} 页内时间倒序",
            ordered,
            f"{batch[0]['created_at']} -> {batch[-1]['created_at']}",
        )
        if previous_oldest is not None:
            report.check(
                f"第 {page} 页与上一页无重叠",
                batch[0]["created_at"] < previous_oldest,
                f"本页最新 {batch[0]['created_at']} < 上页最老 {previous_oldest}",
            )

        report.batches.append(
            {
                "page": page,
                "count": len(batch),
                "newest": batch[0]["created_at"],
                "oldest": batch[-1]["created_at"],
                "final_url": final_url,
            }
        )
        games.extend(batch)
        previous_oldest = batch[-1]["created_at"]
        cursor = previous_oldest
        if len(batch) < 20:
            break

    ids = [game.get("id") for game in games]
    report.check(
        "match_id 全局唯一",
        len(ids) == len(set(ids)),
        f"{len(ids)} 条，去重后 {len(set(ids))} 条",
    )
    report.check("共获取到对局", bool(games), f"{len(games)} 场")

    missing_counter: dict[str, int] = {}
    for game in games:
        for label, path in REQUIRED_PATHS.items():
            if nested(game, path) is None:
                missing_counter[label] = missing_counter.get(label, 0) + 1
    report.check(
        "必需字段完整",
        not missing_counter,
        "缺失统计: " + json.dumps(missing_counter, ensure_ascii=False)
        if missing_counter
        else "全部齐全",
    )

    if args.day and games:
        in_window, window = games_in_window(games, args.day)
        oldest = games[-1]["created_at"]
        covered = (
            oldest < window["start"]
            or len(report.batches[-1:] or [])
            and report.batches[-1]["count"] < 20
        )
        report.check(
            f"覆盖 {args.day}（Asia/Shanghai）",
            covered,
            f"窗口 {window['start']} ~ {window['end']}，当日 {len(in_window)} 场，最老 {oldest}",
            hard=False,
        )

    if args.cross_check:
        jsonld = parse_jsonld_games(html)
        by_id = {game["id"]: game for game in games}
        mismatches = []
        for item in jsonld:
            actual = by_id.get(item["id"])
            if not actual:
                mismatches.append(f"{item['id']}: getGames 未返回")
                continue
            pairs = {
                "result": (item.get("result"), actual.get("game_result")),
                "kills": (item.get("kills"), nested(actual, ("stats", "kda", "kill"))),
                "deaths": (
                    item.get("deaths"),
                    nested(actual, ("stats", "kda", "death")),
                ),
                "assists": (
                    item.get("assists"),
                    nested(actual, ("stats", "kda", "assist")),
                ),
                "cs": (item.get("cs"), nested(actual, ("stats", "cs", "totalCs"))),
            }
            for label, (expected, got) in pairs.items():
                if expected is not None and got is not None and expected != got:
                    mismatches.append(f"{item['id']}: {label} JSON-LD={expected} getGames={got}")
        report.check(
            "与 JSON-LD 交叉核对",
            not mismatches,
            f"核对 {len(jsonld)} 场"
            + (f"，差异 {len(mismatches)} 项: {mismatches[:3]}" if mismatches else "，全部一致"),
            hard=False,
        )

    report.games = games
    report.checks.append(
        {
            "name": "耗时",
            "ok": True,
            "detail": f"{time.time() - started:.1f}s，扫描 chunk {scanned} 个",
            "hard": False,
        }
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 OP.GG getGames 动作的数据获取可行性")
    parser.add_argument("--riot-id", default=DEFAULT_RIOT_ID, help='例如 "斗鱼丶胡凯利#2862"')
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--locale", default=DEFAULT_LOCALE, choices=["en", "zh-cn", "ko"])
    parser.add_argument("--pages", type=int, default=3, help="最多翻页数（每页 20 场）")
    parser.add_argument("--game-type", default="TOTAL")
    parser.add_argument(
        "--day", default=None, help="校验指定日期覆盖（Asia/Shanghai），格式 YYYY-MM-DD"
    )
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--max-chunks", type=int, default=60)
    parser.add_argument("--action-id", default="", help="手工指定 next-action，跳过发现")
    parser.add_argument("--no-cross-check", dest="cross_check", action="store_false", default=True)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    report = run(args)

    print("=" * 72)
    print("OP.GG getGames 可行性验证")
    print("=" * 72)
    for item in report.checks:
        mark = "PASS" if item["ok"] else ("FAIL" if item["hard"] else "WARN")
        print(f"[{mark}] {item['name']}: {item['detail']}")
    if report.batches:
        print("-" * 72)
        for batch in report.batches:
            print(
                f"page {batch['page']}: {batch['count']} 场 | "
                f"{batch['newest']} -> {batch['oldest']}"
            )
    if report.games:
        first = report.games[0]
        print("-" * 72)
        print(
            "样本首场: "
            + json.dumps(
                {
                    "id": first.get("id"),
                    "created_at": first.get("created_at"),
                    "game_length": first.get("game_length"),
                    "result": first.get("game_result"),
                    "champion": nested(first, ("champion", "key")),
                    "position": first.get("position"),
                    "kda": [
                        nested(first, ("stats", "kda", "kill")),
                        nested(first, ("stats", "kda", "death")),
                        nested(first, ("stats", "kda", "assist")),
                    ],
                    "cs": nested(first, ("stats", "cs", "totalCs")),
                },
                ensure_ascii=False,
            )
        )

    if args.json_out:
        payload = {
            "argv": vars(args),
            "checks": report.checks,
            "batches": report.batches,
            "game_count": len(report.games),
            "first_game": report.games[0] if report.games else None,
        }
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"报告已写入 {args.json_out}")

    if report.hard_failed:
        return 1
    if report.soft_failed:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
