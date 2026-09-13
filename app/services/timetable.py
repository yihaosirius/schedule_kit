"""课表文本的**确定性解析**。

刻意不用 LLM：课表结构规整，正则能 100% 可预测地解析，而让模型来做
只会引入不确定性——同一份课表两次解析出不同结果，是没法调试的。

行格式（``|`` 分隔的写法可以消除歧义，推荐）：

    周一 08:00-09:40 高等数学 教三201 1-16周
    周三 10:00-11:40 大学物理 理教105 1-16周 单周
    周一 08:00-09:40 | 高等数学 | 教三201 | 1-16周

空格写法下，第一个词是课程名，其余是地点；课程名含空格时请用 ``|`` 写法。

**解析失败的行原样返回并报错，绝不静默丢弃**——课表少一节课，
之后所有"下节课交"的推断都会错，而且没人会发现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

WEEKDAY_ALIASES: dict[str, int] = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
}

WEEKDAY_RE = re.compile(r"^(?:周|星期|礼拜)([一二三四五六日天1-7])$")
TIME_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*[-~—至]\s*(\d{1,2}):(\d{2})$")
WEEKS_RE = re.compile(r"^第?(\d{1,2})\s*(?:[-~—至]\s*(\d{1,2}))?\s*周?$")
PARITY_RE = re.compile(r"^(单周|双周|每周|单|双|all|odd|even)$")

PARITY_MAP = {
    "单周": "odd", "单": "odd", "odd": "odd",
    "双周": "even", "双": "even", "even": "even",
    "每周": "all", "all": "all",
}

DEFAULT_TOTAL_WEEKS = 18


@dataclass
class ParseError:
    line_number: int
    text: str
    reason: str

    def as_dict(self) -> dict:
        return {"line": self.line_number, "text": self.text, "reason": self.reason}


@dataclass
class ParsedSession:
    weekday: int
    start_time: str
    end_time: str
    start_week: int
    end_week: int
    week_parity: str
    location: str


@dataclass
class ParsedCourse:
    name: str
    location: str
    sessions: list[ParsedSession] = field(default_factory=list)


@dataclass
class ParseOutcome:
    courses: list[ParsedCourse]
    errors: list[ParseError]

    @property
    def session_count(self) -> int:
        return sum(len(course.sessions) for course in self.courses)


def _parse_weekday(token: str) -> int | None:
    match = WEEKDAY_RE.match(token)
    if not match:
        return None
    return WEEKDAY_ALIASES.get(match.group(1))


def _parse_time(token: str) -> tuple[str, str] | None:
    match = TIME_RANGE_RE.match(token)
    if not match:
        return None
    start_h, start_m, end_h, end_m = (int(part) for part in match.groups())
    if not (0 <= start_h <= 23 and 0 <= end_h <= 23 and 0 <= start_m <= 59 and 0 <= end_m <= 59):
        return None
    start = f"{start_h:02d}:{start_m:02d}"
    end = f"{end_h:02d}:{end_m:02d}"
    if end <= start:
        return None
    return start, end


def _parse_weeks(token: str, total_weeks: int) -> tuple[int, int] | None:
    match = WEEKS_RE.match(token)
    if not match:
        return None
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) else first
    if first < 1 or last < first:
        return None
    return first, min(last, max(total_weeks, last))


def _tokenize(line: str) -> list[str]:
    """把一行拆成字段。

    ``|`` 写法下，第一个分段里的「周X 时间」仍需按空格拆开，但**其余分段
    整体作为一个字段**——这样课程名里可以带空格（``高等数学 A``），
    而纯空格写法做不到这一点。
    """
    if "|" not in line:
        return line.split()
    segments = [segment.strip() for segment in line.split("|")]
    head = segments[0].split()
    return head + [segment for segment in segments[1:] if segment]


def parse_line(line: str, *, total_weeks: int = DEFAULT_TOTAL_WEEKS) -> ParsedSession | None:
    """解析单行；无法解析时返回 None（调用方负责记录错误）。

    使用 ``|`` 分隔时可以写成 ``周一 | 08:00-09:40 | 高等数学 | 教三201 | 1-16周``，
    但不强制——先按空格分词，再把可识别的字段从右往左剥掉。
    """
    tokens = _tokenize(line)
    if not tokens:
        return None

    weekday = _parse_weekday(tokens[0])
    if weekday is None:
        return None

    rest = tokens[1:]
    if not rest:
        return None

    times = _parse_time(rest[0])
    if times is None:
        return None
    start_time, end_time = times
    rest = rest[1:]
    if not rest:
        return None

    parity = "all"
    if rest and PARITY_RE.match(rest[-1]):
        parity = PARITY_MAP[rest[-1]]
        rest = rest[:-1]

    start_week, end_week = 1, total_weeks
    if rest and (weeks := _parse_weeks(rest[-1], total_weeks)):
        start_week, end_week = weeks
        rest = rest[:-1]

    if not rest:
        return None

    # 剥到只剩「课程名 [地点]」；第一个词是课程名，其余合并为地点
    location = " ".join(rest[1:]).strip()
    if not rest[0]:
        return None

    return ParsedSession(
        weekday=weekday,
        start_time=start_time,
        end_time=end_time,
        start_week=start_week,
        end_week=end_week,
        week_parity=parity,
        location=location,
    )


def parse_timetable(text: str, *, total_weeks: int = DEFAULT_TOTAL_WEEKS) -> ParseOutcome:
    """解析整份课表。同名课程会合并成一条，含多个上课时段。"""
    courses: dict[str, ParsedCourse] = {}
    errors: list[ParseError] = []

    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # 课程名要在解析时段之前拿到，所以这里单独分词一遍
        tokens = _tokenize(line)
        session = parse_line(line, total_weeks=total_weeks)
        if session is None:
            errors.append(
                ParseError(number, raw_line, "格式无法识别，应形如「周一 08:00-09:40 高等数学 教三201 1-16周」")
            )
            continue

        name = _extract_course_name(tokens)
        if not name:
            errors.append(ParseError(number, raw_line, "缺少课程名"))
            continue

        course = courses.setdefault(name, ParsedCourse(name=name, location=session.location))
        if not course.location and session.location:
            course.location = session.location
        course.sessions.append(session)

    return ParseOutcome(courses=list(courses.values()), errors=errors)


def _extract_course_name(tokens: list[str]) -> str:
    """从分词结果里取出课程名（与 :func:`parse_line` 的剥离规则保持一致）。"""
    if len(tokens) < 3:
        return ""
    rest = tokens[2:]  # 跳过「周X」与时间段
    if rest and PARITY_RE.match(rest[-1]):
        rest = rest[:-1]
    if rest and WEEKS_RE.match(rest[-1]):
        rest = rest[:-1]
    return rest[0].strip() if rest else ""


def format_timetable(courses, sessions_by_course: dict[int, list]) -> str:
    """把库里的课表渲染回文本，便于在页面上直接编辑。"""
    weekday_label = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
    parity_label = {"all": "", "odd": " 单周", "even": " 双周"}
    lines: list[str] = []
    for course in courses:
        for session in sessions_by_course.get(course["id"], []):
            weeks = (
                f"{session['start_week']}周"
                if session["start_week"] == session["end_week"]
                else f"{session['start_week']}-{session['end_week']}周"
            )
            parts = [
                weekday_label.get(session["weekday"], "?"),
                f"{session['start_time']}-{session['end_time']}",
                course["name"],
            ]
            location = session["location"] or course["location"]
            if location:
                parts.append(location)
            line = " ".join(parts) + f" {weeks}"
            if parity_label.get(session["week_parity"]):
                line += parity_label[session["week_parity"]]
            lines.append(line)
    return "\n".join(lines)
