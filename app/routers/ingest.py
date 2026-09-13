"""智能录入：两阶段确认。

``POST /api/ingest`` 只产出草稿，**绝不写 items 表**；确认是独立的一步。
这样模型出错、网络中断、用户改主意都不会留下脏数据。

同一个端点同时承载三种入口（PLAN.md §11）：

* ``channel=image`` —— 拍照/截图，走视觉模型
* ``channel=text`` —— 一段自然语言，走同一个模型
* ``channel=text`` + ``items`` —— 客户端已经拿到了结构化内容（例如快捷指令
  把草稿改过了），直接建草稿等确认，**不再调用 LLM**
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.deps import AuthDep, WriteAuthDep
from app.llm.base import LLMError
from app.logging import get_logger, kv
from app.media import MediaError, resolve_upload, store_image
from app.ratelimit import ingest_limiter
from app.services import context as context_service
from app.services import courses as course_service
from app.services import drafts as draft_service
from app.services import normalize as normalize_service
from app.timeutil import now_utc

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
log = get_logger("ingest")

#: base64 会让体积膨胀约 1/3，所以在配置的图片上限之外再留一点余量给 JSON 包装
BASE64_OVERHEAD = 1.4


class IngestRequest(BaseModel):
    channel: Literal["image", "text"]
    image_base64: str | None = Field(default=None, description="channel=image 时必填")
    mime: str | None = Field(default=None, description="客户端声明的 MIME；服务端以实际字节为准")
    text: str | None = Field(default=None, max_length=8000, description="channel=text 时的内容")
    items: list[dict[str, Any]] | None = Field(
        default=None, description="已结构化的条目；提供时跳过 LLM，直接建草稿等确认"
    )


def _draft_response(request: Request, row, *, elapsed_ms: float | None = None) -> dict[str, Any]:
    payload = draft_service.to_public(row)
    base = request.app.state.config.server.public_url.rstrip("/")
    if row["image_path"]:
        payload["image_url"] = f"/api/ingest/{row['id']}/image"
    else:
        payload["image_url"] = None
    # 快捷指令里编辑 JSON 很别扭，所以直接给一个可打开的确认页
    payload["confirm_url"] = f"{base}/drafts/{row['id']}"
    if elapsed_ms is not None:
        payload["llm_elapsed_ms"] = elapsed_ms
    return payload


@router.post("", status_code=status.HTTP_201_CREATED, summary="上传图片或文本，生成待确认草稿")
async def create_ingest(payload: IngestRequest, request: Request, auth: WriteAuthDep) -> dict:
    cfg = request.app.state.config
    db = request.app.state.db
    timezone = cfg.server.timezone

    allowed, retry_after = ingest_limiter.check(auth.describe())
    if not allowed:
        log.warning("ingest.rate_limited %s", kv(actor=auth.describe(), retry_after=retry_after))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"录入过于频繁，请 {retry_after:.0f} 秒后再试",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    image_path: str | None = None
    image_sha256: str | None = None
    body = ""

    if payload.channel == "image":
        if not payload.image_base64:
            raise HTTPException(status_code=422, detail="channel=image 时必须提供 image_base64")
        max_bytes = cfg.llm.max_image_bytes
        if len(payload.image_base64) > max_bytes * BASE64_OVERHEAD:
            raise HTTPException(
                status_code=413,
                detail=f"图片编码后体积超过上限（约 {int(max_bytes * BASE64_OVERHEAD)} 字符）",
            )
        try:
            raw = base64.b64decode(payload.image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"image_base64 不是合法 base64：{exc}") from exc

        try:
            stored = store_image(raw, uploads_dir=cfg.server.data_dir / "uploads", max_bytes=max_bytes)
        except MediaError as exc:
            log.warning("ingest.image_rejected %s", kv(reason=type(exc).__name__, status=exc.status_code))
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        image_path = stored.relative_path
        image_sha256 = stored.sha256
        body = "[图片]"
    else:
        if not payload.text and not payload.items:
            raise HTTPException(status_code=422, detail="channel=text 时必须提供 text 或 items")
        body = (payload.text or "").strip()

    # 1) 客户端已给出结构化条目 → 不调 LLM，直接建草稿等确认
    if payload.items is not None:
        try:
            validated = draft_service.validate_client_items(payload.items, timezone=timezone)
        except draft_service.DraftValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        row = draft_service.create_draft(
            db,
            channel=payload.channel,
            items=validated,
            ttl_hours=cfg.ingest.confirm_ttl_hours,
            image_path=image_path,
            image_sha256=image_sha256,
            input_text=payload.text,
            context_snapshot=None,
            llm_provider="client",
            llm_model=None,
            llm_raw=None,
        )
        log.info("ingest.structured_accepted %s", kv(draft_id=row["id"], items=len(validated)))
        return _draft_response(request, row)

    # 2) 交给 LLM 抽取
    llm = request.app.state.llm
    if llm is None:
        detail = request.app.state.llm_error or "LLM 未配置"
        log.warning("ingest.llm_unavailable %s", kv(reason=detail[:120]))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{detail}（可在 /settings 里修改，保存后立即生效，无需重启）",
        )

    term = context_service.parse_term(cfg.term.start_date, cfg.term.total_weeks)
    context_text = context_service.build_context(
        now_utc(),
        timezone=timezone,
        term=term,
        sessions=course_service.flat_sessions(db),
    )
    user_text = context_service.combine_user_message(context_text, body)

    images = None
    if payload.channel == "image":
        images = [(cfg.server.data_dir / "uploads" / image_path).read_bytes()]

    try:
        result = await llm.extract(system=cfg.llm.system_prompt, user_text=user_text, images=images)
    except LLMError as exc:
        row = draft_service.create_failed_draft(
            db,
            channel=payload.channel,
            error=str(exc),
            ttl_hours=cfg.ingest.confirm_ttl_hours,
            image_path=image_path,
            image_sha256=image_sha256,
            input_text=payload.text,
            context_snapshot=context_text,
            llm_provider=getattr(llm, "name", None),
            llm_model=cfg.llm.model,
        )
        log.warning("ingest.llm_failed %s", kv(draft_id=row["id"], error=type(exc).__name__))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"message": f"识别失败：{exc}", "draft_id": row["id"], "retryable": True},
        ) from exc

    normalized = normalize_service.normalize_items(result.items, timezone=timezone)
    row = draft_service.create_draft(
        db,
        channel=payload.channel,
        items=normalized,
        ttl_hours=cfg.ingest.confirm_ttl_hours,
        image_path=image_path,
        image_sha256=image_sha256,
        input_text=payload.text,
        context_snapshot=context_text,
        llm_provider=result.provider,
        llm_model=result.model,
        llm_raw=result.raw,
    )
    response = _draft_response(request, row, elapsed_ms=result.elapsed_ms)
    log.info(
        "ingest.draft_ready %s",
        kv(draft_id=row["id"], channel=payload.channel, items=len(normalized), elapsed_ms=result.elapsed_ms),
    )
    return response


def _load_or_404(request: Request, draft_id: int):
    row = draft_service.get_draft(request.app.state.db, draft_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"草稿 {draft_id} 不存在或已过期")
    return row


@router.get("/{draft_id}", summary="读取草稿")
def get_ingest(draft_id: int, request: Request, auth: AuthDep) -> dict:
    return _draft_response(request, _load_or_404(request, draft_id))


@router.get("/{draft_id}/image", summary="读取草稿原图")
def get_ingest_image(draft_id: int, request: Request, auth: AuthDep):
    row = _load_or_404(request, draft_id)
    if not row["image_path"]:
        raise HTTPException(status_code=404, detail="该草稿没有图片")
    path = resolve_upload(request.app.state.config.server.data_dir / "uploads", row["image_path"])
    if path is None:
        raise HTTPException(status_code=404, detail="图片文件已不在")
    return FileResponse(path)


@router.post("/{draft_id}/confirm", summary="确认草稿并入库")
def confirm_ingest(
    draft_id: int, request: Request, auth: WriteAuthDep, payload: dict | None = None
) -> dict:
    """确认入库。

    请求体可选带 ``{"items": [...]}``：即"在回传的 JSON 基础上修改后重新提交"，
    修改与入库在一次事务里完成。
    """
    db = request.app.state.db
    cfg = request.app.state.config
    items = None
    if isinstance(payload, dict) and payload.get("items") is not None:
        items = payload["items"]

    try:
        result = draft_service.confirm_draft(
            db, draft_id, timezone=cfg.server.timezone, items=items
        )
    except draft_service.DraftNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except draft_service.DraftNotPending as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except draft_service.DraftValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    row = draft_service.get_draft(db, draft_id)
    content = _draft_response(request, row)
    content["created_item_ids"] = result.item_ids
    return content


@router.post("/{draft_id}/discard", response_model=None, summary="丢弃草稿")
def discard_ingest(draft_id: int, request: Request, auth: WriteAuthDep) -> dict:
    try:
        row = draft_service.discard_draft(request.app.state.db, draft_id)
    except draft_service.DraftNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except draft_service.DraftNotPending as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _draft_response(request, row)
