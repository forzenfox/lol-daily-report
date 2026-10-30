# sqgg.cc 国服数据源可行性调研

- 文档版本：V1.0
- 调研日期：2026-10-30（CST 12:40 ~ 14:00）
- 调研对象：`https://sqgg.cc/`（「英雄联盟全大区隐藏战绩查询网站」）
- 调研方式：TRAE-browseruse 浏览器抓包 + 前端 JS 静态分析 + Python 纯 HTTP 重放
- 样例账号：半醉与倦容#74796（艾欧尼亚 / `HN1` / puuid `5d1a679b-f085-5e55-942c-abd8938b1274`）
- 测试登录账号：`935383455@qq.com`（由用户提供）
- 调研目的：判断能否用脚本自动完成「登录 → 每日签到 → 战绩查询」，作为国服日报的数据源

## 0. 结论摘要

| 维度 | 结论 |
| --- | --- |
| 纯 HTTP 自动化 | ✅ 可行，全流程无需浏览器（登录、签到、查询均可用 Python `urllib` 完成） |
| 数据字段覆盖 | ✅ 优于 OP.GG：返回 Riot Match-V5 完整对局对象（10 名参与者、队伍、目标物、天赋、装备等 100+ 字段），并附带站内 OP 风格评分 |
| 账号解析 | ✅ 34 个大区映射内嵌在首页 RSC payload 中，`summoner-by-name` 无需鉴权即可换 puuid |
| 分页 | ✅ `count` / `startIndex` 游标翻页，实测无重叠，单次最多 100 场 |
| 访问权限 | ⚠️ **每日签到只授予约 1 小时访问窗口**；到期后所有数据接口返回 `403 捐助时长已到期`，且当天无法再次签到 |
| 会话模型 | ⚠️ **单会话强绑定**：任何一次登录 / 用户信息刷新都会签发新 token 并使旧 token 立即失效（`401 账号在其它设备登录`） |
| 稳定性 | ⚠️ 登录/签到依赖 Next.js Server Action ID（随构建变化，需自动发现）；数据接口前置 nginx WAF，缺少 `Referer` 直接 `403` |
| 合规 | ⚠️ `robots.txt` 允许全站抓取；服务条款要求「已获得被查询账号持有人授权」，禁止商业牟利 |
| 总体判定 | **技术可行、生产可用性中等偏低**：适合"签到即采集"的每日一次性任务，不适合全天候/重试型采集 |

> 重要事实：本账号今日 12:50:46 已完成签到（授予至 13:50:46）。调研期间访问窗口已到期，此后 `lol-match` 与 `rank` 均返回 `捐助时长已到期`，且签到返回 `今日已签到`，**当天无法恢复查询权限**。

## 1. 站点技术架构

```text
nginx/1.24.0 (Ubuntu)
└─ Next.js App Router（RSC + Server Actions）
   ├─ 页面：/、/summoner/{大区中文名}?gameName=&tagLine=&puuid=
   ├─ Server Actions（POST 页面 URL + Next-Action 头，响应 text/x-component）
   │  ├─ 登录 / 注册 / 改密 / 邀请码 / 退出
   │  ├─ 签到 / 用户信息刷新（续期）
   │  └─ 页面刷新类动作
   └─ REST：/sqgg-api/**（application/json，Bearer 鉴权，nginx 前置 WAF）
```

- 前端状态存放在 `localStorage.app-storage`（Zustand 持久化），结构为
  `state.userInfo = { email, status, token, start_time, end_time, rewards_daily_time }`。
- 无 Cookie 会话；登录成功不返回 `Set-Cookie`，鉴权完全依赖请求头 `Authorization: Bearer <JWT>`。
- 数据接口响应头含 `Access-Control-Allow-Origin: *`，但 nginx 层对缺失 `Referer` 的请求直接返回 `403 Forbidden`。

## 2. 请求清单

| 编号 | 动作 | 请求 | 方法 | 鉴权 | 实测结果 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| R-01 | 打开首页 | `GET https://sqgg.cc/` | GET | 无 | 200，79 KB HTML，内嵌 34 个大区映射 | ✅ |
| R-02 | 登录 | `POST https://sqgg.cc/` + `Next-Action: bc7bfa13…`，body `["email","password"]` | POST | 无（凭据在 body） | 200 RSC，返回 `userInfo`（含 token） | ✅ 纯 HTTP 可复现 |
| R-03 | 用户信息/续期 | 同 URL + `Next-Action: e47f3b49…`，body `["<token>"]` | POST | token | 200，返回**新 token**（旧 token 立即失效） | ⚠️ 令牌轮换 |
| R-04 | 签到 | 同 URL + `Next-Action: ca18bf21…`，body `["<token>"]` | POST | token | 200，`{"status":"Success","data":{"end_time":...}}`；重复签到 `今日已签到` | ✅ |
| R-05 | 退出登录 | 同 URL + `Next-Action: 79a0cbd7…` | POST | token | 200 | ✅ |
| R-06 | 账号解析 | `GET /sqgg-api/sgp-service/summoner-by-name?environment=HN1&gameName=&tagLine=` | GET | **无** | 200，返回 puuid / level / profileIconId 等 | ✅ 免鉴权 |
| R-07 | 对局列表 | `GET /sqgg-api/sgp-service/lol-match?environment=HN1&puuid=&count=&startIndex=&tag=` | GET | Bearer | 200，完整 Match-V5 对象 | ✅ 核心接口 |
| R-08 | 段位 | `GET /sqgg-api/sgp-service/rank?environment=HN1&puuid=` | GET | Bearer | 200，`queues`/`seasons` | ✅ |
| R-09 | 英雄成就 | `POST /sqgg-api/sgp-service/champ-masteries`，body `{puuid,environment,limit}` | POST | Bearer | 200，`[{championId,championLevel,championPoints}]` | ✅ |
| R-10 | 玩家综合数据 | `POST /sqgg-api/sgp-service/all-player-data`，body `{puuid,environment}` | POST | Bearer | 200，挑战/等级积分 | 可选 |
| R-11 | 静态数据 | `GET /sqgg-api/data/json?type=hero` | GET | 无 | 200，1991 条 | 与对局无关 |

### 2.1 登录请求（实测）

```http
POST https://sqgg.cc/ HTTP/1.1
accept: text/x-component
content-type: text/plain;charset=UTF-8
next-action: bc7bfa13b6c407306dbb20eed4c1b296cd454b00
origin: https://sqgg.cc
referer: https://sqgg.cc/

["935383455@qq.com","<password>"]
```

响应为 RSC flight 文本，第 2 行（`1:`）即业务 JSON：

```json
{"status":"Success","message":"登录成功","data":{
  "email":"935383455@qq.com","status":1,
  "token":"<JWT>","start_time":"2026-10-30T04:48:50.000Z",
  "end_time":"2026-10-30T05:50:46.000Z",
  "rewards_daily_time":"2026-10-30T04:50:46.219Z","invite_reward":null}}
```

异常分支实测：错误密码 → `邮箱或密码错误`；邮箱格式不合法 → `邮箱格式错误！`；缺少 `Next-Action` → 返回普通 HTML 页面（不执行动作）。

### 2.2 签到请求（用户提供 + 实测一致）

```http
POST https://sqgg.cc/summoner/%E8%89%BE%E6%AC%A7%E5%B0%BC%E1%BA%9A?...（可为任意站内页面）
next-action: ca18bf21f3544b0d8dd37c2d7c3cf8ffca1c01c5
content-type: text/plain;charset=UTF-8

["<JWT>"]
```

```json
0:["$@1",["_QQxpUYei0oawWN9Rx2He",null]]
1:{"status":"Success","message":"签到成功","data":{"end_time":"2026-10-30T05:50:46.000Z"}}
```

重复签到：`{"status":"Fail","message":"今日已签到"}`；无效 token：`{"status":"Fail","message":"invalid token","code":401}`。

> 签到只依赖 `Next-Action` 头 + token，**不需要** `next-router-state-tree`、Cookie 或浏览器环境。

### 2.3 对局查询请求（实测）

```http
GET /sqgg-api/sgp-service/lol-match?environment=HN1&puuid=<puuid>&count=10&startIndex=0&tag=
referer: https://sqgg.cc/<任意路径>     ← 缺失直接 403（nginx WAF）
authorization: Bearer <JWT>
```

- `tag` 取值（前端定义）：`q_0`（全部）、`q_420`（单双）、`q_440`（灵活）、`q_430`（匹配）、`q_2400`（海克斯大乱斗）、`q_450`（大乱斗）、`q_1750`（斗魂竞技场）；另支持 `ranked` / `normal` / `pairs` / `turbo`。
- 实测 `tag=ranked` 只返回 `queueId=420`，`tag=normal` 只返回 `870`（斗魂/极速模式标签一致）。
- 响应结构：`data.games[]`，每场含 `metadata`（`match_id` 形如 `HN1_11419207411`、`tags`、`timestamp`）、`json`（Riot Match-V5 完整对象）、`horse`（按 puuid 的站内评分与 MVP 标记）。

## 3. 认证与权限模型（关键约束）

### 3.1 JWT

`RS256`，payload 为 `{email, status:1, platform:"Web", iat, exp}`，`exp = iat + 30 天`。token 本身有效期 30 天，但**能否读数据由 `end_time` 决定**。

### 3.2 单会话强绑定（实测）

| 操作 | 结果 |
| --- | --- |
| 同一账号再次登录 | 旧 token 立刻失效 → `401 账号在其它设备登录，请重新登录` |
| 调用用户信息动作 `C0`（续期） | 返回新 token，**旧 token 立刻失效** |
| 浏览器与脚本同时使用 | 互相踢下线，后发起者获胜 |

含义：脚本保存 token 时必须"先落盘新 token 再丢弃旧 token"，否则中途崩溃会导致下次必须用账号密码登录。

### 3.3 访问权限（捐助时长）

- 每日签到一次，`end_time = 签到时刻 + 1 小时`（实测 `rewards_daily_time 04:50:46Z` → `end_time 05:50:46Z`）。
- `end_time` 过期后：`lol-match`、`rank` 返回 `{"status":"Fail","message":"捐助时长已到期","code":403}`。
- 过期后无法用签到补救：签到接口返回 `今日已签到`；只能通过捐助/卡密/邀请延长。
- 页面未登录时对局区显示"需要登录后才能查看"；登录但未签到/已到期时数据接口直接 403。

### 3.4 签到时间能否由客户端指定（绕过每日限制）

结论：**不能，也不应尝试**。

| 证据 | 观察 |
| --- | --- |
| 前端调用签名 | 签到动作只接收 token：`await (0, Z.C4)(token)`，没有任何时间参数 |
| 实际请求体 | `["<JWT>"]`，服务端动作未绑定隐藏参数 |
| 重复签到响应 | `{"status":"Fail","message":"今日已签到","data":null}`，说明服务端在进入时长计算前就按**服务端时钟**拒绝 |
| 窗口时间来源 | `end_time` 由服务端在响应中下发（`签到时刻 + 1 小时`），客户端只读 |
| 令牌时间字段 | JWT 的 `iat`/`exp` 仅表示令牌有效期（30 天），不参与访问窗口判定 |

说明：伪造时间参数属于绕过站点访问控制，违反调研的安全前置约束与服务条款 2.2，本次**未做此类测试**。合法的延长方式只有站点内的捐助、卡密兑换或邀请码（界面中的"捐助时长"），脚本应按"每日一次签到 + 1 小时内完成采集"设计。

### 3.5 错误码约定

| HTTP | 业务体 | 含义 | 处理建议 |
| --- | --- | --- | --- |
| 200 | `status=Success` | 正常 | — |
| 200 | `code=401` `账号在其它设备登录` | token 被新会话顶掉 | 重新登录 |
| 200 | `code=401` `invalid token` | token 非法/过期 | 重新登录 |
| 200 | `code=403` `捐助时长已到期` | 今日访问窗口已过 | 次日签到后再采集，当日放弃 |
| 403 | `text/html` nginx | 缺少 `Referer` | 补齐 `Referer` |
| 200 | `status=Fail` `今日已签到` | 重复签到 | 视为已获得窗口，继续查询 |

## 4. 数据能力核对（对照日报必需的标准化字段）

| 标准化字段 | sqgg 来源 | 实测可用性 |
| --- | --- | --- |
| `match_id` | `metadata.match_id` / `json.gameId` | ✅ 全局唯一 |
| `game_start_time` | `json.gameStartTimestamp`（epoch ms） | ✅ 直接换算上海时区 |
| `game_duration_seconds` | `json.gameDuration` | ✅ 秒 |
| `win` | `participant.win` / `teams[].win` | ✅ |
| `champion_id` / `champion_name` | `participant.championId` / `championName` | ✅ 英文名，需本地中文映射（可复用项目现有 champions 模块） |
| `kills/deaths/assists` | `participant.kills/deaths/assists` | ✅ |
| `cs` | `totalMinionsKilled + neutralMinionsKilled` | ✅ |
| `position` | `teamPosition` / `individualPosition` | ✅ |
| `queue_type` | `json.queueId`（420/440/430/450/2400/1750） | ✅ 可直接只取 420 |
| `op_score` | `horse[puuid].score`（如 10.5，另含 `honor:"MVP"`） | ✅ 站内评分，非 OP.GG 分值，不可与历史数据直接对比 |
| 对位数据（伤害/承伤/视野/金币） | `participant.totalDamageDealtToChampions`、`totalDamageTaken`、`visionScore`、`goldEarned` 等 | ✅ 10 人完整下发 |
| 队伍汇总 | `teams[].objectives`、按 teamId 聚合 | ✅ 可自行汇总 |
| 击杀参与率 | 由 `participant.kills+assists` 与队伍总击杀计算 | ✅ 可计算 |
| 数据延迟 | 接口最新一局与页面「最后游戏 2026/10/29 02:19:45」一致（首场 10-29 01:51 开始 + 29 分钟）；对局结束后的实时延迟未实测 | ⚠️ 待观察 |

## 5. 分页与性能实测

| 请求 | 返回场次 | 响应体积 | 耗时 |
| --- | --- | --- | --- |
| `count=1, startIndex=0` | 1 | 115 KB | <1 s |
| `count=10, startIndex=0` | 10 | 1.14 MB | 1.7 s |
| `count=30, startIndex=0` | 30 | 3.02 MB | 4.0 s |
| `count=50, startIndex=0` | 50 | 4.90 MB | 13.9 s |
| `count=100, startIndex=0` | 100 | 9.60 MB | 12.5 s |
| `startIndex=0/10/20`（count=10） | 10/10/10 | — | 批间**无重叠** |

- 无显式时间过滤参数，需按 `startIndex` 翻页并在最老对局早于统计窗口起点时停止；单场约 114 KB。
- 连续 8 次 `count=10` 请求均 200，未见 429/限流响应头（未做更高频压力测试）。

## 6. 账号解析与大区映射

- 首页 RSC payload 内嵌 34 个大区：`{label:"艾欧尼亚", value:"HN1", id:"艾欧尼亚-HN1"}`，脚本可直接正则提取，无需硬编码。
- 接口参数 `environment` 必须是代码（`HN1`/`TJ100`/`NJ100`/`GZ100`/`CQ100`/`BGP2`…），传中文名返回 `大区不存在`。
- `summoner-by-name` 免鉴权即可 puuid 解析；错误名返回 `{"status":"Success","message":"该id不存在","data":null}`（注意：`status` 仍是 Success，需判 `data`）。

## 7. Server Action ID 的稳定性与自动发现

- 登录/签到/续期/退出均为 Server Action，ID 为 40 位十六进制，**硬编码在** `/_next/static/chunks/app/layout-<构建哈希>.js`。
- 站点重新构建后 chunk 哈希与动作 ID 都会变化，因此必须自动发现：

```text
GET / → 正则取 /_next/static/chunks/app/layout-*.js
     → GET 该 chunk
     → 定位导出映射 a.d(t,{BK:…,C0:…,C4:…,VZ:…})
     → (名字:function(){return 变量}) 得到 名字→变量
     → (变量=(0,s.$)("<40位ID>")) 得到 变量→ID
```

实测解析结果与用户提供的签到 ID 完全一致：

| 导出名 | 用途 | Action ID（当前构建） |
| --- | --- | --- |
| `VZ` | 登录 | `bc7bfa13b6c407306dbb20eed4c1b296cd454b00` |
| `C4` | 签到 | `ca18bf21f3544b0d8dd37c2d7c3cf8ffca1c01c5` |
| `C0` | 用户信息/续期 | `e47f3b49dd5957b577d85abb533382e09fe6ee24` |
| `vl` | 退出登录 | `79a0cbd7a61fb6d5ae5e187360b5e4bb45733d12` |
| `Fl` | 注册 | `61259f74dd1b8d3a60fb12fbc407f664b61fdf19` |
| `rA` | 改密 | `c91a4b81cfa5e759ddbe905bc177d91ab92fe7be` |

## 8. 建议的脚本化流程

```text
1. 读取配置（账号、密码、目标召唤师、大区中文名）
2. 取访问令牌
   ├─ 本地有持久化 token → 直接使用（避免踢掉用户浏览器会话）
   └─ 无/失效 → 用登录动作换取新 token，立即持久化
3. 签到：POST 签到动作 [token]
   ├─ Success → 记录 end_time（当前时刻 + 1h），进入采集
   └─ 今日已签到 → 判断当前时间是否仍在 end_time 内；不在则中止（当日无权限）
4. 解析账号：GET summoner-by-name（大区代码由首页 RSC 映射表得到）
5. 采集：GET lol-match?tag=ranked&count=20&startIndex=0,20,… 直到最老对局早于窗口起点
6. 输出 Markdown/JSON/图片（沿用现有 provider 抽象与渲染层）
```

实现要点：

- **必须在签到成功后 1 小时内完成全部查询**；把"签到 → 采集"绑定为同一任务，失败重试只在窗口内进行。
- 所有 `/sqgg-api` 请求都要带 `Referer: https://sqgg.cc/`，否则 nginx 返回 403 HTML。
- token 轮换是单向的：凡是调用续期动作，必须"先写盘新 token"再使用，避免崩溃导致凭据丢失。
- 建议只在 token 失效时登录；每日登录会持续踢掉用户在浏览器/手机上的会话。
- 建议对 `code=403 捐助时长已到期` 做快速失败，不做跨小时重试（会空转一整天）。
- 动作 ID 按构建哈希缓存；构建变化时自动重新解析，解析失败则本日任务失败并告警。

## 9. 风险清单

| 风险 | 等级 | 说明 | 缓解 |
| --- | --- | --- | --- |
| 每日仅 ~1 小时访问窗口 | 高 | 签到即开始计时；错过窗口当天无法补救 | 签到与采集合并为一次任务；窗口内重试；快速失败告警 |
| 单会话强绑定 | 高 | 用户任意设备登录/刷新都会使脚本 token 失效 | 持久化最新 token；失败时用密码登录并告知用户 |
| 凭据托管 | 高 | 需要邮箱+密码（或长期 token）存入 CI Secret | 只放 GitHub Secrets；考虑单独小号；日志脱敏 |
| 构建变更导致动作 ID 变化 | 中 | 登录/签到全部依赖 Next-Action | 自动发现 + 构建哈希缓存 + 失败即停 |
| nginx WAF | 中 | 缺 `Referer` 一律 403；可能存在未公开的频控 | 固定请求头；低频串行 |
| 数据口径差异 | 中 | `horse.score` 为站内评分，与 OP.GG `op_score` 不可比；英雄名为英文 | 建立独立评分字段；复用中文英雄映射 |
| 服务稳定性/条款 | 中 | 第三方站点，条款 2.1 要求"已获得被查询账号持有人授权"，2.2 禁止商业牟利 | 仅采集已获授权的主播账号；标注数据来源；不转售 |
| 触发验证码/风控 | 低 | 本次未遇到验证码；登录无图形校验 | 不在失败时暴力重试 |

## 10. 最终建议

1. **可以接入**，但定位为「国服受限数据源」：技术链路（登录→签到→查询）已全部用纯 HTTP 验证通过，字段覆盖超过日报所需。
2. **必须按"签到即采集"设计**：把每日任务固定在签到后立即执行（例如 05:00 触发时先签到再采集），并在 1 小时窗口内完成；当天错过就跳过输出，不做跨小时重试。
3. **与 OP.GG 并存**：在 `src/providers/` 下新增 `sqgg.py`，实现现有 `Provider` 协议（`kill participation`、`lane opponent`、`op_score` 等字段可按现有 `NormalizedMatch` 映射），由配置选择国服走 sqgg、韩服走 opgg。
4. **需要用户确认的三件事**：
   - 是否接受"每日仅 1 小时可查询"这一硬约束（这决定日报的定时策略）；
   - 是否接受脚本登录会踢掉本人浏览器/手机端会话；
   - 是否接受把账号密码作为 GitHub Actions Secret 托管（或改用长期 token + 手动续期）。
