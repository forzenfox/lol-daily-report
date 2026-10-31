# OP.GG 采集器设计方案（V2：HTTP Server Action 直取）

- 版本：V2.0（2026-10-21）
- 变更：**放弃 Playwright DOM 抓取**，改为直接重放页面自身的 `getGames` Server Action
- 依据：`request-inventory.md` R-09、`validation-report.md` §2.5
- 适用约束：仅使用 OP.GG（用户决策），风险与底线见 `source-policy-review.md`

## 1. 结论先行

`Show more` 确实会触发后台请求，且可被纯 HTTP 重放：

- 动作：Next.js Server Action，内部名 `getGames`，`next-action: 409a2b9ca50d15e50a4dace93552e3a40113dc2753`（当前构建）
- 请求体：`[{"locale":"en","region":"kr","puuid":"<puuid>","gameType":"TOTAL","endedAt":"<游标>","champion":""}]`
- 返回：RSC flight，`1:{"data":[ ... 20 场 ... ]}`
- 分页：`endedAt` = 上一批最老一场的 `created_at`，返回**严格更早**的 20 场
- 鉴权：**匿名可用**（无 Cookie，`credentials: omit` 已验证）
- 字段：`id`、`created_at`（KST）、`game_length`（秒）、`game_result`、`game_type`、`champion.key`、`role/position`、`stats.kda`、`stats.cs`

实测（Python `urllib`，非浏览器）：

```text
batch1: 20 场 2026-10-20T14:58:42+09:00 ~ 2026-10-17T13:40:03+09:00
batch2: 20 场 2026-10-17T13:14:43+09:00 ~ 2026-10-14T14:17:22+09:00
overlap: 0
```

## 2. 架构

```text
AccountConfig(riot_id, region)
        │
        ▼
[1] resolve_account
    GET /en/lol/summoners/{region}/{slug}
    ├─ 200 → 页面 HTML
    └─ 从 flight payload 提取 puuid
        │
        ▼
[2] get action_id
    ├─ 配置/环境变量 OPGG_GET_GAMES_ACTION_ID（优先）
    ├─ 本地缓存 .cache/opgg_action.json（按 build_id）
    └─ 自动发现：解析 chunk URL → 找到含 getGames 的 chunk → 邻近 40-hex
        │
        ▼
[3] 翻页采集 getGames
    cursor = "" → 20 场
    cursor = 最老一场 created_at → 更早 20 场
    直到：oldest < window_start | 批次 < 20 | 页数上限 | 时间预算
        │
        ▼
[4] 标准化 + 去重 + 窗口过滤 + 完整性标记
        │
        ▼
[5] CollectorStatus JSON + 标准对局列表
```

## 3. `getGames` 调用规范

### 3.1 请求

```http
POST https://op.gg/en/lol/summoners/{region}/{urlencode(name)}-{tag}
Headers:
  User-Agent: <固定 UA>
  Accept: text/x-component
  Content-Type: text/plain;charset=UTF-8
  next-action: <action_id>
  Origin: https://op.gg
  Referer: <页面 URL>
  Accept-Language: en-US,en;q=0.9
Body:
  [{"locale":"en","region":"kr","puuid":"...","gameType":"TOTAL","endedAt":"","champion":""}]
```

- 服务器可能返回 **307** 到无 locale 的路径（`/lol/summoners/...`）。必须**保留 POST 方法与请求体**跟随跳转（`requests` 默认对 307 保持方法）。
- 不携带 Cookie；不使用登录态。

### 3.2 响应解析

响应为 Next.js flight 文本，每行形如 `id:payload`。取包含 `"data"` 的行：

```python
for line in body.splitlines():
    if line.startswith("1:") and '"data"' in line:
        games = json.loads(line[2:])["data"]
```

- 若不存在该行 → `SCHEMA_CHANGED`。
- `data` 数组元素为纯 JSON，无 flight 引用（`$...`），可直接解析。
- 单批约 780 KB（主要是 `chartData`），解析后可丢弃 `chartData` 以降低内存。

### 3.3 分页

| 参数 | 含义 |
| --- | --- |
| `endedAt` | 游标，返回 `created_at < endedAt` 的对局；空字符串表示最新 |
| `gameType` | `TOTAL` / `SOLORANKED` / `FLEXRANKED` / `ARAM` … |
| `champion` | 英雄过滤，空字符串表示不过滤 |

停止条件（全部满足即停）：

1. 本批最老一场 `created_at` 早于窗口开始 → 已覆盖；
2. 本批不足 20 场 → 历史到底；
3. 达到 `max_pages`（默认 10，最多 200 场）；
4. 达到请求时间预算（默认 120s）。

## 4. 动作 ID 发现（最脆弱环节）

- 动作 ID **不在 HTML 中**，位于客户端 chunk（当前为 `1534-*.js`）：
  `createServerReference)("409a2b...","getGames")`
- 发现流程：
  1. 从 HTML 提取所有 `/_next/static/chunks/*.js`（当前 46 个）；
  2. 逐个抓取（并发 4，单请求超时 10s），找到包含 `getGames` 的 chunk；
  3. 在 `getGames` 前 500 字符窗口内取 40~42 位十六进制字符串；
  4. 用一次空游标调用验证，成功则写入缓存 `{build_id: action_id}`。
- `build_id` 从 chunk 路径 `releases/production-421-1-f4dc4e1d` 中提取；构建变化时自动失效。
- 缓存命中时**不扫描 chunk**，正常一次采集只有 1 次 HTML GET + N 次动作 POST。
- 配置覆盖：`OPGG_GET_GAMES_ACTION_ID` 可手工钉住，发现失败时优先使用。

## 5. 字段映射（`getGames` → 标准模型）

| 标准字段 | 来源 | 说明 |
| --- | --- | --- |
| `match_id` | `id` | `m_...=`，全局唯一 |
| `start_time` | `created_at` | ISO 8601，含 `+09:00` |
| `duration_seconds` | `game_length` | **秒**，无需文本解析 |
| `champion_key` | `champion.key` | 英文稳定键（`yorick`） |
| `champion_name` | `champion.name` | 展示用 |
| `win` | `game_result` | `WIN` / `LOSE` |
| `kills/deaths/assists` | `stats.kda.kill/death/assist` | 目标玩家口径 |
| `kda_ratio` | `stats.kda.kda` | 页面同口径 |
| `cs` | `stats.cs.totalCs` | 已含野怪 |
| `cs_per_min` | `stats.cs.csPerMin` | 可选 |
| `position` | `position` | `TOP` / `JUNGLE` / `MID` / `BOTTOM` / `SUPPORT` |
| `role` | `role` | `FIGHTER` / `MAGE` … |
| `queue_type` | `game_type.game_type` | `SOLORANKED` / `ARENA` … |
| `queue_label` | `game_type.game_translate` | 展示用 |
| `game_map` | `game_map` | `SUMMONERS_RIFT` … |
| `op_score` | `stats.op_score` | 可选 |
| `kill_participation` | `stats.killParticipation` | 百分比整数，可选 |
| `lane_score` | `stats.lane_score` | 可选 |
| `champion_level` | `stats.champion_level` | 可选 |
| `items/runes/spells/augments` | 同名 | 可选，首期不入图 |
| `source` | 常量 | `opgg-getgames` |
| `fetched_at` | 本项目时间 | 与 `start_time` 严格区分 |

- 队伍信息：`summoner_team`、`team_red`、`team_blue`（可选）。
- 不再需要 `participant_id` 反查：返回的就是目标玩家视角。

## 6. 回退策略

| 场景 | 回退 |
| --- | --- |
| 动作 ID 失效（400/404/500，或返回 HTML） | 重新发现 ID；再失败 → 使用 JSON-LD（10 场），`source=opgg-jsonld` |
| 动作返回结构变更 | 保存原始样本，退出并告警；不静默降级 |
| 页面 403/429/验证码 | 立即停止，不重试、不换代理 |
| JSON-LD 也缺失 | `SCHEMA_CHANGED`，退出 |

回退时战报必须标注 `collector=jsonld`，并按完整性规则标记 `complete`。

## 7. 完整性判定

```json
{
  "account": "斗鱼丶胡凯利#2862",
  "window": {"start": "2026-10-20T00:00:00+08:00", "end": "2026-10-21T00:00:00+08:00"},
  "collector": "getgames",
  "pages": 1,
  "rows_total": 20,
  "rows_in_window": 7,
  "oldest_seen": "2026-10-17T12:40:03+08:00",
  "complete": true,
  "warnings": []
}
```

1. `oldest_seen < window.start` → `complete=true`。
2. 触达 `max_pages` 或批次仍为 20 但 `oldest_seen >= window.start` → `complete=false`，`warnings += ["coverage_may_be_truncated"]`。
3. `rows_in_window == 0` 且 `rows_total > 0` → 正常"当天无对局"。
4. `rows_total == 0` 且请求成功 → `EMPTY_LIST`（与失败区分）。

## 8. 失败分类

| 分类 | 触发 | 行为 |
| --- | --- | --- |
| `HTTP_NOT_200` | HTML GET 非 200 | 5xx/超时重试 ≤2，指数退避 |
| `BLOCKED` | 401/403/429 | 不重试，退出并告警 |
| `CAPTCHA_OR_LOGIN` | 返回登录/验证页 | 退出并告警 |
| `ACTION_ID_INVALID` | 动作返回非 flight 或 400/404 | 重新发现一次，仍失败则回退 JSON-LD |
| `SCHEMA_CHANGED` | flight 无 `data`，或字段集缺失 | 退出并告警，保存样本 |
| `EMPTY_LIST` | 正常返回空数组 | 正常输出"当天无对局" |

## 9. 请求预算与调度

- 正常路径（动作 ID 已缓存）：每账号 **1 次 HTML GET + N 次动作 POST**（N = 覆盖窗口所需页数，通常 1~3）。
- 发现路径（构建变化后首跑）：额外 ~46 个 JS chunk GET（每个几十~百 KB），仅一次。
- 账号串行，账号间随机延迟 3~6 秒；每账号每日最多 1 次完整采集 + 1 次重试。
- 定时：上海时间 05:00（UTC `0 21 * * *`），支持 `workflow_dispatch`。
- GitHub Actions：纯 `requests`，无需 Playwright，CI 成本显著下降。

## 10. 配置增量

```yaml
source:
  provider: opgg
  locale: en
  http_timeout_seconds: 20
  max_retries: 2
  get_games:
    action_id: ""          # 可手工固定，留空则自动发现
    max_pages: 10
    time_budget_seconds: 120
    game_type: TOTAL
    cache_path: ".cache/opgg_action.json"
  rate_limit:
    per_account_per_day: 2
    inter_account_delay_seconds: 5
```

## 11. 测试策略（TDD）

1. **flight 解析**：真实响应截断 fixture，断言解析出 20 条、字段齐全、无 `$` 引用。
2. **映射**：断言 `game_length` → 秒、`champion.key` → 英雄键、`stats.cs.totalCs` → cs、`position`。
3. **游标翻页**：用两批 fixture，断言游标取最老 `created_at`、批间无重叠、停止条件正确。
4. **动作 ID 发现**：chunk fixture 中含 `createServerReference`，断言提取正确 ID；无 `getGames` 时返回 `None`。
5. **失败分类**：403 页面、flight 无 `data`、空数组三个 fixture。
6. **完整性**：`oldest_seen` 早于/晚于窗口、触达页数上限。
7. CI 默认离线；线上验证走手动触发并人工比对。

## 12. 风险与待验证

| # | 风险/待验证 | 处理 |
| --- | --- | --- |
| 1 | 动作 ID 随构建变化 | 自动发现 + 缓存 + 环境变量覆盖；失败即回退 JSON-LD |
| 2 | 动作请求未来可能要求 Cookie/签名 | 监控返回码；出现 401/403 立即停止并告警，不尝试绕过 |
| 3 | 游标语义变化 | 每次采集校验批间 `created_at` 单调递减、无重叠 |
| 4 | 代理/网络环境差异 | 已用 Python 直连验证；生产环境需复测一次 |
| 5 | 通过重放 Server Action 是否属于条款禁止的"自动化脚本" | 属于已有风险的同一范畴；维持低频、串行、失败即停 |
| 6 | 响应体积大（约 780 KB/批） | 解析后丢弃 `chartData`，仅保留报告所需字段 |
