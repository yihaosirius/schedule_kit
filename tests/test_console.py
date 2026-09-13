"""M5：控制台（LLM 配置热加载、API Key 管理、运行状态）。

最重要的一组断言是**权限**：机器客户端（API Key）即使拥有读写密钥，
也不能改配置或签发新密钥——否则一把被窃取的密钥就能自我提权。
"""

from __future__ import annotations

from httpx import AsyncClient

from app.config import Config
from app.llm.mock import MockLLM
from app.services.apikeys import create_key

from conftest import TEST_PASSWORD


def _machine_headers(config: Config, *, read_only: bool = False) -> dict[str, str]:
    from app.db import Database

    db = Database(config.server.data_dir / "schedulekit.db")
    _, plaintext = create_key(db, name="machine", read_only=read_only)
    return {"Authorization": f"Bearer {plaintext}"}


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
async def test_get_settings_never_returns_the_api_key(session_client: AsyncClient) -> None:
    body = (await session_client.get("/api/settings")).json()
    assert "api_key" not in body["llm"], "密钥绝不能出现在响应里"
    assert body["llm"]["api_key_set"] is True
    assert body["llm"]["provider"] == "mock"


async def test_get_settings_includes_runtime_status(session_client: AsyncClient) -> None:
    body = (await session_client.get("/api/settings")).json()
    status = body["status"]
    assert status["version"]
    assert "config_path" in status
    assert "db_bytes" in status
    assert "tasks" in status and "keys" in status
    assert status["llm"]["ready"] is True


async def test_status_endpoint_is_readable_by_machine_key(
    session_client: AsyncClient, config: Config
) -> None:
    """状态是只读信息，允许带密钥的客户端读取（悬浮窗要显示服务端是否正常）。"""
    response = await session_client.get("/api/status", headers=_machine_headers(config))
    assert response.status_code == 200
    assert response.json()["version"]


# --------------------------------------------------------------------------- #
# 保存 LLM 配置
# --------------------------------------------------------------------------- #
async def test_put_settings_updates_and_hot_reloads(session_client: AsyncClient, app) -> None:
    response = await session_client.put(
        "/api/settings",
        json={
            "provider": "mock",
            "base_url": "https://example.invalid/v1",
            "model": "new-model",
            "temperature": 0.3,
            "timeout_seconds": 30,
            "max_tokens": 512,
            "max_image_bytes": 1048576,
            "system_prompt": "新的提示词",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "new-model"
    assert body["reloaded"] is True
    assert body["restart_required"] is False

    # 进程内配置已变
    assert app.state.config.llm.model == "new-model"
    assert app.state.config.llm.temperature == 0.3
    # 适配器已重建
    assert app.state.llm is not None


async def test_omitting_api_key_keeps_the_existing_one(session_client: AsyncClient, config: Config) -> None:
    """前端不改密钥时不该把密钥回传一遍，服务端要保留原值。"""
    before = config.llm.api_key
    response = await session_client.put(
        "/api/settings", json={"provider": "mock", "model": "another-model"}
    )
    assert response.status_code == 200
    assert response.json()["api_key_set"] is True
    assert Config(config.path).llm.api_key == before


async def test_empty_api_key_clears_it_and_marks_unready(session_client: AsyncClient, app) -> None:
    response = await session_client.put(
        "/api/settings", json={"provider": "openai_compat", "base_url": "https://x.invalid/v1",
                               "model": "m", "api_key": ""}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["api_key_set"] is False
    assert body["ready"] is False
    assert body["error"], "未配置时应给出明确原因"
    assert app.state.llm is None


async def test_settings_survive_a_config_reload(session_client: AsyncClient, config: Config) -> None:
    await session_client.put("/api/settings", json={"provider": "mock", "model": "persisted"})
    assert Config(config.path).llm.model == "persisted"


async def test_settings_save_preserves_config_comments(
    session_client: AsyncClient, config: Config
) -> None:
    """控制台就地改 tomlkit 文档，注释必须逐条保留。"""
    before = [
        line for line in config.path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("#")
    ]
    await session_client.put("/api/settings", json={"provider": "mock", "model": "x"})
    after = [
        line for line in config.path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("#")
    ]
    assert after == before


async def test_invalid_settings_are_rejected(session_client: AsyncClient) -> None:
    assert (
        await session_client.put("/api/settings", json={"provider": "mock", "temperature": 9})
    ).status_code == 422
    assert (
        await session_client.put("/api/settings", json={"provider": "mock", "max_tokens": 1})
    ).status_code == 422


# --------------------------------------------------------------------------- #
# API Key
# --------------------------------------------------------------------------- #
async def test_create_key_returns_plaintext_exactly_once(session_client: AsyncClient) -> None:
    created = (await session_client.post("/api/keys", json={"name": "手机", "read_only": False})).json()
    assert created["key"].startswith("sk_")
    assert created["id"]

    listed = (await session_client.get("/api/keys")).json()["keys"]
    entry = next(k for k in listed if k["id"] == created["id"])
    assert "key" not in entry and "key_hash" not in entry
    assert entry["name"] == "手机"


async def test_created_key_works_immediately(session_client: AsyncClient) -> None:
    created = (await session_client.post("/api/keys", json={"name": "浮窗"})).json()
    response = await session_client.get(
        "/api/tasks?view=ordered", headers={"Authorization": f"Bearer {created['key']}"}
    )
    assert response.status_code == 200


async def test_read_only_key_is_marked(session_client: AsyncClient) -> None:
    created = (await session_client.post("/api/keys", json={"name": "小组件", "read_only": True})).json()
    listed = (await session_client.get("/api/keys")).json()["keys"]
    entry = next(k for k in listed if k["id"] == created["id"])
    assert entry["read_only"] is True

    blocked = await session_client.post(
        "/api/tasks",
        json={"title": "x", "category": "other", "priority": 3},
        headers={"Authorization": f"Bearer {created['key']}"},
    )
    assert blocked.status_code == 403


async def test_revoke_key_stops_it_working(session_client: AsyncClient) -> None:
    created = (await session_client.post("/api/keys", json={"name": "临时"})).json()
    headers = {"Authorization": f"Bearer {created['key']}"}
    assert (await session_client.get("/api/tasks", headers=headers)).status_code == 200

    assert (await session_client.delete(f"/api/keys/{created['id']}")).status_code == 200
    assert (await session_client.get("/api/tasks", headers=headers)).status_code == 401


async def test_revoking_twice_is_404(session_client: AsyncClient) -> None:
    created = (await session_client.post("/api/keys", json={"name": "临时2"})).json()
    assert (await session_client.delete(f"/api/keys/{created['id']}")).status_code == 200
    assert (await session_client.delete(f"/api/keys/{created['id']}")).status_code == 404


async def test_revoked_keys_disappear_from_the_list(session_client: AsyncClient) -> None:
    created = (await session_client.post("/api/keys", json={"name": "会消失"})).json()
    await session_client.delete(f"/api/keys/{created['id']}")
    listed = (await session_client.get("/api/keys")).json()["keys"]
    assert all(k["id"] != created["id"] for k in listed)


# --------------------------------------------------------------------------- #
# 权限：机器客户端不得提权
# --------------------------------------------------------------------------- #
async def test_machine_key_cannot_read_or_write_settings(
    session_client: AsyncClient, config: Config
) -> None:
    headers = _machine_headers(config)
    assert (await session_client.get("/api/settings", headers=headers)).status_code == 403
    assert (
        await session_client.put("/api/settings", json={"provider": "mock"}, headers=headers)
    ).status_code == 403


async def test_machine_key_cannot_manage_keys(session_client: AsyncClient, config: Config) -> None:
    headers = _machine_headers(config)
    assert (await session_client.get("/api/keys", headers=headers)).status_code == 403
    assert (
        await session_client.post("/api/keys", json={"name": "提权"}, headers=headers)
    ).status_code == 403
    assert (await session_client.delete("/api/keys/1", headers=headers)).status_code == 403


async def test_anonymous_gets_nothing(client: AsyncClient) -> None:
    assert (await client.get("/api/settings")).status_code == 401
    assert (await client.get("/api/keys")).status_code == 401
    assert (await client.get("/api/status")).status_code == 401


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
async def test_settings_page_renders(session_client: AsyncClient) -> None:
    html = (await session_client.get("/settings")).text
    assert "LLM 配置" in html
    assert "API 密钥" in html
    assert "运行状态" in html
    assert "data-llm-form" in html
    assert "data-key-form" in html
    # 密钥永不回显
    assert 'name="api_key"' in html
    assert "已设置（留空则保持不变）" in html


async def test_settings_page_shows_runtime_numbers(session_client: AsyncClient) -> None:
    html = (await session_client.get("/settings")).text
    assert "常驻内存" in html
    assert "数据库" in html


async def test_settings_page_requires_login(client: AsyncClient) -> None:
    response = await client.get("/settings")
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


async def test_llm_form_warns_about_static_system_prompt(session_client: AsyncClient) -> None:
    """页面要提醒用户不要把时间/课表写进 system prompt（会毁掉 prompt 缓存）。"""
    html = (await session_client.get("/settings")).text
    assert "prompt 缓存" in html


async def test_hot_reload_actually_swaps_the_adapter(session_client: AsyncClient, app) -> None:
    """保存后录入接口应立刻用上新适配器，无需重启。"""
    app.state.llm = None
    app.state.llm_error = "人为置空"

    await session_client.put(
        "/api/settings", json={"provider": "mock", "model": "m", "api_key": "k"}
    )
    assert app.state.llm is not None
    assert app.state.llm_error is None

    app.state.llm = MockLLM(items=[])
    response = await session_client.post("/api/ingest", json={"channel": "text", "text": "x"})
    assert response.status_code == 201, "热加载后录入应立刻可用"
