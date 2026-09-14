# 开发笔记

给未来的自己（和未来的 agent）看的踩坑记录与约定。

---

## 1. Trace 日志约定

实现在 `app/logging.py`。**所有模块都用这一套，不要另起 `print()`。**

### 格式

```
2026-03-20T14:32:01.123 INFO  sk.http          [t=8f3a2b1c] request.start method=POST path=/api/ingest auth=apikey
2026-03-20T14:32:01.150 INFO  sk.ingest        [t=8f3a2b1c] ingest.received channel=image mime=image/jpeg bytes=1048576 sha256=3f9a…(len=64)
2026-03-20T14:32:01.160 INFO  sk.context       [t=8f3a2b1c] context.built in_term=true week=4 lines=6 chars=182
2026-03-20T14:32:02.980 INFO  sk.llm           [t=8f3a2b1c] llm.request provider=responses model=deepseek-flash endpoint=https://api.deepseek.com/responses tier=tool_call images=1
2026-03-20T14:32:05.789 INFO  sk.llm           [t=8f3a2b1c] llm.response tier=tool_call items=2 raw_chars=420 attempts=1 elapsed_ms=2809 input_tokens=1180 output_tokens=210
2026-03-20T14:32:05.801 INFO  sk.normalize     [t=8f3a2b1c] normalize.item index=0 has_due=true priority_dropped=2 due_at=2026-03-27T15:59:00+00:00
2026-03-20T14:32:05.803 WARNING sk.normalize   [t=8f3a2b1c] normalize.item index=1 needs_priority=true defaulted=3
2026-03-20T14:32:05.820 INFO  sk.http          [t=8f3a2b1c] request.end status=201 elapsed_ms=4697
```

### 规则

| 规则 | 原因 |
|---|---|
| 事件名用 `点分.小写`，且稳定不变 | 出问题时 `grep request.end` 就能还原链路 |
| 同一个请求共享 `[t=xxxxxxxx]` | 中间件注入；同时回写响应头 `X-Trace-Id`，客户端报错可直接对上服务端日志 |
| **"代码推翻了模型输出"必须打点** | 例如 `normalize.item ... priority_dropped=2`。否则事后无法判断是模型错了还是后处理改的 |
| 外部调用前后各打一条 | `llm.request` / `llm.response`，带 `elapsed_ms` 与结果规模 |
| 密钥一律不进日志 | `kv()` 自动屏蔽含 `key`/`token`/`password`/`secret`/`cookie` 等子串的字段；其余用 `mask()` |
| 长文本截断到 300 字符 | 避免一条日志刷屏；截断处标注 `…(+N)` |

### 用法

```python
from app.logging import get_logger, kv

log = get_logger("ingest")           # → sk.ingest
log.info("ingest.received %s", kv(channel=channel, mime=mime, bytes=size))
```

### 排查入口

```bash
# 整个服务端都在 stdout（生产由 journald 收集）
journalctl -u schedulekit -f

# 只看某个请求
journalctl -u schedulekit | grep 't=8f3a2b1c'

# 只看 LLM 的耗时与产出规模
journalctl -u schedulekit | grep 'llm.response'

# 只看后处理改动了模型输出的地方
journalctl -u schedulekit | grep -E 'priority_dropped|needs_priority|category_downgraded'
```

---

## 2. 本机（沙箱）环境的坑

这些是 **DSH 沙箱**造成的，不是代码问题。在普通终端里不会遇到。

### 2.1 只允许 Node / uv 走 HTTPS

Windows Schannel 在本会话不可用（`SEC_E_NO_CREDENTIALS`），导致：

| 工具 | 走什么 TLS | 结果 |
|---|---|---|
| `curl.exe` | Schannel | ❌ `exit 35` |
| `git` | Schannel | ❌ `Recv failure` |
| PowerShell `Invoke-RestMethod` | Schannel | ❌ SSL 失败 |
| **Node.js** | 自带 OpenSSL | ✅ 正常 |
| **uv** | 自带 rustls | ✅ 正常 |

需要下载东西时用 `uv` 或 `node`，**不要用 `curl` / `git clone`**。

### 2.2 文件系统：目录删除与重命名被拒

| 操作 | 结果 |
|---|---|
| 建目录、写文件、读文件 | ✅ |
| 文件 `unlink` / `os.replace` | ✅ |
| **`shutil.rmtree` / 删除目录** | ❌ `WinError 5` |
| **目录重命名** | ❌ |

副作用：pytest 默认的 `tmp_path`（在 `%TEMP%` 下建目录再清理）会直接报 `PermissionError`。
因此 `tests/conftest.py` 覆盖了 `tmp_path`，改用工作区内自建目录 `.pytest-run/`，
且不做清理。**测试命令必须带 `-p no:cacheprovider`**，否则 `.pytest_cache` 也会失败。

```bash
uv run pytest -q -p no:cacheprovider
```

### 2.3 写入范围

允许：工作区 `D:\schedule_kit`、会话临时目录。
拒绝：用户目录下的其它位置（例如 `C:\Users\ASUS\dsh-probe.txt`、`AppData\Local\npm-cache`）。

因此 uv 的缓存目录要重定向：

```powershell
$env:UV_CACHE_DIR = "$env:TEMP\sk-uvcache"
uv sync
```

### 2.4 跨进程目录可见性

**PowerShell 创建的目录，Python 子进程可能无权访问**（`scandir` 报 `WinError 5`）。
所以临时目录一律由使用它的那个进程自己创建，不要用 shell 预建。

---

## 3. 常用命令

```bash
# 依赖
uv sync

# 本地起服务（开发模式，改动自动重载）
uv run python -m app.serve --reload

# 测试
uv run pytest -q -p no:cacheprovider

# 只看某个测试
uv run pytest -q -p no:cacheprovider tests/test_config.py -k xor

# 查看生效配置（密钥已脱敏）
uv run python -m app.cli show --json
```

---

## 4. 数据集不变量（改动 schema 前务必读）

- `items` 的 `due_at` 与 `priority` **严格二选一**，由数据库 `CHECK` 约束保证。
  任何新代码路径都必须满足它，不要试图绕过。
- **后处理永远优先于模型输出**：模型给的 `priority` 在存在 `due_at` 时会被强制丢弃。
  这是业务规则，不是模型的责任。
- 时间一律 **UTC ISO8601 字符串**入库；只有渲染和 LLM 交互时才换算成配置时区。

---

## 5. 两处有意偏离原计划的地方

记录原因，免得以后有人"照着计划改回去"。

### 5.1 LLM 的 api_key 明文存储，不做 Fernet 加密

原计划写的是"用 Fernet 加密后写回 config.toml"，实施时去掉了，同时移除了
`cryptography` 依赖。原因：**解密密钥（`[auth].secret_key`）就在同一个文件里**。
拿到 `config.toml` 的人同时拿到密文和解密密钥，加密等于零收益，只增加代码量
和一次解密失败的可能。

真正有效的保护是这些，都已经落实：

- 文件权限 `0600`、属服务账号（`install.sh` 负责）
- `cli show` 默认脱敏，只有 `--secrets` 才输出明文（供 root 部署脚本用）
- HTTP 接口**永不回显**密钥，只返回 `api_key_set` 布尔
- `kv()` 日志脱敏（见 §1）

如果将来真需要"文件泄露也不暴露密钥"，正确做法是把密钥放到 config.toml
**之外**（例如 systemd 的 `LoadCredential=` 或单独的 root-only 文件），
而不是在同文件里加密。

**顺带一个教训**：`cryptography` 在依赖列表里躺了整整五个里程碑都没被 import 过。
加依赖时要问一句"现在就用得上吗"。

### 5.2 课表地点存在 `course_sessions` 而不是 `courses`

原 schema 把 `location` 放在 `courses` 上。但同一门课换教室是常态
（单双周不同教室、前半学期后半学期不同教室），放在课程级别会丢信息。
迁移 `0002_session_location.sql` 把它下沉到时段级别，
`courses.location` 保留为默认值。

---

## 6. 怎么在本地校验 Caddyfile（不必等部署到服务器才发现写错）

Caddy 有 Windows 构建，而 `caddy validate` 是纯配置检查，跨平台等价。
部署目标版本可以用 `caddy version` 问出来，本地下同一个版本即可对齐。

```powershell
# 1. 下 Caddy（用 Node，因为本机 Schannel 坏了，curl 走不通）
node -e "..."   # 见下方「下载脚本」；落盘到 .caddy-check/（已在 .gitignore）
# 2. 解压
Expand-Archive .caddy-check/caddy.zip -DestinationPath .caddy-check -Force

# 3. 生成自签证书 —— validate 会真的去加载 tls 指定的文件，缺了会失败
$rsa = [System.Security.Cryptography.RSA]::Create(2048)
$req = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
    "CN=test.local", $rsa,
    [System.Security.Cryptography.HashAlgorithmName]::SHA256,
    [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
$cert = $req.CreateSelfSigned([DateTimeOffset]::Now.AddDays(-1), [DateTimeOffset]::Now.AddDays(30))
Set-Content .caddy-check/certs/fullchain.pem -Value $cert.ExportCertificatePem() -Encoding ascii
Set-Content .caddy-check/certs/privkey.pem -Value $rsa.ExportPkcs8PrivateKeyPem() -Encoding ascii

# 4. 渲染模板后校验
.caddy-check/caddy.exe validate --config <渲染结果> --adapter caddyfile
```

### 三个实测踩到的坑

**① 文件名决定配置格式。** Caddy 只在文件名为 `Caddyfile` 或以 `.caddyfile`
结尾时才用 caddyfile adapter，其余一律当 JSON。所以校验 `mktemp` 出来的
`/tmp/tmp.XXXX` 必须显式加 `--adapter caddyfile`，否则会报：

```
config is not valid JSON: invalid character '#' looking for beginning of value
```

这个坑真的漏到生产部署里去过（`install.sh` 的"先校验后落盘"改动引入的）。

**② `caddy validate` 会加载证书文件。** 路径不存在就直接失败，
所以本地校验必须先造一对自签证书，否则测不出配置本身对不对。

**③ PowerShell 的 `Select-Object -First N` 会提前终止管道并杀掉原生命程**，
`$LASTEXITCODE` 随之失真。我因此一度误判"校验通过"，实际是 exit=1。
**校验命令必须完整消费输出**：

```powershell
& $caddy validate ... > out.log 2>&1
$code = $LASTEXITCODE      # 这样才可信
Get-Content out.log | Select-String "^Error"
```

**④ `output journal` 不是内置模块。** 标准 Caddy 构建里没有
`caddy.logging.writers.journal`（属第三方插件），写了会让 validate 直接失败。
用 `output stdout` —— Caddy 由 systemd 托管，stdout 就是 journald。

改 Caddyfile 模板后，**先在本地跑一遍 validate 再推**。这条已经写进
`tests/test_deploy.py` 的断言里（禁止非内置模块、必须带 `--adapter`），
但那只能挡住已知的坑，跑一遍真校验才挡得住未知的。

---

## 7. LLM 适配器：一次"每请求必 400"的教训

### 症状与真因

旧适配器打 `/chat/completions`，发的是命名工具选择
`{"type":"function","function":{"name":"submit_tasks"}}`，且**从不带
`thinking` 字段**。DeepSeek 的 chat 文档写着：

> `required` and named tool choices are **not supported in thinking mode**;
> the API returns a **400 error**. Disable thinking mode first to use them.

而 `thinking.type` 的**默认值是 `enabled`**。所以每个请求都是 400。
更糟的是旧代码的错误提示写成"该供应商可能不支持强制 tool_choice，
请换模型或供应商"——**把病因指错了**，照着它去换供应商只会白费功夫。

顺带两个被掩盖的问题：`temperature` 在 thinking 模式下**完全无效**
（配的 `temperature = 0` 一直是被静默忽略的），而思考 token 照价计费。

### 为什么没被测出来

`tests/` 里所有录入用例都走 `MockLLM`，**适配器的 payload 从来没有被断言过**。
`tests/test_llm.py` 现在逐字段断言实际发出的 JSON，就是补这个洞。

教训：把"协议报文长什么样"当成需要断言的事实，而不是实现细节。
凡是靠"应该没问题"的字段（默认值、可选开关），都要有一条用例钉住。

### 现在的约定

| 约定 | 原因 |
|---|---|
| 命名 `tool_choice` 与 thinking **不可共存** | 两边文档对 Responses 是否也 400 说法不一致，显式 `effort: "none"` / `type: "disabled"` 就不用赌 |
| 默认走 `/responses` | 工具调用是 `output[]` 的一等公民；`text.format` 原生支持 JSON 降级 |
| 工具与 `tool_choice` 在 Responses 里是**扁平**的 | 嵌套 `function` 是 chat 的形状，照抄会被拒 |
| `parallel_tool_calls` 在 Responses 里**被忽略** | 并行恒开，必须合并所有 `function_call`，只取第一个会丢数据 |
| 临时性失败才重试 | 401/400 再发一百次也一样；重试与降级是两套正交策略，不要串成 8 次请求 |
| 降级必须响亮 | 日志 + `llm_path` 列 + 确认页提示；悄悄降级会让"模型开始不按工具调用返回"这件事无声无息地变成常态 |

### 两处刻意的取舍

**降级用 `json_object` 而不是 `json_schema`。** `json_schema` 走约束解码，
要求 schema 里所有属性都进 `required`、且不支持 `type: ["string","null"]`
这种联合类型——我们这个 schema 两条都不满足，很可能直接被 400。
`json_object` 是两边文档都保证支持的，格式漂移交给
`app/services/normalize.py` 兜底。等有真实 key 时可以实测一下
`json_schema` 是否被接受，接受的话是一行切换。

**重试有 180 秒总预算。** `retry_count=3` + `timeout_seconds=60` 最坏是
4 分钟，手机端的快捷指令等不了。超预算就停手，并在日志里写明是预算砍掉的
（`llm.retry_budget_exhausted`），而不是供应商恢复了。

---

## 8. 用例给 bug 背书：两个"测过但还是坏的"接口

写 `docs/api.md` 时把 26 条路由逐条实跑了一遍（正常路径 + 每条错误分支 +
权限矩阵），挖出两个**测试全绿却一直坏着**的缺陷。教训比缺陷本身值钱。

### 8.1 `GET /api/courses` 的结构化课表被计数覆盖

```python
return {
    "courses": [ ...精心构造的课程数组... ],
    **course_service.counts(db),          # 也返回 "courses" 键 → 覆盖成整数
}
```

Python 的 `{**a, **b}` 后者胜，于是 `courses` 变成 `int`，结构化课表根本取不到。
`session_count` 同理。

**为什么没被发现**：`tests/test_timetable.py` 当时断言的是

```python
assert fetched["courses"] == 2      # 那个 2 正是计数
```

用例断言的值**恰好等于错误实现的产出**。这种 bug 最难自查——测试不是没覆盖，
而是把错误行为固化成了期望值。写法上的对策：断言**结构**（是数组还是整数、
元素长什么样），而不只是断言一个数字。

### 8.2 课表写进去就删不掉

```python
if not parsed:
    raise HTTPException(422, "课表为空；如需清空请显式提交空课程数组")
```

提交空数组**同样**落进这个分支——错误提示推荐的做法自己也被挡住了。网页上
把文本框清空再保存同样 422，用户看到那句自相矛盾的提示。

**对策**：错误信息里给出的补救动作，必须有一条用例真的去执行它。

### 8.3 现在的守卫

`tests/test_api_docs.py` 把 `docs/api.md §0.2` 的端点清单与权限矩阵钉成断言：
加端点、删端点、换鉴权依赖都会失败，逼着同步文档。它也已经抓到过一次真实
偏差——文档里把路径参数写成 `{id}`，而实现里是 `{item_id}` / `{key_id}`。

**核对接口时不要只读代码。** 这两个缺陷读代码都不显眼（一个是字典展开顺序，
一个是分支覆盖），实跑一遍就立刻现形。核对脚本值得花那个时间写。

---

## 9. 两份渲染器：`index.html` 的宏与 `tasks.js` 的 renderRow

任务行有两份渲染实现，因为首页是**服务端渲染 + 局部刷新**：

| 何时用 | 实现 |
|---|---|
| 首次打开页面 | `app/templates/index.html` 的 `task_row` / `task_notes` 宏 |
| 任何写操作之后（`refresh()`） | `app/static/js/tasks.js` 的 `renderRow()` / `notesBlock()` |

**它们必须一起改，而没有任何机制强制这一点。** 踩过的坑：备注标记
（`<span class="chip" title="...">备注</span>`）当初只加在服务端那一份上，
于是勾选一次完成 → `refresh()` → 备注**凭空消失**。刷新页面又回来，
所以看起来像"偶发"。

没有构建步骤就没法在 Jinja 与 JS 之间共享模板，所以退而求其次：

1. 两边都写了 `⚠️` 注释，指明对方的存在；
2. `tests/test_task_notes.py::test_both_renderers_emit_the_same_notes_markup`
   钉住关键结构标记（`task__notes` / `-peek` / `-open` / `-full` / `task__facts`），
   任何一边少了就红；
3. 同文件还钉住 `SOURCE_LABELS` 在两边的取值一致。

**加新的行内元素时，记得同时改两处。** 如果哪天这类漂移再犯第三次，
就该考虑让 `refresh()` 改为拉服务端渲染的片段（新增一个返回 HTML 的
端点），而不是继续维护两份。

### 顺带：备注为什么必须"看得见"

原来的做法是 `<span class="chip" title="备注内容">备注</span>` —— 悬停提示。
问题是**手机上不存在悬停**，而这个应用是明确要移动端可用的。备注又是视觉
模型从图片里抽出来的内容（"只做奇数题"这种），用户在别的地方看不到第二遍。

现在折叠时显示一行预览、展开后是全文 + 元信息，用纯 `<details>`：无 JS 也能
展开，触屏和键盘都能用。



