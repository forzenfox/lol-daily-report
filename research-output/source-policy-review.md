# 数据源条款与许可核查（2026-10-21 复核）

## 1. 原始依据

### 1.1 使用条款（Terms of Use）

- 地址：`https://op.gg/lol/policies/agreement`（自动跳转 `zh-cn`）
- 页面标注：**Last Updated: 2026-09-14**
- 关键原文（英文正文）：

> `Provided you comply with these Terms, and have an account with us, you are granted a non-exclusive, non-transferable right to access the Sites and Services ... for your personal, non-commercial use. You may not reproduce, distribute, create derivative works, reverse engineer, or use the Sites and Services for commercial purposes without our express written consent. You are further prohibited from:`
>
> - `scraping or data mining while using the Sites or Services;`
> - `blocking, overwriting, modifying and copying of any contents of the Sites; or`
> - `performing any actions that may impair the operability of the Sites’ infrastructure ...`

同页 User Conduct 段另列：

> `use automated scripts to collect information from or otherwise interact with the Services or the Sites;`

### 1.2 帮助中心《Can I use OP.GG data?》

- 地址：`https://help.op.gg/hc/en-us/articles/31091405109401-Can-I-use-OP-GG-data`
- 状态：页面显示"9 months ago Updated"（本次访问确认仍在线）
- 关键原文：

> `All game data currently used by OP.GG is collected through official APIs provided by each game company, and this data is not provided to third parties.`
>
> `In general, OP.GG does not prohibit data crawling or web scraping. However, if there is any commercial use without citing the source or if excessive requests affect our service operation, access to the service may be restricted.`

### 1.3 robots.txt

- 地址：`https://op.gg/robots.txt`（200，1201 字节）
- 通用规则：

```text
User-Agent: *
Allow: /
```

- 仅对 `Googlebot` / `Bingbot` 做了 `Disallow: /*?*`，未禁止 `/lol/summoners/kr/...` 路径。
- 结论：**robots.txt 不禁爬，但这不构成使用许可**；条款明确禁止 scraping 与 automated scripts。

### 1.4 《My latest player stats aren't updating》

- 地址：`https://help.op.gg/hc/en-us/articles/52138391416729`
- 关键原文：`OP.GG's match history is based on data provided by Riot Games' official API.`
- 明确不可见的对局：Clash 等新/活动模式、包含 AI 机器人的对局、`ARAM:Mayhem`。

## 2. 口径冲突分析

| 维度 | 帮助中心 | 使用条款 | 判定 |
| --- | --- | --- | --- |
| 抓取行为 | "一般不禁止" | 明确禁止 scraping / data mining | **冲突** |
| 自动化脚本 | 未直接提及 | 明确禁止 `automated scripts to collect information` | 明确禁止 |
| 商业用途 | 未标注来源/过量请求会限流 | 需 `express written consent` | 更严格 |
| 数据再分发 | 数据"不向第三方提供" | 禁止 reproduce/distribute | 清晰禁止 |

帮助中心的表述是**宽松的运营口径**，使用条款是**具有约束力的合同文本**。冲突时不能用较宽松的一条作为生产依据。

## 3. 本项目适用性判断

- 本项目为"个人/团队内部生成战报并公开发布到斗鱼鱼吧"，且为**定时无人值守**任务。
- 触发的条款风险点：
  1. `scraping or data mining`（低频抓取仍属 scraping）。
  2. `automated scripts to collect information`（GitHub Actions 定时采集直接命中）。
  3. 公开发布到直播平台，可能被认定为内容再利用/商业相关，需要 `express written consent` 或至少明确授权。
- 本项目**不触发**的风险点：
  - 不绕过验证码/登录墙/访问控制（已验证匿名 GET 即可，无需 Cookie）。
  - 不高频压测（1~4 账号、每日 1 次、单号 1~3 次请求量级）。
  - 不转售数据。

## 4. 合规结论

1. **在获得 OP.GG 书面许可前，OP.GG 自动化采集存在被限制访问或追责的风险**。
2. 允许保留的用途：低频、一次性的人工作业核对（运营手动打开页面比对），不属于自动化生产采集。
3. 如需降低风险，应向 `service@op.gg`（条款中出现的联系地址）书面说明：用途、账号数量（1~4）、请求频率（每日 1~2 次）、是否公开发布、是否标注来源、是否转售。

## 4.1 决策留痕（2026-10-21）

**用户明确决策：不使用 Riot 官方 API，继续只从 OP.GG 获取数据，并接受上述条款风险。**

基于该决策，项目保留以下执行底线（写入 PRD V1.1 与 `opgg-collector-design.md`）：

- 每账号每日页面加载 ≤ 2 次，账号串行、不并发；
- 不携带登录态/Cookie，不绕过验证码、登录墙、限流；
- 遇 `401`/`403`/`429`/验证码立即停止并告警，不重试、不换代理；
- 输出统一标注"数据来源：OP.GG"与采集时间；
- 不批量抓取非配置账号，不转售数据。

> 说明：本文件不再把 OP.GG 列为"阻断项"，而是"已接受风险 + 强制约束"。若后续出现封禁、限流或条款变更，应立即回到本文件重新评估。

## 5. 待确认

| 项 | 状态 |
| --- | --- |
| OP.GG 是否已书面回复本项目用途 | 未开展（当前按用户决策直接推进） |
| Riot 官方 API | 已按用户决策排除，不再调研 |
| 缓冲层：是否允许"仅采集 1 个公开账号用于内部测试" | 已默认允许（当前样例账号即为公开账号） |
