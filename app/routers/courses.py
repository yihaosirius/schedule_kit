"""课表接口：``GET /api/courses`` 与整体替换 ``PUT /api/courses``。

``PUT`` 接受两种输入：

* ``{"text": "周一 08:00-09:40 高等数学 教三201 1-16周\\n..."}``
* ``{"courses": [{"name": ..., "sessions": [{"weekday": 1, ...}]}]}``

**任何一行解析失败都整体拒绝**，并把失败行原样回传。理由：课表少一节
课不会立刻出错，但之后所有"下节课交"的推断都会静默偏移，比直接报错
难查得多。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.deps import AuthDep, WriteAuthDep
from app.logging import get_logger, kv
from app.services import courses as course_service
from app.services.timetable import ParsedCourse, ParsedSession, format_timetable, parse_timetable

router = APIRouter(prefix="/api/courses", tags=["courses"])
log = get_logger("courses")


class SessionIn(BaseModel):
    weekday: int = Field(ge=1, le=7)
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    start_week: int = Field(default=1, ge=1, le=30)
    end_week: int = Field(default=18, ge=1, le=30)
    week_parity: str = Field(default="all", pattern="^(all|odd|even)$")
    location: str = ""


class CourseIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    location: str = ""
    sessions: list[SessionIn] = Field(default_factory=list)


class CoursesPayload(BaseModel):
    text: str | None = Field(default=None, max_length=20000)
    courses: list[CourseIn] | None = None


def _serialize(request: Request) -> dict[str, Any]:
    db = request.app.state.db
    courses = course_service.load_courses(db)
    grouped = course_service.sessions_by_course(db)
    return {
        "text": format_timetable(courses, grouped),
        "courses": [
            {
                "id": row["id"],
                "name": row["name"],
                "location": row["location"],
                "sessions": [
                    {
                        "weekday": session["weekday"],
                        "start_time": session["start_time"],
                        "end_time": session["end_time"],
                        "start_week": session["start_week"],
                        "end_week": session["end_week"],
                        "week_parity": session["week_parity"],
                        "location": session["location"],
                    }
                    for session in grouped.get(int(row["id"]), [])
                ],
            }
            for row in courses
        ],
        **course_service.counts(db),
    }


@router.get("", summary="读取课表")
def get_courses(request: Request, auth: AuthDep) -> dict[str, Any]:
    return _serialize(request)


@router.put("", summary="整体替换课表")
def put_courses(payload: CoursesPayload, request: Request, auth: WriteAuthDep) -> dict[str, Any]:
    cfg = request.app.state.config

    if payload.courses is not None:
        parsed = [
            ParsedCourse(
                name=course.name.strip(),
                location=course.location.strip(),
                sessions=[
                    ParsedSession(
                        weekday=session.weekday,
                        start_time=session.start_time,
                        end_time=session.end_time,
                        start_week=session.start_week,
                        end_week=session.end_week,
                        week_parity=session.week_parity,
                        location=session.location.strip(),
                    )
                    for session in course.sessions
                ],
            )
            for course in payload.courses
        ]
    elif payload.text is not None:
        outcome = parse_timetable(payload.text, total_weeks=cfg.term.total_weeks)
        if outcome.errors:
            log.warning("courses.parse_failed %s", kv(errors=len(outcome.errors)))
            raise HTTPException(
                status_code=422,
                detail={
                    "message": f"{len(outcome.errors)} 行无法解析，已全部拒绝（避免课表静默缺课）",
                    "errors": [error.as_dict() for error in outcome.errors],
                },
            )
        parsed = outcome.courses
    else:
        raise HTTPException(status_code=422, detail="需要提供 text 或 courses 之一")

    if not parsed:
        raise HTTPException(status_code=422, detail="课表为空；如需清空请显式提交空课程数组")

    course_count, session_count = course_service.replace_all(request.app.state.db, parsed)
    result = _serialize(request)
    result.update({"saved_courses": course_count, "saved_sessions": session_count})
    return result
