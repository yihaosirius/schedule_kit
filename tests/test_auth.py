"""M1：鉴权（会话 Cookie、CSRF、API Key、限流）。

这里用两个测试专用探针路由来观察依赖的行为，避免把测试绑定到
尚未实现的业务端点上。
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import Config
from app.db import Database
from app.deps import SessionDep, SessionWriteDep, WriteAuthDep, current_auth
from app.ratelimit import ingest_limiter, login_limiter
from app.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    csrf_token,
    generate_api_key,
    hash_api_key,
    sign_session,
)
from app.timeutil import utc_iso

from conftest import TEST_PASSWORD


@pytest.fixture
def probed(app: FastAPI) -> FastAPI:
    """在真实应用上挂几个探针路由，用来观察认证依赖的行为。"""

    @app.get("/_probe/any")
    def _any(auth=Depends(current_auth)):
        return {"kind": auth.kind, "read_only": auth.read_only}

    @app.post("/_probe/write")
    def _write(auth: WriteAuthDep):
        return {"kind": auth.kind, "read_only": auth.read_only}

    @app.get("/_probe/session")
    def _session(auth: SessionDep):
        return {"kind": auth.kind}

    @app.post("/_probe/session-write")
    def _session_write(auth: SessionWriteDep):
        return {"kind": auth.kind}

    return app


@pytest.fixture
async def probed_client(probed: FastAPI):
    transport = ASGITransport(app=probed)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _create_key(config: Config, *, name: str, read_only: bool = False) -> str:
    """直接写库创建 API Key，返回明文。"""
    db = Database(config.server.data_dir / "schedulekit.db")
    raw = generate_api_key()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO api_keys(name, key_hash, read_only, created_at) VALUES (?, ?, ?, ?)",
            (name, hash_api_key(raw), 1 if read_only else 0, utc_iso()),
        )
    return raw


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #
async def test_login_with_correct_password_issues_both_cookies(client: AsyncClient) -> None:
    response = await client.post("/api/login", json={"password": TEST_PASSWORD})
    assert response.status_code == 204
    assert SESSION_COOKIE in client.cookies
    assert CSRF_COOKIE in client.cookies


async def test_login_with_wrong_password_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/api/login", json={"password": "nope"})
    assert response.status_code == 401
    assert SESSION_COOKIE not in client.cookies


async def test_form_login_redirects_and_sets_cookie(client: AsyncClient) -> None:
    response = await client.post(
        "/api/login",
        data={"password": TEST_PASSWORD},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert SESSION_COOKIE in client.cookies


async def test_form_login_failure_redirects_with_error_flag(client: AsyncClient) -> None:
    response = await client.post(
        "/api/login",
        data={"password": "bad"},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=1"


async def test_login_is_rate_limited(client: AsyncClient) -> None:
    for _ in range(5):
        await client.post("/api/login", json={"password": "bad"})
    response = await client.post("/api/login", json={"password": "bad"})
    assert response.status_code == 429
    assert "Retry-After" in response.headers


async def test_logout_clears_cookies(client: AsyncClient) -> None:
    await client.post("/api/login", json={"password": TEST_PASSWORD})
    assert SESSION_COOKIE in client.cookies
    response = await client.post("/api/logout")
    assert response.status_code == 204
    assert SESSION_COOKIE not in client.cookies


# --------------------------------------------------------------------------- #
# 会话校验
# --------------------------------------------------------------------------- #
async def test_unauthenticated_read_is_401(probed_client: AsyncClient) -> None:
    assert (await probed_client.get("/_probe/any")).status_code == 401


async def test_unauthenticated_write_is_401(probed_client: AsyncClient) -> None:
    assert (await probed_client.post("/_probe/write")).status_code == 401


async def test_session_grants_read(probed_client: AsyncClient) -> None:
    await probed_client.post("/api/login", json={"password": TEST_PASSWORD})
    response = await probed_client.get("/_probe/any")
    assert response.status_code == 200
    assert response.json() == {"kind": "session", "read_only": False}


async def test_session_write_requires_csrf_header(probed_client: AsyncClient) -> None:
    await probed_client.post("/api/login", json={"password": TEST_PASSWORD})
    # 缺少 CSRF 头 → 拒绝
    assert (await probed_client.post("/_probe/write")).status_code == 403
    # 带上正确令牌 → 通过
    token = probed_client.cookies.get(CSRF_COOKIE)
    response = await probed_client.post("/_probe/write", headers={CSRF_HEADER: token})
    assert response.status_code == 200
    # 错误令牌 → 拒绝
    assert (
        await probed_client.post("/_probe/write", headers={CSRF_HEADER: "forged"})
    ).status_code == 403


async def test_forged_cookie_signature_is_rejected(probed_client: AsyncClient) -> None:
    probed_client.cookies.set(SESSION_COOKIE, "9999999999.1.forgedsignature")
    assert (await probed_client.get("/_probe/any")).status_code == 401


async def test_expired_session_is_rejected(probed_client: AsyncClient, config: Config) -> None:
    expired = sign_session(config.auth.secret_key, ttl_days=-1, epoch=config.auth.session_epoch)
    probed_client.cookies.set(SESSION_COOKIE, expired)
    assert (await probed_client.get("/_probe/any")).status_code == 401


async def test_bumping_session_epoch_invalidates_existing_cookies(
    probed_client: AsyncClient, app: FastAPI, config: Config
) -> None:
    await probed_client.post("/api/login", json={"password": TEST_PASSWORD})
    assert (await probed_client.get("/_probe/any")).status_code == 200

    # 改密码 → epoch 自增 → 旧 Cookie 立即失效
    config.update_section("auth", {"session_epoch": config.auth.session_epoch + 1})
    app.state.config = Config(config.path)

    assert (await probed_client.get("/_probe/any")).status_code == 401


# --------------------------------------------------------------------------- #
# API Key
# --------------------------------------------------------------------------- #
async def test_api_key_grants_read_and_write(probed_client: AsyncClient, config: Config) -> None:
    raw = _create_key(config, name="float")
    headers = {"Authorization": f"Bearer {raw}"}
    assert (await probed_client.get("/_probe/any", headers=headers)).status_code == 200
    # API Key 不受 CSRF 约束（不是 Cookie 认证）
    assert (await probed_client.post("/_probe/write", headers=headers)).status_code == 200


async def test_api_key_also_accepted_via_x_api_key_header(
    probed_client: AsyncClient, config: Config
) -> None:
    raw = _create_key(config, name="shortcut")
    response = await probed_client.get("/_probe/any", headers={"X-API-Key": raw})
    assert response.status_code == 200
    assert response.json()["kind"] == "apikey"


async def test_read_only_api_key_cannot_write(probed_client: AsyncClient, config: Config) -> None:
    raw = _create_key(config, name="widget", read_only=True)
    headers = {"Authorization": f"Bearer {raw}"}
    read_response = await probed_client.get("/_probe/any", headers=headers)
    assert read_response.status_code == 200
    assert read_response.json()["read_only"] is True
    assert (await probed_client.post("/_probe/write", headers=headers)).status_code == 403


async def test_unknown_api_key_is_rejected(probed_client: AsyncClient) -> None:
    response = await probed_client.get(
        "/_probe/any", headers={"Authorization": "Bearer sk_not-a-real-key"}
    )
    assert response.status_code == 401


async def test_revoked_api_key_is_rejected(probed_client: AsyncClient, config: Config) -> None:
    raw = _create_key(config, name="old")
    db = Database(config.server.data_dir / "schedulekit.db")
    with db.transaction() as conn:
        conn.execute("UPDATE api_keys SET revoked_at = ? WHERE name = 'old'", (utc_iso(),))

    response = await probed_client.get("/_probe/any", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 401


async def test_api_key_cannot_reach_session_only_endpoints(
    probed_client: AsyncClient, config: Config
) -> None:
    raw = _create_key(config, name="machine")
    headers = {"Authorization": f"Bearer {raw}"}
    assert (await probed_client.get("/_probe/session", headers=headers)).status_code == 403
    assert (await probed_client.post("/_probe/session-write", headers=headers)).status_code == 403


# --------------------------------------------------------------------------- #
# 页面与 trace
# --------------------------------------------------------------------------- #
async def test_root_redirects_anonymous_to_login(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_login_page_redirects_when_already_authenticated(client: AsyncClient) -> None:
    assert (await client.get("/login")).status_code == 200
    await client.post("/api/login", json={"password": TEST_PASSWORD})
    response = await client.get("/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


async def test_every_response_carries_trace_id(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.headers.get("X-Trace-Id")


async def test_incoming_trace_id_is_echoed(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Trace-Id": "abc12345"})
    assert response.headers["X-Trace-Id"] == "abc12345"


async def test_csrf_token_derivation_is_stable(config: Config) -> None:
    assert csrf_token(config.auth.secret_key) == csrf_token(config.auth.secret_key)
    assert csrf_token(config.auth.secret_key) != csrf_token("other-key")
