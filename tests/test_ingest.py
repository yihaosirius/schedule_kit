"""M4：LLM function calling 两阶段录入。

最重要的一组断言是"**草稿阶段 items 表零写入**"：模型出错、没调工具、
用户改主意，都不该留下脏数据。
"""

from __future__ import annotations

import base64
import io

import pytest
from httpx import AsyncClient
from PIL import Image

from app.llm.base import (
    PATH_JSON_FALLBACK,
    PATH_TOOL_CALL,
    LLMRequestFailed,
    LLMToolCallMissing,
)
from app.llm.mock import MockLLM
from app.services import normalize as normalize_service


def make_jpeg(width: int = 8, height: int = 8) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


def make_heic_like() -> bytes:
    """伪造一个 ISO-BMFF 头，足以触发 HEIC 分支。"""
    return b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1"


def use_llm(app, items=None, *, error: Exception | None = None, **kwargs) -> MockLLM:
    llm = MockLLM(items=items, error=error, **kwargs)
    app.state.llm = llm
    return llm


async def image_ingest(client: AsyncClient, data: bytes | None = None) -> tuple[int, dict]:
    payload = {
        "channel": "image",
        "image_base64": base64.b64encode(data if data is not None else make_jpeg()).decode(),
        "mime": "image/jpeg",
    }
    response = await client.post("/api/ingest", json=payload)
    return response.status_code, (response.json() if response.content else {})


def count_items(app) -> int:
    with app.state.db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]


# --------------------------------------------------------------------------- #
# 后处理：不信任模型输出
# --------------------------------------------------------------------------- #
def test_normalize_drops_priority_when_deadline_present() -> None:
    """模型同时给出 deadline 与 priority 时，deadline 胜出。"""
    result = normalize_service.normalize_items(
        [{"title": "交作业", "category": "homework", "due_at": "2026-04-01T00:00:00+00:00", "priority": 2}],
        timezone="Asia/Shanghai",
    )
    assert len(result) == 1
    assert result[0]["due_at"] == "2026-04-01T00:00:00+00:00"
    assert result[0]["priority"] is None
    assert "priority-dropped" in result[0]["adjustments"]


def test_normalize_defaults_priority_when_neither_given() -> None:
    result = normalize_service.normalize_items(
        [{"title": "买纸", "category": "other", "due_at": None, "priority": None}],
        timezone="Asia/Shanghai",
    )
    assert result[0]["priority"] == 3
    assert result[0]["needs_priority"] is True


def test_normalize_keeps_model_priority_when_no_deadline() -> None:
    result = normalize_service.normalize_items(
        [{"title": "买纸", "category": "other", "due_at": None, "priority": 5}],
        timezone="Asia/Shanghai",
    )
    assert result[0]["priority"] == 5
    assert result[0]["needs_priority"] is False


@pytest.mark.parametrize(
    ("raw_priority", "expected"),
    [(0, 1), (9, 5), (-3, 1), (100, 5), ("4", 4)],
)
def test_normalize_clamps_priority(raw_priority, expected: int) -> None:
    result = normalize_service.normalize_items(
        [{"title": "x", "category": "other", "due_at": None, "priority": raw_priority}],
        timezone="Asia/Shanghai",
    )
    assert result[0]["priority"] == expected


def test_normalize_downgrades_unknown_category() -> None:
    result = normalize_service.normalize_items(
        [{"title": "x", "category": "购物", "due_at": None, "priority": 3}],
        timezone="Asia/Shanghai",
    )
    assert result[0]["category"] == "other"
    assert "category-downgraded" in result[0]["adjustments"]


def test_normalize_drops_empty_titles() -> None:
    result = normalize_service.normalize_items(
        [
            {"title": "   ", "category": "other", "due_at": None, "priority": 3},
            {"title": "保留", "category": "other", "due_at": None, "priority": 3},
        ],
        timezone="Asia/Shanghai",
    )
    assert [item["title"] for item in result] == ["保留"]


def test_normalize_interprets_naive_time_in_configured_timezone() -> None:
    result = normalize_service.normalize_items(
        [{"title": "x", "category": "other", "due_at": "2026-03-27T23:59:00", "priority": None}],
        timezone="Asia/Shanghai",
    )
    assert result[0]["due_at"] == "2026-03-27T15:59:00+00:00"


def test_normalize_survives_unparsable_time() -> None:
    result = normalize_service.normalize_items(
        [{"title": "x", "category": "other", "due_at": "下周五", "priority": None}],
        timezone="Asia/Shanghai",
    )
    assert result[0]["due_at"] is None
    assert result[0]["priority"] == 3, "时间解析失败且无优先级时应落回默认档"
    assert "due-unparsable" in result[0]["adjustments"]


def test_normalize_caps_item_count() -> None:
    raw = [{"title": f"t{i}", "category": "other", "due_at": None, "priority": 3} for i in range(50)]
    assert len(normalize_service.normalize_items(raw, timezone="Asia/Shanghai")) == 20


def test_normalize_handles_empty_input() -> None:
    assert normalize_service.normalize_items([], timezone="Asia/Shanghai") == []


# --------------------------------------------------------------------------- #
# 草稿阶段零写入
# --------------------------------------------------------------------------- #
async def test_image_ingest_creates_draft_without_touching_items(
    app, session_client: AsyncClient
) -> None:
    use_llm(app, [{"title": "交实验报告", "category": "homework", "due_at": None, "priority": 2}])

    status, body = await image_ingest(session_client)
    assert status == 201, body
    assert body["status"] == "pending"
    assert len(body["items"]) == 1
    assert body["items"][0]["title"] == "交实验报告"
    assert body["confirm_url"].endswith(f"/drafts/{body['draft_id']}")
    assert body["image_url"] == f"/api/ingest/{body['draft_id']}/image"
    assert count_items(app) == 0, "草稿阶段绝不能写 items 表"


async def test_context_snapshot_is_recorded(app, session_client: AsyncClient) -> None:
    use_llm(app, [{"title": "x", "category": "other", "due_at": None, "priority": 3}])
    _, body = await image_ingest(session_client)
    assert "[当前时间]" in body["context_snapshot"]

    # 上下文必须进 user message，而不是 system prompt（否则 prompt 缓存永远失效）。
    # 注意用带方括号的标记做断言：system prompt 的规则文字里本来就有"当前时间"这四个字。
    call = app.state.llm.calls[0]
    assert "[当前时间]" in call["user_text"]
    assert "[当前时间]" not in call["system"]


async def test_empty_extraction_still_produces_a_draft(app, session_client: AsyncClient) -> None:
    use_llm(app, [])
    status, body = await image_ingest(session_client)
    assert status == 201
    assert body["items"] == []


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #
async def test_llm_failure_leaves_failed_draft_and_no_items(
    app, session_client: AsyncClient
) -> None:
    use_llm(app, error=LLMRequestFailed("连接超时"))
    status, body = await image_ingest(session_client)

    assert status == 502
    assert body["detail"]["retryable"] is True
    draft_id = body["detail"]["draft_id"]
    assert count_items(app) == 0

    draft = (await session_client.get(f"/api/ingest/{draft_id}")).json()
    assert draft["status"] == "failed"
    assert "连接超时" in draft["error"]


async def test_missing_tool_call_is_a_failure_not_a_text_parse(
    app, session_client: AsyncClient
) -> None:
    use_llm(app, error=LLMToolCallMissing("模型没有调用 submit_tasks"))
    status, body = await image_ingest(session_client)
    assert status == 502
    assert "submit_tasks" in str(body["detail"]["message"])


async def test_ingest_reports_clearly_when_llm_unconfigured(
    app, session_client: AsyncClient
) -> None:
    app.state.llm = None
    app.state.llm_error = "不支持的 LLM provider：'gemini'"
    status, body = await image_ingest(session_client)
    assert status == 503
    assert "gemini" in body["detail"]


# --------------------------------------------------------------------------- #
# 降级通道要"响亮"
# --------------------------------------------------------------------------- #
async def test_json_fallback_is_recorded_and_surfaced(
    app, session_client: AsyncClient
) -> None:
    """走到 JSON 降级通道时，草稿必须把它记下来，确认页也必须显示出来。

    悄悄降级的话，"模型开始不按工具调用返回"这件事会被无声吞掉，
    等发现时已经不知道退化了多久。
    """
    use_llm(
        app,
        [{"title": "交作业", "category": "homework", "due_at": None, "priority": 3}],
        path=PATH_JSON_FALLBACK,
        fallback_note="响应里没有 function_call",
    )
    status, body = await image_ingest(session_client)
    assert status == 201
    assert body["llm_path"] == PATH_JSON_FALLBACK

    # 存进库了，重新读也还在
    draft = (await session_client.get(f"/api/ingest/{body['draft_id']}")).json()
    assert draft["llm_path"] == PATH_JSON_FALLBACK

    # 确认页上有醒目提示
    page = await session_client.get(f"/drafts/{body['draft_id']}")
    assert page.status_code == 200
    assert "JSON 降级通道" in page.text


async def test_normal_path_is_not_flagged_as_fallback(
    app, session_client: AsyncClient
) -> None:
    use_llm(app, [{"title": "交作业", "category": "homework", "due_at": None, "priority": 3}])
    status, body = await image_ingest(session_client)
    assert status == 201
    assert body["llm_path"] == PATH_TOOL_CALL

    page = await session_client.get(f"/drafts/{body['draft_id']}")
    assert "降级通道" not in page.text


# --------------------------------------------------------------------------- #
# 图片校验
# --------------------------------------------------------------------------- #
async def test_heic_is_rejected_with_actionable_hint(session_client: AsyncClient) -> None:
    status, body = await image_ingest(session_client, make_heic_like())
    assert status == 415
    assert "HEIC" in body["detail"]
    assert "转换图像" in body["detail"], "错误信息要能直接指导用户在快捷指令里怎么改"


async def test_oversized_image_is_rejected(session_client: AsyncClient) -> None:
    status, body = await image_ingest(session_client, b"\xff\xd8\xff" + b"x" * (9 * 1024 * 1024))
    assert status == 413


async def test_non_image_bytes_are_rejected(session_client: AsyncClient) -> None:
    status, body = await image_ingest(session_client, b"not an image at all")
    assert status == 415


async def test_invalid_base64_is_rejected(session_client: AsyncClient) -> None:
    response = await session_client.post(
        "/api/ingest", json={"channel": "image", "image_base64": "!!!not-base64!!!"}
    )
    assert response.status_code == 422


async def test_image_endpoint_serves_the_uploaded_file(
    app, session_client: AsyncClient
) -> None:
    use_llm(app, [])
    _, body = await image_ingest(session_client)
    response = await session_client.get(body["image_url"])
    assert response.status_code == 200
    assert response.content.startswith(b"\xff\xd8\xff")


# --------------------------------------------------------------------------- #
# 客户端已结构化 → 不调 LLM
# --------------------------------------------------------------------------- #
async def test_structured_items_skip_the_llm(app, session_client: AsyncClient) -> None:
    llm = use_llm(app, [])
    response = await session_client.post(
        "/api/ingest",
        json={
            "channel": "text",
            "text": "由客户端改好的内容",
            "items": [{"title": "客户端给的", "category": "exam", "due_at": None, "priority": 1}],
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["items"][0]["title"] == "客户端给的"
    assert llm.calls == [], "已结构化的输入不应再调用 LLM"
    assert count_items(app) == 0


async def test_structured_items_are_validated(app, session_client: AsyncClient) -> None:
    use_llm(app, [])
    response = await session_client.post(
        "/api/ingest",
        json={
            "channel": "text",
            "items": [{"title": "x", "category": "other", "due_at": None, "priority": None}],
        },
    )
    assert response.status_code == 422
    assert "二选一" in response.json()["detail"]


async def test_text_channel_uses_the_llm(app, session_client: AsyncClient) -> None:
    llm = use_llm(app, [{"title": "从文本抽取", "category": "other", "due_at": None, "priority": 3}])
    response = await session_client.post(
        "/api/ingest", json={"channel": "text", "text": "明天下午三点开会"}
    )
    assert response.status_code == 201
    assert response.json()["items"][0]["title"] == "从文本抽取"
    assert "明天下午三点开会" in llm.calls[0]["user_text"]


# --------------------------------------------------------------------------- #
# 确认
# --------------------------------------------------------------------------- #
async def test_confirm_creates_items_and_marks_draft(app, session_client: AsyncClient) -> None:
    use_llm(app, [{"title": "交实验报告", "category": "homework", "due_at": None, "priority": 2}])
    _, draft = await image_ingest(session_client)

    response = await session_client.post(f"/api/ingest/{draft['draft_id']}/confirm")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmed"
    assert len(body["created_item_ids"]) == 1
    assert count_items(app) == 1

    tasks = (await session_client.get("/api/tasks?view=unordered")).json()
    assert tasks[0]["title"] == "交实验报告"
    assert tasks[0]["source"] == "llm"


async def test_confirm_uses_client_edited_items(app, session_client: AsyncClient) -> None:
    use_llm(app, [{"title": "原标题", "category": "other", "due_at": None, "priority": 3}])
    _, draft = await image_ingest(session_client)

    response = await session_client.post(
        f"/api/ingest/{draft['draft_id']}/confirm",
        json={
            "items": [
                {"title": "改过的标题", "category": "exam", "due_at": "2026-06-01T00:00:00+00:00", "priority": None}
            ]
        },
    )
    assert response.status_code == 200
    tasks = (await session_client.get("/api/tasks?view=ordered")).json()
    assert len(tasks) == 1
    assert tasks[0]["title"] == "改过的标题"
    assert tasks[0]["category"] == "exam"
    assert tasks[0]["priority"] is None


async def test_confirm_is_not_repeatable(app, session_client: AsyncClient) -> None:
    use_llm(app, [{"title": "x", "category": "other", "due_at": None, "priority": 3}])
    _, draft = await image_ingest(session_client)

    assert (await session_client.post(f"/api/ingest/{draft['draft_id']}/confirm")).status_code == 200
    second = await session_client.post(f"/api/ingest/{draft['draft_id']}/confirm")
    assert second.status_code == 409
    assert count_items(app) == 1, "重复确认绝不能创建第二条"


async def test_confirm_rejects_invalid_edits(app, session_client: AsyncClient) -> None:
    use_llm(app, [{"title": "x", "category": "other", "due_at": None, "priority": 3}])
    _, draft = await image_ingest(session_client)

    response = await session_client.post(
        f"/api/ingest/{draft['draft_id']}/confirm",
        json={"items": [{"title": "x", "category": "other", "due_at": None, "priority": 3, "extra": 1}]},
    )
    # extra 字段被忽略（我们只取白名单字段），关键是仍然二选一成立
    assert response.status_code == 200

    bad = await session_client.post(
        f"/api/ingest/{draft['draft_id']}/confirm",
        json={"items": [{"title": "x", "category": "other", "due_at": "2026-01-01T00:00:00+00:00", "priority": 3}]},
    )
    assert bad.status_code == 409, "草稿已确认，后续请求应先被状态挡住"


async def test_multi_item_draft_is_atomic_and_all_created(app, session_client: AsyncClient) -> None:
    use_llm(
        app,
        [
            {"title": "A", "category": "homework", "due_at": "2026-04-01T00:00:00+00:00", "priority": 1},
            {"title": "B", "category": "exam", "due_at": None, "priority": 2},
            {"title": "C", "category": "other", "due_at": None, "priority": None},
        ],
    )
    _, draft = await image_ingest(session_client)
    assert len(draft["items"]) == 3

    response = await session_client.post(f"/api/ingest/{draft['draft_id']}/confirm")
    assert response.status_code == 200
    assert len(response.json()["created_item_ids"]) == 3
    assert count_items(app) == 3


async def test_discard_leaves_no_items(app, session_client: AsyncClient) -> None:
    use_llm(app, [{"title": "x", "category": "other", "due_at": None, "priority": 3}])
    _, draft = await image_ingest(session_client)

    response = await session_client.post(f"/api/ingest/{draft['draft_id']}/discard")
    assert response.status_code == 200
    assert response.json()["status"] == "discarded"
    assert count_items(app) == 0

    blocked = await session_client.post(f"/api/ingest/{draft['draft_id']}/confirm")
    assert blocked.status_code == 409


async def test_confirm_unknown_draft_is_404(session_client: AsyncClient) -> None:
    assert (await session_client.post("/api/ingest/9999/confirm")).status_code == 404


# --------------------------------------------------------------------------- #
# 鉴权与限流
# --------------------------------------------------------------------------- #
async def test_ingest_requires_write_access(app, client: AsyncClient, config) -> None:
    from app.db import Database
    from app.security import generate_api_key, hash_api_key
    from app.timeutil import utc_iso

    db = Database(config.server.data_dir / "schedulekit.db")
    raw = generate_api_key()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO api_keys(name, key_hash, read_only, created_at) VALUES (?, ?, 1, ?)",
            ("widget", hash_api_key(raw), utc_iso()),
        )

    use_llm(app, [])
    payload = {"channel": "text", "text": "x"}
    assert (await client.post("/api/ingest", json=payload)).status_code == 401
    assert (
        await client.post("/api/ingest", json=payload, headers={"Authorization": f"Bearer {raw}"})
    ).status_code == 403


async def test_ingest_is_rate_limited(app, session_client: AsyncClient) -> None:
    use_llm(app, [])
    statuses = [
        (await session_client.post("/api/ingest", json={"channel": "text", "text": "x"})).status_code
        for _ in range(11)
    ]
    assert statuses[-1] == 429
    assert statuses[0] == 201


# --------------------------------------------------------------------------- #
# 确认页
# --------------------------------------------------------------------------- #
async def test_draft_page_renders_items_and_context(app, session_client: AsyncClient) -> None:
    use_llm(app, [{"title": "交实验报告", "category": "homework", "due_at": None, "priority": 2}])
    _, draft = await image_ingest(session_client)

    html = (await session_client.get(f"/drafts/{draft['draft_id']}")).text
    assert "确认识别结果" in html
    assert "交实验报告" in html
    assert "本次识别依据的时间上下文" in html
    assert "/image" in html


async def test_draft_page_requires_login(app, client: AsyncClient) -> None:
    use_llm(app, [])
    response = await client.get("/drafts/1")
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


async def test_draft_page_handles_missing_draft(session_client: AsyncClient) -> None:
    html = (await session_client.get("/drafts/9999")).text
    assert "不存在或已过期" in html
