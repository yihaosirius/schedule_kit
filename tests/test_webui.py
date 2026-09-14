"""M3：双列表页面渲染与移动端适配要素。

页面用服务端渲染，所以这里直接断言 HTML 结构：
初始内容必须**不依赖 JS** 就能看到，否则弱网或禁用 JS 时首屏是空的。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
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


# --------------------------------------------------------------------------- #
# 布局契约
# --------------------------------------------------------------------------- #
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CSS = PROJECT_ROOT / "app" / "static" / "css" / "app.css"
TEMPLATES = PROJECT_ROOT / "app" / "templates"


def _media_query_bodies(css: str) -> list[str]:
    """取出所有 @media 块的内容（按花括号配对，够用且不依赖 CSS 解析器）。"""
    bodies: list[str] = []
    for match in re.finditer(r"@media[^{]*\{", css):
        depth = 1
        index = match.end()
        while index < len(css) and depth:
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
            index += 1
        bodies.append(css[match.end() : index - 1])
    return bodies


def test_media_query_never_styles_bare_layout() -> None:
    """两栏布局必须**显式选用**，不能靠"默认两栏 + 某页覆盖"。

    实测踩过的坑：`.layout--single` 定义在媒体查询**之前**，而同优先级的
    `.layout` 在媒体查询里又写了一遍 —— ≥900px 时后者胜出，本该单栏的四个
    页面（设置、课表、草稿确认…）全被静默排成了两栏。CSS 覆盖失败没有任何
    报错，只能靠断言挡住。
    """
    for body in _media_query_bodies(CSS.read_text(encoding="utf-8")):
        assert ".layout {" not in body, (
            "媒体查询里不要再给裸 .layout 写规则，它会静默覆盖单栏页面。"
            "两栏请用 .layout--split / .layout--settings。"
        )


def test_split_layouts_are_the_only_two_column_layouts() -> None:
    css = CSS.read_text(encoding="utf-8")
    two_column = re.findall(r"grid-template-columns:\s*minmax\(0,\s*1fr\)\s+(\d+)px", css)
    assert two_column, "找不到两栏布局定义"

    for name in (".layout--split", ".layout--settings"):
        block = css.split(f"{name} {{")[1].split("}")[0]
        assert "grid-template-columns" in block, f"{name} 应定义自己的列宽"


def test_layout_classes_used_in_templates_are_defined_in_css() -> None:
    """模板里用了 CSS 里不存在的 layout 类 —— 典型的改名后漏改。"""
    css = CSS.read_text(encoding="utf-8")
    for template in sorted(TEMPLATES.glob("*.html")):
        text = template.read_text(encoding="utf-8")
        for attr in re.findall(r'class="(layout[^"]*)"', text):
            for token in attr.split():
                assert f".{token}" in css, f"{template.name} 用了 CSS 里没有的 .{token}"


async def test_task_page_uses_the_split_layout(session_client: AsyncClient) -> None:
    html = (await session_client.get("/")).text
    assert 'class="layout layout--split"' in html


async def test_settings_page_stacks_its_sidebar(session_client: AsyncClient) -> None:
    """侧栏的多个面板必须包在同一个 .layout__column 里。

    网格是**按行**排布的：把两个面板分别丢进第 2 列的第 1、2 行，第二个会从
    "第 1 行最高的单元格"下面开始 —— 于是两栏之间裂开一大片空白。用户截图里
    就是这个现象。
    """
    html = (await session_client.get("/settings")).text
    assert 'class="layout layout--settings"' in html

    columns = html.split('<div class="layout__column">')
    assert len(columns) == 3, f"应当恰好有两个 layout__column，实际 {len(columns) - 1}"

    sidebar = columns[2]  # 第二个容器的内容
    assert "API 密钥" in sidebar, "密钥面板应在侧栏容器内"
    assert "运行状态" in sidebar, "运行状态面板应与密钥面板同容器，不能各自独占一行"


async def test_settings_page_lists_every_provider(session_client: AsyncClient) -> None:
    """三个协议都要能选，且重试参数要能在页面上改。"""
    html = (await session_client.get("/settings")).text
    for provider in ("responses", "openai_compat", "mock"):
        assert f'value="{provider}"' in html, f"协议下拉里缺少 {provider}"
    assert "deepseek-flash" in html, "模型占位符要给出真实可用的例子"
    assert 'name="retry_count"' in html
    assert 'name="retry_backoff_seconds"' in html


@pytest.mark.parametrize("path", ["/courses"])
async def test_content_pages_use_a_single_column(
    session_client: AsyncClient, path: str
) -> None:
    """课表、草稿这类内容页是单栏 —— 不该被任何两栏布局波及。"""
    html = (await session_client.get(path)).text
    match = re.search(r'<main class="(layout[^"]*)"', html)
    assert match, "页面应有 <main class=\"layout...\">"
    classes = match.group(1)
    assert "--split" not in classes and "--settings" not in classes, (
        f"{path} 应当是单栏，实际用了 {classes!r}"
    )


async def test_draft_page_uses_a_single_column(app, session_client: AsyncClient) -> None:
    from tests.test_ingest import image_ingest, use_llm

    use_llm(app, [{"title": "x", "category": "other", "due_at": None, "priority": 3}])
    _, draft = await image_ingest(session_client)

    html = (await session_client.get(f"/drafts/{draft['draft_id']}")).text
    match = re.search(r'<main class="(layout[^"]*)"', html)
    assert match and "--split" not in match.group(1)


# --------------------------------------------------------------------------- #
# 导航：一个共享部分，且每个页面都能回主页
# --------------------------------------------------------------------------- #
STATIC = PROJECT_ROOT / "app" / "static"


def test_nav_is_a_single_shared_partial() -> None:
    """导航必须只有一份。

    原先每个页面各写各的 `<header class="topbar">`，结果标签不一致，
    而且「退出」只有首页有。
    """
    assert (TEMPLATES / "_nav.html").is_file(), "缺少共享导航部分 _nav.html"

    for template in sorted(TEMPLATES.glob("*.html")):
        if template.name in {"_nav.html", "base.html"}:
            continue
        text = template.read_text(encoding="utf-8")
        assert 'class="topnav"' not in text, f"{template.name} 自己写了一份导航"
        assert '<header class="topbar">' not in text, f"{template.name} 自己写了一份顶栏"


@pytest.mark.parametrize("path", ["/", "/courses", "/settings"])
async def test_every_page_has_the_nav_and_a_way_out(
    session_client: AsyncClient, path: str
) -> None:
    html = (await session_client.get(path)).text
    assert html.count('class="topnav"') == 1, f"{path} 应恰好有一个导航"
    assert 'href="/courses"' in html
    assert 'href="/settings"' in html
    assert 'data-action="logout"' in html, f"{path} 缺少退出入口"


@pytest.mark.parametrize("path", ["/courses", "/settings"])
async def test_subpages_offer_an_explicit_way_home(
    session_client: AsyncClient, path: str
) -> None:
    """非首页要把回主页写成显式的「← 主页」。

    只写「任务」会有歧义：分不清它是"当前页"还是"返回入口"。
    """
    html = (await session_client.get(path)).text
    assert 'class="topnav__home"' in html, f"{path} 缺少显式的回主页入口"
    assert "← 主页" in html
    assert 'href="/"' in html


async def test_home_page_marks_itself_instead_of_linking_home(
    session_client: AsyncClient,
) -> None:
    html = (await session_client.get("/")).text
    assert 'aria-current="page">任务<' in html
    assert "← 主页" not in html, "已在首页，不该再显示返回主页"


async def test_brand_links_home_on_every_page(session_client: AsyncClient) -> None:
    for path in ("/", "/courses", "/settings"):
        html = (await session_client.get(path)).text
        assert 'class="topbar__brand" href="/"' in html, f"{path} 的品牌名应可点回主页"


async def test_draft_page_marks_draft_not_tasks(app, session_client: AsyncClient) -> None:
    """草稿页不是任务列表，导航里不该把「任务」标为当前页。"""
    from tests.test_ingest import image_ingest, use_llm

    use_llm(app, [{"title": "x", "category": "other", "due_at": None, "priority": 3}])
    _, draft = await image_ingest(session_client)

    html = (await session_client.get(f"/drafts/{draft['draft_id']}")).text
    assert "← 主页" in html
    assert 'aria-current="page">任务<' not in html


async def test_login_page_has_no_nav(client: AsyncClient) -> None:
    """还没登录，不该出现导航。"""
    html = (await client.get("/login")).text
    assert 'class="topnav"' not in html
    assert 'data-action="logout"' not in html


def test_logout_handler_is_global_not_page_specific() -> None:
    """顶栏在每个页面都有，处理器就必须在每页都加载的 app.js 里。

    原先只写在 tasks.js（仅首页加载），子页面点「退出」没反应。
    """
    app_js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    tasks_js = (STATIC / "js" / "tasks.js").read_text(encoding="utf-8")
    assert "data-action=logout" in app_js, "退出处理器应在 app.js"
    assert "data-action=logout" not in tasks_js, "tasks.js 不该再处理退出，否则会重复请求"
