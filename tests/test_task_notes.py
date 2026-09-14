"""任务备注与详情的展示。

背景：备注原先只是一个 ``<span class="chip" title="...">备注</span>`` ——
悬停提示。手机上永远看不到（没有"悬停"这回事），而且**客户端渲染那一份
根本没写这个标记**，所以任何一次勾选完成触发 refresh() 之后它就消失了。

备注恰恰是最需要被读到的内容：它是视觉模型从图片里抽出来的（"只做奇数题"
这种），用户在别处看不到第二遍。
"""

from __future__ import annotations

from pathlib import Path

from httpx import AsyncClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_TEMPLATE = PROJECT_ROOT / "app" / "templates" / "index.html"
TASKS_JS = PROJECT_ROOT / "app" / "static" / "js" / "tasks.js"


async def add_task(client: AsyncClient, **payload) -> dict:
    body = {"title": "任务", "category": "homework", "due_at": None, "priority": 3}
    body.update(payload)
    response = await client.post("/api/tasks", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 服务端渲染
# --------------------------------------------------------------------------- #
async def test_notes_are_rendered_as_visible_text(
    session_client: AsyncClient, app
) -> None:
    await add_task(session_client, title="做习题", notes="只做奇数题，第 3、5、7 题")
    html = (await session_client.get("/")).text

    assert "只做奇数题，第 3、5、7 题" in html
    assert 'class="task__notes"' in html
    # 不能再是靠悬停才能看到的提示
    assert 'title="只做奇数题' not in html


async def test_task_without_notes_has_no_notes_block(session_client: AsyncClient) -> None:
    await add_task(session_client, title="没备注的任务")
    html = (await session_client.get("/")).text
    assert "没备注的任务" in html
    assert "task__notes" not in html


async def test_notes_show_up_in_both_views(session_client: AsyncClient) -> None:
    """有序表和无序表都要显示 —— 备注不属于某一种视图。"""
    await add_task(
        session_client, title="有截止时间的", notes="有序表的备注",
        due_at="2026-06-01T10:00:00+08:00", priority=None,
    )
    await add_task(session_client, title="只有优先级的", notes="无序表的备注")
    html = (await session_client.get("/")).text

    assert "有序表的备注" in html
    assert "无序表的备注" in html


async def test_long_notes_are_present_in_full(session_client: AsyncClient) -> None:
    """预览是一行，但全文必须在 DOM 里 —— 否则展开后什么也看不到。"""
    long_notes = "这是一段很长的备注。" * 40
    await add_task(session_client, title="长备注", notes=long_notes[:2000])
    html = (await session_client.get("/")).text
    assert long_notes[:2000] in html


async def test_notes_are_html_escaped(session_client: AsyncClient) -> None:
    """备注来自图片识别，是外部内容，绝不能当 HTML 插进去。"""
    await add_task(session_client, title="可疑", notes="<script>alert(1)</script>")
    html = (await session_client.get("/")).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


async def test_notes_block_carries_the_task_facts(session_client: AsyncClient) -> None:
    """展开后除了全文，还要能看到来源与时间——不然"详情"名不副实。"""
    await add_task(session_client, title="带详情的", notes="备注内容", source="shortcut")
    html = (await session_client.get("/")).text

    assert "来源 快捷指令" in html
    assert "创建 " in html


# --------------------------------------------------------------------------- #
# 两份渲染器必须同步
# --------------------------------------------------------------------------- #
def test_both_renderers_emit_the_same_notes_markup() -> None:
    """服务端宏与客户端 renderRow 是同一份结构的两个副本。

    这就是本次要修的 bug 的根源：备注标记当初只加在了服务端那一份上，
    于是勾选一次（触发 refresh 重渲染）备注就没了。

    做不到跨 Jinja/JS 的通用一致性检查（没有构建步骤），所以这里钉住
    关键的结构标记：任何一边少了都会红。
    """
    template = INDEX_TEMPLATE.read_text(encoding="utf-8")
    script = TASKS_JS.read_text(encoding="utf-8")

    for marker in ("task__notes", "task__notes-peek", "task__notes-open",
                   "task__notes-full", "task__facts"):
        assert marker in template, f"index.html 里缺少 {marker}"
        assert marker in script, f"tasks.js 的 renderRow 里缺少 {marker}（与服务端不一致）"


def test_client_renderer_uses_textcontent_for_notes() -> None:
    """客户端渲染必须用 textContent，不能用 innerHTML 拼备注。"""
    script = TASKS_JS.read_text(encoding="utf-8")
    block = script.split("function notesBlock")[1].split("\n  }")[0]
    assert "textContent" in block
    assert "innerHTML" not in block


def test_both_renderers_agree_on_the_source_labels() -> None:
    """来源标签在两份渲染器里各写了一遍，值必须一致。"""
    from app.templating import SOURCE_LABELS

    script = TASKS_JS.read_text(encoding="utf-8")
    line = next(row for row in script.splitlines() if "SOURCE_LABELS = {" in row)
    for value, label in SOURCE_LABELS.items():
        assert f'{value}: "{label}"' in line, f"tasks.js 的 SOURCE_LABELS 与后端不一致：{value}"
