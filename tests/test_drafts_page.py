"""草稿箱页面：筛选、分页、导航，以及"无 JS 也能用"。

面板是服务端渲染的，筛选和翻页都是链接——所以这些行为可以纯 HTTP 断言，
不需要跑浏览器。
"""

from __future__ import annotations

from httpx import AsyncClient

from tests.test_drafts_panel import make_draft, seed_drafts


async def test_drafts_page_requires_login(client: AsyncClient) -> None:
    response = await client.get("/drafts")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/drafts"


async def test_drafts_page_lists_drafts(session_client: AsyncClient) -> None:
    await make_draft(session_client, title="化学实验报告")
    html = (await session_client.get("/drafts")).text

    assert "草稿箱" in html
    assert "化学实验报告" in html
    assert "待确认" in html
    # 单列布局（草稿箱没有侧栏）
    assert 'class="layout"' in html
    assert "layout--split" not in html and "layout--settings" not in html


async def test_drafts_page_shows_all_status_tabs(session_client: AsyncClient) -> None:
    html = (await session_client.get("/drafts")).text
    for label in ("全部", "待确认", "已入库", "已丢弃", "识别失败"):
        assert label in html, f"筛选条里缺少「{label}」"


async def test_filter_tabs_carry_the_right_counts(
    app, session_client: AsyncClient
) -> None:
    """「全部」必须是各状态之和，否则筛选条自己就自相矛盾。"""
    await make_draft(session_client, title="待确认的")
    confirmed = await make_draft(session_client, title="会入库的")
    await session_client.post(f"/api/ingest/{confirmed}/confirm")
    discarded = await make_draft(session_client, title="会丢掉的")
    await session_client.post(f"/api/ingest/{discarded}/discard")

    html = (await session_client.get("/drafts")).text
    assert "全部 3" in html
    assert "待确认 1" in html
    assert "已入库 1" in html
    assert "已丢弃 1" in html
    assert "识别失败 0" in html, "计数为 0 的状态也要显示出来，否则用户以为没这个分类"


async def test_drafts_page_filters_by_status(session_client: AsyncClient) -> None:
    keep = await make_draft(session_client, title="还留着的")
    gone = await make_draft(session_client, title="已经丢掉的")
    await session_client.post(f"/api/ingest/{gone}/discard")

    pending = (await session_client.get("/drafts", params={"status": "pending"})).text
    assert "还留着的" in pending
    assert "已经丢掉的" not in pending

    discarded = (await session_client.get("/drafts", params={"status": "discarded"})).text
    assert "已经丢掉的" in discarded
    assert "还留着的" not in discarded


async def test_drafts_page_treats_an_unknown_status_as_no_filter(
    session_client: AsyncClient,
) -> None:
    """API 上未知 status 是 422，但页面上给个空列表会让人以为草稿丢了。"""
    await make_draft(session_client, title="别藏起来")
    html = (await session_client.get("/drafts", params={"status": "bogus"})).text
    assert "别藏起来" in html


async def test_drafts_page_pages_through_results(app, session_client: AsyncClient) -> None:
    # 直接落库：走接口建 30 条会撞录入限流（10 次/分钟）
    seed_drafts(app, 30)

    first = (await session_client.get("/drafts")).text
    # 每页 25 条：第 1 页有 25 行，并且给出下一页链接
    assert first.count('name="ids"') == 25
    assert "下一页" in first
    assert "上一页" in first

    second = (await session_client.get("/drafts", params={"offset": 25})).text
    assert second.count('name="ids"') == 5


async def test_drafts_page_empty_state(session_client: AsyncClient) -> None:
    html = (await session_client.get("/drafts")).text
    assert "草稿箱是空的" in html
    # 没有草稿时不该出现批量删除表单
    assert "data-drafts-form" not in html


async def test_drafts_page_marks_itself_in_the_nav(session_client: AsyncClient) -> None:
    await make_draft(session_client)
    html = (await session_client.get("/drafts")).text
    assert 'href="/drafts"' in html
    # 当前页要标出来，且首页入口显示成「← 主页」
    assert "← 主页" in html


async def test_confirm_page_marks_the_draft_box_as_current(
    session_client: AsyncClient,
) -> None:
    """确认页也属于草稿箱这一支，否则用户在那儿看不出自己在哪、也点不回去。"""
    draft_id = await make_draft(session_client)
    html = (await session_client.get(f"/drafts/{draft_id}")).text
    assert 'href="/drafts"' in html
    assert "草稿箱" in html


async def test_confirmed_draft_page_links_back_to_the_box(
    session_client: AsyncClient,
) -> None:
    draft_id = await make_draft(session_client)
    await session_client.post(f"/api/ingest/{draft_id}/confirm")
    html = (await session_client.get(f"/drafts/{draft_id}")).text
    assert "回草稿箱" in html


async def test_confirmed_row_warns_that_tasks_are_unaffected(
    session_client: AsyncClient,
) -> None:
    """这是用户点删除前最需要知道的一句话。"""
    draft_id = await make_draft(session_client, title="会被确认的")
    confirmed = await session_client.post(f"/api/ingest/{draft_id}/confirm")
    created = confirmed.json()["created_item_ids"]

    html = (await session_client.get("/drafts")).text
    assert "不会影响它们" in html
    for item_id in created:
        assert f"已创建任务 {item_id}" in html


async def test_intro_states_that_images_are_not_deleted(
    session_client: AsyncClient,
) -> None:
    await make_draft(session_client)
    html = (await session_client.get("/drafts")).text
    assert "不会删掉图片文件" in html
