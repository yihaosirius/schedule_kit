"""任务 CRUD 与双视图查询。

薄路由层：只做参数校验与异常映射，业务规则全在 ``app/services/tasks.py``。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from app.deps import AuthDep, WriteAuthDep
from app.logging import get_logger, kv
from app.schemas import Category, DeleteResult, ItemCreate, ItemOut, ItemStatus, ItemUpdate, TaskView
from app.services import tasks as task_service

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
log = get_logger("tasks")


def _not_found(item_id: int) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务 {item_id} 不存在")


@router.get("", response_model=list[ItemOut], summary="按视图列出任务")
def list_tasks(
    request: Request,
    auth: AuthDep,
    view: TaskView = Query(
        default="ordered",
        description="ordered = 有截止时间（按时间升序）；unordered = 只有优先级（按 Ⅰ→Ⅴ）",
    ),
    item_status: ItemStatus | None = Query(default=None, alias="status"),
    category: Category | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> list[ItemOut]:
    rows = task_service.list_items(
        request.app.state.db,
        view=view,
        status=item_status,
        category=category,
        limit=limit,
    )
    log.info(
        "task.listed %s",
        kv(view=view, status=item_status, category=category, limit=limit, returned=len(rows)),
    )
    return [ItemOut.from_row(row) for row in rows]


@router.post("", response_model=ItemOut, status_code=status.HTTP_201_CREATED, summary="创建任务")
def create_task(payload: ItemCreate, request: Request, auth: WriteAuthDep, response: Response) -> ItemOut:
    row, created = task_service.create_item(
        request.app.state.db,
        payload,
        timezone=request.app.state.config.server.timezone,
        source=payload.source,
    )
    if not created:
        # client_uuid 命中既有任务：幂等返回 200 而不是 201
        response.status_code = status.HTTP_200_OK
    return ItemOut.from_row(row)


@router.patch("/{item_id}", response_model=ItemOut, summary="局部更新任务")
def patch_task(
    item_id: int, payload: ItemUpdate, request: Request, auth: WriteAuthDep
) -> ItemOut:
    try:
        row = task_service.update_item(
            request.app.state.db,
            item_id,
            payload,
            timezone=request.app.state.config.server.timezone,
        )
    except task_service.TaskNotFound:
        raise _not_found(item_id) from None
    except task_service.TaskConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return ItemOut.from_row(row)


@router.delete("/{item_id}", response_model=DeleteResult, summary="删除任务")
def delete_task(item_id: int, request: Request, auth: WriteAuthDep) -> DeleteResult:
    deleted = task_service.delete_item(request.app.state.db, item_id)
    if not deleted:
        raise _not_found(item_id)
    return DeleteResult(deleted=True, id=item_id)
