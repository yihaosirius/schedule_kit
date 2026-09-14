# ScheduleKit HTTP 接口规格

供 Apple 快捷指令、Windows 悬浮窗、脚本，以及将来的 AI agent / MCP 使用。

> **本文档的来历。** 这一版不是照着上一版改的，而是把 26 条路由逐条实跑核对
> 出来的：正常路径、每一条错误分支、以及"哪种凭据能做什么"都用真实请求验过。
> 核对过程中发现并修掉了两个实现缺陷（见文末 §9）。凡本文档写的响应结构，
> 都是实际返回过的。
>
> 核对方法：临时配置启动应用，用 `ASGITransport` 直打 ASGI，逐个端点记录
> 状态码与响应结构。`tests/test_api_docs.py` 会把本文档的端点清单与权限矩阵
> 钉住，改了路由不同步改文档就会失败。

所有接口都在 `[server].public_url` 之下（生产为 `https://canisa1ph.duckdns.org:8443`）。
下文用 `$BASE` 指代。

---

## 0. 通用约定

### 0.1 基础信息

| 项 | 值 |
|---|---|
| 版本字段 | `/healthz` 与 `/api/status` 的 `version`，当前 `0.1.0` |
| 请求体 | `application/json`（`POST /api/login` 额外接受表单编码） |
| 响应体 | 一律 `application/json`，**除了** `GET /api/ingest/{draft_id}/image`（图片二进制） |
| 认证 | 会话 Cookie 或 API Key，二选一 |
| 追踪号 | 每个响应都带 `X-Trace-Id` |

### 0.2 端点总表

共 21 个 API 端点 + 1 个健康检查 + 7 个页面路由。`鉴权` 列是**最低要求**。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/login` | 无 | 登录，下发会话 Cookie |
| POST | `/api/logout` | 无 | 清 Cookie |
| GET | `/api/tasks` | 任何凭据 | 按视图列任务 |
| POST | `/api/tasks` | 写 | 创建任务 |
| PATCH | `/api/tasks/{item_id}` | 写 | 局部更新 |
| DELETE | `/api/tasks/{item_id}` | 写 | 删除 |
| GET | `/api/ingest` | 任何凭据 | **列出草稿（草稿箱）** |
| POST | `/api/ingest` | 写 | 上传图片/文本 → 草稿 |
| POST | `/api/ingest/purge` | **仅网页会话** | **永久删除草稿** |
| GET | `/api/ingest/{draft_id}` | 任何凭据 | 读草稿 |
| GET | `/api/ingest/{draft_id}/image` | 任何凭据 | 草稿原图 |
| POST | `/api/ingest/{draft_id}/confirm` | 写 | 确认入库 |
| POST | `/api/ingest/{draft_id}/discard` | 写 | 丢弃草稿 |
| GET | `/api/courses` | 任何凭据 | 读课表 |
| PUT | `/api/courses` | 写 | 整体替换课表 |
| GET | `/api/status` | 任何凭据 | 运行状态 |
| GET | `/api/settings` | **仅网页会话** | 读控制台设置 |
| PUT | `/api/settings` | **仅网页会话** | 存 LLM 配置 |
| GET | `/api/keys` | **仅网页会话** | 列出 API Key |
| POST | `/api/keys` | **仅网页会话** | 创建 API Key |
| DELETE | `/api/keys/{key_id}` | **仅网页会话** | 吊销 API Key |
| GET | `/healthz` | 无 | 存活探测 |
| GET | `/` `/login` `/courses` `/settings` `/drafts` `/drafts/{draft_id}` `/sw.js` | 见 §7 | HTML 页面与 Service Worker |

"任何凭据" = 只读密钥、读写密钥、网页会话都可以。"写" = 读写密钥或网页会话。

### 0.3 鉴权

两种凭据，**任选一种**：

#### 会话 Cookie（浏览器）

`POST /api/login` 成功后下发两枚 Cookie：

| Cookie | HttpOnly | 用途 |
|---|---|---|
| `sk_session` | 是 | HMAC 签名的会话令牌，`<过期时间戳>.<epoch>.<签名>` |
| `sk_csrf` | 否 | CSRF 令牌，前端读出来放进请求头 |

两枚都是 `Path=/; SameSite=Lax; Max-Age=[auth].session_ttl_days × 86400`。
`Secure` **仅当本次请求 scheme 是 `https`** ——本地 http 开发时不能带，否则浏览器直接丢弃。

**写操作必须带 `X-CSRF-Token` 头**，值等于 `sk_csrf` Cookie 的值。缺了会得到：

```json
{ "detail": "缺少或错误的 X-CSRF-Token" }
```

#### API Key（机器客户端）

三种写法都接受：

```http
Authorization: Bearer sk_xxxxxxxxxxxxxxxxxxxxxxxx
```

```http
Authorization: sk_xxxxxxxxxxxxxxxxxxxxxxxx
```

```http
X-API-Key: sk_xxxxxxxxxxxxxxxxxxxxxxxx
```

密钥只有一个权限开关：

| 权限 | 允许 |
|---|---|
| 读写 | 除控制台外的全部接口 |
| 只读 | 仅 `GET` |

**两条容易踩的规则：**

1. **带了 API Key 就不再回退到 Cookie。** 只要请求里出现了上述三种形式之一的凭据，
   即使它无效也不会去试 Cookie；结果是 `401`，哪怕此时 Cookie 完全有效。
   这是刻意的——否则一个配错的客户端会表现成"莫名能用"，把问题藏起来。
2. **API Key 天然不受 CSRF 影响。** `X-CSRF-Token` 只对会话认证校验。

#### 权限矩阵（实测）

| 请求 | 结果 |
|---|---|
| 只读密钥 `GET /api/tasks` | `200` |
| 只读密钥 `POST /api/tasks` | `403` `该密钥为只读，不能执行写操作` |
| 只读密钥 `POST /api/ingest` | `403` `该密钥为只读，不能执行写操作` |
| 读写密钥 `POST /api/tasks` | `201` |
| 读写密钥 `GET /api/settings` | `403` `该操作仅限网页会话` |
| 读写密钥 `POST /api/keys` | `403` `该操作仅限网页会话` |
| 读写密钥 `PUT /api/settings` | `403` `该操作仅限网页会话` |
| 会话写操作缺 `X-CSRF-Token` | `403` `缺少或错误的 X-CSRF-Token` |
| 无效 Bearer（含有效 Cookie） | `401` |

控制台三个端点（`/api/settings`、`/api/keys`）**只认网页会话**：一把被窃取的
API Key 如果能改配置或签发新密钥，就等于能自我提权。

### 0.4 状态码

| 码 | 含义 | 响应头 |
|---|---|---|
| 200 | 成功 | |
| 201 | 已创建（`POST /api/tasks`、`POST /api/ingest`、`POST /api/keys`） | |
| 204 | 成功且无内容（`POST /api/login` JSON 形式、`POST /api/logout`） | |
| 303 | 表单登录成功 / 未登录访问页面 → 重定向 | `Location` |
| 401 | 缺少或无效的凭据 | `WWW-Authenticate: Bearer` |
| 403 | 权限不足（只读密钥写操作、密钥访问控制台、缺 CSRF） | |
| 404 | 资源不存在；也用于未知路径 | |
| 405 | 方法不允许 | |
| 409 | 状态冲突（草稿已确认/已丢弃、PATCH 会破坏二选一） | |
| 413 | 图片过大 | |
| 415 | 图片格式不支持 | |
| 422 | 参数校验失败 / 业务规则拒绝 | |
| 429 | 触发限流 | `Retry-After`（整数秒） |
| 500 | 未捕获异常 | `X-Trace-Id` |
| 502 | LLM 调用失败（草稿已留档） | |
| 503 | LLM 未配置 | |

### 0.5 错误响应体：三种形状

客户端必须能处理这三种，否则会在某条错误路径上崩掉。

**① 业务错误 —— `detail` 是字符串**

```json
{ "detail": "任务 999999 不存在" }
```

**② 结构化错误 —— `detail` 是对象**

课表解析失败：

```json
{
  "detail": {
    "message": "1 行无法解析，已全部拒绝（避免课表静默缺课）",
    "errors": [
      { "line": 2, "text": "坏行",
        "reason": "格式无法识别，应形如「周一 08:00-09:40 高等数学 教三201 1-16周」" }
    ]
  }
}
```

LLM 失败（`502`）：

```json
{ "detail": { "message": "识别失败：…", "draft_id": 7, "retryable": true } }
```

**③ 参数校验错误 —— `detail` 是数组**（FastAPI/Pydantic 默认形状）

```json
{
  "detail": [
    { "type": "extra_forbidden", "loc": ["body", "nope"],
      "msg": "Extra inputs are not permitted", "input": 1 }
  ]
}
```

**500 是唯一的例外**：它把 trace id 也放进 body，因为那个 id 就是排查入口。

```json
{
  "detail": "服务器内部错误。请用下面的 trace id 在服务端日志中查完整堆栈：journalctl -u schedulekit | grep 't=8f3a2b1c'",
  "trace_id": "8f3a2b1c"
}
```

### 0.6 追踪号

每个响应都带 `X-Trace-Id`。你也可以**主动带上**这个请求头，服务端会原样沿用
（只做 strip）——这样客户端侧的日志和服务端日志能直接对上。

```bash
curl -H "X-Trace-Id: my-run-001" -H "Authorization: Bearer $KEY" "$BASE/api/tasks"
```

报错时把 trace id 一并提供，就能还原那次请求的完整链路：

```bash
journalctl -u schedulekit | grep 't=8f3a2b1c'
```

### 0.7 限流

进程内滑动窗口，单用户够用，不需要 Redis。**被拒绝的请求也计入配额**
（限流在参数校验之前检查）。

| 范围 | 配额 | 计数键 | 超限响应 |
|---|---|---|---|
| 登录 | 5 次 / 60 秒 | 客户端 IP | `429` `尝试过于频繁，请 N 秒后再试` |
| 录入 `POST /api/ingest` | 10 次 / 60 秒 | 凭据 | `429` `录入过于频繁，请 N 秒后再试` |

录入的计数键对 API Key 是**密钥名**（`apikey:手机快捷指令`），对会话统一是
`session`。所以同一个密钥名下的所有客户端共享配额。

IP 取自 `X-Forwarded-For` 最左值（Caddy 会带上），没有则用直连地址。

---

## 1. 认证

### `POST /api/login`

同时接受 JSON 与表单编码：登录页用表单（无 JS 也能登录），脚本用 JSON。

```json
{ "password": "你的管理员密码" }
```

请求体格式由 `Content-Type` 决定：含 `application/json` 走 JSON 分支，
否则当表单读 `password` 字段。非法 JSON 视为空密码。

| 情况 | 结果 |
|---|---|
| JSON + 正确密码 | `204`，下发两枚 Cookie |
| 表单 + 正确密码 | `303` → `Location: /` |
| JSON + 错误密码 | `401` `密码错误` |
| 表单 + 错误密码 | `303` → `Location: /login?error=1` |
| 超过 5 次/分钟 | `429` `尝试过于频繁，请 N 秒后再试` + `Retry-After` |

```bash
curl -i -X POST "$BASE/api/login" \
  -H 'Content-Type: application/json' \
  -d '{"password":"…"}'
```

### `POST /api/logout`

清除两枚 Cookie，`204`。无请求体，无需鉴权（幂等）。

会话是无状态签名的，没法逐个吊销。要一次性踢掉所有会话，改密码即可——
`[auth].session_epoch` 会自增，所有既有 Cookie 立即失效。

```bash
curl -X POST "$BASE/api/logout"
```

---

## 2. 任务

### `ItemOut` —— 任务的对外表示

`GET` / `POST` / `PATCH` 返回的都是这个结构。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | |
| `title` | str | |
| `notes` | str | 没填是 `""`，不是 `null` |
| `category` | str | `homework` / `practice` / `exam` / `appointment` / `other` |
| `due_at` | str \| null | **UTC** ISO8601，形如 `2026-06-01T02:00:00+00:00` |
| `priority` | int \| null | 1=Ⅰ 最紧急 … 5=Ⅴ 最不紧急 |
| `status` | str | `open` / `done` / `cancelled` |
| `source` | str | `web` / `shortcut` / `llm` / `api` |
| `client_uuid` | str \| null | 客户端自带的幂等键 |
| `created_at` | str | UTC ISO8601 |
| `updated_at` | str | UTC ISO8601 |
| `completed_at` | str \| null | 置为 `done` 时写入，离开 `done` 时清空 |

> `due_at` 与 `priority` **严格二选一**：一定恰好有一个非 null。
>
> 时间在存储与传输上一律是 UTC。无时区的输入按 `[server].timezone` 解释。
> 要显示成本地时间由客户端负责。

### `GET /api/tasks`

| 参数 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `view` | `ordered` \| `unordered` | `ordered` | 有序 = 有截止时间，按时间**升序**；无序 = 只有优先级，按 Ⅰ→Ⅴ（即 `priority` 升序） |
| `status` | `open` \| `done` \| `cancelled` | 不传 = 全部 | |
| `category` | 5 个分类之一 | 不传 = 全部 | |
| `limit` | 1–200 | 不传 = 不限制 | |

返回 `ItemOut` 数组。无匹配时返回 `[]`（不是 404）。

```bash
curl -H "Authorization: Bearer $KEY" \
  "$BASE/api/tasks?view=ordered&status=open&limit=5"
```

`view` 或 `limit` 非法 → `422`，`detail` 是数组（形状 ③）。

### `POST /api/tasks`

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `title` | str | ✅ | | 1–200 字，**先 strip 再校验长度**，所以 `"   "` 会被拒 |
| `due_at` | str \| null | 二选一 | `null` | ISO8601；无时区按 `[server].timezone` 解释 |
| `priority` | int \| null | 二选一 | `null` | 1–5 |
| `notes` | str | | `""` | ≤2000 字 |
| `category` | str | | `other` | 5 个分类之一 |
| `source` | str | | `web` | `web` / `shortcut` / `llm` / `api` |
| `client_uuid` | str \| null | | `null` | ≤64 字，用于重试幂等 |

```json
{
  "title": "交实验报告",
  "category": "homework",
  "due_at": "2026-04-01T10:00:00+08:00",
  "priority": null,
  "notes": "交到学委处",
  "client_uuid": "shortcut-20260401-1",
  "source": "shortcut"
}
```

| 情况 | 结果 |
|---|---|
| 正常创建 | `201` + `ItemOut` |
| `client_uuid` 命中既有任务 | `200` + **同一个** `ItemOut`（不重复创建） |
| `due_at` 与 `priority` 都给或都不给 | `422` `due_at 与 priority 必须二选一：…` |
| `title` 为空 | `422` |
| 未知字段 | **被忽略**，照常创建（不报错） |

> `source` 是**客户端自报**的，服务端不会根据凭据推断。用快捷指令时不写
> `source` 就记为 `web`。想让统计准确就自己带上。

`client_uuid` 的幂等性有实际价值：快捷指令网络超时后重试不会产生两条任务。

### `PATCH /api/tasks/{item_id}`

局部更新，**只处理显式出现的字段**。

| 字段 | 类型 | 约束 |
|---|---|---|
| `title` | str \| null | 1–200 字；传 `null` 会被**忽略**，不会清空 |
| `notes` | str \| null | ≤2000 字；传 `null` 会被忽略 |
| `category` | str \| null | 传 `null` 会被忽略 |
| `due_at` | str \| null | 见下 |
| `priority` | int \| null | 1–5，见下 |
| `status` | str \| null | `open` / `done` / `cancelled` |

**二选一的自动维护**（这是本接口最容易搞错的地方）：

| 请求 | 结果 |
|---|---|
| 给非 null `due_at` | `priority` 被**自动清空** |
| 给非 null `priority` | `due_at` 被**自动清空** |
| 显式给 `due_at: null` 但没给 `priority` | `409` `清空截止时间时必须同时给出优先级，否则任务既无时间也无优先级` |
| 显式给 `priority: null` 但没给 `due_at` | `409` `清空优先级时必须同时给出截止时间，否则任务既无时间也无优先级` |
| 同一请求里两个都显式给非 null | `422` |
| 同一请求里两个都显式给 `null` | `422` |
| **未知字段** | `422` `extra_forbidden`（与 `POST` 不同！） |

`status` 的副作用：置为 `done` 会写入 `completed_at`；改为其它值时清空它。

一个字段都没实际变更时，直接返回原记录，**不刷新 `updated_at`**。

```bash
curl -X PATCH -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"status":"done"}' "$BASE/api/tasks/12"
```

已完成 → 未完成，只要 `{"status":"open"}` 即可。

### `DELETE /api/tasks/{item_id}`

```json
{ "deleted": true, "id": 12 }
```

不存在 → `404` `任务 12 不存在`。已删除的再删也是 `404`。

---

## 3. 智能录入（两阶段确认）

**第一阶段只产生草稿，绝不写任务表。** 只有显式确认才落库——模型出错、网络
中断、用户改主意，都不会留下脏数据。

草稿状态机：

```
pending ──confirm──> confirmed
   │
   ├────discard───> discarded
   │
   └────LLM 失败──> failed
```

`confirmed` / `discarded` / `failed` 都是终态，任何操作都得到 `409`。

### `POST /api/ingest`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `channel` | str | ✅ | `image` 或 `text` |
| `image_base64` | str | `channel=image` 时 ✅ | **不要带 `data:` 前缀**，只要 base64 本体 |
| `text` | str | `channel=text` 时二选一 | ≤8000 字 |
| `items` | array | 二选一 | 已结构化的条目；提供时**跳过 LLM** |
| `mime` | str | | 接受但**完全忽略**——服务端按实际字节判断 |

```json
{ "channel": "image", "image_base64": "…", "mime": "image/jpeg" }
```

**图片要求**：JPEG / PNG / WebP / GIF。HEIC **一律拒绝**（iPhone 默认格式），
`415` 会把解决办法写在错误里。快捷指令里加一个「转换图像」动作选 JPEG 即可。

体积上限 `[llm].max_image_bytes`（默认 8 MB）。服务端先按 base64 长度预检
（上限 × 1.4，base64 膨胀约 1/3），超了给 `413`，不会先解码再拒绝。

`channel=text` 且给了 `items` 时**不调用 LLM**：这对应"快捷指令已经拿到了
结构化内容"的场景。

成功返回 `201`：

```json
{
  "draft_id": 12,
  "status": "pending",
  "channel": "image",
  "items": [
    {
      "title": "第三章习题",
      "category": "homework",
      "due_at": "2026-09-15T15:59:00+00:00",
      "priority": null,
      "notes": "只做奇数题",
      "source_quote": "下周一交",
      "needs_priority": false,
      "adjustments": []
    }
  ],
  "context_snapshot": "[当前时间] 2026-09-14 星期一 16:56（第 3 周）\n[下节课] …",
  "image_path": "2026-09-14/b62f0e7961c59640.jpg",
  "image_url": "/api/ingest/12/image",
  "input_text": null,
  "error": null,
  "llm_provider": "responses",
  "llm_model": "deepseek-flash",
  "llm_path": "tool_call",
  "llm_elapsed_ms": 1840.2,
  "created_at": "2026-09-14T08:56:40+00:00",
  "expires_at": "2026-09-16T08:56:40+00:00",
  "created_item_ids": [],
  "confirm_url": "https://canisa1ph.duckdns.org:8443/drafts/12"
}
```

草稿条目字段（**与 `ItemOut` 不同**）：

| 字段 | 说明 |
|---|---|
| `title` `category` `due_at` `priority` `notes` | 同 `ItemOut` |
| `source_quote` | 图片/文本中支撑该判定的**原文片段**，让确认页可以核对"它从哪儿读出来的" |
| `needs_priority` | 模型既没给时间也没给优先级时置 `true`，已默认填 Ⅲ |
| `adjustments` | 服务端改写过模型输出的记录 |

`llm_path` 说明这次结构化结果是怎么拿到的：

| 值 | 含义 |
|---|---|
| `tool_call` | 正常路径——模型按要求返回了 `submit_tasks` 工具调用 |
| `json_fallback` | **降级路径**——模型没返回工具调用（或被供应商拒绝），服务端改用 JSON 模式又取了一遍。结果通常没问题，但反复出现就该换模型了 |
| `null` | 没走 LLM（客户端直接给了 `items`），此时 `llm_provider` 是 `"client"` |

**服务端强制后处理**，所以 `items` 里的值可能与模型说的不同：

- 有 `due_at` 时 `priority` 一律被置为 `null`
- 两者都没有时补默认优先级 `3`，并置 `needs_priority: true`
- 非法分类降级为 `other`，越界优先级截断到 1–5，超长文本截断
- 无时区时间按 `[server].timezone` 解释

**错误码**

| 码 | 含义 |
|---|---|
| `413` | 图片过大 |
| `415` | 格式不支持（HEIC 附带"请先转 JPEG"的具体做法） |
| `422` | 参数错误 / base64 非法 / 客户端给的 `items` 违反二选一 |
| `429` | 录入过于频繁（10 次/分钟/凭据） |
| `502` | LLM 调用失败。**草稿已留档**，`detail.draft_id` 可用，`retryable: true` |
| `503` | LLM 未配置（去 `/settings` 填，保存后立即生效） |

`502` 的 `detail.message` 会带上**每条通道各自失败的原因**——第一条通常才是
有用的那条。临时性失败（连不上、超时、429、5xx）在返回 502 之前已经按
`[llm].retry_count` 自动重试过；`401`/`400` 这类确定性错误不重试。

```bash
curl -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"channel":"text","text":"下周一交第三章习题"}' "$BASE/api/ingest"
```

### `GET /api/ingest/{draft_id}`

读取草稿。结构与 `POST` 的返回**完全相同，只少 `llm_elapsed_ms`**（那个字段
只在刚识别完时有意义）。

不存在或已过期清理 → `404` `草稿 999999 不存在或已过期`。

### `GET /api/ingest/{draft_id}/image`

原始图片的二进制。给确认页的 `<img>` 用，所以它接受会话 Cookie。

| 情况 | 结果 |
|---|---|
| 正常 | `200` + 图片字节 |
| 草稿不存在 | `404` `草稿 N 不存在或已过期` |
| 草稿没有图（`channel=text`） | `404` `该草稿没有图片` |
| 图片文件已不在磁盘上 | `404` `图片文件已不在` |

### `POST /api/ingest/{draft_id}/confirm`

**请求体可以完全省略**（表示"就用识别结果"）。带上 `items` 就是"在回传的
JSON 基础上改完再提交"——修改与入库在**一个事务**里完成，不会出现"改完了
但没确认"的中间态。

```json
{
  "items": [
    { "title": "改过的标题", "category": "exam",
      "due_at": "2026-06-01T00:00:00+08:00", "priority": null,
      "notes": "", "source_quote": "" }
  ]
}
```

返回 `200`，body 是草稿表示（`status` 变成 `confirmed`）**外加**：

```json
{ "created_item_ids": [42, 43] }
```

| 情况 | 结果 |
|---|---|
| 正常 | `200` |
| 已确认过再确认 | `409` `草稿 12 已确认过（任务 [42, 43]）` |
| 已丢弃的草稿 | `409` `草稿 12 状态为 discarded，无法确认` |
| `items` 为空数组 | `422` `至少要有一条事项，全部删除请改用「丢弃」` |
| `items` 里某条违反二选一 | `422` `第 N 条必须二选一：…` |

确认时的校验比识别时**严格得多**：这里处理的是人确认过的输入，任何不合规都
报错，绝不静默丢弃。识别阶段则是容错优先，坏条目直接丢掉。

> 注意：确认入库的任务，`source` 一律记作 `llm`——**即使这次根本没调用 LLM**
> （客户端直接给了 `items`）。想区分来源看草稿的 `llm_provider` 字段，
> 它是 `client`。

### `POST /api/ingest/{draft_id}/discard`

丢弃草稿，不创建任何任务。`200` + 草稿表示（`status` 变成 `discarded`）。

已确认或已丢弃 → `409` `草稿当前状态为 confirmed，不能丢弃`。

---

## 3b. 草稿箱（闲时清理）

`discard` 只改状态、留痕；这一节的两个端点是**给人清理用的**：一个列出草稿，
一个把行真删掉。

### `GET /api/ingest`

列出草稿，新的在前。

| 参数 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `status` | `pending` \| `confirmed` \| `discarded` \| `failed` | 不传 = 全部 | 非法值 `422` |
| `channel` | `image` \| `text` | 不传 = 全部 | |
| `limit` | 1–200 | `50` | |
| `offset` | ≥0 | `0` | |

```json
{
  "drafts": [
    {
      "draft_id": 12,
      "status": "confirmed",
      "channel": "image",
      "item_count": 2,
      "preview": "第三章习题",
      "input_text": null,
      "error": null,
      "has_image": true,
      "llm_provider": "responses",
      "llm_model": "deepseek-flash",
      "llm_path": "tool_call",
      "created_at": "2026-09-14T08:56:40+00:00",
      "expires_at": "2026-09-16T08:56:40+00:00",
      "confirmed_at": "2026-09-14T09:02:11+00:00",
      "created_item_ids": [42, 43]
    }
  ],
  "total": 137,
  "limit": 50,
  "offset": 0,
  "counts": { "pending": 2, "confirmed": 130, "discarded": 4, "failed": 1 }
}
```

- `total` 是**过滤后**的总数（不是本页条数），供分页用。
- `counts` 恒含四个状态，没有就是 `0` ——不用自己折算。（对比 §6 的
  `status.drafts`，那个是稀疏字典。）
- **这是轻量表示**：不含 `items` 与 `context_snapshot`。一次列 50 条时那些
  字段会把响应撑到几百 KB。要看全文走 `GET /api/ingest/{draft_id}`。
- `preview` 优先取第一个事项的标题，没有就用 `input_text` 开头。

只读密钥也能读——小组件可以用它显示待确认数量。

### `POST /api/ingest/purge`

**永久删除**草稿行。

```json
{ "ids": [12, 13, 14] }
```

`ids` 必填，1–200 个。

```json
{ "deleted": 3, "ids": [12, 13, 14], "missing": [] }
```

`missing` 是本次没找到（已经不存在）的 id。重复提交**不报错**，只是
`deleted` 变 0、`missing` 列出它们——重试和并发点击都会走到这里，
让它报错只会制造假故障。

**权限：只认网页会话。** 读写 API Key 也会得到
`403 该操作仅限网页会话`。这是刻意的——见 §9 的说明。

**两条绝不越界的事**（都有回归用例钉住）：

| 不变量 | 原因 |
|---|---|
| 删除草稿**不会**删除已入库的任务 | `items` 与 `ingest_drafts` 之间没有外键，`created_item_ids` 只是个 JSON 数组 |
| 删除草稿**不会**删除图片文件 | `media.store_image` 按内容哈希命名且已存在就不重写，所以同一天上传的相同图片**由多份草稿共用**；已确认的草稿更是永久保留 `image_path`。图片的生命周期只由保留策略（`[backup].upload_retention_days`）管 |

第二条尤其容易写错：在删除路径里顺手 `unlink` 会把别人的图删掉。

### 三种"删除"的关系

| 操作 | 端点 | 效果 | 谁能做 |
|---|---|---|---|
| 丢弃 | `POST /api/ingest/{id}/discard` | 状态改成 `discarded`，**留痕**，到 TTL 才被自动清掉 | 读写密钥 / 会话 |
| 永久删除 | `POST /api/ingest/purge` | **真删行**，不可恢复 | 仅网页会话 |
| 自动清理 | （无端点） | 每小时扫一次，删掉过期的 `pending`/`discarded`/`failed` | 后台任务 |

`confirmed` 的草稿**永远不会被自动清理**，要删只能显式 purge。它是"识别到了
什么 → 创建了哪些任务"的唯一记录。

---

## 4. 课表

### `GET /api/courses`

```json
{
  "text": "周一 08:00-09:40 高等数学 教三201 1-16周",
  "courses": [
    {
      "id": 1,
      "name": "高等数学",
      "location": "教三201",
      "sessions": [
        { "weekday": 1, "start_time": "08:00", "end_time": "09:40",
          "start_week": 1, "end_week": 16, "week_parity": "all",
          "location": "教三201" }
      ]
    }
  ],
  "course_count": 1,
  "session_count": 1
}
```

`text` 可以直接回填到编辑框，与解析它的解析器是同一份实现。

`weekday` 1–7（1 = 周一）。`week_parity`：`all` / `odd`（单周）/ `even`（双周）。

### `PUT /api/courses`

**整体替换**，不是增量。接受两种输入，二选一。

**① 文本形式**（推荐——服务端确定性解析，不用 LLM）

```json
{ "text": "周一 08:00-09:40 高等数学 教三201 1-16周\n周三 10:00-11:40 大学物理 理教105 1-16周 单周" }
```

行格式：`<周X> <HH:MM>-<HH:MM> <课程名> [地点] [周次范围] [单周|双周]`

课程名含空格时用 `|` 分隔：`周一 08:00-09:40 | 高等数学 A | 教三201 | 1-16周`

空行与 `#` 开头的行会被跳过。

**② 结构化形式**

```json
{
  "courses": [
    { "name": "线性代数", "location": "教二101",
      "sessions": [
        { "weekday": 2, "start_time": "08:00", "end_time": "09:40",
          "start_week": 1, "end_week": 16, "week_parity": "all", "location": "" }
      ] }
  ]
}
```

`SessionIn` 的约束：`weekday` 1–7；`start_time`/`end_time` 必须匹配 `^\d{2}:\d{2}$`；
`start_week`/`end_week` 1–30（默认 1 / 18）；`week_parity` ∈ `all|odd|even`。

**清空课表**：提交 `{"courses": []}` 或把文本清空（纯空白）。两者都返回 `200`
且 `course_count` 变 0。

**原子性：任何一行解析失败就整体拒绝**，一条都不入库：

```json
{
  "detail": {
    "message": "1 行无法解析，已全部拒绝（避免课表静默缺课）",
    "errors": [{ "line": 2, "text": "坏行", "reason": "格式无法识别，应形如「…」" }]
  }
}
```

这是刻意的：课表少一节课不会立刻报错，但之后所有「下节课交」的推断都会
静默偏移，比直接报错难查得多。

| 情况 | 结果 |
|---|---|
| 正常 | `200` = `GET` 的结构 + `saved_courses` + `saved_sessions` |
| 有行解析失败 | `422` + 逐行原因，**一条都不入库** |
| `text` 与 `courses` 都没给 | `422` `需要提供 text 或 courses 之一` |
| 非空文本却没解析出课程 | `422` `文本里没有解析出任何课程。要清空课表，请提交空内容或空课程数组。` |

```bash
curl -X PUT -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"text":"周一 08:00-09:40 高等数学 教三201 1-16周"}' "$BASE/api/courses"
```

课表会作为上下文注入到每次识别的 prompt 里，让「下节课交」这类相对时间能
解析成具体日期。

---

## 5. 控制台（仅网页会话）

三个端点都只接受会话 Cookie，API Key 一律 `403 该操作仅限网页会话`。

### `GET /api/settings`

```json
{
  "llm": {
    "provider": "responses",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "api_key_set": true,
    "temperature": 0.0,
    "timeout_seconds": 60,
    "max_tokens": 1024,
    "max_image_bytes": 8388608,
    "system_prompt": "…",
    "retry_count": 3,
    "retry_backoff_seconds": 0.8,
    "ready": true,
    "error": null
  },
  "term": { "start_date": "2026-03-02", "total_weeks": 18 },
  "ingest": { "confirm_ttl_hours": 48 },
  "status": { "…见 §6…" }
}
```

`api_key` **永不回显**，只有 `api_key_set` 布尔。`ready` 表示适配器构建成功；
失败时 `error` 给出原因，此时录入接口返回 `503`。

### `PUT /api/settings`

```json
{
  "provider": "responses",
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-flash",
  "api_key": "sk-…",
  "temperature": 0.0,
  "timeout_seconds": 60,
  "max_tokens": 1024,
  "max_image_bytes": 8388608,
  "system_prompt": "…",
  "retry_count": 3,
  "retry_backoff_seconds": 0.8
}
```

| 字段 | 默认 | 约束 |
|---|---|---|
| `provider` | `responses` | `responses` / `openai_compat` / `mock`，其它值 `422` 并列出可选值 |
| `base_url` | `""` | ≤300 字。Responses 填到域名即可，路径由适配器补 |
| `model` | `""` | ≤120 字 |
| `api_key` | **省略** | **省略 = 保持原值；空串 = 清空**。前端不改密钥时就不发这个字段，避免密钥在浏览器与服务器之间来回搬运 |
| `temperature` | `0.0` | 0.0–2.0 |
| `timeout_seconds` | `60` | 5–600 |
| `max_tokens` | `1024` | 64–32000。Responses 里对应 `max_output_tokens`，且**包含思考 token** |
| `max_image_bytes` | `8388608` | 64 KB–32 MB |
| `system_prompt` | `""` | ≤20000 字 |
| `retry_count` | `3` | 0–5。临时性失败的重试次数，**不含**首次尝试 |
| `retry_backoff_seconds` | `0.8` | 0.0–10.0。指数退避基数，实际会加 ±25% 抖动 |

写入 `config.toml`（保留全部注释），**立即重建适配器，无需重启**。响应是
`GET` 的结构加上：

```json
{ "reloaded": true, "restart_required": false }
```

`restart_required` 恒为 `false`：`[llm]` 段是热加载的。改 `[server]` / `[tls]`
才需要重启。

`system_prompt` 保持静态——**不要写入时间或课表**。时间上下文由服务端在每次
请求时拼进 user message，写进 system prompt 会让 prompt 缓存永远失效。

### `GET /api/keys`

```json
{
  "keys": [
    { "id": 1, "name": "手机快捷指令", "read_only": false,
      "created_at": "2026-09-14T08:00:00+00:00",
      "last_used_at": "2026-09-14T09:12:00+00:00" }
  ]
}
```

**已吊销的密钥不在列表里**，没有任何办法再取回明文。

### `POST /api/keys`

```json
{ "name": "手机快捷指令", "read_only": false }
```

`name` 1–60 字，`read_only` 默认 `false`。返回 `201`：

```json
{
  "id": 1,
  "name": "手机快捷指令",
  "key": "sk_LdQfWrxNupoc-Z3WH7N0Npx51FeTDFghmWxkzeTiKvo",
  "read_only": false
}
```

> **`key` 是明文，只在这里出现这一次。** 服务端只存 SHA-256，之后再也拿不回来。
>
> 注意 `POST` 的返回**没有** `created_at` / `last_used_at`，而 `GET` 的每一项
> **有**——两个结构不一样。

创建只读密钥给不需要写入的客户端（例如 iOS 小组件），能显著缩小密钥泄露的
影响范围。

### `DELETE /api/keys/{key_id}`

```json
{ "revoked": true, "id": 1 }
```

吊销是立即生效的：用它的客户端下一个请求就是 `401`。密钥不存在或已吊销 →
`404` `密钥 1 不存在或已吊销`。

---

## 6. 运行状态与健康检查

### `GET /api/status`

任何凭据都可以读。这是 `/api/settings` 里 `status` 字段的同一份内容。

```json
{
  "version": "0.1.0",
  "config_path": "/etc/schedulekit/config.toml",
  "data_dir": "/var/lib/schedulekit",
  "timezone": "Asia/Shanghai",
  "public_url": "https://canisa1ph.duckdns.org:8443",
  "server_time": "2026-09-14T08:56:40+00:00",
  "rss_bytes": 60014592,
  "db_bytes": 61440,
  "uploads_bytes": 0,
  "tasks": { "open": 1, "done": 1, "ordered": 0, "unordered": 1 },
  "drafts": { "pending": 1, "confirmed": 2 },
  "keys": { "active": 2, "revoked": 1 },
  "courses": { "courses": 1, "sessions": 1 },
  "llm": {
    "provider": "responses", "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash", "api_key_set": true,
    "ready": true, "error": null
  }
}
```

> **`tasks` 与 `drafts` 是稀疏字典**：只出现实际存在的状态，外加 `tasks` 里
> 恒有的 `ordered` / `unordered`。客户端必须用 `.get(key, 0)`，不要直接取值。
> `rss_bytes` 在开发机上可能返回 `null`（采集失败降级为 `null`，不抛错）。

### `GET /healthz`

无需鉴权。给反向代理和监控用。

```json
{ "status": "ok", "version": "0.1.0", "timezone": "Asia/Shanghai", "migrations_applied": [] }
```

`migrations_applied` 是**本次启动**新应用的迁移文件名列表，平时是空数组。

---

## 7. 页面路由

这些返回 HTML，不是 JSON。它们**不走 FastAPI 的依赖注入**——未认证时返回
`303` 重定向，而不是 `401`。写客户端脚本时别把它们和 `/api/*` 混在一起。

| 路径 | 未认证 | 已认证 |
|---|---|---|
| `GET /` | `303` → `/login` | `200` 任务双列表 |
| `GET /login` | `200` 登录页 | `303` → `/` |
| `GET /courses` | `303` → `/login?next=/courses` | `200` 课表页 |
| `GET /settings` | `303` → `/login?next=/settings` | `200` 控制台 |
| `GET /drafts` | `303` → `/login?next=/drafts` | `200` **草稿箱**（支持 `?status=` `?offset=`） |
| `GET /drafts/{draft_id}` | `303` → `/login?next=/drafts/{draft_id}` | `200` 确认页；草稿不存在也返回 `200`（渲染"找不到"页面） |
| `GET /sw.js` | 无需鉴权 | `200`，`Cache-Control: no-cache`，`Service-Worker-Allowed: /` |

`/drafts` 的筛选与分页全走查询参数 + 服务端渲染：无 JS 也能用，URL 可以存书签。
每页 25 条。**未知的 `?status=` 在页面上退化成"不筛选"**（而在 API 上是 `422`）
——页面上给个空列表会让人以为草稿丢了。

`/sw.js` 必须从站点根路径提供，作用域才能覆盖整站。它被放在 `/static/` 下的话
默认作用域只有 `/static/`，页面导航就管不到了。

PWA manifest 在 `/static/manifest.webmanifest`。Service Worker 只在 HTTPS 下注册。

---

## 8. 给未来的 AI agent

上表就是全部接口，**agent 与人是同一套**：

| 意图 | 调用 |
|---|---|
| 查任务 | `GET /api/tasks?view=ordered` / `?view=unordered` |
| 加任务 | `POST /api/tasks`（带 `client_uuid` 获得幂等性） |
| 完成/改期 | `PATCH /api/tasks/{item_id}` |
| 从自然语言或图片录入 | `POST /api/ingest` → 人工确认 → `POST /api/ingest/{draft_id}/confirm` |

**给 agent 的密钥建议用只读**，除非它确实需要写入。只读密钥读不了控制台，
也改不了配置——一把泄露的 agent 密钥影响面仅限于读。

录入的两阶段设计对 agent 尤其重要：`POST /api/ingest` **永远不会直接写任务
表**，所以 agent 判断错时间也不会污染数据，最多产生一条待确认草稿（默认
`[ingest].confirm_ttl_hours` 小时后自动清理）。

对接 MCP 不需要新协议层：`app.openapi()` 能生成完整入参出参描述，把它映射成
MCP 工具即可。注意 `create_app()` 默认关闭了 `docs_url` / `openapi_url`
（不公开暴露），要取 schema 直接在进程内调 `app.openapi()`。

---

## 9. 核对记录：本次发现并修掉的两个缺陷

写这份文档时把每条路由实跑了一遍，发现两处**文档与实现都不对**的地方。
两处都已修复并补了回归用例。

### ① `GET /api/courses` 的结构化课表被计数覆盖

`_serialize()` 先构造了 `courses` 数组，紧接着 `**course_service.counts(db)`
展开进来，而 `counts()` 也返回一个 `"courses"` 键——**展开在后，把数组覆盖成了
整数**。结果是结构化课表根本取不到，只能拿 `text` 自己再解析一遍。
`session_count` 同理。

更值得记的是**为什么一直没被发现**：`tests/test_timetable.py` 当时断言的是
`fetched["courses"] == 2`，而那个 `2` 正是计数。用例在给 bug 背书。
这类"断言的值恰好等于错误实现产出的值"是最难自查的一种。

现在计数改叫 `course_count` / `session_count`，用例断言的是课程名列表。

### ② 课表写进去就删不掉

`put_courses` 里的守卫是 `if not parsed: raise 422 "课表为空；如需清空请显式
提交空课程数组"`——而提交空数组**同样**落进这个分支。于是：

- API 清不掉课表，包括按错误提示推荐的写法；
- 网页上把文本框清空再保存也失败，用户看到那句自相矛盾的提示。

现在空输入就是清空：`{"courses": []}` 或纯空白文本都返回 `200`。
纯注释文本（如 `# 备注`）仍报 `422`——那不是清空的表达方式，不该悄悄擦掉课表。

### ③ 失败草稿的"模型原始返回"根本不存在（2026-09 做草稿箱时发现）

`draft.html` 的失败分支原本写着「草稿已留档（含**模型原始返回**）」。实际上
`create_failed_draft` 的 `llm_raw` 参数**从未被传入**——失败时那一列恒为 `NULL`，
API 也从不下发它。用户被指去看一个查不到的东西。

能拿到的只有 `error`，而它对 HTTP 错误已经内含响应体片段（上限 500 字符）。
文案已改成「错误详情已记在上面」。

**没有**顺手去补 `llm_raw`：传输层失败时根本不存在"模型的原始返回"，硬塞一个
半截的反而更误导。真要保留原始输出，得先在 `extract()` 里把响应体挂到异常上，
那是另一件事。

### ④ 删草稿时最容易顺手毁掉的两样东西（草稿箱引入）

草稿箱唯一的破坏性动作是删行，而它旁边有两样东西**看起来**该跟着删、
实际上绝不能删：

1. **图片文件。** `media.store_image` 按内容哈希命名（`sha256[:16]`）且
   `if not target.exists()` 才写盘——所以同一天上传两次相同图片会**共用同一个
   文件**，已确认的草稿还永久保留 `image_path`。在删除路径里 `unlink` 会把
   别人的图删掉。图片生命周期只归 `housekeeping._purge_old_uploads` 管。
2. **已入库的任务。** `items` 与 `ingest_drafts` 之间没有外键，
   `created_item_ids` 只是 JSON 数组。

两条各有用例，且都用"**另一个对象仍然可用**"来断言（删掉共用图片的两条草稿之一
后，另一条的 `/image` 仍返回 `200`），而不是只断言"删掉了 1 条"。

`purge` 之所以**只认网页会话**、连读写 API Key 都拒绝，也是这个原因：
`discard` 只改状态、留痕，出错了还能查；`purge` 不可恢复，而草稿行里
有 `error` 这些排查线索。机器流程用 `discard`，人用 `purge`。

### 顺带确认无误的几处

以下是怀疑过、实跑后确认**行为正确**的：

- 无效 Bearer 不会回退到有效的 Cookie（`401`），避免掩盖客户端配置错误
- 确认入库的任务 `source` 恒为 `llm`（哪怕没调 LLM）；来源要看草稿的 `llm_provider`
- `POST /api/tasks` 忽略未知字段，`PATCH` 拒绝未知字段——不一致但有原因：
  创建时宽容便于客户端先跑起来，更新时严格避免"以为改了其实没改"
- 被限流拒绝的请求也计入配额（限流在参数校验之前）
