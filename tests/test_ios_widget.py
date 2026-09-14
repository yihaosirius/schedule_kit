"""v2：Scriptable 小组件脚本的结构、安全与端到端渲染校验。

沿用 `tests/test_windows_client.py` 的既定分工：

* **功能**由客户端自带的模式验证（那边是 `-SelfTest`，这边是在 Scriptable
  App 里跑脚本弹出的自检报告）。真机上的渲染没法在 CI 里复现。
* **静态约束**在这里补：几条容易在后续改动中被无意破坏的约定。

额外多做了一件事：`preview.mjs` 用 stub 顶掉 Scriptable 的 API，让**同一份
脚本**在 Node 里真跑一遍。所以下面除了静态断言，还有一组端到端用例 ——
用进程内应用生成**真实**的接口响应当 fixture，跑脚本，断言渲染结果。
这是在没有 iPhone 的情况下能拿到的最强验证：语法错误、字段名写错、
空数组没兜住，都会在这里现形，而不是等到手机上变成一块空白。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLIENT_DIR = PROJECT_ROOT / "clients" / "ios"

WIDGET_JS = CLIENT_DIR / "ScheduleKitWidget.js"
PREVIEW_MJS = CLIENT_DIR / "preview.mjs"
CLIENT_README = CLIENT_DIR / "README.md"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="需要 node 才能跑小组件预览器")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_code(path: Path) -> str:
    """剥掉注释后的代码。

    断言"脚本里不该出现某某写法"时必须用它：解释"为什么不用 X"的注释
    本身就含有 X（这个坑在 Caddyfile 与 tasks.js 的测试里都踩过）。
    """
    text = read(path)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", text)


# --------------------------------------------------------------------------- #
# 文件存在
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [WIDGET_JS, PREVIEW_MJS, CLIENT_README])
def test_client_artifacts_exist(path: Path) -> None:
    assert path.is_file(), f"缺少 {path.name}"
    assert path.stat().st_size > 0


@pytest.mark.parametrize("path", [WIDGET_JS, PREVIEW_MJS])
def test_no_crlf_in_client_scripts(path: Path) -> None:
    assert b"\r\n" not in path.read_bytes(), f"{path.name} 含 CRLF"


# --------------------------------------------------------------------------- #
# 安全：真密钥绝不能进仓库
# --------------------------------------------------------------------------- #
def test_no_api_key_is_committed() -> None:
    """脚本里只能有占位符。

    这条是本次开发中最容易被违反的一条 —— 联调时手上就有一把真密钥，
    顺手粘进去太容易了。仓库是公开的。
    """
    text = read(WIDGET_JS)
    assert 'API_KEY: "sk_在这里粘贴只读密钥"' in text, "API_KEY 应当是明确的占位符"

    leaked = re.findall(r"sk_[A-Za-z0-9_-]{20,}", text)
    assert not leaked, f"脚本里出现了像真密钥的字符串：{leaked}"


def test_no_api_key_in_any_client_file() -> None:
    """README / 预览器里也不许有。"""
    for path in sorted(CLIENT_DIR.rglob("*")):
        if not path.is_file():
            continue
        leaked = re.findall(r"sk_[A-Za-z0-9_-]{20,}", path.read_text(encoding="utf-8"))
        assert not leaked, f"{path.name} 里有像真密钥的字符串：{leaked}"


def test_tls_verification_is_not_disabled() -> None:
    """`allowInsecureRequest` 会跳过证书校验，绝不能打开。

    我们的证书是 Let's Encrypt 且自动续期，没有任何理由关掉它。
    """
    code = read_code(WIDGET_JS)
    assert "allowInsecureRequest = true" not in code
    assert "allowInsecureRequest=true" not in code


# --------------------------------------------------------------------------- #
# 静态约束：Scriptable 的语法与 API 边界
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "syntax",
    ["?.", "??", "Promise.allSettled", "Object.fromEntries"],
)
def test_only_es6_syntax(syntax: str) -> None:
    """Scriptable 文档写的是 ECMAScript 6。

    `?.` / `??` 之类更晚的语法在小组件里一旦不被支持，代价是整个方块渲染不出来，
    而你在手机上很难看出原因。所以宁可写得啰嗦。
    """
    code = read_code(WIDGET_JS)
    assert syntax not in code, f"用到了 ES6 之后的语法：{syntax}"


def test_timeout_is_configured() -> None:
    """默认 60 秒超时会把小组件拖死 —— iOS 给的时间预算很紧。"""
    text = read(WIDGET_JS)
    assert "timeoutInterval" in text
    assert "TIMEOUT_SECONDS: 4" in text, "超时应当是几秒级，不是默认的 60 秒"


def test_uses_the_three_existing_endpoints() -> None:
    """服务端零改动 —— 只用已存在的接口。"""
    text = read(WIDGET_JS)
    assert "view=ordered&status=open" in text
    assert "view=unordered&status=open" in text
    assert "/api/ingest?status=pending" in text
    # 不该有自己发明的新路径
    assert not re.search(r'"/api/(?!tasks|ingest|healthz)[a-z]', text)


def test_handles_both_widget_and_in_app_entry() -> None:
    """在 App 里跑要走自检，不能直接渲染（那样什么都看不到）。"""
    code = read_code(WIDGET_JS)
    assert "config.widgetFamily === null" in code, "没有区分「在 App 里运行」与「在小组件里运行」"
    assert "Script.setWidget" in code
    assert "refreshAfterDate" in code


def test_covers_every_widget_family() -> None:
    """六种尺寸都要有分支，否则以后加个小号会直接崩。"""
    code = read_code(WIDGET_JS)
    for family in (
        "small",
        "medium",
        "accessoryRectangular",
        "accessoryInline",
        "accessoryCircular",
        "large",
    ):
        assert f'"{family}"' in code, f"没有处理 {family}"


def test_auth_failure_is_not_rendered_as_an_empty_list() -> None:
    """401 不能显示成"没有待办"。

    这是开发中实测抓到的 bug：草稿箱那个请求恰好返回 200，于是旧的
    "至少一个成功就算成功"规则让小组件显示「没有待办 🎉」——
    用户会以为自己真的没事可做。
    """
    code = read_code(WIDGET_JS)
    assert "isAuthError" in code, "缺少鉴权错误的专门判定"
    assert "401" in code and "403" in code


def test_two_task_lists_both_failing_is_not_an_empty_list() -> None:
    """/api/tasks 全挂时也不能显示"没有待办"。"""
    code = read_code(WIDGET_JS)
    assert "tasksOk" in code, "缺少「两个任务列表都失败」的判定"


def test_cache_read_write_is_fault_tolerant() -> None:
    """小组件进程里能否读写本地文件没实机验证过，所以必须吞异常。"""
    code = read_code(WIDGET_JS)
    for name in ("readCache", "writeCache"):
        block = code.split(f"function {name}")[1].split("\n}")[0]
        assert "catch" in block, f"{name} 没有兜住异常，缓存出问题会连累主流程"


def test_countdown_uses_live_widget_date() -> None:
    """倒计时靠 WidgetDate 自己走，不依赖刷新。"""
    code = read_code(WIDGET_JS)
    assert "addDate" in code
    assert "applyTimerStyle" in code, "48 小时内应当用走秒计时器"
    assert "applyRelativeStyle" in code, "更远的应当用相对时间"


# --------------------------------------------------------------------------- #
# 端到端：用真实接口响应当 fixture，跑同一份脚本
# --------------------------------------------------------------------------- #
def run_preview(fixtures: Path, family: str = "medium", extra: list[str] | None = None):
    """跑 preview.mjs。

    输出重定向到文件而不是管道：受限沙箱里管道可能被拒，
    而日志重定向一直可用（见 tests/conftest.py 的同类处理）。
    """
    out_file = fixtures / f"_stdout-{family}.txt"
    args = [NODE, str(PREVIEW_MJS), "--fixtures", str(fixtures), "--family", family]
    if extra:
        args.extend(extra)
    with open(out_file, "w+", encoding="utf-8") as handle:
        completed = subprocess.run(
            args, cwd=str(PROJECT_ROOT), stdout=handle, stderr=subprocess.STDOUT
        )
        handle.seek(0)
        output = handle.read()
    return completed.returncode, output


async def write_fixtures(session_client: AsyncClient, target: Path) -> Path:
    """用进程内应用生成三份真实响应。

    走的是**真实接口**，不是手写的 JSON —— 这样接口字段一旦改名，
    小组件用例会跟着红，而不是等到手机上才发现。
    """
    target.mkdir(parents=True, exist_ok=True)

    await add_task(session_client, "第一周作业：习题一", due_at="2026-09-21T15:59:00+00:00")
    await add_task(session_client, "完成课程论文并提交", due_at="2026-11-06T14:00:00+00:00")
    await add_task(session_client, "背单词（每天 30 个）", priority=2)
    await add_task(session_client, "整理实验室器材清单", priority=4)
    # 一条待确认草稿（客户端直供 items，不调 LLM）
    response = await session_client.post(
        "/api/ingest",
        json={
            "channel": "text",
            "items": [
                {"title": "第三章习题", "category": "homework",
                 "due_at": None, "priority": 3}
            ],
        },
    )
    assert response.status_code == 201, response.text

    sources = {
        "ordered.json": "/api/tasks?view=ordered&status=open&limit=2",
        "unordered.json": "/api/tasks?view=unordered&status=open&limit=2",
        "drafts.json": "/api/ingest?status=pending&limit=1",
    }
    for name, url in sources.items():
        body = (await session_client.get(url)).json()
        (target / name).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return target


async def add_task(
    client: AsyncClient, title: str, *, due_at: str | None = None, priority: int | None = None
) -> dict:
    """建一条任务。due_at 与 priority 严格遵守二选一，不能两个都给。"""
    payload: dict = {"title": title, "category": "homework"}
    if due_at:
        payload["due_at"] = due_at
    else:
        payload["priority"] = 3 if priority is None else priority
    response = await client.post("/api/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


@needs_node
async def test_medium_widget_renders_real_api_data(tmp_path: Path, session_client: AsyncClient) -> None:
    fixtures = await write_fixtures(session_client, tmp_path / "fixtures")
    code, output = run_preview(fixtures, "medium")

    assert code == 0, f"预览器退出码 {code}：\n{output}"
    # 有序两条
    assert "第一周作业：习题一" in output
    assert "完成课程论文并提交" in output
    assert "09-21 23:59" in output
    assert "11-06 22:00" in output
    # 无序两条，带罗马数字档位
    assert "Ⅱ" in output and "背单词（每天 30 个）" in output
    assert "Ⅳ" in output and "整理实验室器材清单" in output
    # 待确认角标
    assert "● 1 条待确认" in output
    # 点击目标
    assert "https://canisa1ph.duckdns.org:8443/" in output
    assert "refreshAfterDate" in output


@needs_node
async def test_medium_widget_links_the_pending_badge_to_the_draft_box(
    tmp_path: Path, session_client: AsyncClient
) -> None:
    """角标单独链到 /drafts —— 有东西要确认时，点它就该去确认。"""
    fixtures = await write_fixtures(session_client, tmp_path / "fixtures")
    _, output = run_preview(fixtures, "medium")
    assert "/drafts" in output


@needs_node
async def test_lock_screen_widget_shows_the_nearest_deadline(
    tmp_path: Path, session_client: AsyncClient
) -> None:
    fixtures = await write_fixtures(session_client, tmp_path / "fixtures")
    code, output = run_preview(fixtures, "accessoryRectangular")

    assert code == 0, output
    # 锁屏只放最近的那一条
    assert "第一周作业：习题一" in output
    assert "完成课程论文并提交" not in output
    assert "走秒计时器" in output or "相对时间" in output


@needs_node
async def test_every_family_renders_without_throwing(
    tmp_path: Path, session_client: AsyncClient
) -> None:
    """六种尺寸挨个跑一遍 —— 小组件里抛异常的代价是整块空白。"""
    fixtures = await write_fixtures(session_client, tmp_path / "fixtures")
    for family in (
        "small",
        "medium",
        "large",
        "accessoryRectangular",
        "accessoryCircular",
        "accessoryInline",
    ):
        code, output = run_preview(fixtures, family)
        assert code == 0, f"{family} 渲染失败：\n{output}"
        assert "脚本抛异常" not in output, f"{family} 抛异常：\n{output}"


@needs_node
async def test_empty_lists_do_not_crash(tmp_path: Path) -> None:
    """一条待办都没有的账号也要能渲染。"""
    fixtures = tmp_path / "empty"
    fixtures.mkdir(parents=True)
    (fixtures / "ordered.json").write_text("[]", encoding="utf-8")
    (fixtures / "unordered.json").write_text("[]", encoding="utf-8")
    (fixtures / "drafts.json").write_text(
        json.dumps({"drafts": [], "total": 0, "counts": {"pending": 0}}), encoding="utf-8"
    )
    for family in ("medium", "small", "accessoryRectangular", "accessoryCircular"):
        code, output = run_preview(fixtures, family)
        assert code == 0, f"{family} 空列表渲染失败：\n{output}"
        assert "脚本抛异常" not in output


# --------------------------------------------------------------------------- #
# 端到端：失败路径
# --------------------------------------------------------------------------- #
@needs_node
async def test_revoked_key_is_reported_not_shown_as_empty(tmp_path: Path) -> None:
    """**这是实测抓到的 bug 的回归用例。**

    401 时小组件曾经显示「没有待办 🎉」—— 因为草稿箱那个请求恰好成功，
    旧的"至少一个成功就算成功"规则就放行了。那是在撒谎。
    """
    fixtures = tmp_path / "unauthorized"
    fixtures.mkdir(parents=True)
    (fixtures / "ordered.json").write_text("[]", encoding="utf-8")
    (fixtures / "unordered.json").write_text("[]", encoding="utf-8")
    (fixtures / "drafts.json").write_text(
        json.dumps({"counts": {"pending": 0}}), encoding="utf-8"
    )
    (fixtures / "_status.json").write_text(json.dumps({"tasks": 401}), encoding="utf-8")

    code, output = run_preview(fixtures, "medium")
    assert code == 0, output
    assert "密钥无效" in output
    assert "没有待办" not in output, "密钥失效时绝不能显示成「没有待办」"


@needs_node
async def test_gateway_error_is_reported_not_shown_as_empty(tmp_path: Path) -> None:
    """网关返回 HTML 错误页（非 JSON）时，同样不能显示成"没有待办"。"""
    fixtures = tmp_path / "gateway"
    fixtures.mkdir(parents=True)
    (fixtures / "ordered.json").write_text("[]", encoding="utf-8")
    (fixtures / "unordered.json").write_text("[]", encoding="utf-8")
    (fixtures / "drafts.json").write_text(
        json.dumps({"counts": {"pending": 0}}), encoding="utf-8"
    )
    (fixtures / "_status.json").write_text(json.dumps({"tasks": 502}), encoding="utf-8")
    (fixtures / "_html_error.json").write_text("{}", encoding="utf-8")

    code, output = run_preview(fixtures, "medium")
    assert code == 0, output
    assert "502" in output
    assert "没有待办" not in output


@needs_node
async def test_offline_without_cache_shows_the_reason(tmp_path: Path) -> None:
    fixtures = tmp_path / "offline"
    fixtures.mkdir(parents=True)
    (fixtures / "_offline.json").write_text("{}", encoding="utf-8")

    code, output = run_preview(fixtures, "medium")
    assert code == 0, output
    assert "连不上服务器" in output


@needs_node
async def test_offline_with_cache_falls_back_to_last_known_data(
    tmp_path: Path, session_client: AsyncClient
) -> None:
    """断网时显示上次的数据 + 陈旧标记，而不是一块空白。"""
    fixtures = await write_fixtures(session_client, tmp_path / "fixtures")
    # 保留 fixture 数据（用来预置缓存），但让所有请求都失败
    (fixtures / "_offline.json").write_text("{}", encoding="utf-8")

    code, output = run_preview(fixtures, "medium", extra=["--seed-cache"])
    assert code == 0, output
    # 上次的数据还在
    assert "第一周作业：习题一" in output
    # 而且明确标出这是旧数据
    assert "上次更新" in output
    assert "42 分钟前" in output
