# OP.GG 玩家页面请求清单（R-01 ~ R-08）

- 调研日期：2026-10-21（KST 10:38 ~ 11:10）
- 调研环境：Windows 11 + Chrome 156（MCP Chrome DevTools）
- 样例账号：斗鱼丶胡凯利#2862（KR，大师 277 LP，等级 395）
- 页面：`https://op.gg/lol/summoners/kr/%E6%96%97%E9%B1%BC%E4%B8%B6%E8%83%A1%E5%87%AF%E5%88%A9-2862`
- 实际落地 URL：`https://op.gg/zh-cn/lol/summoners/kr/...`（按语言自动重写）

## 1. 请求清单

| 编号 | 触发动作 | 请求路径（脱敏） | 方法 | 状态码 | 响应类型 | 用途判定 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R-01 | 打开玩家页 | `https://op.gg/zh-cn/lol/summoners/kr/{riotName}-{tag}` | GET | 200 | text/html（约 694 KB） | 玩家页 SSR 文档 | 无 Cookie/Token 亦可获取 |
| R-02 | 页面 hydration / 周期性刷新 | 同 R-01 URL | POST | 200 | RSC flight（`text/x-component`） | Server Action，`next-action: 40f052435807841afcefdd3e993b4a019b4a1bf970` | 响应体约 70 字节，仅表示"已刷新"，**不含对局数据** |
| R-03 | 客户端路由/预取 | `...?_rsc=xxxxxxxxxxx` | GET | 200 | RSC flight | Next.js RSC prefetch | 与 R-02 配对出现 |
| R-04 | 点击"Show more"（对局列表底部） | 同 R-01 URL | POST | 200 | RSC flight（约 780 KB） | **`getGames` 动作，`next-action: 409a2b9ca50d15e50a4dace93552e3a40113dc2753`** | 每次返回 20 场，可按 `endedAt` 游标翻页 |
| R-05 | 展开单局详情 | 无新请求 | — | — | — | 详情数据已随列表下发 | 0 次 fetch/XHR |
| R-06 | 页面静态资源 | `https://c-lol-web.op.gg/app-router/releases/production-421-1-f4dc4e1d/...` | GET | 200 | JS/CSS/图片 | 前端构建产物 | 构建号 `production-421-1-f4dc4e1d` |
| R-07 | 广告/统计 | `s-opgg-kit.op.gg`、`opgg.rybbit.com`、`google-analytics.com`、`ad.doubleclick.net`、`opgg-pdc.our.gg` | GET/POST | 200/204/ERR | JSON/pixel | 广告与埋点 | 与数据无关 |
| R-08 | 旧内部接口探测 | `https://lol-web-api.op.gg/api/v1.0/internal/bypass/games/kr/summoners/{puuid}?limit=5&hl=en_US` | GET | `net::ERR_CONNECTION_CLOSED` | — | 第三方 SDK 提到的旧地址 | 本机不可达，无法据此判断线上是否下线 |
| R-09 | Python 直连重放 `getGames` | 同 R-01 URL（跟随 307 到无 locale 路径） | POST | 200 | RSC flight | 验证生产可行性 | 无 Cookie、纯 `urllib` 成功 |

> R-02 请求体实测：`[{"region":"kr","puuid":"nbJBkHxHe6KE...TYvWmg","locale":"en","includeSummary":false}]`
>
> R-04 请求体实测：`[{"locale":"en","region":"kr","puuid":"nbJBkHxHe6KE...","gameType":"TOTAL","endedAt":"2026-10-17T13:40:03+09:00","champion":""}]`
>
> R-04 说明：早前"点击无网络请求"的结论来自页面内 `window.fetch` 补丁，**未能捕获 Next.js 内部发起的请求，该结论已作废**；DevTools 网络面板与 Python 直连均证实点击会触发 `getGames`。
>
> R-09 结果：第 1 批 20 场（2026-10-20 14:58 ~ 2026-10-17 13:40），第 2 批 20 场（2026-10-17 13:14 ~ 2026-10-14 14:17），**批间重叠 0**。

## 2. 传输层观察

R-01 响应头（匿名 HEAD 验证，`credentials: omit`）：

```text
status: 200
content-type: text/html; charset=utf-8
server: nginx
cache-control: private, no-cache, no-store, max-age=0, must-revalidate
vary: rsc, next-router-state-tree, next-router-prefetch, next-router-segment-prefetch
x-cache: Miss from cloudfront
x-amz-cf-pop: SFO53-P5
x-middleware-rewrite: /zh-cn/lol/summoners/kr/...
x-pathname: /lol/summoners/kr/...
```

- 无 `Set-Cookie` 依赖、无 `Authorization` 要求、无动态令牌要求。
- 页面按请求实时渲染（`no-store`），不存在可缓存的公开 JSON 接口。
- 未观察到 `429`/`401`/`403`；未观察到验证码或登录墙。

## 3. 页面数据架构结论

1. 站点为 **Next.js App Router + RSC（React Server Components）** 架构，不存在类似 `lol-web-api.op.gg/api/v1.0/...` 的公开 JSON 接口被页面调用。
2. 服务端 HTML 中内嵌 `application/ld+json` 结构化数据，**固定包含最近 10 场对局**（`ListItem` → `PlayGameAction`）。
3. 首屏展示 20 场；点击 "Show more" 触发 **`getGames` Server Action**，每次返回更早的 20 场。
4. 页面还存在一个周期性刷新动作（`next-action: 40f052...`，响应约 70 字节），与对局数据无关。
5. 单局详情展开（"Show More Detail Games"）**不产生任何请求**，计分板数据已随列表下发。
6. `matchId` 在行 DOM 中可读（`data-tooltip-id="op-keyword-tooltip-{matchId}"`），但 HTTP 路径已可通过 `getGames` 的 `id` 获得，无需 DOM。

## 4. 分页与 URL 参数验证

| 探测 | 结果 |
| --- | --- |
| `GET {页面}/games` | 404 |
| `GET {页面}?page=2` | 200，但内容与首页一致，参数被忽略 |
| `GET {页面}?game_type=ARAM` / `SOLORANKED` | 参数被忽略 |
| 点击"Show more" | POST `getGames`，返回 20 场（`endedAt` 游标） |
| 连续两批（Python 直连） | 20 + 20 场，时间单调递减，**重叠 0** |
| 再点"Show more"（浏览器） | 列表 20 → 40；按钮随后消失（客户端缓存耗尽） |

结论：**分页由 `getGames` 动作的 `endedAt` 游标完成，可翻任意深度；URL 上没有分页参数。**

## 5. 旧接口线索的验证结果（重要）

- 线索来自第三方 SDK：`https://lol-web-api.op.gg/api/v1.0/internal/bypass/`。
- 本次在页面上下文 `fetch` 该地址：`TypeError: Failed to fetch`（跨域/CORS）。
- 直接新开标签页访问：`net::ERR_CONNECTION_CLOSED`。
- DNS 解析结果：`lol-web-api.op.gg → 198.18.1.52`（`198.18.0.0/15` 为保留网段，本机代理/TUN 常见写法）。
- 判定：**本机网络环境无法访问该主机，不能据此断言该接口已下线**；但也不能把它当作可用数据源。需要在不受本地代理干扰的环境中复测，或直接放弃该线路。

## 6. 待确认项

| 项 | 说明 |
| --- | --- |
| `getGames` 动作 ID 是否随构建变化 | 当前 `409a2b9ca50d15e50a4dace93552e3a40113dc2753`；ID 在 chunk `1534-*.js` 的 `createServerReference("<id>","getGames")` 中，可按构建号缓存 |
| `getGames` 是否要求 Cookie/会话 | **不要求**：Python `urllib` 无 Cookie 调用成功 |
| 单账号可获取的对局上限 | 每页 20 场，游标可一直翻到历史尽头；尚未测到硬上限 |
| 不同队列（ARAM/竞技场）的列表行为 | `gameType=TOTAL` 已验证混合队列；可按 `SOLORANKED` 等参数过滤 |
| POST 的 307 跳转 | `/en/...` 会 307 到无 locale 路径，必须保留 POST 方法与 body 跟随 |
| 英文页与中文页差异 | `/en/` 与 `?hl=en_US` 均返回 200，英雄名 `Yorick`、按钮 `Show more`、时长 `28m 18s` |
