"""pytest 公共夹具。

全程使用临时目录与临时配置，不触碰真实 ``config.toml`` / ``data/``，
且所有测试都不访问网络（LLM 走 mock provider）。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Config
from app.security import CSRF_COOKIE, CSRF_HEADER, hash_password

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = PROJECT_ROOT / "config.toml.example"

#: 测试临时目录的根。放在工作区内，且**不做清理**——见下方 tmp_path 说明。
RUN_ROOT = PROJECT_ROOT / ".pytest-run"

TEST_PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def reset_rate_limiters():
    """限流器是进程级单例，跨用例必须复位。

    否则任何一个用例触发了限流，后续用例的登录都会被 429 打挂——
    而且失败原因看起来跟被测逻辑毫无关系，非常难查。
    """
    from app.ratelimit import ingest_limiter, login_limiter

    login_limiter.reset()
    ingest_limiter.reset()
    yield
    login_limiter.reset()
    ingest_limiter.reset()


@pytest.fixture
def tmp_path() -> Path:
    """覆盖 pytest 内置的 ``tmp_path``。

    内置实现会在 ``%TEMP%`` 下建目录并在会话结束时清理，但受限沙箱里
    **目录删除与重命名会被拒绝**（``WinError 5``），导致全部用例报
    ``PermissionError``。这里改成由 Python 自己在工作区内创建、且不清理：
    每次运行留下的目录可直接事后翻查，反而方便排查。

    覆盖后 tmp_path_factory 不会被实例化，pytest 也就不会去碰 ``%TEMP%``。
    """
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    path = RUN_ROOT / uuid.uuid4().hex[:12]
    path.mkdir()
    return path


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """临时配置文件（从模板复制，保证与真实文件同构）。"""
    target = tmp_path / "config.toml"
    shutil.copyfile(EXAMPLE_CONFIG, target)
    return target


@pytest.fixture
def config(config_path: Path, tmp_path: Path) -> Config:
    """就绪的配置：测试用 data_dir、固定 secret_key、已知密码。"""
    cfg = Config(config_path)
    cfg.update_section(
        "server",
        {
            "data_dir": str(tmp_path / "data"),
            "public_url": "http://testserver",
            "timezone": "Asia/Shanghai",
        },
    )
    cfg.update_section(
        "auth",
        {
            "secret_key": "unit-test-secret-key-do-not-use-in-production",
            "password_hash": hash_password(TEST_PASSWORD),
            "session_ttl_days": 30,
            "session_epoch": 1,
        },
    )
    cfg.update_section("llm", {"provider": "mock", "model": "mock-model", "api_key": "test-key"})
    return Config(config_path)


@pytest.fixture
def app(config: Config):
    """按真实装配路径构建的应用（含迁移）。"""
    from app.main import create_app

    return create_app(config)


@pytest.fixture
async def client(app):
    """异步 HTTP 客户端，直接打 ASGI，不经过网络。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
async def session_client(client: AsyncClient) -> AsyncClient:
    """已登录、且默认带上 CSRF 头的客户端。

    绝大多数用例不关心 CSRF 细节，把它设成默认请求头可以让用例专注在业务上；
    CSRF 本身的拒绝行为由 test_auth.py 专门覆盖。
    """
    response = await client.post("/api/login", json={"password": TEST_PASSWORD})
    assert response.status_code == 204, "测试夹具登录失败"
    client.headers[CSRF_HEADER] = client.cookies.get(CSRF_COOKIE)
    return client
