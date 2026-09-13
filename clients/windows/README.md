# ScheduleKit Windows 悬浮窗

无边框、始终置顶的常驻小窗，显示服务端「有序表」（有截止时间）的前几条任务，
可以直接勾选完成。

## 为什么是 PowerShell + WinForms

| 方案 | 安装成本 | 内存 | 结论 |
|---|---|---|---|
| **PowerShell + WinForms** | **零** —— Windows 自带 .NET | ~55MB | ✅ 选它 |
| Python + PySide6 | 需装 Qt（约 150MB） | ~120MB | 为了一个悬浮窗不值得 |
| Electron | 需 Node + npm | 250MB+ | 太重 |
| Tauri | 需 Rust 工具链 + 编译 | ~40MB | 环境配置成本高于收益 |

代价是外观朴素（纯色无边框窗口），但配色、字号、透明度都可调。

## 安装

先在服务端 `/settings` 页面**创建一个 API Key**，然后：

```powershell
cd clients\windows
pwsh -File install-autostart.ps1
```

> ⚠️ **钥匙的权限别选错**：悬浮窗要能勾选任务，所以需要**读写**密钥。
> **只读**密钥留给未来的 Scriptable 小组件（v2）——用只读密钥装悬浮窗，
> 界面能显示任务，但一点勾选就会 403。

脚本会依次询问服务端地址与 API Key（隐藏输入），然后：

1. 写入 `%APPDATA%\ScheduleKit\config.json`
2. 用 **DPAPI** 加密保存 API Key 到 `%APPDATA%\ScheduleKit\credential.txt`
3. 在**启动文件夹**创建快捷方式（登录即自动运行，无需管理员权限）
4. 跑一次自检验证整条链路

## 配置文件

`%APPDATA%\ScheduleKit\config.json`（键名与服务端 `config.toml` 的 `[server]` 刻意对齐）：

```json
{
  "server":       { "public_url": "https://schedulekit.duckdns.org:8443" },
  "poll_seconds": 60,
  "max_items":    5,
  "opacity":      0.92,
  "width":        340,
  "x":            null,
  "y":            null,
  "open_url":     true
}
```

- `x` / `y` 为 `null` 时自动放到工作区右上角；拖动后会被记住
- 窗口跑到屏幕外了：`pwsh -File ScheduleKitFloat.ps1 -Reset`

**API Key 不在这个文件里**。它由 DPAPI 加密后单独存放，且**绑定当前 Windows 用户**——
把 `credential.txt` 拷到别的机器或别的用户下都解不开，这是刻意的。

## 操作

| 操作 | 效果 |
|---|---|
| 拖动标题栏 | 移动窗口（位置自动记住） |
| 双击标题栏 | 打开 WebUI |
| 勾选复选框 | 标记完成 |
| 右键 | 立即刷新 / 打开 WebUI / 暂停轮询 / 退出 |
| ✕ | 退出 |

## 排障

### 先跑自检

```powershell
pwsh -File ScheduleKitFloat.ps1 -SelfTest
```

它会逐项检查 WinForms 加载、密钥读取、接口连通、窗口构造、日志写入，
并直接打印失败原因。

### 日志

`%APPDATA%\ScheduleKit\float.log`（超过 256KB 会自动重置）。

### `SSL 连接失败` / `schannel: AcquireCredentialsHandle failed`

**这是 Windows Schannel 的问题，不是网络问题。** 判断方法：

```powershell
curl.exe -s -o NUL -w "%{http_code}" https://你的域名:8443/healthz
```

如果 `curl` 也失败而浏览器能打开，那就是 Schannel 凭据异常（常见于受限运行环境）。
`Invoke-RestMethod` 与 `curl.exe` 都依赖 Schannel，浏览器不依赖，所以会出现
"浏览器能开、脚本不行"的现象。

**应急方案**：用 Node（走自带 OpenSSL，不依赖 Schannel）验证连通性：

```powershell
node -e "fetch('https://你的域名:8443/healthz').then(r=>r.text()).then(console.log)"
```

如果 Node 能通，说明服务端正常，问题在本机 Schannel。这种情况下的退路是
让悬浮窗改由 `node` 子进程发起请求（改动集中在 `Invoke-SkApi` 一个函数里）。

### `401` / 密钥无效

API Key 被吊销或复制错了。去服务端 `/settings` 重新创建一个，然后重跑
`install-autostart.ps1`。

### 开机没自动启动

确认启动文件夹里有快捷方式：

```powershell
explorer "$([Environment]::GetFolderPath('Startup'))"
```

如果杀软拦截了 `WScript.Shell` 创建快捷方式，手动把
`ScheduleKitFloat.ps1` 的快捷方式拖进去即可，参数照抄脚本里生成的那一行。

## 取消自启 / 卸载

```powershell
# 只取消自启
Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'ScheduleKit 悬浮窗.lnk')

# 彻底清理（含配置与密钥）
Remove-Item -Recurse "$env:APPDATA\ScheduleKit"
```
