<#
.SYNOPSIS
    ScheduleKit 悬浮窗的初次配置与开机自启安装。

.DESCRIPTION
    做四件事：
      1. 写入 %APPDATA%\ScheduleKit\config.json（服务端地址与轮询参数）
      2. 把 API Key 交给悬浮窗脚本，用 DPAPI 加密保存
      3. 在启动目录创建快捷方式，实现开机自启
      4. 跑一次 -SelfTest 验证整条链路

    幂等：重复运行只会覆盖配置与快捷方式，不会重复添加启动项。

.PARAMETER ServerUrl
    服务端地址，如 https://schedulekit.duckdns.org:8443。不传则交互询问。

.PARAMETER ApiKey
    API Key（服务端 /settings 页面创建）。不传则交互询问（隐藏输入）。

.PARAMETER PollSeconds
    轮询间隔秒数，默认 60。

.PARAMETER SkipSelfTest
    跳过安装后的自检。

.EXAMPLE
    pwsh -File install-autostart.ps1
.EXAMPLE
    pwsh -File install-autostart.ps1 -ServerUrl https://x.duckdns.org:8443 -ApiKey sk_xxx
#>
#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$ServerUrl,
    [string]$ApiKey,
    [int]$PollSeconds = 60,
    [switch]$SkipSelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:AppDir = Join-Path $env:APPDATA 'ScheduleKit'
$script:ConfigPath = Join-Path $script:AppDir 'config.json'
$script:FloatScript = Join-Path $PSScriptRoot 'ScheduleKitFloat.ps1'
$script:StartupDir = [Environment]::GetFolderPath('Startup')
$script:ShortcutPath = Join-Path $script:StartupDir 'ScheduleKit 悬浮窗.lnk'

function Write-Step { param([string]$Text) Write-Host "==> $Text" -ForegroundColor Green }
function Write-Note { param([string]$Text) Write-Host "    $Text" -ForegroundColor DarkGray }
function Write-Warn { param([string]$Text) Write-Host "[!] $Text" -ForegroundColor Yellow }

if (-not (Test-Path $script:FloatScript)) {
    throw "找不到 $script:FloatScript —— 请在 clients\windows 目录下运行本脚本"
}

if (-not (Test-Path $script:AppDir)) {
    New-Item -ItemType Directory -Path $script:AppDir -Force | Out-Null
}

# ── 1. 服务端地址 ───────────────────────────────────────────────────
Write-Step '配置服务端地址'
if (-not $ServerUrl) {
    if (Test-Path $script:ConfigPath) {
        $existing = Get-Content $script:ConfigPath -Raw | ConvertFrom-Json
        $ServerUrl = $existing.server.public_url
    }
}
if (-not $ServerUrl) {
    $ServerUrl = Read-Host '    服务端地址（如 https://schedulekit.duckdns.org:8443）'
}
if (-not $ServerUrl) { throw '服务端地址不能为空' }
$ServerUrl = $ServerUrl.TrimEnd('/')

if ($ServerUrl -notmatch '^https?://') {
    Write-Warn "地址不像 URL（应以 http:// 或 https:// 开头）：$ServerUrl"
}
if ($ServerUrl -match '^http://' -and $ServerUrl -notmatch '127\.0\.0\.1|localhost') {
    Write-Warn '这是明文 HTTP 地址：任务内容与 API Key 会以明文经过网络。建议改用 HTTPS。'
}

# ── 2. 写入 config.json ─────────────────────────────────────────────
Write-Step "写入 $script:ConfigPath"
$config = [ordered]@{
    server       = [ordered]@{ public_url = $ServerUrl }
    poll_seconds = $PollSeconds
    max_items    = 5
    opacity      = 0.92
    width        = 340
    x            = $null
    y            = $null
    open_url     = $true
}
# 保留已有的窗口位置，避免每次安装都把窗口弹回右上角
if (Test-Path $script:ConfigPath) {
    try {
        $old = Get-Content $script:ConfigPath -Raw | ConvertFrom-Json
        if ($null -ne $old.x) { $config['x'] = $old.x }
        if ($null -ne $old.y) { $config['y'] = $old.y }
        if ($old.PSObject.Properties.Name -contains 'width') { $config['width'] = $old.width }
    } catch {
        Write-Note '旧配置无法解析，忽略其中的窗口位置'
    }
}
$config | ConvertTo-Json -Depth 4 | Set-Content -Path $script:ConfigPath -Encoding utf8
Write-Note "服务端：$ServerUrl"
Write-Note "轮询间隔：${PollSeconds}s"

# ── 3. 保存 API Key（DPAPI）──────────────────────────────────────────
Write-Step '保存 API Key'
if (-not $ApiKey) {
    $secure = Read-Host '    API Key（服务端 /settings 页面创建，输入时不显示）' -AsSecureString
    $ApiKey = [System.Net.NetworkCredential]::new('', $secure).Password
}
if (-not $ApiKey) { throw 'API Key 不能为空' }

& $script:FloatScript -StoreApiKey -ApiKey $ApiKey -ConfigPath $script:ConfigPath
Write-Note '已用 DPAPI 加密，绑定当前 Windows 用户；换用户或换机器需重新安装'

# ── 4. 开机自启快捷方式 ─────────────────────────────────────────────
Write-Step '创建开机自启快捷方式'
$pwshPath = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $pwshPath) {
    throw '找不到 pwsh（PowerShell 7+）。请先安装 PowerShell 7，或改用 Windows PowerShell 5.1 并自行调整快捷方式。'
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($script:ShortcutPath)
$shortcut.TargetPath = $pwshPath
# -WindowStyle Hidden 避免启动时闪一个控制台窗口
$shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script:FloatScript`" -ConfigPath `"$script:ConfigPath`""
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = 'ScheduleKit 悬浮窗'
$shortcut.Save()
Write-Note "快捷方式：$script:ShortcutPath"
Write-Note '（放在「启动」文件夹里，登录时自动运行，不需要管理员权限）'

# ── 5. 自检 ─────────────────────────────────────────────────────────
if (-not $SkipSelfTest) {
    Write-Host ''
    Write-Step '运行自检'
    & $script:FloatScript -SelfTest -ConfigPath $script:ConfigPath
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Warn '自检未通过。常见原因：'
        Write-Note '· 服务端地址写错或服务未启动'
        Write-Note '· API Key 无效或已被吊销（在服务端 /settings 重新创建）'
        Write-Note '· 本机 Schannel 异常导致 HTTPS 失败（见 README 排障）'
        Write-Note '修好后重新运行本脚本，或直接跑：'
        Write-Note "  pwsh -File `"$script:FloatScript`" -SelfTest"
        exit 1
    }
}

Write-Host ''
Write-Host '安装完成。' -ForegroundColor Green
Write-Host '  立即启动：双击桌面上的快捷方式，或运行：'
Write-Host "    pwsh -File `"$script:FloatScript`""
Write-Host '  取消自启：删除启动文件夹里的「ScheduleKit 悬浮窗.lnk」，或运行：'
Write-Host "    Remove-Item `"$script:ShortcutPath`""
