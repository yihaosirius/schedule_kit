"""M2：任务 CRUD、双视图排序与二选一约束。"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.services import tasks as task_service

from conftest import TEST_PASSWORD


async def _create(client: AsyncClient, **payload) -> dict:
    payload.setdefault("title", "任务")
    payload.setdefault("category", "homework")
    if "due_at" not in payload and "priority" not in payload:
        payload["priority"] = 3
    response = await client.post("/api/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 二选一约束
# --------------------------------------------------------------------------- #
async def test_create_with_deadline_only(session_client: AsyncClient) -> None:
    item = await _create(session_client, due_at="2026-03-27T15:59:00+00:00")
    assert item["due_at"] == "2026-03-27T15:59:00+00:00"
    assert item["priority"] is None


async def test_create_with_priority_only(session_client: AsyncClient) -> None:
    item = await _create(session_client, priority=2)
    assert item["priority"] == 2
    assert item["due_at"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"due_at": "2026-03-27T15:59:00+00:00", "priority": 3},
        {"due_at": None, "priority": None},
        {},
    ],
)
async def test_create_rejects_non_xor(session_client: AsyncClient, payload: dict) -> None:
    response = await session_client.post("/api/tasks", json={"title": "x", "category": "other", **payload})
    assert response.status_code == 422


@pytest.mark.parametrize("priority", [0, 6, -1, 99])
async def test_create_rejects_priority_out_of_range(session_client: AsyncClient, priority: int) -> None:
    response = await session_client.post(
        "/api/tasks", json={"title": "x", "category": "other", "priority": priority}
    )
    assert response.status_code == 422


async def test_create_rejects_empty_title(session_client: AsyncClient) -> None:
    response = await session_client.post(
        "/api/tasks", json={"title": "   ", "category": "other", "priority": 3}
    )
    assert response.status_code == 422


async def test_naive_datetime_is_interpreted_in_configured_timezone(
    session_client: AsyncClient,
) -> None:
    """无时区时间按 [server].timezone 解释（测试配置为 Asia/Shanghai = UTC+8）。"""
    item = await _create(session_client, due_at="2026-03-27T23:59:00")
    assert item["due_at"] == "2026-03-27T15:59:00+00:00"


# --------------------------------------------------------------------------- #
# 双视图排序
# --------------------------------------------------------------------------- #
async def test_ordered_view_sorts_by_due_at_ascending(session_client: AsyncClient) -> None:
    for due in ["2026-05-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00"]:
        await _create(session_client, title=due[:10], due_at=due)

    items = (await session_client.get("/api/tasks?view=ordered")).json()
    assert [i["due_at"] for i in items] == sorted(i["due_at"] for i in items)
    assert items[0]["due_at"].startswith("2026-03-01")


async def test_ordered_view_excludes_priority_only_items(session_client: AsyncClient) -> None:
    await _create(session_client, title="有截止", due_at="2026-03-01T00:00:00+00:00")
    await _create(session_client, title="只有优先级", priority=1)

    items = (await session_client.get("/api/tasks?view=ordered")).json()
    assert [i["title"] for i in items] == ["有截止"]


async def test_unordered_view_sorts_by_priority_then_creation(session_client: AsyncClient) -> None:
    for title, priority in [("丙", 3), ("甲", 1), ("乙", 2), ("甲二", 1)]:
        await _create(session_client, title=title, priority=priority)

    items = (await session_client.get("/api/tasks?view=unordered")).json()
    assert [i["priority"] for i in items] == [1, 1, 2, 3]
    # 同优先级按创建顺序
    assert [i["title"] for i in items][:2] == ["甲", "甲二"]


async def test_unordered_view_excludes_deadline_items(session_client: AsyncClient) -> None:
    await _create(session_client, title="有截止", due_at="2026-03-01T00:00:00+00:00")
    await _create(session_client, title="只有优先级", priority=4)

    items = (await session_client.get("/api/tasks?view=unordered")).json()
    assert [i["title"] for i in items] == ["只有优先级"]


async def test_view_filtering_and_limit(session_client: AsyncClient) -> None:
    await _create(session_client, title="A", priority=1, category="exam")
    await _create(session_client, title="B", priority=2, category="homework")
    await _create(session_client, title="C", priority=3, category="exam")

    exams = (await session_client.get("/api/tasks?view=unordered&category=exam")).json()
    assert {i["title"] for i in exams} == {"A", "C"}

    limited = (await session_client.get("/api/tasks?view=unordered&limit=2")).json()
    assert len(limited) == 2


async def test_status_filter_defaults_to_all_but_can_narrow(session_client: AsyncClient) -> None:
    first = await _create(session_client, title="done-me", priority=1)
    await _create(session_client, title="open-me", priority=2)
    await session_client.patch(f"/api/tasks/{first['id']}", json={"status": "done"})

    open_only = (await session_client.get("/api/tasks?view=unordered&status=open")).json()
    assert [i["title"] for i in open_only] == ["open-me"]

    done_only = (await session_client.get("/api/tasks?view=unordered&status=done")).json()
    assert [i["title"] for i in done_only] == ["done-me"]


# --------------------------------------------------------------------------- #
# 更新：deadline 胜出
# --------------------------------------------------------------------------- #
async def test_patching_due_at_clears_existing_priority(session_client: AsyncClient) -> None:
    item = await _create(session_client, priority=2)
    response = await session_client.patch(
        f"/api/tasks/{item['id']}", json={"due_at": "2026-06-01T00:00:00+00:00"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["due_at"] == "2026-06-01T00:00:00+00:00"
    assert body["priority"] is None, "提供截止时间后必须清空优先级"


async def test_patching_priority_clears_existing_due_at(session_client: AsyncClient) -> None:
    item = await _create(session_client, due_at="2026-06-01T00:00:00+00:00")
    response = await session_client.patch(f"/api/tasks/{item['id']}", json={"priority": 5})
    assert response.status_code == 200
    body = response.json()
    assert body["priority"] == 5
    assert body["due_at"] is None


async def test_clearing_deadline_without_priority_conflicts(session_client: AsyncClient) -> None:
    item = await _create(session_client, due_at="2026-06-01T00:00:00+00:00")
    response = await session_client.patch(f"/api/tasks/{item['id']}", json={"due_at": None})
    assert response.status_code == 409
    assert "优先级" in response.json()["detail"]


async def test_clearing_priority_without_deadline_conflicts(session_client: AsyncClient) -> None:
    item = await _create(session_client, priority=3)
    response = await session_client.patch(f"/api/tasks/{item['id']}", json={"priority": None})
    assert response.status_code == 409


async def test_switching_deadline_to_priority_in_one_request(session_client: AsyncClient) -> None:
    item = await _create(session_client, due_at="2026-06-01T00:00:00+00:00")
    response = await session_client.patch(
        f"/api/tasks/{item['id']}", json={"due_at": None, "priority": 4}
    )
    assert response.status_code == 200
    assert response.json()["priority"] == 4
    assert response.json()["due_at"] is None


async def test_patch_title_and_category_round_trip(session_client: AsyncClient) -> None:
    item = await _create(session_client, priority=3)
    response = await session_client.patch(
        f"/api/tasks/{item['id']}", json={"title": "改过的标题", "category": "exam"}
    )
    body = response.json()
    assert body["title"] == "改过的标题"
    assert body["category"] == "exam"
    assert body["priority"] == 3, "未提供的字段不应被改动"


async def test_completing_sets_completed_at(session_client: AsyncClient) -> None:
    item = await _create(session_client, priority=1)
    assert item["completed_at"] is None
    body = (await session_client.patch(f"/api/tasks/{item['id']}", json={"status": "done"})).json()
    assert body["completed_at"] is not None

    reopened = (
        await session_client.patch(f"/api/tasks/{item['id']}", json={"status": "open"})
    ).json()
    assert reopened["completed_at"] is None


async def test_patch_unknown_field_is_rejected(session_client: AsyncClient) -> None:
    item = await _create(session_client, priority=3)
    response = await session_client.patch(f"/api/tasks/{item['id']}", json={"nope": 1})
    assert response.status_code == 422


async def test_patch_missing_item_is_404(session_client: AsyncClient) -> None:
    response = await session_client.patch("/api/tasks/9999", json={"title": "x"})
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 幂等与删除
# --------------------------------------------------------------------------- #
async def test_client_uuid_makes_create_idempotent(session_client: AsyncClient) -> None:
    first = await session_client.post(
        "/api/tasks",
        json={"title": "重复提交", "category": "other", "priority": 3, "client_uuid": "abc-123"},
    )
    assert first.status_code == 201

    second = await session_client.post(
        "/api/tasks",
        json={"title": "重复提交", "category": "other", "priority": 3, "client_uuid": "abc-123"},
    )
    assert second.status_code == 200, "幂等命中应返回 200 而非 201"
    assert second.json()["id"] == first.json()["id"]

    items = (await session_client.get("/api/tasks?view=unordered")).json()
    assert len(items) == 1


async def test_delete_removes_item(session_client: AsyncClient) -> None:
    item = await _create(session_client, priority=3)
    response = await session_client.delete(f"/api/tasks/{item['id']}")
    assert response.status_code == 200
    assert response.json() == {"deleted": True, "id": item["id"]}
    assert (await session_client.get("/api/tasks?view=unordered")).json() == []


async def test_delete_missing_item_is_404(session_client: AsyncClient) -> None:
    assert (await session_client.delete("/api/tasks/9999")).status_code == 404


# --------------------------------------------------------------------------- #
# 鉴权与数据库不变量
# --------------------------------------------------------------------------- #
async def test_anonymous_cannot_read_or_write(client: AsyncClient) -> None:
    assert (await client.get("/api/tasks")).status_code == 401
    assert (await client.post("/api/tasks", json={"title": "x", "priority": 3})).status_code == 401


async def test_session_write_without_csrf_is_rejected(client: AsyncClient) -> None:
    await client.post("/api/login", json={"password": TEST_PASSWORD})
    response = await client.post("/api/tasks", json={"title": "x", "priority": 3})
    assert response.status_code == 403


async def test_database_xor_holds_after_updates(app) -> None:
    """不论走哪条更新路径，库里都不该出现"既有时间又有优先级"的行。"""
    db = app.state.db
    row, _ = task_service.create_item(db, _StubPayload(priority=2), timezone="Asia/Shanghai")
    task_service.update_item(
        db,
        row["id"],
        _StubUpdate(due_at="2026-06-01T00:00:00+00:00"),
        timezone="Asia/Shanghai",
    )
    with db.connect() as conn:
        stored = conn.execute(
            "SELECT due_at, priority FROM items WHERE id = ?", (row["id"],)
        ).fetchone()
    assert (stored["due_at"] is None) != (stored["priority"] is None)
    assert stored["due_at"] == "2026-06-01T00:00:00+00:00"
    assert stored["priority"] is None


async def test_service_accepts_naive_string_time(app) -> None:
    """service 层要能被非 HTTP 调用方直接用（草稿确认、测试、未来的 agent 工具）。"""
    db = app.state.db
    row, _ = task_service.create_item(
        db, _StubPayload(due_at="2026-03-27T23:59:00"), timezone="Asia/Shanghai"
    )
    assert row["due_at"] == "2026-03-27T15:59:00+00:00"


class _StubPayload:
    """直接调 service 时替代 Pydantic 模型的轻量桩。"""

    def __init__(self, **kwargs):
        self.title = kwargs.get("title", "stub")
        self.notes = kwargs.get("notes", "")
        self.category = kwargs.get("category", "other")
        self.due_at = kwargs.get("due_at")
        self.priority = kwargs.get("priority")
        self.client_uuid = kwargs.get("client_uuid")
        self.source = kwargs.get("source", "web")


class _StubUpdate:
    def __init__(self, **kwargs):
        self._fields = set(kwargs)
        for key, value in kwargs.items():
            setattr(self, key, value)

    @property
    def model_fields_set(self) -> set[str]:
        return self._fields

    def __getattr__(self, item):  # 未提供的字段返回 None
        return None
