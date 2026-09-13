"""M7：Windows 悬浮窗脚本的结构与安全校验。

功能本身已经对着真实服务端跑过 ``-SelfTest``（WinForms 加载 → DPAPI 读密钥 →
HTTP 取任务 → 窗口构造释放 → 日志写入，全通过），这里补的是**静态约束**：
几条容易在后续改动中被无意破坏的安全与可用性约定。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLIENT_DIR = PROJECT_ROOT / "clients" / "windows"

FLOAT_PS1 = CLIENT_DIR / "ScheduleKitFloat.ps1"
INSTALL_PS1 = CLIENT_DIR / "install-autostart.ps1"
CLIENT_README = CLIENT_DIR / "README.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_code(path: Path) -> str:
    """剥掉注释后的脚本文本。

    断言"脚本里不该出现某某写法"时必须用这个：解释"为什么不用 X"的注释
    本身就含有 X，直接搜全文会误报（这类误报在 Caddyfile 那个测试里也踩过）。
    """
    text = read(path)
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    return "\n".join(
        re.sub(r"#.*$", "", line) if not line.lstrip().startswith("#") else ""
        for line in text.splitlines()
    )


@pytest.mark.parametrize("path", [FLOAT_PS1, INSTALL_PS1, CLIENT_README])
def test_client_artifacts_exist(path: Path) -> None:
    assert path.is_file(), f"缺少 {path.name}"
    assert path.stat().st_size > 0


@pytest.mark.parametrize("path", [FLOAT_PS1, INSTALL_PS1])
def test_scripts_target_powershell_7_and_are_strict(path: Path) -> None:
    text = read(path)
    assert "#Requires -Version 7.0" in text, f"{path.name} 应声明最低 PowerShell 版本"
    assert "Set-StrictMode -Version Latest" in text, f"{path.name} 应开启严格模式"
    assert "$ErrorActionPreference = 'Stop'" in text, f"{path.name} 应让错误中断执行"


@pytest.mark.parametrize("path", [FLOAT_PS1, INSTALL_PS1])
def test_no_crlf_in_client_scripts(path: Path) -> None:
    """PowerShell 对 CRLF 不敏感，但保持全仓库一致，避免 diff 噪音。"""
    assert b"\r\n" not in path.read_bytes(), f"{path.name} 含 CRLF"


# --------------------------------------------------------------------------- #
# 密钥处理
# --------------------------------------------------------------------------- #
def test_api_key_is_protected_with_dpapi() -> None:
    """不带 -Key 的 ConvertFrom-SecureString 才是 DPAPI（绑定当前用户）。"""
    text = read(FLOAT_PS1)
    assert "ConvertFrom-SecureString" in text
    assert "ConvertTo-SecureString" in text
    # 带 -Key 就退化成"用固定密钥加密"，那不是 DPAPI
    assert not re.search(r"ConvertFrom-SecureString[^\n]*-Key\b", text), (
        "ConvertFrom-SecureString 不应带 -Key，否则不是用户级 DPAPI 加密"
    )


def test_api_key_is_not_written_into_the_config_json() -> None:
    """密钥必须与 config.json 分开存放，后者是可读的明文文件。"""
    text = read(FLOAT_PS1)
    config_block = text.split("$script:DefaultConfig")[1].split("}")[0]
    assert "api_key" not in config_block.lower()

    install_text = read(INSTALL_PS1)
    config_write = install_text.split("$config = [ordered]@{")[1].split("}\n")[0]
    assert "api_key" not in config_write.lower()


def test_key_is_sent_via_header_not_query_string() -> None:
    """密钥进 URL 会落进 Caddy 访问日志，必须走请求头。"""
    text = read(FLOAT_PS1)
    assert "'X-API-Key'" in text
    assert not re.search(r"uri\s*=.*api_key", text, re.IGNORECASE)


def test_key_input_is_hidden_during_install() -> None:
    assert "-AsSecureString" in read(INSTALL_PS1), "交互输入密钥时必须隐藏"


def test_scripts_contain_no_hardcoded_keys() -> None:
    suspicious = re.compile(r"sk_[A-Za-z0-9_-]{20,}")
    for path in (FLOAT_PS1, INSTALL_PS1, CLIENT_README):
        hits = suspicious.findall(read(path))
        assert not hits, f"{path.name} 里疑似写死了 API Key：{hits}"


# --------------------------------------------------------------------------- #
# 界面行为
# --------------------------------------------------------------------------- #
def test_window_is_topmost_and_borderless() -> None:
    text = read(FLOAT_PS1)
    assert "$form.TopMost = $true" in text
    assert "$form.FormBorderStyle = 'None'" in text
    assert "$form.ShowInTaskbar = $false" in text


def test_window_position_is_persisted() -> None:
    text = read(FLOAT_PS1)
    assert "FormClosing" in text, "关闭时应保存窗口位置"
    assert "$Config['x'] = $form.Location.X" in text
    # 脚本自身不会写 "-Reset"（那只在调用时出现），要断言参数声明与分支
    assert "[switch]$Reset" in text, "应提供把窗口拉回屏幕内的开关"
    assert "if ($Reset)" in text


def test_dragging_uses_managed_code_not_pinvoke() -> None:
    """纯 .NET 的偏移量拖动比 ReleaseCapture+SendMessage 更稳，且无平台调用风险。"""
    code = read_code(FLOAT_PS1)
    assert "dragOffset" in code
    assert "ReleaseCapture" not in code, "应使用托管代码拖动，避免 P/Invoke"
    assert "DllImport" not in code
    assert "Add-Type" not in code.replace("Add-Type -AssemblyName", ""), "除加载程序集外不应编译原生代码"


def test_completing_a_task_uses_patch() -> None:
    text = read(FLOAT_PS1)
    assert "Set-SkTaskDone" in text
    assert "status = 'done'" in text


def test_only_ordered_view_is_shown() -> None:
    """悬浮窗只显示有截止时间的任务 —— 与"有序表权重更大"的产品决定一致。"""
    assert "view=ordered" in read(FLOAT_PS1)


def test_overdue_and_soon_are_visually_distinguished() -> None:
    text = read(FLOAT_PS1)
    assert "Get-SkDueColor" in text
    assert "已逾期" in text


# --------------------------------------------------------------------------- #
# 自检与可诊断性
# --------------------------------------------------------------------------- #
def test_selftest_covers_the_whole_chain() -> None:
    text = read(FLOAT_PS1)
    for marker in ("[1/5]", "[2/5]", "[3/5]", "[4/5]", "[5/5]"):
        assert marker in text, f"自检缺少 {marker} 环节"
    assert "-SelfTest" in text


def test_selftest_does_not_show_a_window() -> None:
    """自检必须能在无图形界面的情况下跑完（只是构造窗口对象再释放）。"""
    text = read(FLOAT_PS1)
    selftest = text.split("function Invoke-SkSelfTest")[1].split("function Start-SkFloat")[0]
    assert "ShowDialog" not in selftest
    assert "Dispose()" in selftest


def test_http_errors_surface_the_server_detail() -> None:
    """服务端返回的 detail 里往往有可操作信息（比如"该密钥为只读"），别丢掉。"""
    text = read(FLOAT_PS1)
    assert "ErrorDetails" in text


def test_log_file_is_bounded() -> None:
    text = read(FLOAT_PS1)
    assert "262144" in text, "日志应有大小上限，避免无限增长"


# --------------------------------------------------------------------------- #
# 开机自启
# --------------------------------------------------------------------------- #
def test_autostart_uses_the_startup_folder() -> None:
    text = read(INSTALL_PS1)
    assert "GetFolderPath('Startup')" in text
    assert "CreateShortcut" in text
    assert "WScript.Shell" in text


def test_autostart_hides_the_console_window() -> None:
    """否则每次登录都会闪一个黑框。"""
    assert "-WindowStyle Hidden" in read(INSTALL_PS1)


def test_autostart_verifies_through_selftest() -> None:
    text = read(INSTALL_PS1)
    assert "-SelfTest" in text
    assert "LASTEXITCODE" in text


def test_autostart_is_idempotent() -> None:
    text = read(INSTALL_PS1)
    assert "Test-Path $script:ConfigPath" in text, "已存在配置时应保留窗口位置等"
    assert "Test-Path" in text


def test_autostart_warns_about_plain_http() -> None:
    """明文 HTTP 会把任务内容与密钥摊在链路上，安装时就该提醒。"""
    text = read(INSTALL_PS1)
    assert "明文" in text


# --------------------------------------------------------------------------- #
# 文档与配置形状一致
# --------------------------------------------------------------------------- #
def test_readme_json_sample_matches_script_defaults() -> None:
    readme = read(CLIENT_README)
    block = readme.split("```json")[1].split("```")[0]
    documented = json.loads(block)

    script = read(FLOAT_PS1)
    defaults = script.split("$script:DefaultConfig = [ordered]@{")[1]
    defaults = defaults[: defaults.index("\n}")]

    for key in documented:
        assert key in defaults, f"文档里的 {key!r} 在脚本默认配置中不存在"
    for key in ("server", "poll_seconds", "max_items", "opacity", "width", "x", "y", "open_url"):
        assert key in defaults, f"脚本默认配置缺少 {key}"
        assert key in documented, f"文档没写 {key}"


def test_readme_notes_the_client_key_needs_write_access() -> None:
    """悬浮窗要勾选任务，只读密钥用不了 —— 这是最容易踩的一脚。"""
    text = read(CLIENT_README)
    assert "读写" in text
    assert "只读" in text, "应说明只读密钥是留给小组件（v2）的，悬浮窗用不了"


def test_readme_documents_the_schannel_workaround() -> None:
    text = read(CLIENT_README)
    assert "Schannel" in text
    assert "node -e" in text, "应给出不依赖 Schannel 的验证办法"
    assert "不是网络问题" in text
