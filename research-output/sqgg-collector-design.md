# SQGG 国服采集器详细设计

- 文档版本：V1.0（2026-10-30）
- 上游 PRD：[国服数据源接入_PRD.md](../国服数据源接入_PRD.md)
- 实证依据：[sqgg-feasibility.md](sqgg-feasibility.md)
- 参照实现：[opgg-collector-design.md](opgg-collector-design.md)（`src/providers/opgg.py`）
- 技术约束：仅用 Python 标准库；不引入浏览器、不引入新三方依赖；TDD 先测后码

## 1. 设计目标

1. 新增 `SqggProvider`，在不改动韩服链路的前提下，把国服账号接入现有 `run()` 流程。
2. 把"签到 → 会话 → 采集"封装为一个**有状态、有截止时间**的会话对象，任何越界请求立即失败。
3. 全部外部差异（Next-Action ID、REST 路径、Referer 要求、单会话轮换）收敛在 `src/providers/sqgg.py` 内，业务层零感知。
4. 可通过注入的 HTTP 接口做离线契约测试，测试不触网。

## 2. 现状与扩展点

| 现有资产 | 位置 | 复用方式 |
| --- | --- | --- |
| `ProviderLike` 协议 | `src/main.py` L44 | `SqggProvider.collect_day(riot_id, day, *, region)` 对齐 |
| `DayCollection` / `NormalizedMatch` / `ParticipantPerformance` | `src/providers/opgg.py` L71~133 | 直接复用，不新增数据类 |
| provider 工厂 | `src/main.py` L384~386 | 改为按账号 `provider` 字段分派 |
| 日报口径 `is_report_match` | `src/stats.py` | 通过 `queue_type="SOLORANKED"`、`game_map="SUMMONERS_RIFT"` 对齐 |
| 配置加载 | `src/main.py` L150 | 追加 `provider` / `server` / `environment` 字段，默认值保持韩服行为 |
| 日志与退出码 | `src/main.py` L500~515 | 新增"无权限"失败类型，退出码非 0 |

## 3. 总体架构

```text
main.run()
  └─ provider_factory(account)
       ├─ account.provider == "opgg" → OpggProvider（不变）
       └─ account.provider == "sqgg" → SqggProvider
                                        ├─ SqggSession（进程内单例，跨账号共享）
                                        │    ├─ TokenStore（.cache/sqgg-session.json）
                                        │    ├─ ActionIdResolver（.cache/sqgg_actions.json）
                                        │    └─ SqggHttp（注入，可替换为假实现）
                                        └─ collect_day()
                                             ├─ resolve_account → puuid
                                             ├─ GET /sqgg-api/sgp-service/lol-match
                                             └─ normalize → DayCollection
```

会话对象在一次 `run()` 内只有一个实例：多个国服账号共用同一 token 与同一访问窗口，签到只执行一次，任何"刷新令牌"动作（`userinfo`）也全进程只出现一次。

## 4. 模块设计

### 4.1 配置变更

```yaml
timezone: Asia/Shanghai
schedule:
  cron_utc: "0 21 * * *"

accounts:
  - display_name: "半醉与倦容"
    riot_id: "半醉与倦容#74796"
    provider: "sqgg"        # 新增；缺省 "opgg"
    server: "艾欧尼亚"       # 新增；sqgg 必填，站点中文大区名
    environment: "HN1"      # 可选；缺省由站点大区映射解析
    enabled: true
  - display_name: "斗鱼丶胡凯利"
    riot_id: "斗鱼丶胡凯利#2862"
    region: "kr"            # opgg 账号保持原样
    enabled: true

report:
  output_dir: "output"
  minimum_games_for_judgement: 3
  source_label: "OP.GG"     # 保持兼容；sqgg 账号按 provider 覆盖为 "SQGG"

sqgg:
  email_env: "SQGG_EMAIL"         # 必填，指向环境变量名
  password_env: "SQGG_PASSWORD"   # 必填
  token_env: "SQGG_TOKEN"         # 可选，优先级高于本地缓存
  count_per_page: 20              # 可选，默认 20
  max_pages: 5                    # 可选，默认 5
  timeout: 30                     # 可选，秒
  retries: 2                      # 可选，窗口内重试次数
  reserve_seconds: 300            # 可选，窗口结束前预留
  token_cache: ".cache/sqgg-session.json"
```

`AppConfig` 新增：

```python
@dataclass(frozen=True)
class AccountConfig:
    display_name: str
    riot_id: str
    region: str = "kr"
    provider: str = "opgg"
    server: str | None = None
    environment: str | None = None
    enabled: bool = True

@dataclass(frozen=True)
class SqggSettings:
    email_env: str = "SQGG_EMAIL"
    password_env: str = "SQGG_PASSWORD"
    token_env: str = "SQGG_TOKEN"
    count_per_page: int = 20
    max_pages: int = 5
    timeout: float = 30.0
    retries: int = 2
    reserve_seconds: int = 300
    token_cache: str = ".cache/sqgg-session.json"
```

校验规则：`provider == "sqgg"` 时 `server` 必填，否则 `load_config` 抛 `ValueError("sqgg 账号必须提供 server")`（对应 PRD FR-01 异常）。

### 4.2 动作 ID 发现

Next.js Server Action ID 随构建变化，不能硬编码。

```python
@dataclass(frozen=True)
class ActionIds:
    login: str
    checkin: str
    userinfo: str
    logout: str
```

发现流程（实测通过）：

```text
GET https://sqgg.cc/                      → 79 KB HTML
正则 /_next/static/chunks/app/layout-(\w+)\.js → 构建哈希 + chunk 路径
GET chunk                                  → 定位 "a.d(t,{BK:"
  ├─ (\w+):function\(\)\{return (\w+)\}     → C4→d, VZ→n, C0→u, vl→c
  └─ (\w+)=\(0,s\.\$\)\("([0-9a-f]{40})"\)  → d→ca18bf21…, n→bc7bfa13e…
```

- 缓存键为构建哈希，写入 `.cache/sqgg_actions.json`；哈希变化即重新发现。
- 环境变量覆盖：`SQGG_LOGIN_ACTION` / `SQGG_CHECKIN_ACTION` / `SQGG_USERINFO_ACTION`，便于站点改版时人工兜底。
- 解析失败抛 `SqggSchemaChanged("未能解析 Server Action 映射")`。

### 4.3 HTTP 客户端

```python
class SqggHttp(Protocol):
    def action(self, action_id: str, args: list[Any], *, page: str = "/") -> dict[str, Any]: ...
    def get_json(
        self,
        path: str,
        params: dict[str, str | int] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]: ...

class UrllibSqggHttp(SqggHttp):
    def __init__(self, timeout: float = 30.0, retries: int = 2, deadline: Callable[[], datetime] | None = None) -> None: ...
```

请求契约（实测值，必须逐项对齐）：

| 项 | 值 |
| --- | --- |
| 动作请求 URL | `https://sqgg.cc/` + `page`（`/` 即可） |
| 动作请求头 | `accept: text/x-component`、`content-type: text/plain;charset=UTF-8`、`next-action: <ID>`、`origin: https://sqgg.cc`、`referer: https://sqgg.cc/`、`user-agent: <浏览器 UA>` |
| 动作请求体 | `json.dumps(args)`，如 `["email","password"]`、`["<token>"]` |
| 动作响应 | `text/x-component`，逐行匹配 `^([0-9a-z]+):(.*)$`，取首个 `1:` 行 JSON |
| REST 请求头 | `authorization: Bearer <token>`（可选）、`referer: https://sqgg.cc/`（**必填**，否则 nginx 403 HTML）、`accept: */*` |
| REST 响应 | `application/json`，业务体 `{"status","message","data","code"?}` |

错误映射：

```python
def _raise_for_body(body: dict[str, Any]) -> None:
    if body.get("status") == "Success":
        return
    code = body.get("code")
    message = str(body.get("message") or "")
    if code == 401:
        raise SqggAuthError(message)          # invalid token / 账号在其它设备登录
    if code == 403:
        raise SqggAccessExpired(message)      # 捐助时长已到期
    raise SqggRequestError(message)           # 邮箱或密码错误 / 今日已签到 等由调用方判断
```

重试策略：仅对网络异常、5xx、`403 text/html`（WAF 抖动）重试；`401`/业务 `403` 不重试；每次重试前检查 `deadline`，超出即抛 `SqggAccessExpired("访问窗口余量不足")`。

### 4.4 会话管理

```python
@dataclass(frozen=True)
class AccessWindow:
    token: str
    start_time: datetime
    end_time: datetime
    rewards_daily_time: datetime | None

class TokenStore(Protocol):
    def load(self) -> AccessWindow | None: ...
    def save(self, window: AccessWindow) -> None: ...

class FileTokenStore(TokenStore):
    """原子写：写临时文件 → os.replace；权限 0o600；不入库（.gitignore 已含 .cache/）。"""
```

状态机：

```text
[无会话] --login--> [已登录]
[已登录] --now < end_time--> 可用
[已登录] --now >= end_time--> 到期（当日不可恢复）
[已登录] --401 被顶号--> 失效 --login--> 已登录
```

`ensure_access()` 算法（最小化请求与令牌轮换）：

```python
def ensure_access(now: datetime) -> AccessWindow:
    window = self.store.load()
    if window and now < window.end_time:
        return window                                   # 命中缓存，0 请求

    if window is None:                                  # 无令牌 → 登录
        window = self.login()                           # 登录返回 token/end_time/rewards_daily_time
        self.store.save(window)                         # 先落盘
        if window.rewards_daily_time == now.date() and now < window.end_time:
            return window
        return self._checkin(window)                    # 今日尚未签到

    try:                                                # 有令牌但已过期 → 尝试签到续期
        return self._checkin(window)
    except SqggAuthError:
        window = self.login()                           # 令牌失效 → 重新登录
        self.store.save(window)
        return self._checkin(window)


def _checkin(window: AccessWindow) -> AccessWindow:
    result = self.http.action(self.ids.checkin, [window.token])
    if result.get("status") == "Success":
        end = _parse_time(result["data"]["end_time"])
        updated = replace(window, end_time=end, rewards_daily_time=datetime.now(self.tz))
        self.store.save(updated)
        return updated
    if str(result.get("message")) == "今日已签到":
        info = self.http.action(self.ids.userinfo, [window.token])   # 唯一会轮换令牌的分支
        state = _window_from_userinfo(info)
        self.store.save(state)                                       # 先落盘新令牌
        return state
    raise SqggAccessExpired("签到失败")
```

关键约束：

- 签到动作只接受 token，没有时间参数；窗口完全由服务端时钟决定，脚本不得尝试注入时间或改写 `end_time`。
- `userinfo` 会签发新令牌并立刻使旧令牌失效，因此必须"拿到新令牌 → 立即 `save` → 再发后续请求"；任何异常都不得继续使用旧令牌。
- `ensure_access` 成功返回后，`SqggProvider` 记 `deadline = end_time - reserve_seconds`，所有后续请求与翻页都以此为准。
- 若 `now >= end_time`，直接抛 `SqggAccessExpired("访问窗口已结束")`，不发起任何数据请求（对应 PRD FR-02）。
- 若 `end_time - now < reserve_seconds`（默认 300 秒），同样抛 `SqggAccessExpired("访问窗口余量不足")`，避免产出必然不完整的日报。

### 4.5 账号解析与大区映射

```python
def resolve_account(self, server: str, game_name: str, tag_line: str) -> str:
    environment = self.environment_code(server)         # 配置优先，其次站点映射
    body = self.http.get_json(
        "/sqgg-api/sgp-service/summoner-by-name",
        params={"environment": environment, "gameName": game_name, "tagLine": tag_line},
        token=None,
    )
    data = body.get("data")
    if not data:
        raise SqggAccountNotFound(str(body.get("message") or "未找到该召唤师"))
    return data["puuid"]
```

大区映射解析：

- 来源：`GET https://sqgg.cc/` 的 RSC payload，正则
  `\{\\"label\\":\\"(.+?)\\",\\"value\\":\\"(HN\d+|TJ1\d+|NJ100|GZ100|CQ100|BGP2)\\"`
- 结果写入 `.cache/sqgg_environments.json`（34 条，`label→value`）。
- `summoner-by-name` 的 `environment` 必须是代码（`HN1`）；传中文名返回 `大区不存在`。
- 注意该接口正常时 `status=Success`、失败时同样 `status=Success` 但 `data=null` 且带 `message=该id不存在`，判定以 `data` 为准。

### 4.6 采集与分页

```python
def collect_day(self, riot_id: str, day: date | None = None, *, region: str = "cn") -> DayCollection:
    day = day or datetime.now(self.tz).date()
    window_start = datetime.combine(day, dt_time.min, tzinfo=self.tz)
    window_end = window_start + timedelta(days=1)

    access = self.session.ensure_access(datetime.now(self.tz))     # 可能抛 SqggAccessExpired
    deadline = access.end_time - timedelta(seconds=self.settings.reserve_seconds)

    game_name, tag_line = _split_riot_id(riot_id)
    puuid = self.resolve_account(self.server, game_name, tag_line)

    matches, pages, oldest_seen = [], 0, None
    exhausted = partial_failed = hit_cap = timed_out = False
    warnings: list[str] = []
    start_index = 0
    fetched_at = datetime.now(UTC)

    while pages < self.settings.max_pages:
        if datetime.now(self.tz) >= deadline:
            warnings.append("time_budget_exhausted")
            timed_out = True
            break
        try:
            body = self.http.get_json(
                "/sqgg-api/sgp-service/lol-match",
                params={"environment": self.environment, "puuid": puuid,
                        "count": self.settings.count_per_page,
                        "startIndex": start_index, "tag": "q_420"},
                token=access.token,
            )
        except SqggRequestError:
            if not matches:
                raise                                   # 一条都没有 → 账号失败
            warnings.append("partial_page_failure")      # 已有数据 → 输出不完整
            partial_failed = True
            break
        games = (body.get("data") or {}).get("games") or []
        if not games:
            exhausted = True
            break
        pages += 1
        for game in games:
            match = self._to_match(game, puuid, fetched_at)
            oldest_seen = min(oldest_seen, match.start_time) if oldest_seen else match.start_time
            if window_start <= match.start_time < window_end:
                matches.append(match)
        if len(games) < self.settings.count_per_page or (oldest_seen and oldest_seen < window_start):
            exhausted = True
            break
        start_index += len(games)
    else:
        hit_cap = True

    if hit_cap:
        warnings.append("coverage_may_be_truncated")

    reached_end = exhausted or (oldest_seen is not None and oldest_seen < window_start)
    complete = reached_end and not partial_failed and not timed_out
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
        page_url=self.page_url,
    )
```

停止条件（与 PRD FR-03 一致）：

1. 返回空列表；
2. 最老对局早于 `window_start`；
3. 本页不足 `count_per_page`；
4. 页数达到 `max_pages`；
5. 到达 `deadline`（窗口剩余不足预留时间）。

`complete` 计算：满足 1/2/3 之一为 `True`；4/5 触顶且未覆盖窗口起点则 `complete=False` 并写 `warnings`。

历史补跑：`day` 允许传入任意历史日期。站点对查询时间范围没有额外限制，因此窗口内即可补齐；目标日期越早，需要的 `startIndex` 越深，需相应调高 `max_pages`（默认 5 页 = 100 场，约覆盖 2 周）。集合只保留 `[window_start, window_end)` 内的对局，翻页在最老对局早于 `window_start` 时停止。

### 4.7 标准化映射

单局来源：`data.games[i]`，其中 `metadata.match_id` 为 `HN1_<gameId>`，`json` 为 Riot Match-V5 对象，`horse` 为 10 人站内评分。

| 字段 | 取值 | 备注 |
| --- | --- | --- |
| `match_id` | `metadata.match_id`，缺失回退 `HN1_{json.gameId}` | 与韩服一致的可读 ID |
| `start_time` | `datetime.fromtimestamp(json.gameStartTimestamp/1000, tz)` | 毫秒 epoch |
| `duration_seconds` | `json.gameDuration` | 秒 |
| `win` | 本人 `participant.win` | 布尔 |
| `champion_key` | `participant.championName.lower()` | 与英雄目录（小写 Data Dragon ID）一致 |
| `champion_name` | `participant.championName` | 由 `_ensure_catalog` 回填中文 |
| `kills/deaths/assists` | 同名 | |
| `cs` | `totalMinionsKilled + neutralMinionsKilled` | 合计口径 |
| `position` | `teamPosition or individualPosition or None` | 空串按 `None` |
| `queue_type` | `queueId==420 → "SOLORANKED"` | 其它队列不进入日报 |
| `queue_label` | `"单双排"` | |
| `game_map` | `mapId==11 → "SUMMONERS_RIFT"` | |
| `kda_ratio` | `(k+d)/max(d,1)` | 与 OP.GG 口径一致 |
| `cs_per_min` | `cs / (duration/60)` | 时长为 0 时为 `None` |
| `op_score` | `horse[puuid].score` | 站内评分，非 OP.GG 分值 |
| `op_score_rank` | 按 `horse[*].score` 降序，`1 + 严格大于本人分数的人数` | 并列同名次 |
| `kill_participation` | `(k+a)/队伍总击杀*100` | 队伍总击杀按 `teamId` 求和 |
| `own_performance` | 本人 participant + 队伍汇总 | 见下 |
| `lane_opponent` | 敌方队伍同 `teamPosition` 的唯一一人 | 无位置或多候选时 `None` |
| `source` | `"sqgg"` | 产物标注 |
| `fetched_at` | 采集时刻（UTC） | |

`ParticipantPerformance` 映射：

| 字段 | 取值 |
| --- | --- |
| `summoner_name` | `riotIdGameName#riotIdTagline`，缺失用 `summonerName` |
| `champion_key` / `champion_name` | 同上（小写键 + 英文名） |
| `position` | `teamPosition or individualPosition` |
| `team_key` | `teamId==100 → "BLUE"`，`200 → "RED"` |
| `team_kills` / `team_damage_to_champions` / `team_damage_taken` | 按 `teamId` 聚合 |
| `gold_earned` | `goldEarned` |
| `damage_to_champions` | `totalDamageDealtToChampions` |
| `damage_taken` | `totalDamageTaken` |
| `vision_score` | `visionScore` |
| `op_score` | `horse[puuid].score` |
| `win` | `win` |

结构校验（对应 PRD FR-03"结构变化"）：必需键

```python
REQUIRED_KEYS = ("metadata", "json", "horse")
REQUIRED_JSON_KEYS = ("gameId", "gameStartTimestamp", "gameDuration", "queueId", "participants", "teams")
REQUIRED_PARTICIPANT_KEYS = ("puuid", "championName", "kills", "deaths", "assists", "teamId", "win")
```

任一缺失抛 `SqggSchemaChanged`；找不到本人在 10 名参与者中时同样抛出（不静默跳过）。

### 4.8 main.py 集成

```python
def run(config, day, provider_factory=None, output_dir=None, renderer=None):
    ...
    sqgg_session: SqggSession | None = None

    def default_factory(account: AccountConfig) -> ProviderLike:
        nonlocal sqgg_session
        if account.provider == "sqgg":
            if sqgg_session is None:
                sqgg_session = build_sqgg_session(config.sqgg, timezone=config.timezone)
            return SqggProvider(session=sqgg_session, client=sqgg_session.http,
                                server=account.server, environment=account.environment,
                                timezone=config.timezone, settings=config.sqgg)
        return OpggProvider(timezone=config.timezone, champion_names=champion_names)
```

- 签到发生在首个 sqgg 账号的 `collect_day` 内，未配置 sqgg 账号时完全不触碰 sqgg。
- 单账号异常仍由 `run()` 的 `try/except` 捕获，写入 `AccountResult.error`，退出码非 0。
- `source` 字段：`_build_payload` 中 `provider=account.provider`；`ReportContext.source_label` 在账号级覆盖：
  `"SQGG"`（sqgg）/ 配置值（opgg）。
- CLI 不变：`python src/main.py --date YYYY-MM-DD`。

### 4.9 CI 变更

`.github/workflows/daily-report.yml` 追加：

```yaml
      - name: 生成战报
        env:
          TZ: Asia/Shanghai
          SQGG_EMAIL: ${{ secrets.SQGG_EMAIL }}
          SQGG_PASSWORD: ${{ secrets.SQGG_PASSWORD }}
          # 可选：sqgg 站点改版时人工固定动作 ID
          # SQGG_LOGIN_ACTION: ${{ vars.SQGG_LOGIN_ACTION }}
        run: python src/main.py --date "${TARGET_DATE}"
```

- 不缓存令牌：CI 每次运行登录一次，符合已确认的"会踢下线"预期；避免过期令牌导致二次登录。
- `timeout-minutes` 由 30 调整为 45（窗口内最多 2 次重试）。
- 作业失败即视为告警；不额外接入通知渠道。

## 5. 错误模型

| 异常 | 触发 | 上层表现 | 重试 |
| --- | --- | --- | --- |
| `SqggError` | 基类 | 账号失败 | 视子类 |
| `SqggAuthError` | `401 invalid token`、`401 账号在其它设备登录` | 账号失败；下一次任务自动登录 | 仅"重新登录后重试 1 次" |
| `SqggAccessExpired` | `403 捐助时长已到期`、`now >= end_time`、余量不足 | 账号失败，日志"访问窗口已结束" | 否 |
| `SqggAccountNotFound` | `data == null` 且 `message=该id不存在` | 账号失败，提示"s-server 或名称标签错误" | 否 |
| `SqggSchemaChanged` | 必需字段缺失、动作 ID 无法解析、RSC 无法解析 | 账号失败，提示结构变更 | 否 |
| `SqggRequestError` | 其它业务失败、HTTP 5xx、WAF HTML 403 | 账号失败 | 窗口内最多 2 次 |

## 6. 安全设计

- 凭据读取顺序：环境变量（`SQGG_EMAIL`/`SQGG_PASSWORD`/`SQGG_TOKEN`）→ 本地令牌缓存；配置文件与日志永不出现明文。
- 令牌缓存 `.cache/sqgg-session.json`：`0o600`、原子写、内容含邮箱（脱敏展示时只露前 2 位）。
- 日志脱敏函数 `mask_email()`、`mask_token()`：令牌仅输出 `前4…后4(长度)`。
- 产物 JSON 不写邮箱、不写令牌；`source.url` 只写召唤师页 URL。
- `.gitignore` 已忽略 `.cache/`，产物提交路径仅 `output/`。

## 7. TDD 测试设计

顺序：先写 `tests/test_sqgg_provider.py` 契约测试（全部离线，使用 `FakeSqggHttp`），再实现 `src/providers/sqgg.py`；最后补 `tests/test_main.py` 的集成用例。

| 编号 | 用例 | 断言要点 |
| --- | --- | --- |
| T-01 | 登录响应解析 | RSC 取 `1:` 行；返回 token/end_time/rewards_daily_time |
| T-02 | 登录失败 | `邮箱或密码错误` → `SqggRequestError`，不写缓存 |
| T-03 | 签到成功 | 写入 `end_time`，令牌不变 |
| T-04 | 已签到且窗口有效 | 调 `userinfo` 一次，**新令牌先落盘**，返回窗口 |
| T-05 | 已签到且窗口过期 | 抛 `SqggAccessExpired`，不发数据请求 |
| T-06 | 缓存令牌有效 | 0 次动作请求直接返回窗口 |
| T-07 | 令牌被顶号 | `401` → 重新登录 → 再签到，最多一次 |
| T-08 | 动作 ID 发现 | 解析 fixture chunk 得到 4 个 ID；构建哈希变化触发重解析 |
| T-09 | 大区映射 | 由首页 RSC 提取 34 条；中文名查不到时抛配置错误 |
| T-10 | 账号不存在 | `data=null` → `SqggAccountNotFound` |
| T-11 | 分页翻页 | `startIndex=0/20/40`，批间无重叠，覆盖窗口后停止 |
| T-12 | 窗口边界 | `00:00:00` 计入、`24:00:00` 不计入 |
| T-13 | 队列过滤 | 仅 `queueId==420` 进入结果，870/450 被丢弃 |
| T-14 | 标准化字段 | KDA/CS/位置/评分/参团率与 fixture 期望一致 |
| T-15 | `op_score_rank` 并列 | 同分同名次，范围 1~10 |
| T-16 | 对位匹配 | 同 `teamPosition` 唯一候选；缺失位置为 `None` |
| T-17 | 结构变更 | 删除 `json.participants` → `SqggSchemaChanged` |
| T-18 | Referer 契约 | 所有 REST 请求都带 `referer: https://sqgg.cc/` |
| T-19 | WAF 403 | `text/html` 403 视为可重试；重试 2 次后 `SqggRequestError` |
| T-20 | 时间预算 | `deadline` 触发 `complete=False` + 告警 |
| T-21 | 无排位对局 | 返回空列表 → `run()` 标记 skipped，不产出目录 |
| T-22 | 单账号失败隔离 | 一个 sqgg 账号抛错，另一个仍生成产物，退出码 1 |
| T-23 | 历史日期补跑 | 目标日期为历史日期时只归集该日窗口内的对局，翻到更早即停 |
| T-24 | 登录后已签到 | 登录响应显示今日已签到且窗口有效时，不再调用签到接口 |

Fixture 策略：以调研抓取的真实响应为样本，**替换 puuid、昵称、tagLine**，保留结构与数值；样本裁剪为 1~2 局，控制体积（单局原始约 114 KB，裁剪后 < 20 KB）。

## 8. 文件变更清单

| 文件 | 变更 |
| --- | --- |
| `src/providers/sqgg.py` | 新增：`SqggHttp`/`UrllibSqggHttp`、`ActionIdResolver`、`TokenStore`、`SqggSession`、`SqggProvider`、标准化函数、异常类 |
| `src/main.py` | `AccountConfig`/`AppConfig` 增加 `provider`/`server`/`environment`/`SqggSettings`；`load_config` 校验；`run()` 工厂分派；来源标注 |
| `config.yaml` | 增加 sqgg 示例账号与 `sqgg` 配置块（凭据仅引用环境变量名） |
| `.github/workflows/daily-report.yml` | 注入 `SQGG_EMAIL`/`SQGG_PASSWORD`，超时调整为 45 分钟 |
| `tests/test_sqgg_provider.py` | 新增 T-01~T-20 |
| `tests/test_main.py` | 追加 T-21、T-22 |
| `tests/fixtures/sqgg/*.json` | 新增脱敏样本：login、checkin、userinfo、lol-match、layout chunk 片段 |
| `README`/PRD | 运维说明补充 Secret 配置与窗口约束 |

## 9. 风险与回滚

| 风险 | 应对 |
| --- | --- |
| 站点构建变更导致动作 ID 失效 | 自动发现 + 环境变量覆盖 + 明确失败，不影响韩服链路 |
| 用户其它设备登录踢掉脚本 | 失败即在窗口内重登一次；仍失败则当日放弃 |
| 窗口被提前消耗（用户先签到） | 任务判定"窗口已结束"并失败，日志提示"当日签到已使用" |
| 大区名变更 | 映射每次任务实时解析，不硬编码 |
| 数据口径差异 | `op_score` 独立字段，不改动韩服历史产物；统计口径统一为 420 |
| 回滚 | 删除 sqgg 账号配置或 `provider` 置回 `opgg` 即恢复原状；sqgg 代码为独立模块，不侵入 OP.GG 路径 |

## 10. 开放问题

| 事项 | 影响 |
| --- | --- |
| 已确认账号：`热心市民张合#65360`（艾欧尼亚 `HN1`，puuid `4e009383-921f-54b2-82ec-5cddeaf0ec34`，`privacy=PRIVATE`）；验证样例 `半醉与倦容#74796` 是否纳入待定 | 决定 `config.yaml` 账号条目 |
| `[Assumption, TBD]` 单次任务内 sqgg 账号数量上限 | 建议 ≤ 4，保证在 1 小时窗口内完成（按每账号约 2 MB、2 次请求估算） |
| `[TBD]` 是否需要每日产物中提示"窗口限制" | 影响 Markdown 文案 |
