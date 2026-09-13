"""时间上下文：给 LLM 的"现在是什么时候、正在上哪门课"。

**纯函数、确定性**——把不确定性留给模型，把能算准的都算准。让模型自己
推"今天是第几周、这门课今天上不上"既浪费 token 又不可靠。

注入位置很关键：**拼进 user message，绝不拼进 system prompt**。
上下文分钟级变化，进 system prompt 会让 prompt 缓存永久失效（PLAN.md §9.3）。

另一个关键点：模型很容易"过度联想"（看到"正在进行：高等数学"就把无关内容
归到这门课名下），所以产出里必须自带使用约束，见 :data:`USAGE_RULES`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from app.logging import get_logger, kv
from app.timeutil import load_zone

log = get_logger("context")

WEEKDAY_CN = ("一", "二", "三", "四", "五", "六", "日")

#: 距开始多久算"即将开始"；结束后多久还算"刚刚结束"
UPCOMING_WINDOW = timedelta(minutes=60)
JUST_ENDED_WINDOW = timedelta(minutes=60)

#: 跟随上下文一起注入的使用约束。放在上下文块里而不是 system prompt，
#: 是为了让 system prompt 保持静态（prompt 缓存友好）。
USAGE_RULES = (
    "（以上上下文仅用于解析相对时间与课程指代；不得据此推断内容归属，"
    "内容与课表无关时忽略它；内容中已有明确日期时以内容为准）"
)


@dataclass(frozen=True)
class TermInfo:
    """学期信息。``start_date`` 是第 1 周的周一。"""

    start_date: date
    total_weeks: int

    def week_of(self, day: date) -> int:
        """返回该日期属于第几周（1 起）；不在学期内时返回 0。"""
        delta = (day - self.start_date).days
        if delta < 0:
            return 0
        week = delta // 7 + 1
        return week if week <= self.total_weeks else 0


def parse_term(start_date: str, total_weeks: int) -> TermInfo | None:
    """解析配置里的学期起始日；格式不对就返回 None 而不是崩溃。"""
    if not start_date:
        return None
    try:
        parsed = date.fromisoformat(start_date.strip())
    except ValueError:
        log.warning("context.term_unparsable %s", kv(start_date=start_date))
        return None
    return TermInfo(start_date=parsed, total_weeks=max(1, total_weeks))


# --------------------------------------------------------------------------- #
# 计算
# --------------------------------------------------------------------------- #
def _at(moment: datetime, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return moment.replace(hour=hour, minute=minute, second=0, microsecond=0)


def session_applies(session: dict[str, Any], week_number: int) -> bool:
    """该时段在这一周是否上课（考虑起止周与单双周）。"""
    if week_number <= 0:
        return False
    if not (session["start_week"] <= week_number <= session["end_week"]):
        return False
    parity = session.get("week_parity", "all")
    if parity == "odd":
        return week_number % 2 == 1
    if parity == "even":
        return week_number % 2 == 0
    return True


def _describe(session: dict[str, Any], location_sep: str = " · ") -> str:
    parts = [session["course_name"]]
    if session.get("location"):
        parts.append(session["location"])
    parts.append(f"{session['start_time']}–{session['end_time']}")
    return location_sep.join(parts)


def _minutes(delta: timedelta) -> str:
    total = int(abs(delta.total_seconds()) // 60)
    if total < 60:
        return f"{total} 分钟"
    hours, minutes = divmod(total, 60)
    return f"{hours} 小时" if minutes == 0 else f"{hours} 小时 {minutes} 分钟"


def _course_lines(
    local: datetime, week_number: int, sessions: Sequence[dict[str, Any]]
) -> list[str]:
    if week_number <= 0 or not sessions:
        return []

    today = local.weekday() + 1  # 1=周一 … 7=周日
    todays = sorted(
        (s for s in sessions if s["weekday"] == today and session_applies(s, week_number)),
        key=lambda s: s["start_time"],
    )

    current: list[str] = []
    just_ended: list[str] = []
    upcoming: list[str] = []
    later: list[str] = []

    for session in todays:
        start = _at(local, session["start_time"])
        end = _at(local, session["end_time"])
        if start <= local <= end:
            current.append(f"{_describe(session)}（已进行 {_minutes(local - start)}）")
        elif local < start:
            if start - local <= UPCOMING_WINDOW:
                upcoming.append(f"{_describe(session)}（{_minutes(start - local)}后）")
            else:
                later.append(f"{_describe(session)}")
        elif local - end <= JUST_ENDED_WINDOW:
            just_ended.append(f"{_describe(session)}（已结束 {_minutes(local - end)}）")

    lines: list[str] = []
    if current:
        lines.append("[正在进行] " + "；".join(current))
    if just_ended:
        lines.append("[刚刚结束] " + "；".join(just_ended))
    if upcoming:
        lines.append("[即将开始] " + "；".join(upcoming))
    if later:
        lines.append("[今日其余] " + "；".join(later))
    elif not (current or just_ended or upcoming):
        lines.append("[今日其余] 无")

    # 本周剩余：今天之后的日期里，本周仍需上课的时段
    rest_of_week: list[str] = []
    for session in sessions:
        if session["weekday"] <= today:
            continue
        if not session_applies(session, week_number):
            continue
        rest_of_week.append(
            f"{'周' + WEEKDAY_CN[session['weekday'] - 1]} {session['start_time']} {session['course_name']}"
        )
    if rest_of_week:
        lines.append("[本周剩余] " + "；".join(sorted(rest_of_week, key=lambda s: (s[1:2], s))))

    return lines


def build_context(
    now: datetime,
    *,
    timezone: str,
    term: TermInfo | None = None,
    sessions: Sequence[dict[str, Any]] = (),
) -> str:
    """产出注入给模型的上下文块（约 100–150 token）。"""
    zone = load_zone(timezone)
    local = now.astimezone(zone)
    weekday = WEEKDAY_CN[local.weekday()]
    week_number = term.week_of(local.date()) if term else 0

    if term is None:
        head = f"[当前时间] {local:%Y-%m-%d} 星期{weekday} {local:%H:%M}"
    elif week_number == 0:
        head = f"[当前时间] {local:%Y-%m-%d} 星期{weekday} {local:%H:%M}（非学期周）"
    else:
        head = f"[当前时间] {local:%Y-%m-%d} 星期{weekday} {local:%H:%M}（第 {week_number} 周）"

    lines = [head]
    lines.extend(_course_lines(local, week_number, sessions))
    if len(lines) > 1:
        lines.append(USAGE_RULES)

    context = "\n".join(lines)
    log.info(
        "context.built %s",
        kv(in_term=week_number > 0, week=week_number, lines=len(lines),
           chars=len(context), sessions=len(sessions)),
    )
    return context


def combine_user_message(context: str, body: str) -> str:
    """把上下文与待识别内容拼成一条 user message。

    顺序固定为上下文在前、内容在后：内容才是任务，上下文只是辅助。
    """
    return f"{context}\n\n[待识别内容]\n{body}".strip()
