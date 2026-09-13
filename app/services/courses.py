"""课表的存取。

整份替换（而非逐条 CRUD）：个人的课表 ≤20 门课，一次 PUT 提交全部内容
比维护 4 个 REST 端点简单得多。代价是并发编辑会互相覆盖——单用户场景
可以接受（PLAN.md §9.1）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.logging import get_logger, kv
from app.services.timetable import ParsedCourse

log = get_logger("courses")


def load_courses(db) -> list[sqlite3.Row]:
    with db.connect() as conn:
        return list(
            conn.execute("SELECT * FROM courses ORDER BY sort_order ASC, id ASC")
        )


def sessions_by_course(db) -> dict[int, list[sqlite3.Row]]:
    grouped: dict[int, list[sqlite3.Row]] = {}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM course_sessions ORDER BY weekday ASC, start_time ASC"
        )
        for row in rows:
            grouped.setdefault(int(row["course_id"]), []).append(row)
    return grouped


def flat_sessions(db) -> list[dict[str, Any]]:
    """给时间上下文用的扁平列表：每个时段一条，已带上课程名。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT s.weekday, s.start_time, s.end_time, s.start_week, s.end_week,"
            "       s.week_parity, s.location AS session_location,"
            "       c.name AS course_name, c.location AS course_location"
            "  FROM course_sessions s JOIN courses c ON c.id = s.course_id"
            " ORDER BY s.weekday ASC, s.start_time ASC"
        )
        return [
            {
                "weekday": int(row["weekday"]),
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "start_week": int(row["start_week"]),
                "end_week": int(row["end_week"]),
                "week_parity": row["week_parity"],
                "course_name": row["course_name"],
                "location": row["session_location"] or row["course_location"] or "",
            }
            for row in rows
        ]


def replace_all(db, courses: list[ParsedCourse]) -> tuple[int, int]:
    """整体替换课表，返回 (课程数, 时段数)。单个事务完成。"""
    with db.transaction() as conn:
        conn.execute("DELETE FROM courses")  # course_sessions 由外键级联删除
        session_count = 0
        for order, course in enumerate(courses):
            cursor = conn.execute(
                "INSERT INTO courses(name, teacher, location, note, sort_order) VALUES (?, '', ?, '', ?)",
                (course.name, course.location, order),
            )
            course_id = int(cursor.lastrowid)
            for session in course.sessions:
                conn.execute(
                    "INSERT INTO course_sessions(course_id, weekday, start_time, end_time,"
                    " start_week, end_week, week_parity, location)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        course_id,
                        session.weekday,
                        session.start_time,
                        session.end_time,
                        session.start_week,
                        session.end_week,
                        session.week_parity,
                        session.location,
                    ),
                )
                session_count += 1

    log.info("courses.replaced %s", kv(courses=len(courses), sessions=session_count))
    return len(courses), session_count


def counts(db) -> dict[str, int]:
    with db.connect() as conn:
        return {
            "courses": conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0],
            "sessions": conn.execute("SELECT COUNT(*) FROM course_sessions").fetchone()[0],
        }
