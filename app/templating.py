"""Jinja2 模板引擎与共用过滤器。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.paths import STATIC_DIR, TEMPLATES_DIR
from app.timeutil import load_zone, parse_iso

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _static_version() -> str:
    """静态资源的缓存破坏串 —— 全部前端文件内容的哈希。

    为什么需要它：Service Worker 曾经对 ``/static/`` 用 cache-first，于是新的
    CSS / JS 永远到不了已经装过 SW 的浏览器（docs/dev-notes.md §10）。SW 已经
    改成 network-first，但**已经装了旧 SW 的设备**依然会命中旧缓存，而旧 SW
    要等下一次导航才会被替换掉——用户在那一瞬间看到的就是"改了没生效"。

    带版本串的 URL 是**不同的缓存键**，`caches.match()` 匹配不到，直接绕过旧
    缓存。所以它不只是优化，是给这类事故兜底的保险。

    在导入时算一次（几个小文件，几十 KB）。不需要手工维护版本号——内容变了
    它就变，没变就不变。
    """
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file() and path.suffix in {".css", ".js", ".webmanifest"}:
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


#: 模板里用 ``?v={{ asset_version }}`` 引静态资源。
ASSET_VERSION = _static_version()

#: 分类 → 中文标签。顺序即 UI 中的展示顺序。
CATEGORY_LABELS: dict[str, str] = {
    "homework": "作业",
    "practice": "练习",
    "exam": "考试",
    "appointment": "要约",
    "other": "其他",
}

#: 优先级 1..5 → 罗马数字 Ⅰ..Ⅴ
PRIORITY_LABELS: dict[int, str] = {1: "Ⅰ", 2: "Ⅱ", 3: "Ⅲ", 4: "Ⅳ", 5: "Ⅴ"}

#: 任务来源 → 中文标签。``source`` 是客户端自报的，见 docs/api.md §2。
SOURCE_LABELS: dict[str, str] = {
    "web": "网页",
    "shortcut": "快捷指令",
    "llm": "智能录入",
    "api": "接口",
}

#: 草稿状态 → 中文标签。顺序即草稿箱筛选条里的顺序。
DRAFT_STATUS_LABELS: dict[str, str] = {
    "pending": "待确认",
    "confirmed": "已入库",
    "discarded": "已丢弃",
    "failed": "识别失败",
}

WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"]


def category_label(value: str) -> str:
    return CATEGORY_LABELS.get(value, value)


def priority_label(value: int | None) -> str:
    if value is None:
        return ""
    return PRIORITY_LABELS.get(int(value), str(value))


def draft_status_label(value: str) -> str:
    return DRAFT_STATUS_LABELS.get(value, value)


def source_label(value: str | None) -> str:
    if not value:
        return ""
    return SOURCE_LABELS.get(value, value)


def format_local(value: str | None, timezone: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """把存储的 UTC ISO8601 渲染成配置时区的可读时间。"""
    if not value:
        return ""
    try:
        return parse_iso(value).astimezone(load_zone(timezone)).strftime(fmt)
    except (ValueError, TypeError):
        return value


def countdown(value: str | None, timezone: str) -> str:
    """生成人类可读的倒计时/逾期描述。"""
    if not value:
        return ""
    try:
        moment = parse_iso(value).astimezone(load_zone(timezone))
    except (ValueError, TypeError):
        return ""
    now = datetime.now(load_zone(timezone))
    delta = moment - now
    seconds = delta.total_seconds()
    if seconds < 0:
        overdue = abs(seconds)
        if overdue < 3600:
            return f"已逾期 {int(overdue // 60)} 分钟"
        if overdue < 86400:
            return f"已逾期 {int(overdue // 3600)} 小时"
        return f"已逾期 {int(overdue // 86400)} 天"
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} 分钟后"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时后"
    return f"{int(seconds // 86400)} 天后"


def due_state(value: str | None, timezone: str) -> str:
    """给截止时间套一个严重度样式类：逾期 / 24 小时内 / 普通。"""
    if not value:
        return ""
    try:
        moment = parse_iso(value).astimezone(load_zone(timezone))
    except (ValueError, TypeError):
        return ""
    seconds = (moment - datetime.now(load_zone(timezone))).total_seconds()
    if seconds < 0:
        return "is-overdue"
    if seconds < 86400:
        return "is-soon"
    return ""


def _urlencode_filter(value: Any) -> str:
    from urllib.parse import quote

    return quote("" if value is None else str(value), safe="")


def _local_filter(value: str | None, timezone: str, fmt: str = "%m-%d %H:%M") -> str:
    return format_local(value, timezone, fmt)


def _date_filter(value: str | None, timezone: str) -> str:
    return format_local(value, timezone, "%Y-%m-%d")


def _local_input_filter(value: str | None, timezone: str) -> str:
    """渲染 <input type="datetime-local"> 需要的值（本地时间、无时区后缀）。"""
    return format_local(value, timezone, "%Y-%m-%dT%H:%M")


def filesize(value: int | None) -> str:
    """把字节数渲染成人读的形式；采集不到时显示破折号。"""
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


templates.env.filters["category_label"] = category_label
templates.env.filters["priority_label"] = priority_label
templates.env.filters["draft_status_label"] = draft_status_label
templates.env.filters["source_label"] = source_label
templates.env.filters["local"] = _local_filter
templates.env.filters["local_date"] = _date_filter
templates.env.filters["local_input"] = _local_input_filter
templates.env.filters["filesize"] = filesize
templates.env.filters["countdown"] = countdown
templates.env.filters["due_state"] = due_state
templates.env.filters["urlencode_q"] = _urlencode_filter
templates.env.globals["category_label"] = category_label
templates.env.globals["priority_label"] = priority_label
templates.env.globals["draft_status_label"] = draft_status_label
templates.env.globals["source_label"] = source_label
templates.env.globals["WEEKDAY_LABELS"] = WEEKDAY_LABELS
templates.env.globals["DRAFT_STATUS_LABELS"] = DRAFT_STATUS_LABELS
templates.env.globals["SOURCE_LABELS"] = SOURCE_LABELS


def render(request: Request, template_name: str, **context: Any):
    """统一渲染入口：自动注入配置、CSRF 与分类表。"""
    cfg = request.app.state.config
    base_context: dict[str, Any] = {
        "request": request,
        "config": cfg,
        "timezone": cfg.server.timezone,
        "categories": CATEGORY_LABELS,
        "priorities": PRIORITY_LABELS,
        "csrf_token": request.cookies.get("sk_csrf", ""),
        "asset_version": ASSET_VERSION,
    }
    base_context.update(context)
    return templates.TemplateResponse(request, template_name, base_context)
