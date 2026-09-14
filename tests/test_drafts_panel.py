"""草稿箱：列表、永久删除，以及两条**绝不能破的不变量**。

这个面板唯一的破坏性动作是删草稿行，而它周围有两样东西很容易被"顺手"删掉：

1. **图片文件。** ``media.store_image`` 按内容哈希命名且已存在就不重写，
   所以同一天上传的两张相同图片共用同一个文件；已确认的草稿更是永久保留
   ``image_path``。删草稿时 unlink 图片 = 删掉别人的图。
2. **已入库的任务。** ``items`` 与 ``ingest_drafts`` 之间没有外键，
   ``created_item_ids`` 只是个 JSON 数组。删草稿绝不能连任务一起删。

这两条各有用例，且都用"另一个对象仍然可用"来断言，而不是只断言删掉了什么。
"""

from __future__ import annotations

from httpx import AsyncClient

from app.services import drafts as draft_service

from tests.test_ingest import image_ingest, make_jpeg, use_llm


def item(title: str = "事项", **overrides) -> dict:
    base = {"title": title, "category": "other", "due_at": None, "priority": 3}
    base.update(overrides)
    return base


async def make_draft(client: AsyncClient, *, title: str = "草稿事项", text: str | None = None) -> int:
    """走真实接口建一条 pending 草稿（走客户端直供 items 那条路，不调 LLM）。"""
    payload: dict = {"channel": "text", "items": [item(title)]}
    if text is not None:
        payload["text"] = text
    response = await client.post("/api/ingest", json=payload)
    assert response.status_code == 201, response.text
    return int(response.json()["draft_id"])


def seed_drafts(app, count: int, *, title_prefix: str = "种子") -> list[int]:
    """直接落库建草稿，返回 id 列表（按创建顺序）。

    批量用例**不能**走 ``POST /api/ingest``：录入限流是 10 次/分钟，建 30 条
    会直接撞 429，而失败原因看起来跟被测逻辑毫无关系。
    """
    ids: list[int] = []
    for index in range(count):
        row = draft_service.create_draft(
            app.state.db,
            channel="text",
            items=[item(f"{title_prefix} {index:02d}")],
            ttl_hours=48,
            llm_provider="client",
        )
        ids.append(int(row["id"]))
    return ids


def count_items(app) -> int:
    with app.state.db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]


def draft_row(app, draft_id: int):
    return draft_service.get_draft(app.state.db, draft_id)


# --------------------------------------------------------------------------- #
# 不变量①：删草稿不动已入库的任务
# --------------------------------------------------------------------------- #
async def test_purge_leaves_confirmed_tasks_alone(app, session_client: AsyncClient) -> None:
    draft_id = await make_draft(session_client, title="要被删掉的草稿")
    confirmed = await session_client.post(f"/api/ingest/{draft_id}/confirm")
    assert confirmed.status_code == 200
    created = confirmed.json()["created_item_ids"]
    assert count_items(app) == 1

    purged = await session_client.post("/api/ingest/purge", json={"ids": [draft_id]})
    assert purged.status_code == 200, purged.text
    assert purged.json()["deleted"] == 1

    # 草稿没了
    assert draft_row(app, draft_id) is None
    # 任务还在
    assert count_items(app) == 1
    listed = (await session_client.get("/api/tasks", params={"view": "unordered"})).json()
    assert [row["id"] for row in listed] == created


# --------------------------------------------------------------------------- #
# 不变量②：删草稿不动图片文件（同内容图片由多份草稿共用）
# --------------------------------------------------------------------------- #
async def test_purge_does_not_delete_image_files(app, session_client: AsyncClient) -> None:
    use_llm(app, [item("图里的作业")])
    _, first = await image_ingest(session_client)
    assert first["image_path"] or first["draft_id"]

    image_url = first["image_url"]
    assert (await session_client.get(image_url)).status_code == 200

    purged = await session_client.post(
        "/api/ingest/purge", json={"ids": [first["draft_id"]]}
    )
    assert purged.json()["deleted"] == 1

    # 图片文件仍在磁盘上（保留策略管它，不归草稿删除管）
    uploads = app.state.config.server.data_dir / "uploads"
    files = [path for path in uploads.rglob("*") if path.is_file()]
    assert files, "删草稿把图片文件一起删了——同内容的其它草稿会跟着坏掉"


async def test_two_drafts_sharing_one_image_stay_readable(
    app, session_client: AsyncClient
) -> None:
    """同一天上传两次完全相同的图片 → 复用同一个文件。

    删掉其中一条草稿后，另一条的 ``/image`` 必须照常返回。
    只断言"文件还在"不够——要断言**另一个草稿仍然能取到图**。
    """
    use_llm(app, [item("作业")])
    data = make_jpeg()

    _, first = await image_ingest(session_client, data)
    _, second = await image_ingest(session_client, data)

    row_first = draft_row(app, first["draft_id"])
    row_second = draft_row(app, second["draft_id"])
    assert row_first["image_path"] == row_second["image_path"], (
        "同内容图片应当共用同一个文件，否则这条用例测不到共用场景"
    )

    purged = await session_client.post(
        "/api/ingest/purge", json={"ids": [first["draft_id"]]}
    )
    assert purged.json()["deleted"] == 1

    response = await session_client.get(f"/api/ingest/{second['draft_id']}/image")
    assert response.status_code == 200
    assert response.content.startswith(b"\xff\xd8\xff")


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
async def test_list_returns_the_documented_shape(session_client: AsyncClient) -> None:
    await make_draft(session_client, title="第一条")
    response = await session_client.get("/api/ingest")
    assert response.status_code == 200
    body = response.json()

    assert set(body) == {"drafts", "total", "limit", "offset", "counts"}
    assert body["total"] == 1
    assert body["limit"] == 50 and body["offset"] == 0
    # counts 恒含四个状态，筛选标签不需要自己折算
    assert set(body["counts"]) == {"pending", "confirmed", "discarded", "failed"}
    assert body["counts"]["pending"] == 1

    entry = body["drafts"][0]
    assert entry["status"] == "pending"
    assert entry["item_count"] == 1
    assert entry["preview"] == "第一条"
    # 轻量表示：不带 items / context_snapshot
    assert "items" not in entry
    assert "context_snapshot" not in entry


async def test_list_is_newest_first_and_pages(session_client: AsyncClient) -> None:
    ids = [await make_draft(session_client, title=f"第 {n} 条") for n in range(5)]

    first_page = (await session_client.get("/api/ingest", params={"limit": 2})).json()
    assert [row["draft_id"] for row in first_page["drafts"]] == [ids[4], ids[3]]
    # total 是过滤后的总数，不是本页条数
    assert first_page["total"] == 5

    second_page = (
        await session_client.get("/api/ingest", params={"limit": 2, "offset": 2})
    ).json()
    assert [row["draft_id"] for row in second_page["drafts"]] == [ids[2], ids[1]]
    assert second_page["offset"] == 2


async def test_list_filters_by_status(session_client: AsyncClient) -> None:
    keep = await make_draft(session_client, title="留着")
    gone = await make_draft(session_client, title="丢掉")
    await session_client.post(f"/api/ingest/{gone}/discard")

    body = (await session_client.get("/api/ingest", params={"status": "discarded"})).json()
    assert [row["draft_id"] for row in body["drafts"]] == [gone]
    assert body["total"] == 1
    # counts 仍然是全量的
    assert body["counts"]["pending"] == 1
    assert body["counts"]["discarded"] == 1

    pending = (await session_client.get("/api/ingest", params={"status": "pending"})).json()
    assert [row["draft_id"] for row in pending["drafts"]] == [keep]


async def test_list_rejects_an_unknown_status(session_client: AsyncClient) -> None:
    """API 上是 422；页面上则退化成"不筛选"（见 test_drafts_page）。"""
    response = await session_client.get("/api/ingest", params={"status": "bogus"})
    assert response.status_code == 422


async def test_list_needs_a_credential(client: AsyncClient) -> None:
    assert (await client.get("/api/ingest")).status_code == 401


async def test_list_is_readable_with_a_read_only_key(
    session_client: AsyncClient, app
) -> None:
    """列表是读操作，只读密钥应当能用——不然小组件没法展示待确认数。"""
    created = await session_client.post("/api/keys", json={"name": "ro", "read_only": True})
    key = created.json()["key"]
    response = await session_client.get("/api/ingest", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200
    assert "drafts" in response.json()


# --------------------------------------------------------------------------- #
# 永久删除
# --------------------------------------------------------------------------- #
async def test_purge_deletes_only_the_given_ids(
    app, session_client: AsyncClient
) -> None:
    first = await make_draft(session_client, title="删我")
    second = await make_draft(session_client, title="留我")

    body = (
        await session_client.post("/api/ingest/purge", json={"ids": [first]})
    ).json()
    assert body == {"deleted": 1, "ids": [first], "missing": []}

    assert draft_row(app, first) is None
    assert draft_row(app, second) is not None


async def test_purge_reports_missing_ids_instead_of_failing(
    app, session_client: AsyncClient
) -> None:
    """重复提交不该报错——重试和并发点击都会走到这里。"""
    draft_id = await make_draft(session_client)
    first = (await session_client.post("/api/ingest/purge", json={"ids": [draft_id]})).json()
    assert first["deleted"] == 1

    again = (
        await session_client.post("/api/ingest/purge", json={"ids": [draft_id, 999999]})
    ).json()
    assert again["deleted"] == 0
    assert again["missing"] == [draft_id, 999999]


async def test_purge_can_remove_a_pending_draft(app, session_client: AsyncClient) -> None:
    """待确认的草稿也能直接删——不是只有终态才能清。"""
    draft_id = await make_draft(session_client)
    body = (await session_client.post("/api/ingest/purge", json={"ids": [draft_id]})).json()
    assert body["deleted"] == 1
    assert draft_row(app, draft_id) is None


async def test_purge_rejects_an_empty_id_list(session_client: AsyncClient) -> None:
    response = await session_client.post("/api/ingest/purge", json={"ids": []})
    assert response.status_code == 422


async def test_purge_rejects_an_oversized_batch(session_client: AsyncClient) -> None:
    """一次最多 200 条：再多就该怀疑调用方把 id 搞错了。"""
    response = await session_client.post(
        "/api/ingest/purge", json={"ids": list(range(1, 202))}
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# 永久删除的权限：只认网页会话
# --------------------------------------------------------------------------- #
async def test_purge_is_refused_for_a_write_api_key(
    session_client: AsyncClient, app
) -> None:
    """读写密钥能丢弃草稿，但**不能永久删除**。

    删除不可恢复，而草稿行里有 error / llm_raw 这些排查线索——它属于控制台。
    丢弃仍然对密钥开放，因为它只改状态、留痕。
    """
    draft_id = await make_draft(session_client)
    key = (
        await session_client.post("/api/keys", json={"name": "rw", "read_only": False})
    ).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}

    # 丢弃可以
    assert (
        await session_client.post(f"/api/ingest/{draft_id}/discard", headers=headers)
    ).status_code == 200
    # 永久删除不行
    refused = await session_client.post(
        "/api/ingest/purge", headers=headers, json={"ids": [draft_id]}
    )
    assert refused.status_code == 403
    assert "仅限网页会话" in refused.json()["detail"]
    # 而且真的没删掉
    assert draft_row(app, draft_id) is not None


async def test_purge_needs_csrf_on_a_session(app, session_client: AsyncClient) -> None:
    from app.security import CSRF_HEADER

    draft_id = await make_draft(session_client)
    saved = session_client.headers.pop(CSRF_HEADER)
    try:
        response = await session_client.post("/api/ingest/purge", json={"ids": [draft_id]})
    finally:
        session_client.headers[CSRF_HEADER] = saved

    assert response.status_code == 403
    assert "X-CSRF-Token" in response.json()["detail"]
    assert draft_row(app, draft_id) is not None


# --------------------------------------------------------------------------- #
# 服务层
# --------------------------------------------------------------------------- #
def test_status_counts_always_has_all_four_keys(app) -> None:
    counts = draft_service.status_counts(app.state.db)
    assert counts == {"pending": 0, "confirmed": 0, "discarded": 0, "failed": 0}


def test_purge_drafts_with_no_ids_is_a_noop(app) -> None:
    assert draft_service.purge_drafts(app.state.db, []) == ([], [])


async def test_purge_drafts_deduplicates_ids(app, session_client: AsyncClient) -> None:
    """同一 id 传两次不该被算成两条已删除。"""
    draft_id = await make_draft(session_client)
    deleted, missing = draft_service.purge_drafts(app.state.db, [draft_id, draft_id])
    assert deleted == [draft_id]
    assert missing == []


async def test_purge_deletes_by_id_precisely(app, session_client: AsyncClient) -> None:
    """服务层直接调用时也要精确按 id 删，不能顺手清掉"看起来没用"的行。"""
    keep = await make_draft(session_client, title="留着")
    drop = await make_draft(session_client, title="删掉")

    deleted, missing = draft_service.purge_drafts(app.state.db, [drop])
    assert (deleted, missing) == ([drop], [])
    assert draft_service.get_draft(app.state.db, keep) is not None
    assert draft_service.get_draft(app.state.db, drop) is None
