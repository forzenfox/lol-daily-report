# LOL 主播每日战报生成器

采集主播的《英雄联盟》单双排对局，生成 Markdown、JSON 与竖版 PNG 日报。支持韩服（OP.GG）与国服（SQGG）两个数据源，可通过 GitHub Actions 定时运行。

## 数据源

| 数据源 | 区服 | 说明 |
| --- | --- | --- |
| OP.GG | 韩服 `kr` | 从召唤师页 SSR 与 `getGames` 动作采集，无需登录 |
| SQGG | 国服（如艾欧尼亚 `HN1`） | 需邮箱登录并每日签到，签到后访问窗口约 1 小时 |

SQGG 注意事项：

- 单会话机制：同一账号再次登录会使旧令牌失效，浏览器端也会被踢下线。
- 访问窗口过期后当日不可恢复，建议在窗口内完成当日采集与补跑。
- 凭据只通过环境变量读取，绝不写入配置文件或仓库。

## 目录结构

```
src/                 # 运行代码（采集、统计、渲染、入口）
tests/               # pytest 测试（离线，不访问外网）
data/                # 英雄中文名映射与本地头像
assets/fonts/        # 渲染用字体
docs/                # PRD、接口调研与设计文档
output/<日期>/       # 产物：<主播名>.md / .json / .png
config.yaml          # 账号与运行配置
scripts/             # 辅助脚本
```

## 环境准备

```powershell
py -3.12 -m venv .venv          # 或使用本机可用的 Python 3.12+
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
```

## 配置

`config.yaml` 示例：

```yaml
accounts:
  - display_name: "余小C"          # 主播名，用于产物文件名与图片标题
    riot_id: "斗鱼丶胡凯利#2862"
    region: "kr"                  # 韩服账号
    enabled: true
  - display_name: "洞主"
    riot_id: "热心市民张合#65360"
    provider: "sqgg"              # 国服账号
    server: "艾欧尼亚"             # 大区中文名
    environment: "HN1"            # 大区代码，可省略由站点自动解析
    enabled: true

sqgg:
  email_env: "SQGG_EMAIL"
  password_env: "SQGG_PASSWORD"
  token_env: "SQGG_TOKEN"         # 可选，优先级高于本地令牌缓存
  count_per_page: 20
  max_pages: 5                    # 补跑多日时可适当提高
  reserve_seconds: 300

report:
  output_dir: "output"
  minimum_games_for_judgement: 3
  render_image: true
  auto_update_champions: true
```

### 凭据

- 必填环境变量：`SQGG_EMAIL`、`SQGG_PASSWORD`
- 本地调试可写入项目根目录 `.env`（已被 `.gitignore` 忽略）
- 令牌缓存在 `.cache/sqgg-session.json`，同样不入库

## 使用

```powershell
# 生成本地当天战报（上海时区）
.\.venv\Scripts\python.exe src/main.py

# 指定统计日期
.\.venv\Scripts\python.exe src/main.py --date 2026-10-30

# 跳过 PNG
.\.venv\Scripts\python.exe src/main.py --no-image

# 自定义输出目录
.\.venv\Scripts\python.exe src/main.py --output-dir output

# 从已有 JSON 离线重建产物（不访问数据源，窗口过期后补图用）
.\.venv\Scripts\python.exe src/main.py --render-json output/2026-10-30/洞主.json
```

运行前加载 `.env`（PowerShell）：

```powershell
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
    Set-Item -Path "env:$($matches[1])" -Value $matches[2].Trim()
  }
}
```

### 规则说明

- 日报口径只统计 5v5 单双排（`queueId=420`），其他模式不计入任何产物。
- 当日无有效对局时不创建目录、不产出文件，仅在日志输出 `[SKIP]`。
- 图片大标题格式为 `{主播名}{服务器}单双排日报`（韩服/国服）。
- 段位展示：大师、宗师、王者显示胜点（如「大师 277 胜点」）；钻石及以下显示小段位（如「钻石 2」）。
- JSON 落盘包含本人与对位的完整参战数据（`own_performance` / `lane_opponent`），可用于离线重建图片。

## 产物

```
output/
└── 2026-10-30/
    ├── 洞主.md       # 战报正文
    ├── 洞主.json     # 结构化数据（统计、称号、对局、段位）
    └── 洞主.png      # 1080x1920 竖版战报图（含对位雷达）
```

## 测试与检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m ruff format --check src tests
```

## 定时任务

`.github/workflows/daily-report.yml` 每天 05:00（Asia/Shanghai）运行，也可手动触发；产物提交到 `output/<日期>` 分支并归档 Artifact。

## 文档

- [韩服战报 PRD](docs/LOL韩服主播每日战报生成器_PRD.md)
- [国服数据源 PRD](docs/国服数据源接入_PRD.md)
- [OP.GG 接口调研计划](docs/OPGG网站接口调研计划书.md)
- [接口调研与设计文档](docs/research-output/)
