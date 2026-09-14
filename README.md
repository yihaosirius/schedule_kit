# ScheduleKit

个人任务管理系统。远端 Ubuntu 服务端 + 双列表 WebUI + Windows 常驻悬浮窗 + iPhone 快捷指令拍照录入。

完整设计见 [`PLAN.md`](PLAN.md)，开发约定与踩坑记录见 [`docs/dev-notes.md`](docs/dev-notes.md)。

## 核心特征

- **三栏视图**：有 deadline 的进有序表（按时间升序），无 deadline 的进无序表（按 Ⅰ–Ⅴ 排序），已完成的进侧栏「已完成」（按完成时间倒序）。无序与已完成 UI 权重更小，移动端默认折叠。
- **两阶段录入**：图片/文本 → LLM 以强制 function calling 返回结构化草稿 → 人工确认/修改后才入库。**草稿阶段任务表零写入**。模型没按工具调用返回时自动降级到 JSON 一次，并在确认页标明。
- **课表上下文**：注入「现在正在上/刚结束/即将开始哪门课」，让「下节课交」这类相对指代能被解析成具体日期。
- **单一配置文件**：全服务器只有 `/etc/schedulekit/config.toml`，控制台可改且**保留注释**。
- **低内存**：uvicorn + Caddy 合计约 100–140MB。

## 文档索引

| 文档 | 内容 |
|---|---|
| [`PLAN.md`](PLAN.md) | 完整设计：数据模型、接口、部署、测试、里程碑 |
| [`docs/api.md`](docs/api.md) | HTTP 接口规格：19 个端点逐条实跑核对，含错误码与权限矩阵（快捷指令 / agent 对接看这个） |
| [`docs/shortcuts.md`](docs/shortcuts.md) | Apple 快捷指令逐步搭建 + 四个必踩的坑 |
| [`docs/widget-v2.md`](docs/widget-v2.md) | Scriptable 小组件设计（v2） |
| [`docs/dev-notes.md`](docs/dev-notes.md) | trace 日志约定、开发环境坑、数据集不变量 |
| [`deploy/README.md`](deploy/README.md) | 部署步骤与排障 |
| [`clients/windows/README.md`](clients/windows/README.md) | 悬浮窗安装与排障 |
| [`clients/ios/README.md`](clients/ios/README.md) | iPhone 小组件安装与排障（Scriptable，无需 Mac） |

## 本地开发

需要 [uv](https://docs.astral.sh/uv/)（本机已安装）。

```bash
cp config.toml.example config.toml
uv sync
uv run python -m app.cli init          # 生成 secret_key、设置管理员密码
uv run python -m app.serve --reload    # 启动，默认 127.0.0.1:8000
```

打开 http://127.0.0.1:8000 用刚设的密码登录。

运行测试：

```bash
uv run pytest -q -p no:cacheprovider
```

> `-p no:cacheprovider` 在受限沙箱里是必需的（`.pytest_cache` 建不出来）。
> 普通终端下可以省略。

## CLI

| 命令 | 作用 |
|---|---|
| `python -m app.cli init` | 生成 `secret_key`、交互式设置管理员密码、建库 |
| `python -m app.cli set-password` | 修改密码（`session_epoch` 自增，旧会话全部失效） |
| `python -m app.cli migrate` | 应用数据库迁移 |
| `python -m app.cli show --json` | 输出配置 JSON（密钥已脱敏），供部署脚本渲染 Caddyfile |
| `python -m app.cli show --json --secrets` | 同上但含密钥，**仅供 root 部署脚本使用** |
| `python -m app.cli new-key --name X` | 创建 API Key（`--read-only` 建只读密钥） |
| `python -m app.cli backup` | 用 sqlite3 backup API 备份数据库 |

## 部署

一台只有公网 IP、不开 80/443 的 Ubuntu 服务器：

```bash
sudo bash deploy/install.sh --init   # 交互式生成 config.toml 并部署
sudo bash deploy/install.sh          # 改完配置后幂等重部署
```

细节与排障见 [`deploy/README.md`](deploy/README.md)。

## 目录结构

```
app/                服务端（唯一 Python 包）
  routers/          只做鉴权与参数校验
  services/         业务规则，可脱离 HTTP 单测
  llm/              统一适配器（Responses API + 强制 function calling + JSON 降级）
  templates/ static/  无构建步骤的前端
clients/windows/    PowerShell + WinForms 悬浮窗
clients/ios/        Scriptable 小组件脚本 + Node 预览器
deploy/             install.sh / Caddyfile 模板 / systemd 单元
docs/               接口、快捷指令、小组件、开发笔记
tests/              pytest（全程不访问网络）
data/               运行时生成，不入库
```

## 实现状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 骨架、统一配置、迁移、CLI、trace 日志 | ✅ |
| M1 | 鉴权（签名 Cookie / CSRF / API Key / 限流） | ✅ |
| M2 | 任务 CRUD、双视图排序、二选一约束 | ✅ |
| M3 | 双列表 WebUI、移动端适配 | ✅ |
| M4 | LLM function calling、两阶段录入、确认页 | ✅ |
| M4.5 | 课表解析、时间上下文注入 | ✅ |
| M5 | 控制台（LLM 热加载、密钥管理、运行状态） | ✅ |
| M6 | 部署产物（install.sh / Caddy / acme.sh / systemd） | ✅ |
| M7 | Windows 悬浮窗 + 开机自启 | ✅ |
| M8 | PWA 外壳 + 文档 | ✅ |
| v2-a | iPhone 小组件（Scriptable，桌面 + 锁屏） | ✅ |
| v2-b | ntfy 推送、课表截图识别、MCP server | 未开始 |
