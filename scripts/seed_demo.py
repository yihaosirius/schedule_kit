"""播种演示数据，用于在本地查看前端界面。

    uv run python scripts/seed_demo.py --reset

会写入：六条有序任务（覆盖逾期 / 5 小时内 / 数天后 / 数周后，用于看颜色分级）、
五条无序任务（Ⅰ–Ⅴ 各一条）、一张完整周课表，以及**一条待确认草稿**
（带合成图片，用于看确认页）。

``--reset`` 会清空 items / ingest_drafts / courses —— 这是开发库，
但脚本仍会先把将要做的事打印出来。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.llm.base import PATH_TOOL_CALL  # noqa: E402
from app.migrations.runner import apply_migrations  # noqa: E402
from app.services import context as context_service  # noqa: E402
from app.services import courses as course_service  # noqa: E402
from app.services import drafts as draft_service  # noqa: E402
from app.services.timetable import parse_timetable  # noqa: E402
from app.timeutil import now_utc, utc_iso  # noqa: E402

# ── 演示内容 ────────────────────────────────────────────────────────
# 相对「现在」的偏移，这样倒计时和颜色分级在任何时候跑都合理
ORDERED_TASKS = [
    # (标题, 分类, 距现在的小时数, 备注)
    ("交实验报告", "homework", -3, "交到学委处"),
    ("高数第三章习题 3.1-3.5", "homework", 5, ""),
    ("英语期中考试", "exam", 48, "教三 201，带学生证"),
    ("小组项目会议", "appointment", 120, "讨论分工"),
    ("物理实验预习报告", "practice", 288, ""),
    ("提交下学期选课申请", "other", 600, ""),
]

UNORDERED_TASKS = [
    ("准备答辩提纲", "practice", 1, "先列三级标题"),
    ("归还图书馆借的书", "other", 2, ""),
    ("买打印纸", "other", 3, ""),
    ("整理上学期笔记", "practice", 4, ""),
    ("研究一下 RSS 阅读器", "other", 5, ""),
]

TIMETABLE = """\
# 演示课表。改完在 /courses 页面保存即可。
周一 08:00-09:40 高等数学 教三201 1-16周
周一 14:00-15:40 大学物理 理教105 1-16周
周二 10:00-11:40 线性代数 教二101 1-16周
周三 08:00-09:40 高等数学 教三201 1-16周
周三 14:00-15:40 大学物理实验 物理楼302 1-16周 双周
周四 10:00-11:40 英语听说 外语楼302 1-16周
周五 14:00-15:40 形势与政策 报告厅 1-8周
"""

#: 草稿里的条目 —— 模拟"拍了一张群通知截图"的识别结果
DRAFT_ITEMS = [
    {
        "title": "第三章习题 3.1-3.5",
        "category": "homework",
        "due_at": None,  # 下面按相对时间填
        "priority": None,
        "notes": "交到学委处",
        "source_quote": "下周五前交到学委处",
        "needs_priority": False,
        "adjustments": [],
    },
    {
        "title": "实验课调课",
        "category": "appointment",
        "due_at": None,
        "priority": None,
        "notes": "下周一实验课调到周三 14:00，物理楼 302",
        "source_quote": "下周一实验课调到周三",
        "needs_priority": False,
        "adjustments": ["priority-dropped"],
    },
]

NOTICE_LINES = [
    ("@全体成员", 26),
    ("第三章习题 3.1-3.5 下周五前交到学委处", 30),
    ("另外：下周一实验课调到周三 14:00，物理楼 302", 30),
    ("收到请回复", 26),
]


def make_notice_image(width: int = 760, height: int = 520) -> bytes:
    """合成一张"群通知截图"，让确认页有图可看。"""
    import io

    from PIL import Image, ImageDraw, ImageFont

    def font(size: int):
        for candidate in (
            r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
        return ImageFont.load_default()

    image = Image.new("RGB", (width, height), (237, 237, 237))
    draw = ImageDraw.Draw(image)

    # 聊天气泡
    draw.rounded_rectangle([48, 120, width - 48, height - 120], radius=12, fill=(255, 255, 255))
    y = 158
    for text, size in NOTICE_LINES:
        draw.text((78, y), text, fill=(28, 30, 33), font=font(size))
        y += size + 26

    draw.text((48, 60), "课程群 · 刚刚", fill=(120, 128, 138), font=font(22))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ── 播种 ────────────────────────────────────────────────────────────
def reset_tables(db: Database) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM ingest_drafts")
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM courses")  # course_sessions 由外键级联


def seed_tasks(db: Database) -> None:
    now = now_utc()
    stamp = utc_iso(now)
    with db.transaction() as conn:
        for title, category, offset_hours, notes in ORDERED_TASKS:
            due = utc_iso(now + timedelta(hours=offset_hours))
            conn.execute(
                "INSERT INTO items(title, notes, category, due_at, priority, status, source,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, NULL, 'open', 'web', ?, ?)",
                (title, notes, category, due, stamp, stamp),
            )
        for title, category, priority, notes in UNORDERED_TASKS:
            conn.execute(
                "INSERT INTO items(title, notes, category, due_at, priority, status, source,"
                " created_at, updated_at) VALUES (?, ?, ?, NULL, ?, 'open', 'web', ?, ?)",
                (title, notes, category, priority, stamp, stamp),
            )
        # 一条已完成，用于验证默认视图不显示它
        conn.execute(
            "INSERT INTO items(title, notes, category, due_at, priority, status, source,"
            " created_at, updated_at, completed_at) VALUES (?, '', 'other', NULL, 3, 'done', 'web', ?, ?, ?)",
            ("已经做完的事", stamp, stamp, stamp),
        )


def seed_courses(db: Database, total_weeks: int) -> tuple[int, int]:
    outcome = parse_timetable(TIMETABLE, total_weeks=total_weeks)
    if outcome.errors:
        for error in outcome.errors:
            print(f"    [!] 课表第 {error.line_number} 行解析失败：{error.reason}")
    return course_service.replace_all(db, outcome.courses)


def seed_draft(db: Database, config: Config) -> int:
    from app.media import store_image

    image = make_notice_image()
    stored = store_image(
        image,
        uploads_dir=config.server.data_dir / "uploads",
        max_bytes=config.llm.max_image_bytes,
    )

    now = now_utc()
    # 下周五 23:59 —— 演示"相对时间被解析成具体日期"
    days_ahead = (4 - now.weekday()) % 7 or 7
    next_friday = (now + timedelta(days=days_ahead)).replace(hour=23, minute=59, second=0, microsecond=0)

    items = []
    for index, item in enumerate(DRAFT_ITEMS):
        entry = dict(item)
        entry["due_at"] = utc_iso(next_friday + timedelta(days=7 * index))
        items.append(entry)

    term = context_service.parse_term(config.term.start_date, config.term.total_weeks)
    snapshot = context_service.build_context(
        now, timezone=config.server.timezone, term=term, sessions=[]
    )

    row = draft_service.create_draft(
        db,
        channel="image",
        items=items,
        ttl_hours=config.ingest.confirm_ttl_hours,
        image_path=stored.relative_path,
        image_sha256=stored.sha256,
        context_snapshot=snapshot,
        llm_provider="responses",
        llm_model=config.llm.model,
        llm_raw='{"items":[...]}  （演示数据）',
        llm_path=PATH_TOOL_CALL,
    )
    return int(row["id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="播种演示数据")
    parser.add_argument("-c", "--config", default=str(PROJECT_ROOT / "config.toml"))
    parser.add_argument("--reset", action="store_true", help="先清空 tasks / drafts / courses")
    args = parser.parse_args(argv)

    config = Config(args.config)
    db = Database(config.server.data_dir / "schedulekit.db")
    apply_migrations(db)

    print(f"配置文件：{config.path}")
    print(f"数据库　：{db.path}")

    if args.reset:
        print("\n清空 items / ingest_drafts / courses …")
        reset_tables(db)
    else:
        print("\n[提示] 未指定 --reset，将在现有数据之上追加")

    print("\n写入任务 …")
    seed_tasks(db)
    ordered = len(ORDERED_TASKS)
    unordered = len(UNORDERED_TASKS)
    print(f"    {ordered} 条有序 + {unordered} 条无序 + 1 条已完成")

    print("\n写入课表 …")
    courses, sessions = seed_courses(db, config.term.total_weeks)
    print(f"    {courses} 门课 / {sessions} 个时段")

    print("\n写入待确认草稿 …")
    draft_id = seed_draft(db, config)
    print(f"    draft_id = {draft_id}")

    base = config.server.public_url.rstrip("/")
    print("\n完成。打开：")
    print(f"    首页      {base}/")
    print(f"    课表      {base}/courses")
    print(f"    控制台    {base}/settings")
    print(f"    确认页    {base}/drafts/{draft_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
