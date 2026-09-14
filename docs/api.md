# HTTP 接口规格

供快捷指令、悬浮窗、脚本，以及将来的 AI agent / MCP 使用。
所有接口都在 `public_url` 下（如 `https://schedulekit.duckdns.org:8443`）。

---

## 鉴权

两种凭据，二选一：

### 会话 Cookie（浏览器）

`POST /api/login` 拿到 `sk_session`。写操作还必须带 `X-CSRF-Token` 头，
值从 `sk_csrf` Cookie 里读。

### API Key（机器客户端）

```http
Authorization: Bearer sk_xxxxxxxxxxxxxxxxxxxxxxxx
```

或：

```http
X-API-Key: sk_xxxxxxxxxxxxxxxxxxxxxxxx
```

密钥只有一个权限开关：

| 权限 | 允许 |
|---|---|
| 读写 | 全部接口（含录入与确认） |
| 只读 | 仅 `GET` |

**能做什么、不能做什么：**

- ✅ 任务增删改查、录入、读课表、读状态
- ❌ 改 LLM 配置、读设置、创建/吊销密钥 —— 这些只接受网页会话。
  否则一把被窃取的密钥就能自我提权。

---

## 会话

### `POST /api/login`

同时接受 JSON 与表单编码：浏览器登录页用表单（无 JS 也能登录），脚本用 JSON。

```json
{ "password": "你的管理员密码" }
```

- JSON 请求成功返回 **204**，并下发两枚 Cookie：`sk_session`（HttpOnly 签名令牌）
  与 `sk_csrf`（非 HttpOnly，供前端读取后放进请求头）
- 表单请求成功返回 **303** 重定向到 `/`
- 密码错误返回 401（表单则跳回 `/login?error=1`）
- 失败尝试限流 **5 次/分钟/IP**，超限返回 429

### `POST /api/logout`

清除两枚 Cookie，返回 204。无状态 Cookie 无法逐个吊销——要一次性踢掉所有会话，
改密码即可（`session_epoch` 会自增）。

---

## 任务

### `GET /api/tasks`

| 参数 | 取值 | 说明 |
|---|---|---|
| `view` | `ordered`（默认）\| `unordered` | 有序 = 有截止时间，按时间升序；无序 = 只有优先级，按 Ⅰ→Ⅴ |
| `status` | `open` \| `done` \| `cancelled` | 不传则返回全部 |
| `category` | `homework` \| `practice` \| `exam` \| `appointment` \| `other` | |
| `limit` | 1–200 | |

```bash
curl -H "Authorization: Bearer $KEY" "$BASE/api/tasks?view=ordered&status=open&limit=5"
```

### `POST /api/tasks`

```json
{
  "title": "交实验报告",
  "category": "homework",
  "due_at": "2026-04-01T10:00:00+08:00",
  "priority": null,
  "notes": "交到学委处",
  "client_uuid": "可选，用于重试幂等",
  "source": "api"
}
```

**`due_at` 与 `priority` 严格二选一**，同时给或都不给都会得到 422。

- 给了 `due_at` → 任务进**有序表**
- 只给 `priority`（1=Ⅰ … 5=Ⅴ）→ 任务进**无序表**
- 无时区的 `due_at` 按服务端 `[server].timezone` 解释

带 `client_uuid` 时具备幂等性：重复提交返回 **200**（而不是 201）和同一个任务。

### `PATCH /api/tasks/{id}`

只处理显式提供的字段。**给 `due_at` 会自动清空 `priority`，反之亦然**
（对应"有截止时间就忽略优先级"的领域规则）。

```json
{ "status": "done" }
```

清空某一侧却不提供另一侧会返回 **409**，因为那会让任务既无时间也无优先级。

### `DELETE /api/tasks/{id}`

成功返回 `{"deleted": true, "id": 12}`。

---

## 智能录入（两阶段）

### `POST /api/ingest`

**这一步只产生草稿，绝不写任务表。**

```json
{
  "channel": "image",
  "image_base64": "…",
  "mime": "image/jpeg"
}
```

| 字段 | 说明 |
|---|---|
| `channel` | `image` 或 `text` |
| `image_base64` | `channel=image` 时必填。**JPEG/PNG/WebP/GIF；HEIC 会得到 415** |
| `text` | `channel=text` 时必填 |
| `items` | 可选。已经拿到结构化条目时直接传，**跳过 LLM**（仍要确认） |

返回：

```json
{
  "draft_id": 12,
  "status": "pending",
  "items": [ { "title": "…", "category": "homework", "due_at": "…", "priority": null,
               "notes": "…", "source_quote": "…", "needs_priority": false } ],
  "context_snapshot": "[当前时间] …",
  "image_url": "/api/ingest/12/image",
  "confirm_url": "https://…/drafts/12",
  "llm_path": "tool_call",
  "llm_elapsed_ms": 1840
}
```

`llm_path` 说明这次结构化结果是怎么拿到的：

| 值 | 含义 |
|---|---|
| `tool_call` | 正常路径——模型按要求返回了 `submit_tasks` 工具调用 |
| `json_fallback` | 降级路径——模型没返回工具调用（或被供应商拒绝），服务端改用 JSON 模式重取了一遍。**识别结果通常没问题，但反复出现就该换模型或供应商了** |

客户端已给出 `items`（跳过 LLM）时该字段为 `null`。

服务端会对模型输出做**强制后处理**，所以 `items` 里的值可能与模型说的不同：

- 有 `due_at` 时 `priority` 一律被置为 `null`
- 两者都没有时补默认优先级 `3`，并置 `needs_priority: true`
- 非法分类降级为 `other`，越界优先级截断到 1–5
- 无时区时间按 `[server].timezone` 解释为 UTC

**错误码**

| 码 | 含义 |
|---|---|
| 413 | 图片过大（超过 `[llm].max_image_bytes`） |
| 415 | 格式不支持（HEIC 会附带"请先转 JPEG"的提示） |
| 422 | 参数错误 / base64 非法 / 客户端提供的 items 违反二选一 |
| 429 | 录入过于频繁（10 次/分钟/凭据） |
| 502 | LLM 调用失败。**草稿已留档**，响应里带 `draft_id`，`retryable: true` |
| 503 | LLM 未配置（去 `/settings` 填 base_url / model / API Key） |

502 的 `detail.message` 会带上**每条通道各自的原因**——第一条通常是真正
有用的那条（"模型不支持强制 tool_choice" 比"降级通道里也没有 items"重要）。
临时性失败（连接、超时、429、5xx）在返回 502 之前已经按
`[llm].retry_count` 自动重试过；401/400 这类确定性错误不重试。

### `GET /api/ingest/{draft_id}`

读取草稿（含 `context_snapshot`、`llm_provider`、`llm_path`、`error` 等）。

### `GET /api/ingest/{draft_id}/image`

原始图片。给确认页的 `<img>` 用，所以它接受会话 Cookie。

### `POST /api/ingest/{draft_id}/confirm`

```json
{ "items": [ { "title": "改过的标题", "category": "exam",
               "due_at": "2026-06-01T00:00:00+08:00", "priority": null } ] }
```

请求体可以省略（表示"就用识别结果"）。带上 `items` 就是**"在回传 JSON 基础上
修改后重新提交"**——修改与入库在一次事务里完成。

- 幂等：已确认过再确认会返回 **409**，不会重复创建
- 多条目是一次事务，中途失败全部回滚

### `POST /api/ingest/{draft_id}/discard`

丢弃草稿，不创建任何任务。

---

## 课表

### `GET /api/courses`

返回 `{ text, courses, sessions }`，`text` 可直接回填到编辑框。

### `PUT /api/courses`

文本写法（推荐，服务端确定性解析，不用 LLM）：

```json
{ "text": "周一 08:00-09:40 高等数学 教三201 1-16周\n周三 10:00-11:40 大学物理 理教105 1-16周 单周" }
```

行格式：`<周X> <HH:MM>-<HH:MM> <课程名> [地点] [周次范围] [单周|双周]`。
课程名含空格时用 `|` 分隔：`周一 08:00-09:40 | 高等数学 A | 教三201 | 1-16周`。

也可直接提交结构化数组：

```json
{ "courses": [ { "name": "线性代数", "location": "教二101",
                 "sessions": [ { "weekday": 2, "start_time": "08:00", "end_time": "09:40",
                                 "start_week": 1, "end_week": 16, "week_parity": "all" } ] } ] }
```

**任何一行解析失败就整体拒绝（422）**，并把失败行原样回传：

```json
{ "detail": { "message": "1 行无法解析，已全部拒绝（避免课表静默缺课）",
              "errors": [ { "line": 2, "text": "坏行", "reason": "格式无法识别，应形如…" } ] } }
```

这是刻意的：课表少一节课不会立刻报错，但之后所有「下节课交」的推断都会静默偏移。

---

## 控制台（仅网页会话）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/settings` | LLM 配置（密钥只返回 `api_key_set` 布尔）+ 运行状态 |
| PUT | `/api/settings` | 保存 LLM 配置，**立即生效无需重启**。省略 `api_key` 表示保持原值，传空串表示清空 |
| GET | `/api/keys` | 列出密钥（仅元数据） |
| POST | `/api/keys` | 创建，**明文仅返回一次** |
| DELETE | `/api/keys/{id}` | 吊销 |

## 其它

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 无需鉴权。`{"status":"ok","version":"…","timezone":"…"}` |
| GET | `/api/status` | 运行状态：内存、数据库大小、任务/草稿/密钥计数 |

---

## 追踪号

每个响应都带 `X-Trace-Id`。报错时把它一并提供，就能在服务端日志里
直接定位到那次请求的完整链路：

```bash
journalctl -u schedulekit | grep 't=8f3a2b1c'
```

---

## 给未来的 AI agent

上表就是全部接口，**agent 与人是同一套**：查询用 `GET /api/tasks`，
录入用 `POST /api/ingest`（两阶段确认保证 agent 不会把错误直接写进数据库）。

对接 MCP 时不需要新协议层——FastAPI 的 OpenAPI 描述已经能覆盖全部入参出参，
把它包成 MCP 工具的映射即可。参考实现可看
[donetick 的 MCP server](https://github.com/donetick/donetick)。
