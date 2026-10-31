# 字段映射报告（OP.GG 玩家页 → 标准化对局字段）

- 调研对象：`op.gg` 韩服玩家页（Next.js App Router / RSC）
- 数据载体：服务端 HTML 内嵌的 JSON-LD（`application/ld+json`）+ RSC flight payload
- 样例：斗鱼丶胡凯利#2862，最近 10 场

## 1. 可用字段

单个对局在 JSON-LD 中的结构（真实样本，节选）：

```json
{
  "@type": "ListItem",
  "position": 1,
  "item": {
    "@type": "PlayGameAction",
    "name": "牧魂人 WIN - 单排/双排",
    "startTime": "2026-10-20T14:58:42+09:00",
    "agent": { "@id": "https://op.gg/lol/summoners/kr/{slug}#summoner" },
    "object": { "@id": "https://www.leagueoflegends.com/#game" },
    "additionalProperty": [
      { "name": "matchId", "value": "m_eaDVNbCj9XPNmwZU06uilzLPaKff7Qu8CAYKjKouk=" },
      { "name": "queueType", "value": "SOLORANKED" },
      { "name": "champion", "value": "牧魂人" },
      { "name": "result", "value": "WIN" },
      { "name": "kills", "value": 6 },
      { "name": "deaths", "value": 4 },
      { "name": "assists", "value": 1 },
      { "name": "kda", "value": "1.75:1" },
      { "name": "cs", "value": 279 },
      { "name": "opScore", "value": 4.8 },
      { "name": "opScoreRank", "value": 6 },
      { "name": "arenaPlacement", "value": "" }
    ]
  }
}
```

## 2. 映射表

| 标准化字段（PRD 6.3） | 必需性 | OP.GG 来源 | 字段路径 | 备注 |
| --- | --- | --- | --- | --- |
| `match_id` | 必需 | JSON-LD `additionalProperty` | `matchId`，如 `m_eaDVNbCj9XPNmw...=` | 全局唯一，可直接用于去重 |
| `game_start_time` | 必需 | JSON-LD `item.startTime` | ISO 8601，含 `+09:00` | 已带 KST 偏移，无需猜测时区 |
| `game_duration_seconds` | 推荐 | **仅 DOM 文本** | 行内文本形如 `28分钟18秒` | JSON-LD 无该字段，需解析文本或换算 |
| `account_puuid` | 推荐 | RSC payload | `puuid`（如 `nbJBkHxHe6KE...TYvWmg`） | 可从页面 RSC 中提取，非每场重复下发 |
| `champion_id` / `champion_name` | 必需 | JSON-LD `champion` + `<img alt>` | `牧魂人`；图片文件名 `Yorick.png` | 值为**本地化名称**，随 `hl`/locale 变化；稳定 ID 需另建映射表 |
| `win` | 必需 | JSON-LD `result` | `WIN` / `LOSE` | 映射为布尔值 |
| `kills` / `deaths` / `assists` | 必需 | JSON-LD 同名属性 | 数值型 | 目标玩家口径，非全队 |
| `cs` | 推荐 | JSON-LD `cs` | 数值型 | 已是合计值；页面另显示每分钟值 |
| `game_mode` | 推荐 | JSON-LD `queueType` | `SOLORANKED` / `ARENA` 等 | 见下表 |
| `source` | 必需 | 本项目赋值 | `op.gg-ssr` | — |
| `fetched_at` | 必需 | 本项目采集时间 | — | 页面另有"上次更新: 15分钟前"提示 |
| `raw_reference` | 可选 | JSON-LD 原始对象 | — | 建议仅调试期保存 |

## 2.1 `getGames` 字段（主路径，2026-10-21 补充）

`getGames` 返回的 `data[]` 每项即目标玩家视角的一局，字段比 JSON-LD 更完整：

| 标准字段 | 来源 |
| --- | --- |
| `match_id` | `id` |
| `start_time` | `created_at`（含 `+09:00`） |
| `duration_seconds` | `game_length`（整数秒，如 `1698`） |
| `win` | `game_result`（`WIN`/`LOSE`） |
| `champion_key` / `champion_name` | `champion.key` / `champion.name`（如 `yorick` / `Yorick`） |
| `kills/deaths/assists` | `stats.kda.kill` / `.death` / `.assist` |
| `kda_ratio` | `stats.kda.kda`（如 `1.75`） |
| `cs` | `stats.cs.totalCs`（如 `279`） |
| `cs_per_min` | `stats.cs.csPerMin` |
| `position` / `role` | `position`（`TOP` 等） / `role`（`FIGHTER` 等） |
| `queue_type` / `queue_label` | `game_type.game_type` / `game_type.game_translate` |
| `game_map` | `game_map`（`SUMMONERS_RIFT` 等） |
| `op_score` / `kill_participation` / `lane_score` | `stats.op_score` / `stats.killParticipation` / `stats.lane_score` |
| 装备/符文/召唤师技能/强化 | `items` / `runes` / `spells` / `augments`（可选） |

- 响应约 780 KB/20 场，体积主要来自 `chartData`（逐分钟经济曲线），解析后可丢弃。
- 无需再解析 DOM、时长文本或本地化英雄名。

## 3. 队列类型观察

- 样例账号最近 20 场全部为 `SOLORANKED`（该账号近期只打单双排）。
- 交叉验证 `Hide on bush#KR1`：10 场中 `SOLORANKED` 9 场 + `ARENA` 1 场 → **默认列表为混合队列**，不只排位。
- 页面可见筛选项：全部 / 排位赛 / 极地大乱斗 / 斗魂竞技场；筛选发生在客户端，未在服务端 URL 上生效。

## 3.1 英文页（`/en/`）格式对照

固定英文 locale 后，字段文本变得可稳定解析：

| 内容 | 中文页 | 英文页 |
| --- | --- | --- |
| 英雄名 | `牧魂人` | `Yorick` |
| 胜负 | `勝利` / `敗北` | `Victory` / `Defeat` |
| 时长 | `28分钟18秒` | `28m 18s` |
| 队列 | `单排/双排` | `Ranked Solo/Duo` |
| 分页按钮 | `显示更多` | `Show more` |
| 行展开按钮 | `Show More Detail Games` | `Show More Detail Games` |
| 开始时间提示 | `data-tooltip-content="2026/10/20 13:58"` | `data-tooltip-content="10/20/2026, 1:58 PM"`（浏览器本地时区） |

- 开始时间提示按**运行机器时区**渲染：本机 UTC+8 显示 `1:58 PM`，真实 KST 为 `14:58`。浏览器采集必须设置 `timezone_id="Asia/Seoul"`。
- 行内 `a[href*="/lol/champions/"]` 给出 `yorick` 这类**语言无关 slug**，建议作为英雄主键。

## 3.2 DOM 行结构（浏览器采集用）

| 字段 | 定位方式 | 示例 |
| --- | --- | --- |
| `match_id` | `[data-tooltip-id^="op-keyword-tooltip-"]` 去掉前缀 | `m_eaDVNbCj9XPNmwZU06uilzLPaKff7Qu8CAYKjKouk=` |
| `champion_key` | `a[href*="/lol/champions/"]` | `/lol/champions/yorick/build` |
| `start_time` | `span[data-tooltip-content]` 中符合日期格式者 | `10/20/2026, 1:58 PM`（需 KST） |
| `win` | 行文本 `Victory` / `Defeat` | — |
| `kills/deaths/assists` | 正则 `(\d+)\s*/\s*(\d+)\s*/\s*(\d+)` | `6 / 4 / 1` |
| `cs` | 正则 `CS\s*(\d+)` | `CS 279 (9.9)` |
| `duration_seconds` | 正则 `(\d+)m\s*(\d+)s` | `28m 18s` |

- `match_id` 在行未展开时即可读取，无需逐行点击。

## 4. 数据质量与陷阱

| 陷阱 | 说明 | 处理建议 |
| --- | --- | --- |
| 英雄名为本地化文本 | 中文页面返回 `牧魂人`，英文页面返回 `Yorick` | 固定 `hl` 参数或建立名称映射；不要用名称做主键 |
| 时长不在结构化数据中 | 仅中文文本 `28分钟18秒` | 适配器需解析，且需覆盖 `1小时2分钟` 等格式 |
| 字段缺省 vs 0 | `arenaPlacement` 空字符串代表"非竞技场对局" | 区分"未返回/不适用"与数值 0，不得默认补 0 |
| 列表条数有限 | 无 JS 时仅 10 场；浏览器首屏 20 场；点击 "Show more" 后 40 场，且不产生网络请求 | 必须校验是否覆盖当日完整窗口，否则标记"不完整"；不要假设点击次数 ×20 |
| 队内重名/相似 ID | 页面含 10 名玩家名称，需确认目标玩家 | 以 `agent`/PUUID 定位，勿用名称匹配 |
| 页面更新时间 | 页面显示"上次更新: 15分钟前"，非对局入榜时间 | 采集时间 ≠ 比赛时间，须分开记录 |
| 不支持的模式 | 帮助中心：Clash、含 AI 机器人对局、ARAM:Mayhem 等无数据 | 战报中声明覆盖范围，避免虚报完整 |

## 5. 字段覆盖结论

- PRD 的**全部必需字段均可获得**：`match_id`、`game_start_time`、`champion`、`win`、`kills/deaths/assists`。
- 推荐字段中 `cs` 可得，`duration` 需文本解析，`game_mode` 以 `queueType` 提供。
- 缺失：精确位置（上单/打野等）、队伍分边、金币/伤害等扩展指标（DOM 中有部分展示但非结构化）。
