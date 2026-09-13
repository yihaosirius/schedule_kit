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
2026-03-20T14:32:02.980 INFO  sk.llm           [t=8f3a2b1c] llm.request provider=openai_compat model=deepseek-vl2 images=1 system_chars=412 user_chars=210
2026-03-20T14:32:05.789 INFO  sk.llm           [t=8f3a2b1c] llm.response elapsed_ms=2809 tool_call=submit_tasks items=2 raw_chars=420
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

