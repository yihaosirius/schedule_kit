"""M3：双列表页面渲染与移动端适配要素。

页面用服务端渲染，所以这里直接断言 HTML 结构：
初始内容必须**不依赖 JS** 就能看到，否则弱网或禁用 JS 时首屏是空的。
"""

from __future__ import annotations

import re

from httpx import AsyncClient


async def _seed(session_client: AsyncClient) -> dict[str, int]:
    """造两条有序、两条无序任务，返回标题到 id 的映射。"""
    payloads = [
        {"title": "交实验报告", "category": "homework", "due_at": "2026-04-01T02:00:00+00:00"},
        {"title": "复习线代", "category": "exam", "due_at": "2026-05-01T02:00:00+00:00"},
        {"title": "买打印纸", "category": "other", "priority": 4},
        {"title": "写周报", "category": "practice", "priority": 1},
    ]
    created: dict[str, int] = {}
    for payload in payloads:
        response = await session_client.post("/api/tasks", json=payload)
        assert response.status_code == 201, response.text
        created[payload["title"]] = response.json()["id"]
    return created


def _section(html: str, view: str) -> str:
    """取出某个列表容器内的 HTML 片段。"""
    match = re.search(
        rf'data-list="{view}"[^>]*>(.*?)</ul>', html, re.DOTALL
    )
    assert match, f"页面里找不到 {view} 列表容器"
    return match.group(1)


async def test_root_renders_both_panels(session_client: AsyncClient) -> None:
    html = (await session_client.get("/")).text
    assert 'data-list="ordered"' in html
    assert 'data-list="unordered"' in html
    assert "有序 · 有截止时间" in html
    assert "无序 · 按优先级" in html


async def test_items_land_in_the_correct_panel(session_client: AsyncClient) -> None:
    await _seed(session_client)
    html = (await session_client.get("/")).text

    ordered = _section(html, "ordered")
    unordered = _section(html, "unordered")

    assert "交实验报告" in ordered
    assert "复习线代" in ordered
    assert "买打印纸" not in ordered

    assert "买打印纸" in unordered
    assert "写周报" in unordered
    assert "交实验报告" not in unordered


async def test_ordered_panel_renders_deadline_unordered_renders_priority(
    session_client: AsyncClient,
) -> None:
    await _seed(session_client)
    html = (await session_client.get("/")).text

    ordered = _section(html, "ordered")
    unordered = _section(html, "unordered")

    # 有序表显示时间与倒计时，不显示优先级徽章
    assert "due" in ordered
    assert "badge-priority" not in ordered

    # 无序表显示优先级徽章，不显示时间
    assert 'class="badge-priority" data-p="1"' in unordered
    assert 'data-p="4"' in unordered
    assert "due " not in unordered


async def test_counts_reflect_open_items(session_client: AsyncClient) -> None:
    created = await _seed(session_client)
    await session_client.patch(f"/api/tasks/{created['写周报']}", json={"status": "done"})

    html = (await session_client.get("/")).text
    ordered_count = re.search(r'data-count-for="ordered">(\d+)<', html)
    unordered_count = re.search(r'data-count-for="unordered">(\d+)<', html)
    assert ordered_count and ordered_count.group(1) == "2"
    # 完成的任务不再出现在默认（open）视图里
    assert unordered_count and unordered_count.group(1) == "1"


async def test_empty_state_is_rendered_when_no_items(session_client: AsyncClient) -> None:
    html = (await session_client.get("/")).text
    assert "暂无带截止时间的任务" in html
    assert "暂无仅按优先级排的任务" in html
    # 没有数据时不应渲染空的任务行
    assert html.count('class="task"') == 0


async def test_empty_state_is_hidden_once_items_exist(session_client: AsyncClient) -> None:
    await _seed(session_client)
    html = (await session_client.get("/")).text
    assert re.search(r'data-empty-for="ordered" hidden', html)
    assert re.search(r'data-empty-for="unordered" hidden', html)


async def test_add_form_offers_exclusive_controls(session_client: AsyncClient) -> None:
    html = (await session_client.get("/")).text
    assert 'data-add-form' in html
    assert 'data-mode' in html
    assert 'data-due' in html
    assert 'data-priority' in html
    # 五个分类齐全
    for label in ("作业", "练习", "考试", "要约", "其他"):
        assert label in html
    # 五级优先级齐全
    for label in ("Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ", "Ⅴ"):
        assert label in html


async def test_topbar_links_and_csrf_meta(session_client: AsyncClient) -> None:
    html = (await session_client.get("/")).text
    assert 'href="/courses"' in html
    assert 'href="/settings"' in html
    assert 'name="csrf-token"' in html
    assert 'data-action="logout"' in html


async def test_unordered_panel_is_collapsible(session_client: AsyncClient) -> None:
    """移动端折叠无序表：它是 details/summary，且带 data-collapsible 交给 JS 定初值。"""
    html = (await session_client.get("/")).text
    assert re.search(r"<details[^>]*data-collapsible", html)
    assert "<summary>" in html


async def test_mobile_viewport_and_fab_present(session_client: AsyncClient) -> None:
    html = (await session_client.get("/")).text
    assert "width=device-width" in html
    assert 'data-action="focus-add"' in html


async def test_manifest_is_linked(session_client: AsyncClient) -> None:
    html = (await session_client.get("/")).text
    assert 'rel="manifest"' in html


async def test_static_assets_are_served(session_client: AsyncClient) -> None:
    for path in ("/static/css/app.css", "/static/js/app.js", "/static/js/tasks.js"):
        response = await session_client.get(path)
        assert response.status_code == 200, path
        assert response.content


async def test_anonymous_cannot_see_the_task_page(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_task_titles_are_html_escaped(session_client: AsyncClient) -> None:
    """标题来自用户输入（也可能来自 LLM 识别的截图文字），必须转义。"""
    await session_client.post(
        "/api/tasks",
        json={"title": "<script>alert(1)</script>", "category": "other", "priority": 3},
    )
    html = (await session_client.get("/")).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
