"""页面路由。

初始渲染在服务端完成（拿到数据即可见，不依赖 JS），
后续的增删改由 ``/static/js/tasks.js`` 走 JSON API 局部刷新。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.deps import authenticate
from app.paths import STATIC_DIR
from app.services import apikeys as apikey_service
from app.services import context as context_service
from app.services import courses as course_service
from app.services import drafts as draft_service
from app.services import tasks as task_service
from app.services.timetable import format_timetable
from app.status import runtime_status
from app.templating import render
from app.timeutil import now_utc

router = APIRouter(tags=["ui"])


@router.get("/login", include_in_schema=False)
def login_page(request: Request):
    if authenticate(request) is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.get("/sw.js", include_in_schema=False)
def service_worker():
    """Service Worker 必须从站点根路径提供，作用域才覆盖整站。

    放在 /static/ 下的话默认作用域只有 /static/，页面导航就管不到了。
    """
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript; charset=utf-8",
        headers={
            "Service-Worker-Allowed": "/",
            # 外壳脚本本身必须每次校验，否则更新发布不出去
            "Cache-Control": "no-cache",
        },
    )


@router.get("/", include_in_schema=False)
def index(request: Request):
    if authenticate(request) is None:
        return RedirectResponse("/login", status_code=303)

    db = request.app.state.db
    return render(
        request,
        "index.html",
        ordered=task_service.list_items(db, view="ordered", status="open"),
        unordered=task_service.list_items(db, view="unordered", status="open"),
        active="tasks",
    )


@router.get("/drafts/{draft_id}", include_in_schema=False)
def draft_page(draft_id: int, request: Request):
    """草稿确认页。快捷指令识别完直接跳到这里核对，比在快捷指令里编辑 JSON 现实得多。"""
    if authenticate(request) is None:
        return RedirectResponse(f"/login?next=/drafts/{draft_id}", status_code=303)

    row = draft_service.get_draft(request.app.state.db, draft_id)
    if row is None:
        return render(request, "draft_missing.html", draft_id=draft_id, active="draft")

    return render(
        request,
        "draft.html",
        draft_id=draft_id,
        draft=draft_service.to_public(row),
        # 草稿页不是任务列表，导航里应显示「← 主页」而不是把"任务"标为当前页
        active="draft",
    )


@router.get("/settings", include_in_schema=False)
def settings_page(request: Request):
    if authenticate(request) is None:
        return RedirectResponse("/login?next=/settings", status_code=303)

    cfg = request.app.state.config
    db = request.app.state.db
    status = runtime_status(request.app)
    return render(
        request,
        "settings.html",
        llm={
            "provider": cfg.llm.provider,
            "base_url": cfg.llm.base_url,
            "model": cfg.llm.model,
            "api_key_set": cfg.llm.api_key_set,
            "temperature": cfg.llm.temperature,
            "timeout_seconds": cfg.llm.timeout_seconds,
            "max_tokens": cfg.llm.max_tokens,
            "max_image_bytes": cfg.llm.max_image_bytes,
            "system_prompt": cfg.llm.system_prompt,
        },
        keys=apikey_service.list_keys(db),
        ingest={"confirm_ttl_hours": cfg.ingest.confirm_ttl_hours},
        status=status,
        active="settings",
    )


@router.get("/courses", include_in_schema=False)
def courses_page(request: Request):
    if authenticate(request) is None:
        return RedirectResponse("/login?next=/courses", status_code=303)

    cfg = request.app.state.config
    db = request.app.state.db
    courses = course_service.load_courses(db)
    grouped = course_service.sessions_by_course(db)

    term = context_service.parse_term(cfg.term.start_date, cfg.term.total_weeks)
    local_today = now_utc().astimezone(context_service.load_zone(cfg.server.timezone)).date()
    week_number = term.week_of(local_today) if term else 0

    return render(
        request,
        "courses.html",
        text=format_timetable(courses, grouped),
        courses=[
            {
                "id": row["id"],
                "name": row["name"],
                "location": row["location"],
                "sessions": grouped.get(int(row["id"]), []),
            }
            for row in courses
        ],
        counts=course_service.counts(db),
        week_number=week_number,
        active="courses",
    )
