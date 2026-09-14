"""M4.5：课表解析与时间上下文。

这是"识别更具体化"的核心：把「下节课」「这门课」这类相对指代解析成
具体日期。所以测试重点放在**确定性与边界**上——纯函数算错一个边界，
所有推断都会静默偏移。
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

import pytest
from httpx import AsyncClient

from app.services.context import TermInfo, build_context, parse_term, session_applies
from app.services.timetable import format_timetable, parse_timetable

TZ = "Asia/Shanghai"
#: 2026-03-02 是周一，作为第 1 周；
#: 于是 2026-03-20 是第 3 周的周五，2026-03-23 是第 4 周的周一。
TERM = TermInfo(start_date=__import__("datetime").date(2026, 3, 2), total_weeks=18)


def local_dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """构造一个上海本地时间对应的 aware datetime。"""
    from zoneinfo import ZoneInfo

    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(TZ))


# --------------------------------------------------------------------------- #
# 文本解析
# --------------------------------------------------------------------------- #
def test_parses_basic_line() -> None:
    outcome = parse_timetable("周一 08:00-09:40 高等数学 教三201 1-16周")
    assert outcome.errors == []
    assert len(outcome.courses) == 1
    course = outcome.courses[0]
    assert course.name == "高等数学"
    session = course.sessions[0]
    assert (session.weekday, session.start_time, session.end_time) == (1, "08:00", "09:40")
    assert (session.start_week, session.end_week) == (1, 16)
    assert session.week_parity == "all"
    assert session.location == "教三201"


def test_parses_parity_suffix() -> None:
    outcome = parse_timetable("周三 10:00-11:40 大学物理 理教105 1-16周 单周")
    session = outcome.courses[0].sessions[0]
    assert session.week_parity == "odd"
    assert session.location == "理教105"


def test_parses_pipe_separated_line_with_spaces_in_name() -> None:
    outcome = parse_timetable("周一 08:00-09:40 | 高等数学 A | 教三201 | 1-16周")
    assert outcome.errors == []
    course = outcome.courses[0]
    assert course.name == "高等数学 A"
    assert course.sessions[0].location == "教三201"


def test_defaults_weeks_when_omitted() -> None:
    outcome = parse_timetable("周五 14:00-15:40 形势与政策", total_weeks=18)
    session = outcome.courses[0].sessions[0]
    assert (session.start_week, session.end_week) == (1, 18)


def test_groups_same_course_across_weekdays() -> None:
    outcome = parse_timetable(
        "周一 08:00-09:40 高等数学 教三201 1-16周\n"
        "周三 14:00-15:40 高等数学 教三201 1-16周\n"
        "周五 10:00-11:40 大学物理 理教105 1-8周"
    )
    assert len(outcome.courses) == 2
    by_name = {course.name: course for course in outcome.courses}
    assert len(by_name["高等数学"].sessions) == 2
    assert len(by_name["大学物理"].sessions) == 1


def test_single_week_range_is_supported() -> None:
    outcome = parse_timetable("周二 08:00-09:40 讲座 报告厅 5周")
    session = outcome.courses[0].sessions[0]
    assert (session.start_week, session.end_week) == (5, 5)


def test_weekday_aliases_are_accepted() -> None:
    for text in ("星期一 08:00-09:40 课 1-2周", "周1 08:00-09:40 课 1-2周"):
        outcome = parse_timetable(text)
        assert outcome.errors == [], text
        assert outcome.courses[0].sessions[0].weekday == 1


def test_comments_and_blank_lines_are_ignored() -> None:
    outcome = parse_timetable("# 这是注释\n\n周一 08:00-09:40 高等数学 教三201 1-16周\n")
    assert outcome.errors == []
    assert len(outcome.courses) == 1


@pytest.mark.parametrize(
    "line",
    [
        "今天 08:00-09:40 高等数学",       # 星期无法识别
        "周一 8点-9点40 高等数学",          # 时间格式不对
        "周一 09:40-08:00 高等数学",        # 结束早于开始
        "周一 08:00-09:40",                # 缺课程名
    ],
)
def test_bad_lines_are_reported_not_silently_dropped(line: str) -> None:
    outcome = parse_timetable(line)
    assert outcome.courses == []
    assert len(outcome.errors) == 1
    assert outcome.errors[0].line_number == 1
    assert outcome.errors[0].text == line


def test_good_and_bad_lines_together_report_only_bad_ones() -> None:
    outcome = parse_timetable(
        "周一 08:00-09:40 高等数学 教三201 1-16周\n"
        "这行是错的\n"
        "周三 10:00-11:40 大学物理 理教105 1-16周\n"
    )
    assert len(outcome.courses) == 2
    assert [error.line_number for error in outcome.errors] == [2]


def test_format_round_trips_back_to_parseable_text() -> None:
    outcome = parse_timetable("周三 10:00-11:40 大学物理 理教105 1-16周 单周")
    courses = [{"id": 1, "name": "大学物理", "location": "理教105"}]
    sessions = {
        1: [
            {
                "weekday": 3, "start_time": "10:00", "end_time": "11:40",
                "start_week": 1, "end_week": 16, "week_parity": "odd", "location": "理教105",
            }
        ]
    }
    text = format_timetable(courses, sessions)
    assert text == "周三 10:00-11:40 大学物理 理教105 1-16周 单周"
    assert parse_timetable(text).errors == []
    assert len(outcome.courses[0].sessions) == 1


# --------------------------------------------------------------------------- #
# 周次与单双周
# --------------------------------------------------------------------------- #
def test_week_number_computation() -> None:
    import datetime as dt

    assert TERM.week_of(dt.date(2026, 3, 2)) == 1
    assert TERM.week_of(dt.date(2026, 3, 8)) == 1
    assert TERM.week_of(dt.date(2026, 3, 9)) == 2
    assert TERM.week_of(dt.date(2026, 3, 20)) == 3
    assert TERM.week_of(dt.date(2026, 3, 23)) == 4
    # 学期前后
    assert TERM.week_of(dt.date(2026, 2, 1)) == 0
    assert TERM.week_of(dt.date(2027, 1, 1)) == 0


def test_parse_term_rejects_garbage() -> None:
    assert parse_term("不是日期", 18) is None
    assert parse_term("", 18) is None
    assert parse_term("2026-03-02", 18) is not None


def _session(**overrides) -> dict:
    base = {
        "weekday": 1, "start_time": "08:00", "end_time": "09:40",
        "start_week": 1, "end_week": 16, "week_parity": "all",
        "course_name": "高等数学", "location": "教三201",
    }
    base.update(overrides)
    return base


def test_session_applies_respects_week_range_and_parity() -> None:
    assert session_applies(_session(), 3) is True
    assert session_applies(_session(start_week=5, end_week=8), 3) is False
    assert session_applies(_session(start_week=5, end_week=8), 6) is True
    assert session_applies(_session(week_parity="odd"), 3) is True
    assert session_applies(_session(week_parity="odd"), 4) is False
    assert session_applies(_session(week_parity="even"), 4) is True
    assert session_applies(_session(week_parity="even"), 3) is False


# --------------------------------------------------------------------------- #
# 上下文生成
# --------------------------------------------------------------------------- #
def test_context_without_terM_has_only_time() -> None:
    context = build_context(local_dt(2026, 3, 20, 14, 32), timezone=TZ)
    assert "[当前时间]" in context
    assert "星期" in context
    assert "第 3 周" not in context


def test_context_reports_week_number() -> None:
    context = build_context(local_dt(2026, 3, 20, 14, 32), timezone=TZ, term=TERM)
    assert "第 3 周" in context


def test_context_says_out_of_term_when_outside() -> None:
    context = build_context(local_dt(2026, 1, 5, 9, 0), timezone=TZ, term=TERM)
    assert "非学期周" in context
    assert "正在进行" not in context


def test_context_reports_just_ended_class() -> None:
    """2026-03-20 是第 3 周的周五，10:00-11:40 有课；12:10 时刚结束 30 分钟。"""
    sessions = [_session(weekday=5, start_time="10:00", end_time="11:40")]
    context = build_context(local_dt(2026, 3, 20, 12, 10), timezone=TZ, term=TERM, sessions=sessions)
    assert "[刚刚结束]" in context
    assert "已结束 30 分钟" in context


def test_context_does_not_report_long_past_class() -> None:
    """超出「刚刚结束」窗口（60 分钟）的课不该再冒出来。"""
    sessions = [_session(weekday=5, start_time="10:00", end_time="11:40")]
    context = build_context(local_dt(2026, 3, 20, 14, 30), timezone=TZ, term=TERM, sessions=sessions)
    assert "[刚刚结束]" not in context
    assert "[正在进行]" not in context


def test_context_reports_ongoing_class() -> None:
    sessions = [_session(weekday=5, start_time="14:00", end_time="15:40")]
    context = build_context(local_dt(2026, 3, 20, 14, 32), timezone=TZ, term=TERM, sessions=sessions)
    assert "[正在进行]" in context
    assert "已进行 32 分钟" in context


def test_context_reports_upcoming_class_within_window() -> None:
    sessions = [_session(weekday=5, start_time="15:00", end_time="16:40")]
    context = build_context(local_dt(2026, 3, 20, 14, 32), timezone=TZ, term=TERM, sessions=sessions)
    assert "[即将开始]" in context
    assert "28 分钟" in context


def test_context_puts_far_away_class_into_today_rest() -> None:
    sessions = [_session(weekday=5, start_time="19:00", end_time="20:40")]
    context = build_context(local_dt(2026, 3, 20, 9, 0), timezone=TZ, term=TERM, sessions=sessions)
    assert "[即将开始]" not in context
    assert "[今日其余]" in context
    assert "19:00" in context


def test_context_ignores_class_not_this_week() -> None:
    """第 3 周是单周；标记为双周的课不该出现。"""
    sessions = [_session(weekday=5, start_time="14:00", end_time="15:40", week_parity="even")]
    context = build_context(local_dt(2026, 3, 20, 14, 32), timezone=TZ, term=TERM, sessions=sessions)
    assert "[正在进行]" not in context


def test_context_excludes_out_of_range_weeks() -> None:
    sessions = [_session(weekday=5, start_time="14:00", end_time="15:40", start_week=10, end_week=16)]
    context = build_context(local_dt(2026, 3, 20, 14, 32), timezone=TZ, term=TERM, sessions=sessions)
    assert "[正在进行]" not in context


def test_context_lists_rest_of_week() -> None:
    sessions = [_session(weekday=6, start_time="10:00", end_time="11:40", course_name="形势与政策")]
    context = build_context(local_dt(2026, 3, 20, 14, 32), timezone=TZ, term=TERM, sessions=sessions)
    assert "[本周剩余]" in context
    assert "周六 10:00 形势与政策" in context


def test_context_carries_usage_rules_to_prevent_over_association() -> None:
    sessions = [_session(weekday=5, start_time="14:00", end_time="15:40")]
    context = build_context(local_dt(2026, 3, 20, 14, 32), timezone=TZ, term=TERM, sessions=sessions)
    assert "不得据此推断内容归属" in context
    assert "以内容为准" in context


def test_context_boundary_exactly_at_start_and_end() -> None:
    """边界时刻：刚好开始算"正在进行"，刚好结束也算"正在进行"。"""
    sessions = [_session(weekday=5, start_time="14:00", end_time="15:40")]
    at_start = build_context(local_dt(2026, 3, 20, 14, 0), timezone=TZ, term=TERM, sessions=sessions)
    assert "[正在进行]" in at_start

    at_end = build_context(local_dt(2026, 3, 20, 15, 40), timezone=TZ, term=TERM, sessions=sessions)
    assert "[正在进行]" in at_end


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
async def test_courses_api_round_trip(session_client: AsyncClient) -> None:
    response = await session_client.put(
        "/api/courses",
        json={"text": "周一 08:00-09:40 高等数学 教三201 1-16周\n周三 10:00-11:40 大学物理 理教105 1-16周 单周"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved_courses"] == 2
    assert body["saved_sessions"] == 2

    fetched = (await session_client.get("/api/courses")).json()
    assert "高等数学" in fetched["text"]

    # 结构化课表必须真的能取到。
    #
    # 这里曾经断言 fetched["courses"] == 2 —— 而那个 2 是**计数**：
    # `**counts()` 里的 "courses" 键把上面刚构造好的课程数组覆盖掉了，
    # 于是结构化课表整整一版都取不到，用例却在给这个 bug 背书。
    assert fetched["course_count"] == 2
    assert fetched["session_count"] == 2
    assert [course["name"] for course in fetched["courses"]] == ["高等数学", "大学物理"]
    assert fetched["courses"][0]["sessions"][0]["start_time"] == "08:00"


async def test_courses_api_rejects_whole_batch_on_any_bad_line(session_client: AsyncClient) -> None:
    response = await session_client.put(
        "/api/courses",
        json={"text": "周一 08:00-09:40 高等数学 教三201 1-16周\n坏行\n"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["errors"][0]["line"] == 2
    assert "坏行" in detail["errors"][0]["text"]

    # 关键：一条都不该入库
    empty = (await session_client.get("/api/courses")).json()
    assert empty["courses"] == []
    assert empty["course_count"] == 0


async def test_courses_api_accepts_structured_input(session_client: AsyncClient) -> None:
    response = await session_client.put(
        "/api/courses",
        json={
            "courses": [
                {
                    "name": "线性代数",
                    "location": "教二101",
                    "sessions": [
                        {"weekday": 2, "start_time": "08:00", "end_time": "09:40", "start_week": 1, "end_week": 16}
                    ],
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["saved_sessions"] == 1


async def test_courses_page_renders(session_client: AsyncClient) -> None:
    await session_client.put("/api/courses", json={"text": "周一 08:00-09:40 高等数学 教三201 1-16周"})
    html = (await session_client.get("/courses")).text
    assert "课表" in html
    assert "高等数学" in html
    assert "第 1 周周一" in html
    assert "data-courses-form" in html


async def test_courses_can_actually_be_cleared(session_client: AsyncClient) -> None:
    """清空课表必须真的可行。

    原先 `if not parsed: raise 422 "如需清空请显式提交空课程数组"` 把
    **所有**空输入都挡了，包括它自己推荐的"提交空课程数组"。网页上把文本框
    清掉再保存也只会看到那句自相矛盾的提示——课表一旦写进去就再也删不掉。
    """
    line = {"text": "周一 08:00-09:40 高等数学 教三201 1-16周"}
    assert (await session_client.put("/api/courses", json=line)).json()["course_count"] == 1

    # ① 结构化空数组：这是错误提示原本推荐的做法
    cleared = await session_client.put("/api/courses", json={"courses": []})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["course_count"] == 0
    assert (await session_client.get("/api/courses")).json()["courses"] == []

    # ② 文本框清空：网页上就是这个路径
    await session_client.put("/api/courses", json=line)
    blank = await session_client.put("/api/courses", json={"text": "   \n  "})
    assert blank.status_code == 200, blank.text
    assert blank.json()["course_count"] == 0


async def test_comment_only_text_does_not_silently_wipe_the_timetable(
    session_client: AsyncClient,
) -> None:
    """纯注释不是"清空"的表达方式，不该被当成清空。

    这是清空功能与防误删之间那条线：空白 = 清空；有内容却解析不出课程 = 报错。
    """
    await session_client.put(
        "/api/courses", json={"text": "周一 08:00-09:40 高等数学 教三201 1-16周"}
    )
    response = await session_client.put("/api/courses", json={"text": "# 只是备注\n"})
    assert response.status_code == 422
    assert "没有解析出任何课程" in response.json()["detail"]
    # 课表原封不动
    assert (await session_client.get("/api/courses")).json()["course_count"] == 1


async def test_courses_require_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/courses")).status_code == 401
    assert (await client.put("/api/courses", json={"text": "x"})).status_code == 401


async def test_timetable_reaches_the_llm_prompt(app, session_client: AsyncClient) -> None:
    """整条链路：录入课表 → 图片识别时上下文里带上课程。"""
    from tests.test_ingest import image_ingest, use_llm

    await session_client.put("/api/courses", json={"text": "周一 08:00-09:40 高等数学 教三201 1-16周"})
    use_llm(app, [{"title": "x", "category": "other", "due_at": None, "priority": 3}])

    _, body = await image_ingest(session_client)
    # 课表已生效：上下文里出现了学期周次（是否含课程取决于运行时是否是周一）
    assert "周" in body["context_snapshot"]
    assert "[当前时间]" in body["context_snapshot"]
