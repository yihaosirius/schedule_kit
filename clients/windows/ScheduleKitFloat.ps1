<#
.SYNOPSIS
    ScheduleKit Windows 悬浮窗。

.DESCRIPTION
    无边框、始终置顶的常驻小窗，显示服务端"有序表"（有截止时间）的前若干条，
    可以直接勾选完成。

    为什么用 PowerShell + WinForms 而不是 Electron / Tauri：
      * 零安装 —— Windows 自带 .NET，不需要 Node、Rust 或任何包管理器
      * 内存约 55MB，而 Electron 起步 200MB+
      * 开机启动只需在启动目录放一个快捷方式

.PARAMETER SelfTest
    不显示窗口，只做一次完整的自检：加载 WinForms、读取配置、调用接口、
    构造窗口对象再释放。用于部署后验证，也用于在没有图形界面时排查。

.PARAMETER ApiKey
    配合 -SelfTest 临时指定密钥，跳过 DPAPI 读取（CI / 排障用）。

.PARAMETER ConfigPath
    覆盖配置文件路径，默认 %APPDATA%\ScheduleKit\config.json。

.PARAMETER Reset
    重置窗口位置到屏幕右上角（窗口跑到屏幕外时用）。

.PARAMETER StoreApiKey
    配合 -ApiKey 使用：只把密钥用 DPAPI 加密保存后退出，不显示窗口。
    由 install-autostart.ps1 调用，这样 DPAPI 的读写逻辑只存在一处。

.EXAMPLE
    pwsh -File ScheduleKitFloat.ps1
.EXAMPLE
    pwsh -File ScheduleKitFloat.ps1 -SelfTest
#>
#Requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$SelfTest,
    [string]$ApiKey,
    [string]$ConfigPath,
    [switch]$Reset,
    [switch]$StoreApiKey
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── 常量 ────────────────────────────────────────────────────────────
$script:AppDir = Join-Path $env:APPDATA 'ScheduleKit'
$script:DefaultConfigPath = Join-Path $script:AppDir 'config.json'
$script:CredentialPath = Join-Path $script:AppDir 'credential.txt'
$script:LogPath = Join-Path $script:AppDir 'float.log'

$script:DefaultConfig = [ordered]@{
    server       = [ordered]@{ public_url = 'http://127.0.0.1:8000' }
    poll_seconds = 60
    max_items    = 5
    opacity      = 0.92
    width        = 340
    # null 表示"首次运行时自动放到右上角"
    x            = $null
    y            = $null
    open_url     = $true
}

# ── 日志 ────────────────────────────────────────────────────────────
function Write-SkLog {
    param(
        [Parameter(Mandatory)][string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level = 'INFO'
    )
    $line = '{0} {1,-5} {2}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $Level, $Message
    if ($Level -ne 'INFO') { Write-Host $line } else { Write-Verbose $line }
    try {
        # 日志文件控制在 ~256KB，避免无限增长
        if ((Test-Path $script:LogPath) -and (Get-Item $script:LogPath).Length -gt 262144) {
            Remove-Item $script:LogPath -Force
        }
        Add-Content -Path $script:LogPath -Value $line -Encoding utf8
    } catch {
        # 日志写不了不能影响主流程
    }
}

# ── 配置 ────────────────────────────────────────────────────────────
function Get-SkConfigPath {
    if ($ConfigPath) { return $ConfigPath }
    return $script:DefaultConfigPath
}

function Read-SkConfig {
    $path = Get-SkConfigPath
    if (-not (Test-Path $path)) {
        Write-SkLog "找不到配置 $path，使用默认值（请先运行 install-autostart.ps1）" 'WARN'
        return $script:DefaultConfig
    }
    try {
        $raw = Get-Content -Path $path -Raw -Encoding utf8
        $parsed = $raw | ConvertFrom-Json -AsHashtable
    } catch {
        throw "配置文件 $path 解析失败：$($_.Exception.Message)"
    }

    # 用默认值补齐缺失字段，避免旧配置在升级后缺键
    $merged = [ordered]@{}
    foreach ($key in $script:DefaultConfig.Keys) {
        $merged[$key] = if ($parsed.ContainsKey($key)) { $parsed[$key] } else { $script:DefaultConfig[$key] }
    }
    if ($merged['server'] -isnot [hashtable] -or -not $merged['server']['public_url']) {
        throw "配置缺少 server.public_url"
    }
    return $merged
}

function Save-SkConfig {
    param([Parameter(Mandatory)][hashtable]$Config)
    $dir = Split-Path -Parent (Get-SkConfigPath)
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $Config | ConvertTo-Json -Depth 5 | Set-Content -Path (Get-SkConfigPath) -Encoding utf8
}

# ── 密钥（DPAPI，绑定当前 Windows 用户）─────────────────────────────
function Set-SkApiKey {
    param([Parameter(Mandatory)][string]$Key)
    $dir = Split-Path -Parent $script:CredentialPath
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    try {
        $secure = ConvertTo-SecureString -String $Key -AsPlainText -Force
        # 不带 -Key 时走 DPAPI：换用户或换机器都解不开
        $encrypted = ConvertFrom-SecureString -SecureString $secure
    } catch {
        throw "无法用 DPAPI 保护密钥（$($_.Exception.Message)）。这是 Windows 用户级加密，通常在受限环境下才会失败。"
    }
    Set-Content -Path $script:CredentialPath -Value $encrypted -Encoding ascii
    Write-SkLog "密钥已用 DPAPI 保存到 $script:CredentialPath"
}

function Get-SkApiKey {
    if ($ApiKey) { return $ApiKey }
    if (-not (Test-Path $script:CredentialPath)) {
        throw "尚未保存 API Key。请运行 install-autostart.ps1 完成初次配置。"
    }
    try {
        $encrypted = Get-Content -Path $script:CredentialPath -Raw
        $secure = ConvertTo-SecureString -String $encrypted.Trim()
        return [System.Net.NetworkCredential]::new('', $secure).Password
    } catch {
        throw "无法解出 API Key（$($_.Exception.Message)）。若换了 Windows 用户或机器，请重新运行 install-autostart.ps1。"
    }
}

# ── HTTP ────────────────────────────────────────────────────────────
function Invoke-SkApi {
    <#
      所有请求走这一个出口，便于统一处理鉴权头与错误。
      注意：Invoke-RestMethod 在 Windows 上依赖 Schannel。若本机 Schannel 异常
      （例如受限沙箱），HTTPS 会失败且错误信息很难懂 —— 这时改用 http 或
      参考 clients/windows/README.md 的排障一节。
    #>
    param(
        [Parameter(Mandatory)][ValidateSet('GET', 'POST', 'PATCH', 'DELETE')][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        $Body,
        [Parameter(Mandatory)][hashtable]$Config
    )

    $base = $Config['server']['public_url'].TrimEnd('/')
    $uri = "$base$Path"
    $headers = @{ 'X-API-Key' = (Get-SkApiKey); 'Accept' = 'application/json' }

    $params = @{ Method = $Method; Uri = $uri; Headers = $headers; TimeoutSec = 20 }
    if ($null -ne $Body) {
        $params['Body'] = ($Body | ConvertTo-Json -Depth 5 -Compress)
        $params['ContentType'] = 'application/json; charset=utf-8'
    }

    try {
        return Invoke-RestMethod @params
    } catch {
        $detail = $_.Exception.Message
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $detail = $_.ErrorDetails.Message }
        Write-SkLog "请求失败 $Method $Path -> $detail" 'ERROR'
        throw
    }
}

function Get-SkTasks {
    param([Parameter(Mandatory)][hashtable]$Config)
    $limit = [int]$Config['max_items']
    return Invoke-SkApi -Method GET -Path "/api/tasks?view=ordered&status=open&limit=$limit" -Config $Config
}

function Set-SkTaskDone {
    param(
        [Parameter(Mandatory)][int]$Id,
        [Parameter(Mandatory)][hashtable]$Config
    )
    return Invoke-SkApi -Method PATCH -Path "/api/tasks/$Id" -Body @{ status = 'done' } -Config $Config
}

# ── 界面 ────────────────────────────────────────────────────────────
function New-SkWindow {
    param([Parameter(Mandatory)][hashtable]$Config)

    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing

    $width = [int]$Config['width']

    $form = New-Object System.Windows.Forms.Form
    $form.FormBorderStyle = 'None'
    $form.TopMost = $true
    $form.ShowInTaskbar = $false
    $form.StartPosition = 'Manual'
    $form.ClientSize = New-Object System.Drawing.Size($width, 120)
    $form.BackColor = [System.Drawing.Color]::FromArgb(28, 31, 36)
    $form.Opacity = [double]$Config['opacity']
    $form.Text = 'ScheduleKit'
    $form.AutoScaleMode = 'Dpi'

    # 首次运行放到工作区右上角
    $work = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
    $form.Location = if ($null -ne $Config['x'] -and $null -ne $Config['y']) {
        New-Object System.Drawing.Point([int]$Config['x'], [int]$Config['y'])
    } else {
        New-Object System.Drawing.Point(($work.Right - $width - 24), ($work.Top + 24))
    }

    # 标题栏：整条都是拖动把手
    $header = New-Object System.Windows.Forms.Panel
    $header.Dock = 'Top'
    $header.Height = 30
    $header.BackColor = [System.Drawing.Color]::FromArgb(20, 22, 26)

    $title = New-Object System.Windows.Forms.Label
    $title.Text = 'ScheduleKit'
    $title.ForeColor = [System.Drawing.Color]::FromArgb(200, 205, 212)
    $title.Font = New-Object System.Drawing.Font('Segoe UI', 9, [System.Drawing.FontStyle]::Bold)
    $title.AutoSize = $true
    $title.Location = New-Object System.Drawing.Point(10, 7)

    $close = New-Object System.Windows.Forms.Button
    $close.Text = '✕'
    $close.FlatStyle = 'Flat'
    $close.FlatAppearance.BorderSize = 0
    $close.ForeColor = [System.Drawing.Color]::FromArgb(150, 155, 162)
    $close.BackColor = [System.Drawing.Color]::FromArgb(20, 22, 26)
    $close.Size = New-Object System.Drawing.Size(30, 30)
    $close.Anchor = 'Top,Right'
    $close.Location = New-Object System.Drawing.Point(($width - 30), 0)
    $close.Add_Click({ $form.Close() })

    $header.Controls.Add($title)
    $header.Controls.Add($close)

    # 任务区
    $list = New-Object System.Windows.Forms.FlowLayoutPanel
    $list.Dock = 'Fill'
    $list.FlowDirection = 'TopDown'
    $list.WrapContents = $false
    $list.AutoScroll = $true
    $list.Padding = New-Object System.Windows.Forms.Padding(6, 6, 6, 6)
    $list.BackColor = [System.Drawing.Color]::FromArgb(28, 31, 36)

    $status = New-Object System.Windows.Forms.Label
    $status.Dock = 'Bottom'
    $status.Height = 20
    $status.ForeColor = [System.Drawing.Color]::FromArgb(120, 128, 138)
    $status.Font = New-Object System.Drawing.Font('Segoe UI', 7.5)
    $status.TextAlign = 'MiddleLeft'
    $status.Padding = New-Object System.Windows.Forms.Padding(8, 0, 8, 2)
    $status.Text = '正在载入…'

    $form.Controls.Add($list)
    $form.Controls.Add($status)
    $form.Controls.Add($header)

    # 拖动：记录按下时的偏移，MouseMove 里平移窗口。
    # 不用 P/Invoke ReleaseCapture+SendMessage，纯 .NET 更稳且无平台调用风险。
    $script:dragging = $false
    $script:dragOffset = New-Object System.Drawing.Point(0, 0)
    $onMouseDown = {
        param($sender, $e)
        if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
            $script:dragging = $true
            $script:dragOffset = $e.Location
        }
    }
    $onMouseMove = {
        param($sender, $e)
        if ($script:dragging) {
            $screenPos = $sender.PointToScreen($e.Location)
            $form.Location = New-Object System.Drawing.Point(
                ($screenPos.X - $script:dragOffset.X),
                ($screenPos.Y - $script:dragOffset.Y)
            )
        }
    }
    $onMouseUp = { param($sender, $e) $script:dragging = $false }

    foreach ($control in @($header, $title)) {
        $control.Add_MouseDown($onMouseDown)
        $control.Add_MouseMove($onMouseMove)
        $control.Add_MouseUp($onMouseUp)
    }

    # 双击标题栏打开 WebUI
    $title.Add_DoubleClick({
        if ($Config['open_url']) {
            Start-Process $Config['server']['public_url'].TrimEnd('/')
        }
    })

    # 右键菜单
    $menu = New-Object System.Windows.Forms.ContextMenuStrip
    $menu.Items.Add('立即刷新', $null, { $script:refreshRequested = $true }) | Out-Null
    $menu.Items.Add('打开 WebUI', $null, {
        Start-Process $Config['server']['public_url'].TrimEnd('/')
    }) | Out-Null
    $pausedItem = New-Object System.Windows.Forms.ToolStripMenuItem('暂停轮询')
    $pausedItem.CheckOnClick = $true
    $menu.Items.Add($pausedItem) | Out-Null
    $menu.Items.Add('-') | Out-Null
    $menu.Items.Add('退出', $null, { $form.Close() }) | Out-Null
    $form.ContextMenuStrip = $menu

    return [pscustomobject]@{
        Form   = $form
        List   = $list
        Status = $status
        Menu   = $menu
        Paused = $pausedItem
    }
}

function Update-SkTaskList {
    param(
        [Parameter(Mandatory)]$Ui,
        [Parameter(Mandatory)][hashtable]$Config
    )

    $list = $Ui.List
    $list.SuspendLayout()
    try {
        $list.Controls.Clear()

        $tasks = @(Get-SkTasks -Config $Config)
        if ($tasks.Count -eq 0) {
            $empty = New-Object System.Windows.Forms.Label
            $empty.Text = '没有带截止时间的任务'
            $empty.ForeColor = [System.Drawing.Color]::FromArgb(120, 128, 138)
            $empty.Font = New-Object System.Drawing.Font('Segoe UI', 9)
            $empty.AutoSize = $true
            $empty.Margin = New-Object System.Windows.Forms.Padding(4, 8, 0, 0)
            $list.Controls.Add($empty) | Out-Null
        }

        foreach ($task in $tasks) {
            $row = New-Object System.Windows.Forms.Panel
            $row.Width = $list.ClientSize.Width - 18
            $row.Height = 34
            $row.Margin = New-Object System.Windows.Forms.Padding(0, 0, 0, 4)

            $check = New-Object System.Windows.Forms.CheckBox
            $check.Width = 16
            $check.Location = New-Object System.Drawing.Point(2, 9)
            $check.Tag = $task.id
            $check.Add_CheckedChanged({
                param($sender, $e)
                if (-not $sender.Checked) { return }
                $sender.Enabled = $false
                try {
                    Set-SkTaskDone -Id ([int]$sender.Tag) -Config $Config | Out-Null
                    Write-SkLog "任务 $($sender.Tag) 已标记完成"
                    $script:refreshRequested = $true
                } catch {
                    $sender.Checked = $false
                    $sender.Enabled = $true
                    $Ui.Status.Text = '勾选失败，详见 float.log'
                }
            })

            $text = New-Object System.Windows.Forms.Label
            $text.Text = [string]$task.title
            $text.ForeColor = [System.Drawing.Color]::FromArgb(232, 234, 237)
            $text.Font = New-Object System.Drawing.Font('Segoe UI', 9.5)
            $text.AutoEllipsis = $true
            $text.Location = New-Object System.Drawing.Point(22, 5)
            $text.Size = New-Object System.Drawing.Size(($row.Width - 26), 16)

            $due = New-Object System.Windows.Forms.Label
            $due.Text = Format-SkDue -Iso $task.due_at
            $due.ForeColor = Get-SkDueColor -Iso $task.due_at
            $due.Font = New-Object System.Drawing.Font('Segoe UI', 7.5)
            $due.Location = New-Object System.Drawing.Point(22, 20)
            $due.AutoSize = $true

            $row.Controls.Add($check)
            $row.Controls.Add($text)
            $row.Controls.Add($due)
            $list.Controls.Add($row) | Out-Null
        }
    } finally {
        $list.ResumeLayout()
    }

    $Ui.Status.Text = '更新于 {0} · {1} 条' -f (Get-Date -Format 'HH:mm:ss'), $tasks.Count
}

function Format-SkDue {
    param([string]$Iso)
    if (-not $Iso) { return '' }
    try {
        $due = [datetimeoffset]::Parse($Iso).LocalDateTime
    } catch {
        return ''
    }
    $delta = $due - (Get-Date)
    if ($delta.TotalSeconds -lt 0) {
        $overdue = [math]::Abs($delta.TotalSeconds)
        if ($overdue -lt 3600) { return ('已逾期 {0} 分钟' -f [int]($overdue / 60)) }
        if ($overdue -lt 86400) { return ('已逾期 {0} 小时' -f [int]($overdue / 3600)) }
        return ('已逾期 {0} 天' -f [int]($overdue / 86400))
    }
    if ($delta.TotalMinutes -lt 60) { return ('{0} 分钟后 · {1:MM-dd HH:mm}' -f [int]$delta.TotalMinutes, $due) }
    if ($delta.TotalHours -lt 24) { return ('{0} 小时后 · {1:MM-dd HH:mm}' -f [int]$delta.TotalHours, $due) }
    return ('{0} 天后 · {1:MM-dd HH:mm}' -f [int]$delta.TotalDays, $due)
}

function Get-SkDueColor {
    param([string]$Iso)
    if (-not $Iso) { return [System.Drawing.Color]::FromArgb(120, 128, 138) }
    try { $due = [datetimeoffset]::Parse($Iso).LocalDateTime } catch { return [System.Drawing.Color]::FromArgb(120, 128, 138) }
    $delta = $due - (Get-Date)
    if ($delta.TotalSeconds -lt 0) { return [System.Drawing.Color]::FromArgb(220, 38, 38) }
    if ($delta.TotalHours -lt 24) { return [System.Drawing.Color]::FromArgb(234, 88, 12) }
    return [System.Drawing.Color]::FromArgb(150, 158, 168)
}

# ── 自检 ────────────────────────────────────────────────────────────
function Invoke-SkSelfTest {
    param([Parameter(Mandatory)][hashtable]$Config)

    $ok = $true
    Write-Host '== ScheduleKit 悬浮窗自检 =='

    try {
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        Write-Host '  [1/5] WinForms 程序集加载         OK'
    } catch {
        Write-Host "  [1/5] WinForms 程序集加载         失败：$($_.Exception.Message)"
        return $false
    }

    try {
        $key = Get-SkApiKey
        Write-Host ('  [2/5] API Key 读取               OK（{0}…，长度 {1}）' -f $key.Substring(0, [Math]::Min(6, $key.Length)), $key.Length)
    } catch {
        Write-Host "  [2/5] API Key 读取               失败：$($_.Exception.Message)"
        return $false
    }

    try {
        $tasks = @(Get-SkTasks -Config $Config)
        Write-Host ('  [3/5] 接口连通（{0}）    OK，返回 {1} 条' -f $Config['server']['public_url'], $tasks.Count)
        foreach ($task in $tasks) {
            Write-Host ('          · {0}  [{1}]' -f $task.title, (Format-SkDue -Iso $task.due_at))
        }
    } catch {
        Write-Host "  [3/5] 接口连通                    失败：$($_.Exception.Message)"
        if ($_.Exception.Message -match 'SSL|schannel|凭据') {
            Write-Host '         提示：本机 Schannel 可能不可用，见 README 排障一节。'
        }
        $ok = $false
    }

    try {
        $ui = New-SkWindow -Config $Config
        $ui.Form.Dispose()
        Write-Host '  [4/5] 窗口构造与释放             OK'
    } catch {
        Write-Host "  [4/5] 窗口构造与释放             失败：$($_.Exception.Message)"
        $ok = $false
    }

    try {
        Write-SkLog '自检完成'
        Write-Host "  [5/5] 日志写入                   OK（$script:LogPath）"
    } catch {
        Write-Host "  [5/5] 日志写入                   失败：$($_.Exception.Message)"
        $ok = $false
    }

    Write-Host ''
    Write-Host $(if ($ok) { '自检通过。' } else { '自检未全部通过，请看上面标"失败"的项。' })
    return $ok
}

# ── 主流程 ──────────────────────────────────────────────────────────
function Start-SkFloat {
    param([Parameter(Mandatory)][hashtable]$Config)

    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $ui = New-SkWindow -Config $Config

    $script:refreshRequested = $true
    $script:lastRefresh = [datetime]::MinValue
    $interval = [int]$Config['poll_seconds']

    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = 1000  # 每秒检查一次是否到点，便于立即响应"手动刷新"
    $timer.Add_Tick({
        $due = ((Get-Date) - $script:lastRefresh).TotalSeconds -ge $interval
        if ($ui.Paused.Checked -and -not $script:refreshRequested) { return }
        if (-not ($due -or $script:refreshRequested)) { return }
        $script:refreshRequested = $false
        $script:lastRefresh = Get-Date
        try {
            Update-SkTaskList -Ui $ui -Config $Config
        } catch {
            $ui.Status.Text = '刷新失败：{0}' -f $_.Exception.Message
        }
    })

    $form = $ui.Form
    $form.Add_Shown({
        # 记住窗口位置，下次原地打开
        try {
            Update-SkTaskList -Ui $ui -Config $Config
            $script:lastRefresh = Get-Date
        } catch {
            $ui.Status.Text = '首次载入失败：{0}' -f $_.Exception.Message
        }
        $timer.Start()
    })

    $form.Add_FormClosing({
        $timer.Stop()
        try {
            $Config['x'] = $form.Location.X
            $Config['y'] = $form.Location.Y
            Save-SkConfig -Config $Config
        } catch {
            Write-SkLog "保存窗口位置失败：$($_.Exception.Message)" 'WARN'
        }
    })

    Write-SkLog "悬浮窗启动，轮询间隔 ${interval}s"
    [void]$form.ShowDialog()
    Write-SkLog '悬浮窗已退出'
}

# ── 入口 ────────────────────────────────────────────────────────────
try {
    $config = Read-SkConfig

    if ($StoreApiKey) {
        if (-not $ApiKey) { throw '-StoreApiKey 需要同时提供 -ApiKey' }
        Set-SkApiKey -Key $ApiKey
        Write-Host "API Key 已加密保存到 $script:CredentialPath"
        exit 0
    }

    if ($Reset) {
        $config['x'] = $null
        $config['y'] = $null
        Save-SkConfig -Config $config
        Write-Host '窗口位置已重置。'
    }

    if ($SelfTest) {
        if (Invoke-SkSelfTest -Config $config) { exit 0 } else { exit 1 }
    }

    Start-SkFloat -Config $config
} catch {
    Write-SkLog "致命错误：$($_.Exception.Message)" 'ERROR'
    Write-Host "ScheduleKit 悬浮窗启动失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "详细日志：$script:LogPath"
    exit 1
}
