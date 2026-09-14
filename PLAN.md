# ScheduleKit — 个人任务管理系统实施计划（v3 定稿）

## 1. 目标与成功标准

以 to-do-list 为核心的自建任务系统：远端 Ubuntu 服务端（低内存）+ 双列表 WebUI + 设置控制台 + Windows 常驻悬浮窗 + iPhone 快捷指令拍照录入（LLM + 课表上下文解读、双重确认）。

**成功标准**
1. 图片经快捷指令上传后，LLM 以 **function calling** 返回结构化草稿，可修改后确认入库；结构化文本走同样两阶段确认。
2. 双列表正确：有 deadline 进有序表（按时间升序），无 deadline 进无序表（按 Ⅰ–Ⅴ 排序）；无序表 UI 权重明显小于有序表。
3. 识别时注入"课表派生时间上下文"，使「下节课」「这门课」「明天」等相对指代可被正确解析。
4. 全部接口鉴权，无匿名可写路径。
5. 服务端常驻内存（uvicorn + Caddy）≤ 150MB。
6. 悬浮窗开机自启、始终置顶、可拖动、可勾选任务。
7. 移动端可用且可"添加到主屏幕"。
8. **全服务器只有一份配置文件** `/etc/schedulekit/config.toml`。

## 2. 复用调研结论（已完成）

| 项目 | 星 | 语言 | 许可 | 结论 |
|---|---|---|---|---|
| [go-vikunja/vikunja](https://github.com/go-vikunja/vikunja) | 5.4k | Go | AGPL-3.0 | 最接近，但 projects/kanban 模型、无 Ⅰ–Ⅴ、无五分类、无 LLM 录入；二开 > 自研 |
| [donetick/donetick](https://github.com/donetick/donetick) | 2.5k | Go | AGPL-3.0 | chores/circles 模型不匹配；其 MCP server 是 v3 参考 |
| [dohsimpson/TaskTrove](https://github.com/dohsimpson/TaskTrove) | 1.1k | TS | — | 无上述规格 |
| [binwiederhier/ntfy](https://github.com/binwiederhier/ntfy) | 34k | Go | Apache-2.0 | v2 推送候选（本期不做） |

**结论：无现成项目匹配规格，自研本体 + 复用运维组件（Caddy / acme.sh / tomlkit）。**

## 3. 技术选型（均经本机实测）

| 层 | 选择 | 实测结果 |
|---|---|---|
| 运行时 | Python 3.13.14（uv 托管） | `uv 0.11.31` 已装且有网 ✅ |
| 框架 | FastAPI 0.141.1 + uvicorn 0.52.4 + Jinja2 | 与 Pillow 12.3.0 / httpx 0.28.1 一起 **2 秒装完并导入成功** ✅ |
| 配置 | **tomlkit 0.15.1**（风格保留型 TOML 库，MIT，纯 Python 48KB） | 控制台就地改写且不破坏注释 ✅ |
| 数据库 | SQLite（stdlib `sqlite3`，WAL） | 单文件、零进程 |
| 密钥加密 | `cryptography`（Fernet） | 加密存储 LLM API Key |
| 反代/TLS | Caddy + acme.sh DNS-01 | 证书热加载；约 30–40MB |
| 前端 | Jinja2 服务端渲染 + 原生 CSS/JS | 无构建步骤、服务器不装 Node |
| 悬浮窗 | PowerShell 7 + WinForms | `TopMost`/无边框/不进任务栏实测生效 ✅ |
| LLM | **OpenAI 兼容 function calling**（httpx 直连） | 唯一结构化机制 |

**内存预算**：uvicorn+FastAPI ≈ 70–100MB，Caddy ≈ 30–40MB，合计 ≈ **100–140MB**。

## 4. 统一配置（唯一配置文件）

`/etc/schedulekit/config.toml`，**仅此一份**。所有者为服务账号 `schedulekit`，权限 `0600`（控制台需写 `[llm]` 段）。

```toml
# ScheduleKit 唯一配置文件。控制台会就地修改并保留以下全部注释。

[server]
public_url  = "https://schedulekit.duckdns.org:8443"   # 生成 confirm_url 等绝对链接
timezone    = "Asia/Shanghai"                          # 亦为无时区时间的解释基准
listen_host = "127.0.0.1"                              # uvicorn 仅回环，对外一律经 Caddy
listen_port = 8000
data_dir    = "/var/lib/schedulekit"

[auth]
password_hash    = ""      # 由 `python -m app.cli set-password` 写入，勿手改
secret_key       = ""      # Cookie 签名根密钥，首次部署自动生成
session_ttl_days = 30
session_epoch    = 1       # 改密码时自增，使所有既有会话立即失效

[term]
start_date  = "2026-03-02" # 第 1 周的周一，每学期手改一次
total_weeks = 18

[llm]
provider        = "responses"   # responses | openai_compat | mock
base_url        = "https://api.deepseek.com"
model           = "deepseek-flash"
api_key         = ""
temperature     = 0.0
timeout_seconds = 60
max_tokens      = 1024     # 精简输出的硬保险（Responses 里叫 max_output_tokens）
max_image_bytes = 8388608
system_prompt   = """
从图片或文本中抽取所有可执行事项，调用 submit_tasks 提交。只调用工具，不输出任何解释文字。

规则：
1. 有明确截止时间：输出 due_at（ISO8601 带时区偏移），priority 必须为 null。
2. 无截止时间：due_at 为 null，按档位给出 priority：
   1=Ⅰ 24小时内必须处理 / 2=Ⅱ 本周关键 / 3=Ⅲ 常规 / 4=Ⅳ 可延后 / 5=Ⅴ 有空再说。
3. title ≤20 字，不重复时间、地点、课程名（已由独立字段承载）。
4. notes ≤60 字，无额外信息时不输出该键。
5. source_quote ≤20 字，填图中支撑该判定的原文。
6. 同一事项的多次提及合并为一条；最多 20 条。

时间上下文使用规则（见用户消息）：
- 仅用于解析相对时间与课程指代（如"下节课""这门课""明天"）。
- 不得据此推断待识别内容的主语或归属；内容与课表无关时完全忽略上下文。
- 若内容中已含明确日期，一律以内容为准，不得用当前时间替换。
- 上下文不得影响 priority 判定。
"""

[ingest]
confirm_ttl_hours = 48      # 未确认草稿保留时长

[tls]
provider      = "duckdns"   # duckdns | manual
domain        = "schedulekit.duckdns.org"
port          = 8443
acme_email    = "you@example.com"
duckdns_token = ""          # 仅部署脚本读取，应用不加载本段
cert_dir      = "/etc/schedulekit/certs"

[backup]
enabled               = true
hour                  = 4
keep                  = 7
upload_retention_days = 30
```

**规则**
- **读取顺序仅**：`--config` 参数 → `SK_CONFIG` 环境变量 → 默认路径。**无多来源合并、无优先级规则。**
- **写入**：tomlkit 读-改-写 + 临时文件 + `fsync` + `os.replace` 原子替换 + `fcntl.flock` 排他锁。
- **不变量**：`secret_key` 为空时拒绝启动（fail-closed）。
- **热加载分级**：

| 段落 | 生效方式 |
|---|---|
| `[llm]` | 控制台保存后立即生效（进程内重载该段） |
| `[term]` `[ingest]` `[backup]` | 下次操作 / 下次定时任务生效 |
| `[server]` `[tls]` `[auth]` | 页面提示需重启并给出准确命令 |

- **部署脚本不自行解析 TOML**：`install.sh` 通过 `python -m app.cli show --json` 取配置来渲染 Caddyfile 与定时任务。改域名/端口只动 config.toml 一处。
- 仓库只放 `config.toml.example`（无密钥）。
- **已删除**：`.env` / `SK_*` 变量体系 / DB `settings` 表。

## 5. 文件架构总览

```
D:\schedule_kit\
├── PLAN.md  README.md  pyproject.toml  uv.lock
├── config.toml.example         # 唯一配置的模板（无密钥）
├── .gitignore                  # 忽略 data/、.venv、__pycache__、真实 config.toml
│
├── app/                        # ── 服务端应用（唯一 Python 包）
│   ├── main.py                 # FastAPI 装配
│   ├── serve.py                # 入口：读 config.toml 后 uvicorn.run(...)
│   ├── config.py               # tomlkit 读写 / 原子替换 / 分段热加载 / 不变量
│   ├── db.py                   # 连接、WAL PRAGMA、事务上下文
│   ├── migrations/{0001_init.sql, runner.py}
│   ├── models.py  schemas.py
│   ├── security.py             # scrypt、签名 Cookie、CSRF 派生、API Key
│   ├── deps.py                 # current_session / require_write / require_admin
│   ├── ratelimit.py            # 进程内滑动窗口
│   ├── media.py                # 图片校验、SHA-256、落盘（不做 HEIC 解码）
│   ├── housekeeping.py         # 每小时清理过期草稿与超期图片
│   ├── cli.py                  # init / set-password / show --json / new-key / backup
│   │
│   ├── routers/{auth,tasks,ingest,courses,settings,ui}.py
│   │
│   ├── services/
│   │   ├── tasks.py            # 排序、筛选、二选一约束
│   │   ├── drafts.py           # 草稿状态机 + 确认事务
│   │   ├── normalize.py        # LLM 输出的强制后处理
│   │   ├── timetable.py        # 课表文本确定性解析
│   │   └── context.py          # 时间上下文纯函数（重点单测）
│   │
│   ├── llm/
│   │   ├── base.py             # VisionLLM 协议 + LLMResult + 错误分级
│   │   ├── registry.py         # provider 名 → 适配器
│   │   ├── structured.py       # 共享编排：重试策略 + 通道降级
│   │   ├── responses.py        # /responses 传输层（默认）
│   │   ├── chat.py             # /chat/completions 传输层（备选）
│   │   ├── tools.py            # submit_tasks 的 JSON Schema（唯一真相）
│   │   └── mock.py             # 测试用
│   │
│   ├── templates/              # base / login / index / draft / courses / settings
│   └── static/                 # css / js / manifest.webmanifest / sw.js / icons
│
├── clients/windows/            # ScheduleKitFloat.ps1 / install-autostart.ps1 / README.md
├── deploy/                     # install.sh / schedulekit.service / backup.sh / README.md
├── docs/                       # api.md / shortcuts.md / widget-v2.md
├── scripts/dev.sh
├── tests/                      # conftest + 9 个测试模块
└── data/                       # 运行时生成：schedulekit.db、uploads/
```

**分层约定**：`routers/` 只做鉴权与参数校验，业务规则全部落在 `services/`，使核心规则可脱离 HTTP 单测。

**⚠️ 客户端配置无法合并进 config.toml**：悬浮窗在另一台机器，且 PowerShell 无原生 TOML 解析器（只有 `ConvertFrom-Json`）。故客户端用 `config.json`，但**键名与结构刻意对齐**（`[server].public_url` ↔ `server.public_url`）。

## 6. 数据模型

SQLite，`journal_mode=WAL, synchronous=NORMAL, foreign_keys=ON, busy_timeout=5000`。时间一律存 **UTC ISO8601**，UI 按 `Asia/Shanghai` 渲染。**单租户：无 `users` 表，无 `user_id` 外键。**

```sql
items(
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL CHECK (category IN ('homework','practice','exam','appointment','other')),
  due_at TEXT,            -- ISO8601 UTC；NULL ⇒ 无序表
  priority INTEGER,       -- 1..5 即 Ⅰ..Ⅴ；NULL ⇒ 有序表
  status TEXT NOT NULL CHECK (status IN ('open','done','cancelled')) DEFAULT 'open',
  source TEXT NOT NULL CHECK (source IN ('web','shortcut','llm','api')) DEFAULT 'web',
  client_uuid TEXT UNIQUE,          -- 客户端幂等键，可空
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
  CHECK ((due_at IS NULL) <> (priority IS NULL))    -- 严格二选一
);
CREATE INDEX idx_items_due ON items(status, due_at);
CREATE INDEX idx_items_pri ON items(status, priority, created_at);

ingest_drafts(
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('pending','confirmed','discarded','failed')) DEFAULT 'pending',
  channel TEXT NOT NULL CHECK (channel IN ('image','text')),
  image_path TEXT, image_sha256 TEXT, input_text TEXT,
  context_snapshot TEXT,            -- 本次识别注入的时间上下文，供确认页核对与排障
  llm_provider TEXT, llm_model TEXT, llm_raw TEXT,
  draft_json TEXT NOT NULL,         -- {"items":[...]}，支持一图多事项
  created_item_ids TEXT,            -- JSON 数组：一次确认可产生多条 items
  error TEXT,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, confirmed_at TEXT
);

api_keys(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,    -- SHA-256，仅创建时明文展示一次
  read_only INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, last_used_at TEXT, revoked_at TEXT
);

courses(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
        teacher TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0);

course_sessions(
  id INTEGER PRIMARY KEY,
  course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  weekday     INTEGER NOT NULL CHECK (weekday BETWEEN 1 AND 7),
  start_time  TEXT NOT NULL, end_time TEXT NOT NULL,          -- "HH:MM"
  start_week  INTEGER NOT NULL DEFAULT 1,
  end_week    INTEGER NOT NULL DEFAULT 18,
  week_parity TEXT NOT NULL CHECK (week_parity IN ('all','odd','even')) DEFAULT 'all'
);
CREATE INDEX idx_sessions_course ON course_sessions(course_id);
```

## 7. 鉴权方案

**WebUI（人）**
- 单密码。`hashlib.scrypt(n=2¹⁴, r=8, p=1)` + 16 字节随机盐，存 `[auth].password_hash`。
- **无状态签名 Cookie**：`sk_session = base64(expiry).epoch.HMAC-SHA256(secret_key, expiry|epoch)`；校验签名 + 未过期 + `epoch == session_epoch`。
  - **改密码时 `session_epoch` 自增 ⇒ 所有既有会话立即失效**（解决无状态 Cookie 无法吊销的问题）。
- Cookie：`HttpOnly; Secure; SameSite=Lax; Path=/`。
- **CSRF**：`sk_csrf = HMAC(secret_key, "csrf")`，非 HttpOnly，JS 取出发到 `X-CSRF-Token`；服务端同法重算比对。仅对 Cookie 认证的写请求校验。
- `/api/login` 限流 5 次/分/IP。

**机器客户端（快捷指令 / 悬浮窗 / 未来 agent）**
- `Authorization: Bearer <key>` 或 `X-API-Key: <key>`。密钥 32 字节随机串，服务端只存 SHA-256，按键哈希查表，`hmac.compare_digest` 比对。
- **仅一个 `read_only` 布尔**（无多级作用域）：`=1` 只允许 GET。
- **悬浮窗配读写密钥**（需勾选任务）；只读密钥留给 v2 Scriptable 小组件。

## 8. 服务端 API（共 17 个）

| # | 方法 | 路径 | 说明 |
|---|---|---|---|
| 1 | POST | `/api/login` | 密码登录，下发签名 Cookie |
| 2 | POST | `/api/logout` | 清除 Cookie |
| 3 | GET | `/api/tasks?view=ordered\|unordered&status=&category=&limit=` | 有序表按 `due_at ASC`；无序表按 `priority ASC, created_at ASC` |
| 4 | POST | `/api/tasks` | 支持 `client_uuid` 幂等 |
| 5 | PATCH | `/api/tasks/{id}` | 改标题/分类/deadline/优先级/状态（勾选完成即 `status=done`） |
| 6 | DELETE | `/api/tasks/{id}` | 删除 |
| 7 | POST | `/api/ingest` | `{channel:"image"\|"text", image_base64?, mime?, text?}` → 草稿（**不写 items**） |
| 8 | GET | `/api/ingest/{draft_id}` | 读取草稿（含 `context_snapshot`） |
| 9 | POST | `/api/ingest/{draft_id}/confirm` | 确认入库（单事务、幂等） |
| 10 | POST | `/api/ingest/{draft_id}/discard` | 丢弃 |
| 11 | GET | `/api/courses` | 课程及其全部 sessions |
| 12 | PUT | `/api/courses` | **整体替换**；接受结构化 JSON 或课表文本 |
| 13 | GET | `/api/settings` | 读 LLM 配置（密钥仅返回"已设置/未设置"） |
| 14 | PUT | `/api/settings` | 写 `[llm]` 段并热加载 |
| 15 | GET | `/api/keys` | 列出密钥（仅元数据） |
| 16 | POST | `/api/keys` | 创建密钥，**仅此一次返回明文** |
| 17 | DELETE | `/api/keys/{id}` | 吊销 |

页面 5 个：`/`、`/login`、`/drafts/{id}`、`/courses`、`/settings`（即控制台）。`/docs` 仅登录后可见。

## 9. 课表与时间上下文

### 9.1 录入：文本粘贴 + 确定性解析（不引入 LLM）

`PUT /api/courses` 接受文本，`app/services/timetable.py` 用正则解析，结果 100% 可预测：

```
周一 08:00-09:40 高等数学 教三201 1-16周
周三 10:00-11:40 大学物理 理教105 1-16周 单周
周五 14:00-15:40 高等数学 教三201 1-8周
```

行格式：`<周次> <HH:MM>-<HH:MM> <课程名> [地点] [周次范围] [单周|双周]`。解析失败的行**原样返回并报错**，不做静默丢弃。整体替换语义（个人小数据集，≤20 门课，简化掉 4 个 REST 端点；代价是并发编辑互相覆盖，单用户可接受）。

### 9.2 时间上下文：纯函数、确定性

`app/services/context.py`：`build_context(now, courses, sessions, term_start, total_weeks) -> str`，**纯函数、重点单测**。输出约 100–150 token：

```
[当前时间] 2026-03-20 星期五 14:32（第 4 周）
[正在进行] 高等数学 · 教三201 · 14:00–15:40（已进行 32 分钟）
[刚刚结束] 大学物理 · 理教105 · 12:00–13:40（已结束 52 分钟）
[即将开始] 英语听说 · 外语楼302 · 16:00–17:40（58 分钟后）
[今日其余] 无
[本周剩余] 周六 10:00 形势与政策
```

判定阈值（代码内常量，可调）：`进行中` start ≤ now ≤ end；`即将开始` 0 < start−now ≤ 60 分钟；`刚刚结束` 0 ≤ now−end ≤ 60 分钟。
周次换算：`week = floor((today − term_start_monday)/7) + 1`；按 `start_week`/`end_week`/`week_parity` 过滤。**若当前不在学期内（week < 1 或 > total_weeks），只输出当前时间与"非学期周"，不输出任何课程。**

### 9.3 注入位置（关键工程细节）

**上下文拼进 user message，绝不拼进 system prompt**。理由：上下文分钟级变化，拼进 system prompt 会使 **prompt 缓存永久失效**；且会把"规则"与"数据"混在一起。

```
system: <静态规则 + submit_tasks 工具 Schema>
user:   [时间与课表上下文]
        ...
        [待识别内容]
        <图片 / 文本>
```

拼好的上下文同时存进 `ingest_drafts.context_snapshot`，确认页可折叠展示"本次识别依据"，用于区分"上下文算错"与"模型理解错"。

### 9.4 防误用规则（必须写进提示词）

注入课程上下文的最大风险是模型**过度联想**（看到"正在进行：高等数学"，就把无关海报归到该课程名下）。已写入 `system_prompt`：

- 上下文仅用于解析相对时间与课程指代，不得据此推断待识别内容的主语或归属；
- **内容中已含明确日期时，一律以内容为准**（避免翻出旧截图时"明天"被当成今天）；
- **上下文不得影响 priority 判定**，否则行为不可预测。

### 9.5 边界与不做

- 支持单双周与周次范围；**不建模调休、节假日、临时停课补课**（那周上下文会不准，需在任务里写清日期）。
- **考试不进课表**：考试是确定日期的一次性事件，本就作为 task 的 deadline 存在。
- **课表截图 LLM 识别不做**（v2 可加，复用同一 function calling 管线，但需另做一套确认 UI）。
- **主界面不显示今日课程**：上下文仅供识别使用，不干扰双列表权重结构。

## 10. LLM 适配器（function calling 单一机制）

**接口**
```python
class VisionLLM(Protocol):
    async def extract(self, *, system: str, user_text: str,
                      images: list[bytes]) -> LLMResult
```

**唯一结构化机制 = 强制 function calling**，不做多档回退。工具 Schema 定义在 `app/llm/tools.py`，是唯一真相：

```json
{"type":"function","function":{"name":"submit_tasks",
 "description":"提交从图片或文本中抽取出的全部事项",
 "parameters":{"type":"object","additionalProperties":false,"required":["items"],
  "properties":{"items":{"type":"array","maxItems":20,"items":{
    "type":"object","additionalProperties":false,
    "required":["title","category","due_at","priority"],
    "properties":{
      "title":       {"type":"string","description":"≤20字，不含时间地点课程名"},
      "category":    {"type":"string","enum":["homework","practice","exam","appointment","other"]},
      "due_at":      {"type":["string","null"],"description":"ISO8601带时区；无截止时间则null"},
      "priority":    {"type":["integer","null"],"minimum":1,"maximum":5,
                      "description":"仅在due_at为null时给出"},
      "notes":       {"type":"string","description":"≤60字，无则不输出此键"},
      "source_quote":{"type":"string","description":"≤20字，图中支撑该判定的原文"}
    }}}}}}}
```

- `required` 仅 4 个核心字段；`notes`/`source_quote` 选填，无内容时模型直接不输出该键。
- 请求默认走 **Responses API**（`{base_url}/responses`）：`tool_choice: {"type":"function","name":"submit_tasks"}`（**扁平**，没有嵌套 `function`）、`reasoning: {"effort":"none"}`（关 thinking）、`temperature: 0`、`max_output_tokens: 1024`。
- 工具定义也是扁平的 `{"type":"function","name","description","parameters"}`；`max_tokens` 改名 `max_output_tokens` 且**包含**思考 token。
- **必须显式关闭 thinking**：命名 `tool_choice` 在 thinking 模式下会被服务端 `400` 拒绝，而 `thinking` / `reasoning` 的默认值是开启。chat 传输层同理（`thinking: {"type":"disabled"}`）。
- `parallel_tool_calls` 在 Responses 里**被忽略**（并行恒开），所以一条响应可能有多个 `function_call`，必须全部合并。
- 适配器从 `output[]` 里取 `type == "function_call"` 的项，`json.loads(arguments)` 后合并 `items`。
- **未产生 tool call ⇒ 自动降级到 JSON 输出一次**（`text.format` / `response_format` 置 `json_object`，并在 prompt 追加 JSON 格式说明）。降级是响亮的：写 `llm.fallback_used` 警告日志、草稿记 `llm_path`、确认页显示提示。
- 临时性失败（连接、超时、429、5xx）按 `retry_count` 重试（默认 3 次），指数退避 + 抖动，另有 180 秒总预算兜底。确定性错误（401/403/400 等）不重试。
- 两条通道都失败 ⇒ 草稿置 `failed`，保留 `llm_raw`，报错里带上**每条通道各自的原因**。
- **不做能力探测**：`provider` 写的是什么就用什么，配置页对不支持的取值直接报错。
- `api_key` 以明文存于 `config.toml`（该文件本身按 0600 保护）；控制台永不回显明文。

**`source_quote` 的作用**：图里支撑该判定的原文片段，让确认页可核对"它从哪儿读出来的"。若为空或与图片不符，即说明模型幻觉，可当场发现。同时用于区分识别错误的来源（上下文算错 vs 模型理解错）。

## 11. 智能录入流程

1. 校验 `channel` / MIME / 大小（≤ `max_image_bytes`）。**HEIC 一律拒绝并返回可操作错误**；客户端负责转 JPEG。
2. 存 `data/uploads/YYYY-MM-DD/`，算 SHA-256。
3. `build_context()` 生成时间上下文并留存。
4. 组装 system（静态）+ user（上下文 + 内容 + 图片），调 `extract()`。
5. **服务端强制后处理（不信任模型）**，逐条：
   - `due_at` 非空 ⇒ **强制 `priority = None`**（落实"读到 deadline 就忽略优先级"）
   - `due_at` 空且 `priority` 空 ⇒ 预置 `priority = 3`（Ⅲ），标记 `needs_priority` 供确认页高亮
   - `priority` 越界 ⇒ 截断到 1..5；`category` 非法 ⇒ 降级 `other`
   - 无时区的 `due_at` ⇒ 按 `[server].timezone` 解释后转 UTC
   - `title` 为空 ⇒ 丢弃该条
6. 写 `ingest_drafts`（含 `context_snapshot`，`expires_at = now + confirm_ttl_hours`），返回 `{draft_id, items[], image_url, confirm_url}`。**此时 items 表零写入。**
7. 客户端 `PATCH`（可选）后 `confirm`：单事务插入 N 条 items，落 `created_item_ids`，状态置 `confirmed`。
8. `confirm` 幂等：状态非 `pending` ⇒ `409`。
9. LLM 失败/超时/无 tool call/非法 JSON ⇒ 草稿 `failed`，**不产生脏数据**。

`housekeeping.py` 每小时清理过期未确认草稿与超期图片。

## 12. WebUI

- 桌面端：有序表为主栏（宽），无序表为右侧栏（窄、字号与行高更小）；移动端单列，有序表置顶，无序表折叠。
- 任务行：标题 + 分类标签 + 右侧「倒计时/绝对时间」**或**「Ⅰ–Ⅴ 优先级徽章」（色阶递减）。
- 添加入口：桌面顶部常驻输入行 + 移动端右下浮动按钮；表单含分类、deadline 或优先级二选一控件（填 deadline 时优先级控件禁用并清空；两者都空时默认 Ⅲ）。
- **课表页 `/courses`**：一个文本框（支持上述文本格式）+ 解析预览 + 保存；顶部显示当前学期周次。
- 草稿确认页 `/drafts/{id}`：图片缩略图 + 可编辑的 N 项表单（逐项可删、显示 `source_quote`）+ 可折叠的「本次识别依据的时间上下文」+ 确认入库 / 丢弃。
- 控制台 `/settings`：LLM 配置（含 system prompt）、API Key 管理、运行状态（RSS、DB 大小、条数）。
- PWA：`manifest.webmanifest` + 图标 + 极小 service worker（仅缓存静态外壳），仅 `https:` 下注册。

## 13. Windows 悬浮窗

- `ScheduleKitFloat.ps1`：WinForms 无边框窗口，`TopMost=$true`、`ShowInTaskbar=$false`、`Opacity` 可调；拖拽移动；右键菜单（刷新/打开 WebUI/暂停轮询/设置/退出）；双击打开 WebUI。
- 内容：有序表前 N 条（最近 deadline 优先），可勾选回写（`PATCH /api/tasks/{id}`）。
- 轮询 `GET /api/tasks?view=ordered&status=open&limit=5`，默认 60s。
- 配置 `%APPDATA%\ScheduleKit\config.json`（键名对齐服务端）；API Key 用 DPAPI（`ConvertFrom-SecureString`）单独存放。
- `install-autostart.ps1` 在启动目录创建 `.lnk`（路径已确认：`C:\Users\ASUS\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup`），命令 `pwsh.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File ...`。

⚠️ **已实测风险**：本会话中 Windows Schannel 故障（`SEC_E_NO_CREDENTIALS`）导致 `curl`/`git`/`Invoke-RestMethod` 的 HTTPS 全部失败，而 Node（OpenSSL）正常。判断为沙箱受限令牌所致。**实施首步必须在普通 PowerShell 会话验证 `Invoke-RestMethod`**；若确实不可用，退路为改由 `node` 子进程发请求。

## 14. iPhone 侧

- **快捷指令**（你编写，我提供规格与逐步说明）：拍照 → **转换为 JPEG** → Base64 → `POST /api/ingest` → 展示草稿 → 确认 / 打开 `confirm_url` 修改。响应同时返回 `confirm_url`，规避快捷指令内编辑 JSON 的笨拙。
- **PWA**：响应式 WebUI + 添加到主屏幕（8443 上的 HTTPS 仍是安全上下文）。
- **v2**：Scriptable 小组件。机制已查实——JS 驱动其 Widget Extension，`Keychain` 取 token、`Request` 调 `GET /api/tasks`（只读密钥，无需新接口）、`ListWidget` 渲染、`Script.setWidget()`。**限制**：刷新率由 iOS 决定（官方文档明示 `refreshAfterDate` 仅为提示，实际约 15–60 分钟），非实时；小组件内按钮不能执行代码。

## 15. 部署（方案 B：DNS-01 + 8443）

**仅需你做的 3 件事**（约 10–15 分钟）：
1. 登录 https://www.duckdns.org （OAuth，无需注册）建 `schedulekit.duckdns.org`，填 VPS 公网 IP，复制 account token。
2. 在 VPS 服务商网页控制台放行**入站 TCP 8443**（**不开 80/443**）。注意服务商安全组与服务器 `ufw` 是两道独立的墙。
3. 服务器执行：
```bash
sudo bash deploy/install.sh --init   # 交互式生成 config.toml（隐藏输入 token 与密码，不进 shell history）
sudo bash deploy/install.sh          # 幂等部署
```
随后浏览器打开 `/settings` 填 LLM 配置，再到 `/courses` 粘贴课表。

`install.sh` 自动完成：装 uv + Python 3.13 + venv + 依赖 → 建目录 → 写 systemd unit（uvicorn 仅 `127.0.0.1:8000`）→ 装 Caddy 并按 `app.cli show --json` 输出渲染 Caddyfile（8443 + 显式证书路径 + 反代 + 安全头）→ 装 acme.sh 执行 `--issue --dns dns_duckdns -d <domain> --keylength ec-256` 与 `--install-cert --reloadcmd "systemctl reload caddy"` → `ufw allow 8443/tcp` → 装 DuckDNS A 记录自更新任务（token 写入 `/etc/schedulekit/duckdns-update.sh`，`0700`）→ 启动并健康检查。

- 证书续期全自动（acme.sh 自带 cron，约 60 天一次）。
- 备份由 `backup.sh` 调用 `python -m app.cli backup`（用 Python 的 sqlite3 backup API，**不依赖 sqlite3 命令行**），保留 `keep` 份。
- Caddy 读取证书：`fullchain` 0644、`privkey` 0640 且属组 `caddy`。
- 已验证 acme.sh 的 DuckDNS 适配器：环境变量 `DuckDNS_Token`，接口 `https://www.duckdns.org/update`，自动增删 `_acme-challenge` TXT。

**已实测的固有代价**：DuckDNS 权威 DNS 在境外（实测解析到 AWS 北美），未缓存解析耗时 **200–780ms**，国内域名对照 **45–90ms**。手机冷启动/切网后首个请求可能多等 0.3–0.6 秒。**升级到方案 C（¥10/年域名 + 国内 DNS）只需改 config.toml 一处 + 重跑 install.sh，代码与客户端零改动。**

## 16. 测试与验收

`pytest` + `httpx.ASGITransport`（全程无网络）+ `mock` LLM provider。

- **约束**：`due_at` 与 `priority` 同时给或都不给 → 422。
- **后处理**：mock 同时返回 deadline 与优先级 → 入库时优先级被强制清空；无时区时间按配置时区解释；越界优先级被截断；未知分类降级 `other`。
- **排序**：有序表按时间升序；无序表先按 Ⅰ→Ⅴ 再按创建时间。
- **鉴权**：错误密码恒定时间失败；无 CSRF 头的写请求被拒；`read_only` 密钥 POST → 403；`session_epoch` 自增后旧 Cookie 失效；`secret_key` 为空时启动失败。
- **草稿流**：重复确认 → 409 且只产生一次；多事项确认事务性（中途失败全回滚）；无 tool call → 草稿 `failed` 且 items 表零写入；HEIC 上传 → 明确 415 与可操作提示。
- **课表解析**：三种行格式；非法行原样报错不静默丢弃；单双周与周次范围。
- **时间上下文（纯函数，重点）**：周次换算；单双周过滤；学期外时段只输出时间不输出课程；边界时刻（课程开始 0 分钟 / 结束 0 分钟 / 恰好 60 分钟）；无课时段的输出。
- **配置**：tomlkit 改 `[llm]` 后注释与其余段落逐字节保持；并发写入被 flock 串行化；`[llm]` 保存后立即生效。
- **限流**：登录超限 → 429。
- **手工验收**：手机拍作业截图 → 上传 → 修改 → 确认 → 双列表正确；悬浮窗开机自启并显示最近 deadline；课表录入后「下节课交」能被解析为具体日期。

## 17. 实施顺序

| 里程碑 | 内容 |
|---|---|
| M0 | uv 骨架、`config.py`（tomlkit 读写 + 热加载）、迁移、`app.cli`、`serve.py`、健康检查 |
| M1 | 鉴权：签名 Cookie、CSRF、API Key、限流、登录页 |
| M2 | 任务 CRUD + 双视图排序 + 单测 |
| M3 | WebUI 双列表 + 增删改 + 移动端适配 |
| M4 | LLM 适配器（function calling）+ 两阶段录入 + 确认页 |
| **M4.5** | **课表：文本解析 + `/courses` 页 + `context.py` + 注入 + 防误用规则** |
| M5 | 控制台 `/settings`（LLM 配置热加载 + 密钥管理 + 状态） |
| M6 | 部署：install.sh + Caddyfile 渲染 + acme.sh DNS-01 + systemd + 备份 |
| M7 | Windows 悬浮窗 + 开机自启 |
| M8 | PWA 收尾 + 快捷指令联调文档 |
| v2 | Scriptable 小组件（只读密钥）；ntfy 推送；课表截图 LLM 识别；MCP server |

**M4.5 排在 M4 之后**：上下文注入点就在 prompt 组装那一处，是纯增量改动。先让录入管线跑通再加课表，出问题容易定位是哪一边的锅。

## 18. 已修正的冲突与遗漏

| # | 问题 | 修正 |
|---|---|---|
| ① | 删除 `users` 表后，`items`/`api_keys`/`ingest_drafts` 仍带 `user_id` 外键 | 全面去 `user_id`，改单租户 schema |
| ② | 无状态 Cookie 无法吊销会话 | 新增 `[auth].session_epoch`，改密码自增即失效全部会话 |
| ③ | 一次确认可产生多条 items，但 `item_id` 只能存一条 | 改为 `created_item_ids`（JSON 数组） |
| ④ | **iPhone 照片默认是 HEIC，Pillow 原生不解码** | 客户端用快捷指令原生「转换图像」转 JPEG；服务端对 HEIC 返回 415 + 可操作提示；不引入 `pillow-heif` |
| ⑤ | 悬浮窗要能勾选任务，但 `read_only` 密钥做不到 | 明确悬浮窗配**读写**密钥；`read_only` 专供 v2 小组件 |
| ⑥ | `config.toml` 若 root 所有 0600，服务进程无法写 `[llm]` | 文件属服务账号 `schedulekit`、权限 0600 |
| ⑦ | shell 脚本无法解析 TOML，却被要求渲染 Caddyfile | 统一走 `python -m app.cli show --json` |
| ⑧ | 上一版称"13 个端点"，漏计 login/logout | 更正为 15，加入课表 2 个后共 **17** |
| ⑨ | 残留 `view=float` 专用视图与"写审计日志"表述 | 删除 `view=float`（复用 `view=ordered&limit=5`）；清理 `audit_log` 相关表述 |
| ⑩ | base64 上传 8MB 图片 ⇒ 约 10.7MB JSON 请求体，内存无上限 | 明确请求体上限与流式读取，超限返回 413 |
| ⑪ | 缺 `client_uuid` 列定义；无时区时间语义不明 | 补 `client_uuid TEXT UNIQUE` 列；明确无时区时间按 `[server].timezone` 解释 |
| ⑫ | **上下文若拼进 system prompt 会破坏 prompt 缓存** | 明确拼进 user message；并存 `context_snapshot` 供核对 |
| ⑬ | 注入课程上下文会让模型过度联想、误判归属 | 写入三条防误用规则（不推断归属 / 有明确日期以内容为准 / 不影响 priority） |
| ⑭ | `notes`/`source_quote`/`confidence` 被设为必填，无内容也要输出空值 | 改为选填；**删除 `confidence`**（LLM 自评置信度校准极差，无判别力） |

## 19. 假设、风险与明确不做

**假设**：单用户；界面中文；LLM 供应商支持**强制 function calling + 视觉输入**；服务器可出站访问 DuckDNS 与 ACME。

**风险与应对**
- *Schannel 故障*（§13）：实施首步验证，退路为 node 子进程发请求。
- *大陆运营商拦截非标端口或 SNI 过滤*：退路为换端口或 Cloudflare Tunnel（需域名）。
- *境外 DNS 解析慢*（§15 实测 200–780ms）：可平滑升级至方案 C。
- *证书签发跨境超时*：脚本区分"改 TXT 失败"与"CA 查询失败"两类报错；退路为 ZeroSSL 或方案 C。
- *LLM 不支持强制 tool_choice*：先自动降级到 JSON 输出一次（响亮记录），仍失败则明确报错；不静默忍受。
- *课表调休不准*：已知限制，需在任务里写清日期；不做节假日建模。

**明确不做（本期）**：提醒推送、Scriptable 小组件、离线可用、多用户、任务重复规则、附件存储、重复任务去重、审计留痕、课表截图识别、主界面显示今日课程。
