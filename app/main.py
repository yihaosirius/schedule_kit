"""FastAPI 应用装配。

路由与中间件按里程碑逐步挂载；``create_app`` 是唯一入口，
便于测试注入临时配置。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, suppress
from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import Config, load_config
from app.db import Database
from app.housekeeping import run_forever as housekeeping_loop
from app.llm import build_llm
from app.llm.base import LLMNotConfigured
from app.logging import get_logger, kv, set_trace_id
from app.migrations.runner import apply_migrations
from app.paths import STATIC_DIR
from app.routers import auth as auth_router
from app.routers import courses as courses_router
from app.routers import ingest as ingest_router
from app.routers import settings as settings_router
from app.routers import tasks as tasks_router
from app.routers import ui as ui_router

TRACE_HEADER = "X-Trace-Id"


def create_app(config: Config | None = None) -> FastAPI:
    """构建应用。会校验配置不变量并应用数据库迁移。"""
    cfg = config or load_config()
    cfg.assert_ready()

    log = get_logger("app")
    db = Database(cfg.server.data_dir / "schedulekit.db")
    applied = apply_migrations(db)

    # LLM 配置错误不该让整个服务起不来——否则用户没法打开 /settings 去修它。
    # 这里把错误留在 state 里，等真正调用录入接口时再以明确的信息返回。
    llm = None
    llm_error: str | None = None
    try:
        llm = build_llm(cfg)
    except LLMNotConfigured as exc:
        llm_error = str(exc)

    log.info(
        "app.start %s",
        kv(
            version=__version__,
            config=str(cfg.path),
            data_dir=str(cfg.server.data_dir),
            timezone=cfg.server.timezone,
            public_url=cfg.server.public_url,
            migrations=applied or "none",
            llm_provider=cfg.llm.provider,
            llm_model=cfg.llm.model,
            llm_api_key_set=cfg.llm.api_key_set,
            llm_ready=llm is not None,
        ),
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        task = asyncio.create_task(housekeeping_loop(application))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="ScheduleKit",
        version=__version__,
        lifespan=lifespan,
        # OpenAPI 文档不公开暴露；控制台里的受鉴权入口在 M5 挂载。
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = cfg
    app.state.db = db
    app.state.migrations_applied = applied
    app.state.llm = llm
    app.state.llm_error = llm_error

    @app.middleware("http")
    async def trace_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """给每个请求分配 trace_id，并记录起止与耗时。

        trace_id 会回写到响应头，客户端报错时可直接与服务端日志对上。
        """
        trace_id = set_trace_id(request.headers.get(TRACE_HEADER))
        http_log = get_logger("http")
        started = time.perf_counter()
        client = request.client.host if request.client else None

        http_log.info(
            "request.start %s",
            kv(
                method=request.method,
                path=request.url.path,
                query=request.url.query or None,
                client=client,
            ),
        )
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - 记录后继续抛出，交给上层处理
            http_log.exception(
                "request.error %s",
                kv(
                    method=request.method,
                    path=request.url.path,
                    error=type(exc).__name__,
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                ),
            )
            raise

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        http_log.info(
            "request.end %s",
            kv(
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                elapsed_ms=elapsed_ms,
                auth=getattr(request.state, "auth_kind", None),
            ),
        )
        response.headers[TRACE_HEADER] = trace_id
        return response

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(auth_router.router)
    app.include_router(tasks_router.router)
    app.include_router(ingest_router.router)
    app.include_router(courses_router.router)
    app.include_router(settings_router.router)
    app.include_router(ui_router.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "version": __version__,
            "timezone": cfg.server.timezone,
            "migrations_applied": applied,
        }

    return app
