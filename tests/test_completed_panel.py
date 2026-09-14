"""已完成面板：展示、排序、折叠，以及"不要动布局契约"。

加这一栏刻意**不加新接口**：`GET /api/tasks?status=done` 已经存在，
deadline 与 priority 严格二选一，所以 ordered + unordered 两个视图合起来
就是全集。（「已完成」没有专门端点，客户端取两次再合并。）

它也刻意**不加第三个网格列**：侧栏是一个 `.layout__column`，面板在里头
纵向堆叠。`.layout--split` 仍然是两栏 —— 这是文档里写死的布局契约。
"""

from __future__ import annotations

import re
from pathlib import Path

from httpx import AsyncClient

from app.services import tasks as task_service

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CSS = PROJECT_ROOT / "app" / "static" / "css" / "app.css"
INDEX_TEMPLATE = PROJECT_ROOT / "app" / "templates" / "index.html"
TASKS_JS = PROJECT_ROOT / "app" / "static" / "js" / "tasks.js"


async def add_task(client: AsyncClient, title: str, **payload) -> dict:
    body = {"title": title, "category": "homework", "due_at": None, "priority": 3}
    body.update(payload)
    response = await client.post("/api/tasks", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def complete(client: AsyncClient, item_id: int) -> None:
    response = await client.patch(f"/api/tasks/{item_id}", json={"status": "done"})
    assert response.status_code == 200, response.text


def set_completed_at(app, item_id: int, moment: str) -> None:
    """把 completed_at 改成确定值。

    PATCH 写的是"现在"，同一秒内完成的多条会并列，测不出时间序，
    所以这里直接改库。
    """
    with app.state.db.transaction() as conn:
        conn.execute("UPDATE items SET completed_at = ? WHERE id = ?", (moment, item_id))


def strip_comments(text: str) -> str:
    """去掉注释再做源码断言。

    注释里会**提到**被禁止的写法（"不能再写 X，因为……"），
    所以直接对全文断言会被自己的说明文字绊倒。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


# --------------------------------------------------------------------------- #
# 服务层：排序
# --------------------------------------------------------------------------- #
async def test_completed_is_newest_first(app, session_client: AsyncClient) -> None:
    first = await add_task(session_client, "最早完成的")
    second = await add_task(session_client, "中间完成的")
    third = await add_task(session_client, "最近完成的")
    for item in (first, second, third):
        await complete(session_client, item["id"])

    set_completed_at(app, first["id"], "2026-09-01T00:00:00+00:00")
    set_completed_at(app, second["id"], "2026-09-05T00:00:00+00:00")
    set_completed_at(app, third["id"], "2026-09-09T00:00:00+00:00")

    rows = task_service.list_completed(app.state.db)
    assert [row["title"] for row in rows] == ["最近完成的", "中间完成的", "最早完成的"]


async def test_completed_ties_fall_back_to_newest_id(
    app, session_client: AsyncClient
) -> None:
    """同一秒完成的（completed_at 相同）按 id 倒序，保证顺序确定。"""
    for index in range(3):
        item = await add_task(session_client, f"同秒 {index}")
        await complete(session_client, item["id"])
        set_completed_at(app, item["id"], "2026-09-05T00:00:00+00:00")

    rows = task_service.list_completed(app.state.db)
    assert [row["id"] for row in rows] == sorted((row["id"] for row in rows), reverse=True)


async def test_completed_excludes_open_tasks(app, session_client: AsyncClient) -> None:
    done = await add_task(session_client, "做完的")
    await add_task(session_client, "还没做的")
    await complete(session_client, done["id"])

    assert [row["title"] for row in task_service.list_completed(app.state.db)] == ["做完的"]


async def test_completed_respects_the_limit(app, session_client: AsyncClient) -> None:
    for index in range(5):
        item = await add_task(session_client, f"任务 {index}")
        await complete(session_client, item["id"])

    assert len(task_service.list_completed(app.state.db, limit=2)) == 2


async def test_unchecking_removes_it_from_completed(
    app, session_client: AsyncClient
) -> None:
    """撤销完成要立刻从这一栏消失，并且 completed_at 要清掉。"""
    item = await add_task(session_client, "手滑点错了")
    await complete(session_client, item["id"])
    assert len(task_service.list_completed(app.state.db)) == 1

    reopened = await session_client.patch(f"/api/tasks/{item['id']}", json={"status": "open"})
    assert reopened.json()["completed_at"] is None
    assert task_service.list_completed(app.state.db) == []


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
async def test_completed_panel_is_rendered(app, session_client: AsyncClient) -> None:
    item = await add_task(session_client, "已经做完的作业")
    await complete(session_client, item["id"])

    html = (await session_client.get("/")).text
    assert "已完成" in html
    assert "已经做完的作业" in html
    assert 'data-list="completed"' in html
    assert 'data-count-for="completed">1<' in html


async def test_completed_panel_shows_open_tasks_separately(
    app, session_client: AsyncClient
) -> None:
    """已完成的任务不该同时出现在"有序/无序"里 —— 那两栏只列未完成的。"""
    item = await add_task(session_client, "唯一的任务")
    await complete(session_client, item["id"])

    html = (await session_client.get("/")).text
    # 只应出现在 completed 那一栏里
    assert html.count("唯一的任务") == 1


async def test_completed_panel_empty_state(session_client: AsyncClient) -> None:
    html = (await session_client.get("/")).text
    assert 'data-empty-for="completed"' in html
    assert "还没有完成的任务" in html


async def test_completed_panel_is_collapsible(session_client: AsyncClient) -> None:
    """和"无序"一样是 details/summary，移动端由 JS 收起。"""
    html = (await session_client.get("/")).text
    collapsibles = re.findall(r"<details[^>]*data-collapsible[^>]*>", html)
    assert len(collapsibles) == 2, f"应当有两个可折叠侧栏面板，实际 {len(collapsibles)}"
    assert "已完成" in html.split("data-list=\"completed\"")[0]


async def test_limit_is_passed_to_the_client(session_client: AsyncClient) -> None:
    """渲染端与 refresh() 的条数上限必须同源。

    前端自己写一个常数的话，页面刚渲染出来的条数和刷新后的条数会不一致。
    """
    html = (await session_client.get("/")).text
    assert f'data-completed-limit="{task_service.COMPLETED_LIMIT}"' in html

    script = strip_comments(TASKS_JS.read_text(encoding="utf-8"))
    assert "data-completed-limit" not in script, "JS 应当读 dataset，而不是再写一份常量"
    assert "page.dataset.completedLimit" in script


# --------------------------------------------------------------------------- #
# 布局契约：不能多出第三个网格列
# --------------------------------------------------------------------------- #
async def test_sidebar_panels_share_one_column(app, session_client: AsyncClient) -> None:
    """两个侧栏面板必须同处一个 .layout__column。

    网格是**按行**排布的：把两个面板分别丢进第 2 列的第 1、2 行，第二个会从
    "第 1 行最高的单元格"下面开始 —— 两栏之间裂开一大片空白。
    """
    html = (await session_client.get("/")).text
    assert html.count('<div class="layout__column">') == 1

    sidebar = html.split('<div class="layout__column">')[1]
    assert "无序 · 按优先级" in sidebar, "无序面板应在侧栏容器内"
    assert "已完成" in sidebar, "已完成面板应与无序面板同容器，不能各自独占一行"


def test_split_layout_is_still_two_columns() -> None:
    """加一栏不能变成三列。"""
    css = CSS.read_text(encoding="utf-8")
    block = css.split(".layout--split {")[1].split("}")[0]
    assert "grid-template-columns: minmax(0, 1fr) 320px" in block


def test_sidebar_column_is_pinned_to_the_second_grid_column() -> None:
    """包进 column 之后，`.layout--split > .panel--minor` 会静默失效。

    那条规则只匹配直接子元素；面板进了 column 就不再是。如果不补上
    `> .layout__column { grid-column: 2 }`，两个侧栏面板会被自动排到第 1 列，
    和主栏叠在一起。
    """
    css = strip_comments(CSS.read_text(encoding="utf-8"))
    assert ".layout--split > .layout__column { grid-column: 2; }" in css
    assert ".layout--split > .panel--minor" not in css, (
        "这条规则已经没有匹配对象了（面板不再是直接子元素），留着会误导"
    )


def test_client_refresh_covers_all_three_panels() -> None:
    """refresh() 必须同时刷新三栏，否则写完一次操作后某一栏会停在旧数据。"""
    script = TASKS_JS.read_text(encoding="utf-8")
    block = script.split("async function refresh()")[1].split("\n  }")[0]

    assert "status=done" in block, "refresh 没有拉取已完成"
    for view in ("ordered", "unordered", "completed"):
        assert f'["{view}"' in block, f"refresh 没有刷新 {view} 栏"


def test_client_sort_matches_the_server_rule() -> None:
    """两边的排序规则必须一致：completed_at 倒序、id 倒序兜底。

    服务端是 SQL 的 ORDER BY，客户端是数组 sort —— 没有共享实现，
    所以在源码层面钉住这条规则的两个关键点。
    """
    script = TASKS_JS.read_text(encoding="utf-8")
    block = script.split("function byCompletedDesc")[1].split("\n  }")[0]
    assert "completed_at" in block
    assert "return b.id - a.id" in block, "并列时要按 id 倒序，否则顺序不确定"

    python_source = (
        PROJECT_ROOT / "app" / "services" / "tasks.py"
    ).read_text(encoding="utf-8")
    assert "ORDER BY completed_at DESC, id DESC" in python_source
