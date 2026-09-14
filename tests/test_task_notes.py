"""任务备注与详情的展示。

背景：备注原先只是一个 ``<span class="chip" title="...">备注</span>`` ——
悬停提示。手机上永远看不到（没有"悬停"这回事），而且**客户端渲染那一份
根本没写这个标记**，所以任何一次勾选完成触发 refresh() 之后它就消失了。

备注恰恰是最需要被读到的内容：它是视觉模型从图片里抽出来的（"只做奇数题"
这种），用户在别处看不到第二遍。
"""

from __future__ import annotations

import re
from pathlib import Path

from httpx import AsyncClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_TEMPLATE = PROJECT_ROOT / "app" / "templates" / "index.html"
TASKS_JS = PROJECT_ROOT / "app" / "static" / "js" / "tasks.js"
DRAFT_JS = PROJECT_ROOT / "app" / "static" / "js" / "draft.js"
CSS = PROJECT_ROOT / "app" / "static" / "css" / "app.css"


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

    这就是"备注消失"那个 bug 的根源：备注标记当初只加在了服务端那一份上，
    于是勾选一次（触发 refresh 重渲染）备注就没了。

    做不到跨 Jinja/JS 的通用一致性检查（没有构建步骤），所以这里钉住
    关键的结构标记：任何一边少了都会红。
    """
    template = INDEX_TEMPLATE.read_text(encoding="utf-8")
    script = TASKS_JS.read_text(encoding="utf-8")

    for marker in ("task__notes", "task__notes-label", "task__notes-peek",
                   "task__notes-full", "task__facts"):
        assert marker in template, f"index.html 里缺少 {marker}"
        assert marker in script, f"tasks.js 的 renderRow 里缺少 {marker}（与服务端不一致）"


def test_notes_summary_does_not_repeat_itself() -> None:
    """折叠与展开状态都不该出现重复文案。

    上一版把"备注 · 收起"和预览**同时**渲染出来（CSS 没生效时两个都可见），
    看起来就是同一句话说了两遍。
    """
    template = INDEX_TEMPLATE.read_text(encoding="utf-8")
    script = TASKS_JS.read_text(encoding="utf-8")
    for text in ("备注 · 收起", "task__notes-open"):
        assert text not in template, f"index.html 里还留着旧写法：{text}"
        assert text not in script, f"tasks.js 里还留着旧写法：{text}"


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


def test_notes_component_looks_like_a_control_not_a_text_glyph() -> None:
    """三角形的样式必须钉住。

    浏览器默认的 disclosure 三角是个**文字字形**（▶），字号跟着正文走、
    还不像控件。所以这里要求：隐藏原生 marker，并用边框自己画一个。
    """
    css = CSS.read_text(encoding="utf-8")
    # 整段备注样式：从 .task__notes 块到 .task__notes-full 之前
    block = css.split(".task__notes {")[1].split(".task__notes-full")[0]

    assert "list-style: none" in block, "没有隐藏原生三角，它会以文字字形出现"
    assert "-webkit-details-marker" in block, "Safari/Chrome 需要这个才隐藏原生三角"
    assert "border-left: 4.5px solid currentColor" in block, "三角形应当用边框画"
    # 字号要小于任务标题（14.5px），否则备注比标题还抢眼
    assert "font-size: 11.5px" in block


# --------------------------------------------------------------------------- #
# 网页二次审核不能丢备注
# --------------------------------------------------------------------------- #
DRAFT_NOTES = "1(1,3),4,5(1),10(1),14(1,3,5,7)"
DRAFT_QUOTE = "第一周作业"


def _field_value(html: str, field: str) -> str:
    """从确认页里读出某个 data-field 控件的值。

    读不出来就断言失败 —— 这正是被测的行为：字段必须真的在页面上，
    collect() 才拿得到。
    """
    textarea = re.search(
        rf'<textarea[^>]*data-field="{field}"[^>]*>(.*?)</textarea>', html, re.S
    )
    if textarea:
        return textarea.group(1)
    hidden = re.search(rf'<input[^>]*data-field="{field}"[^>]*value="([^"]*)"', html)
    if hidden:
        return hidden.group(1)
    raise AssertionError(f"确认页上没有 data-field={field} 的控件，draft.js 取不到它的值")


async def ingest_draft_with_notes(client: AsyncClient) -> int:
    response = await client.post(
        "/api/ingest",
        json={
            "channel": "text",
            "items": [
                {
                    "title": "第一周作业 习题一",
                    "category": "homework",
                    "due_at": None,
                    "priority": 3,
                    "notes": DRAFT_NOTES,
                    "source_quote": DRAFT_QUOTE,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["draft_id"])


async def test_confirm_page_exposes_notes_and_source_quote(
    session_client: AsyncClient,
) -> None:
    """审核页必须能看到备注 —— 看不到就无从审核，等于没有。"""
    draft_id = await ingest_draft_with_notes(session_client)
    html = (await session_client.get(f"/drafts/{draft_id}")).text

    assert _field_value(html, "notes") == DRAFT_NOTES
    assert _field_value(html, "source_quote") == DRAFT_QUOTE
    assert DRAFT_NOTES in html


#: collect() 里**显式**处理的字段。其余一律要靠 data-submit 声明式透传。
EXPLICITLY_HANDLED = {"title", "category", "mode", "due_at", "priority"}


def _controls(item_html: str) -> list[tuple[str, str, str]]:
    """列出这段 HTML 里带 data-field 的控件：``(字段名, 完整标签, 值)``。

    三种控件的值位置不同（input 在属性里、textarea 在标签之间、select 在
    选中项上），所以要分开取。不用 HTML 解析库是刻意的：测试环境不装额外
    依赖，而这里的 HTML 是自己模板渲染出来的，形状可控。
    """
    found: list[tuple[str, str, str]] = []

    for tag in re.findall(r"<input[^>]*data-field=\"[a-z_]+\"[^>]*>", item_html):
        field = re.search(r'data-field="([a-z_]+)"', tag).group(1)
        value = re.search(r'value="([^"]*)"', tag)
        found.append((field, tag, value.group(1) if value else ""))

    for match in re.finditer(
        r"<textarea[^>]*data-field=\"(?P<field>[a-z_]+)\"[^>]*>(?P<value>.*?)</textarea>",
        item_html,
        re.S,
    ):
        found.append((match.group("field"), match.group(0), match.group("value")))

    for match in re.finditer(
        r"<select[^>]*data-field=\"(?P<field>[a-z_]+)\"[^>]*>(?P<body>.*?)</select>",
        item_html,
        re.S,
    ):
        body = match.group("body")
        chosen = re.search(r'<option value="([^"]*)"[^>]*\sselected', body)
        if chosen is None:
            chosen = re.search(r'<option value="([^"]*)"', body)
        found.append(
            (match.group("field"), match.group(0), chosen.group(1) if chosen else "")
        )

    return found


def _control(item_html: str, field: str) -> tuple[str, str]:
    """取某个字段的 ``(完整标签, 值)``；找不到就直接失败。

    找不到正是被测的行为：控件必须真的在页面上，collect() 才拿得到它的值。
    """
    for name, tag, value in _controls(item_html):
        if name == field:
            return tag, value
    raise AssertionError(f"没有 data-field={field} 的控件，draft.js 取不到它的值")


def simulate_collect(item_html: str) -> dict:
    """按 draft.js 的规则，从确认页 HTML 拼出一条 items 元素。

    关键是**忠实**而不是理想化：透传字段完全由 HTML 上的 ``data-submit``
    决定，测试里不另写一份字段名清单。所以"用户看到的值会不会被提交"
    这件事可以在 Python 侧验证——不需要跑浏览器。
    """
    entry: dict = {}
    for field, tag, value in _controls(item_html):
        if "data-submit" in tag:
            entry[field] = value

    # 这三个由 collect() 显式处理，测试里给出确定值即可
    entry["title"] = _control(item_html, "title")[1].strip()
    entry["category"] = _control(item_html, "category")[1]
    entry["due_at"] = None
    entry["priority"] = 3
    return entry


async def test_every_rendered_field_is_submitted_or_explicitly_handled(
    session_client: AsyncClient,
) -> None:
    """**这条是防"再次静默丢字段"的总闸。**

    确认页上的每个 data-field 控件，要么被 collect() 显式处理，要么带
    data-submit。两者都没有 ⇒ 用户在页面上看到、甚至能编辑的值会被丢掉，
    而且没有任何报错 —— notes 当初就是这么丢的。

    以后往核对页加字段时，漏了标记这条就会红。
    """
    draft_id = await ingest_draft_with_notes(session_client)
    html = (await session_client.get(f"/drafts/{draft_id}")).text
    item_html = html.split('<li class="draft-item')[1]

    controls = _controls(item_html)
    assert controls, "确认页上一个 data-field 控件都没有，用例失去意义"

    seen = {field for field, _tag, _value in controls}
    assert {"title", "notes", "source_quote"} <= seen, f"控件不全：{sorted(seen)}"

    for field, tag, _value in controls:
        if field in EXPLICITLY_HANDLED:
            assert "data-submit" not in tag, (
                f"{field} 由 collect() 显式处理，不该再标 data-submit —— "
                "那会让未规整的原始值覆盖掉处理后的值"
            )
        else:
            assert "data-submit" in tag, (
                f"{field} 既不在 EXPLICITLY_HANDLED 里、也没有 data-submit，"
                "用户在这个控件里的值会被静默丢弃"
            )


async def test_web_review_does_not_lose_notes(
    session_client: AsyncClient,
) -> None:
    """**回归用例：这是实际发生过的数据丢失。**

    原先确认页既不渲染 notes、collect() 也不回传它，于是"在网页上二次审核"
    这条路上，模型抽出来的备注被静默清空；而接口直接确认（不带 items）反而
    保留 —— 所以表现得很像"偶发"。

    这个用例刻意**先读页面、再按声明式规则拼 payload**，而不是直接写一个
    理想 payload：后者在字段缺失时会照样通过，正是它当初没被发现的原因。
    """
    draft_id = await ingest_draft_with_notes(session_client)
    html = (await session_client.get(f"/drafts/{draft_id}")).text
    item_html = html.split('<li class="draft-item')[1]

    payload = {"items": [simulate_collect(item_html)]}
    confirmed = await session_client.post(
        f"/api/ingest/{draft_id}/confirm", json=payload
    )
    assert confirmed.status_code == 200, confirmed.text

    item = confirmed.json()["items"][0]
    assert item["notes"] == DRAFT_NOTES
    assert item["source_quote"] == DRAFT_QUOTE

    # 真的落到了任务表里
    tasks = (await session_client.get("/api/tasks", params={"view": "unordered"})).json()
    assert [row["notes"] for row in tasks] == [DRAFT_NOTES]


async def test_switching_mode_keeps_notes(session_client: AsyncClient) -> None:
    """把「无序」改成「有序」（换 mode）之后确认，备注同样不能丢。

    这是用户实际操作的路径：调度完时间再确认。mode 切换只影响 due_at /
    priority 两个字段，不该波及透传字段。
    """
    draft_id = await ingest_draft_with_notes(session_client)
    html = (await session_client.get(f"/drafts/{draft_id}")).text
    item_html = html.split('<li class="draft-item')[1]

    entry = simulate_collect(item_html)
    entry["due_at"] = "2026-06-01T02:00:00+00:00"  # 相当于切到「截止时间」
    entry["priority"] = None

    confirmed = await session_client.post(
        f"/api/ingest/{draft_id}/confirm", json={"items": [entry]}
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["items"][0]["notes"] == DRAFT_NOTES

    ordered = (await session_client.get("/api/tasks", params={"view": "ordered"})).json()
    assert [row["notes"] for row in ordered] == [DRAFT_NOTES]


def test_client_collect_is_declarative() -> None:
    """源码级守卫：collect() 靠 data-submit 通用收集，不硬编码字段名。

    硬编码就是这次事故的根因 —— 模板加了字段、JS 没跟上，谁也不会发现。
    """
    script = DRAFT_JS.read_text(encoding="utf-8")
    block = script.split("function passthrough")[1].split("function collect()")[0]

    assert "[data-submit]" in block, "collect() 应当按 data-submit 通用收集"
    # 不得再出现 "notes:" 这类硬编码字段
    assert "notes:" not in script, "字段名不该写死在 JS 里"
    assert "source_quote:" not in script, "字段名不该写死在 JS 里"
