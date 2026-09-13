"""控制台接口：LLM 配置与 API Key 管理。

两者都**只接受网页会话**（``SessionWriteDep``）——机器客户端即使有
读写密钥也不能改配置或签发新密钥，否则一把被窃取的密钥就能自我提权。

``PUT /api/settings`` 保存后立即重建适配器，**无需重启**（PLAN.md §4）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config import ConfigError
from app.deps import AuthDep, SessionDep, SessionWriteDep
from app.llm import build_llm
from app.llm.base import LLMNotConfigured
from app.logging import get_logger, kv
from app.services import apikeys as apikey_service
from app.status import runtime_status

router = APIRouter(prefix="/api", tags=["console"])
log = get_logger("console")


# --------------------------------------------------------------------------- #
# LLM 配置
# --------------------------------------------------------------------------- #
class LLMSettingsIn(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    base_url: str = Field(default="", max_length=300)
    model: str = Field(default="", max_length=120)
    #: 省略 = 保持原值；空字符串 = 清空；其它 = 覆盖。
    #: 这样前端不需要为了改 temperature 而把密钥回传一遍。
    api_key: str | None = Field(default=None, max_length=400)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    max_tokens: int = Field(default=1024, ge=64, le=32000)
    max_image_bytes: int = Field(default=8 * 1024 * 1024, ge=64 * 1024, le=32 * 1024 * 1024)
    system_prompt: str = Field(default="", max_length=20000)


def _llm_view(request: Request) -> dict[str, Any]:
    cfg = request.app.state.config
    return {
        "provider": cfg.llm.provider,
        "base_url": cfg.llm.base_url,
        "model": cfg.llm.model,
        "api_key_set": cfg.llm.api_key_set,
        "temperature": cfg.llm.temperature,
        "timeout_seconds": cfg.llm.timeout_seconds,
        "max_tokens": cfg.llm.max_tokens,
        "max_image_bytes": cfg.llm.max_image_bytes,
        "system_prompt": cfg.llm.system_prompt,
        "ready": request.app.state.llm is not None,
        "error": request.app.state.llm_error,
    }


@router.get("/settings", summary="读取控制台设置")
def get_settings(request: Request, auth: SessionDep) -> dict[str, Any]:
    cfg = request.app.state.config
    return {
        "llm": _llm_view(request),
        "term": {"start_date": cfg.term.start_date, "total_weeks": cfg.term.total_weeks},
        "ingest": {"confirm_ttl_hours": cfg.ingest.confirm_ttl_hours},
        "status": runtime_status(request.app),
    }


@router.put("/settings", summary="保存 LLM 配置（立即生效）")
def put_settings(
    payload: LLMSettingsIn, request: Request, auth: SessionWriteDep
) -> dict[str, Any]:
    cfg = request.app.state.config

    values: dict[str, Any] = {
        "provider": payload.provider.strip(),
        "base_url": payload.base_url.strip(),
        "model": payload.model.strip(),
        "temperature": payload.temperature,
        "timeout_seconds": payload.timeout_seconds,
        "max_tokens": payload.max_tokens,
        "max_image_bytes": payload.max_image_bytes,
        "system_prompt": payload.system_prompt,
    }
    if payload.api_key is not None:
        values["api_key"] = payload.api_key.strip()

    before = cfg.llm
    try:
        cfg.update_section("llm", values)
    except ConfigError as exc:
        # 走到这里通常是文件权限问题（目录属主不对）。给出可操作提示，
        # 而不是让它变成没有线索的 500。
        log.error("console.settings_save_failed %s", kv(error=str(exc)[:200]))
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    log.info(
        "console.settings_saved %s",
        kv(
            provider=cfg.llm.provider,
            model=cfg.llm.model,
            base_url_changed=before.base_url != cfg.llm.base_url,
            model_changed=before.model != cfg.llm.model,
            api_key_written=payload.api_key is not None,
            api_key_set=cfg.llm.api_key_set,
        ),
    )

    # 热加载：换适配器，不重启进程
    try:
        request.app.state.llm = build_llm(cfg)
        request.app.state.llm_error = None
    except LLMNotConfigured as exc:
        request.app.state.llm = None
        request.app.state.llm_error = str(exc)
        log.warning("console.llm_not_ready %s", kv(reason=str(exc)[:160]))

    result = _llm_view(request)
    result["reloaded"] = True
    result["restart_required"] = False
    return result


# --------------------------------------------------------------------------- #
# API Key
# --------------------------------------------------------------------------- #
class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    read_only: bool = False


def _key_view(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "read_only": bool(row["read_only"]),
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
    }


@router.get("/keys", summary="列出 API Key（不含明文）")
def list_keys(request: Request, auth: SessionDep) -> dict[str, Any]:
    rows = apikey_service.list_keys(request.app.state.db)
    return {"keys": [_key_view(row) for row in rows]}


@router.post("/keys", status_code=status.HTTP_201_CREATED, summary="创建 API Key（明文仅返回一次）")
def create_key(payload: ApiKeyIn, request: Request, auth: SessionWriteDep) -> dict[str, Any]:
    key_id, plaintext = apikey_service.create_key(
        request.app.state.db, name=payload.name, read_only=payload.read_only
    )
    log.info("console.key_issued %s", kv(key_id=key_id, read_only=payload.read_only))
    # 明文只在这里出现一次；库里只有 SHA-256
    return {"id": key_id, "name": payload.name.strip(), "key": plaintext, "read_only": payload.read_only}


@router.delete("/keys/{key_id}", summary="吊销 API Key")
def delete_key(key_id: int, request: Request, auth: SessionWriteDep) -> dict[str, Any]:
    revoked = apikey_service.revoke_key(request.app.state.db, key_id)
    if not revoked:
        raise HTTPException(status_code=404, detail=f"密钥 {key_id} 不存在或已吊销")
    return {"revoked": True, "id": key_id}


@router.get("/status", summary="运行状态")
def get_status(request: Request, auth: AuthDep) -> dict[str, Any]:
    return runtime_status(request.app)
