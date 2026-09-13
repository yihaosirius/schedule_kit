"""时间工具：全系统统一 UTC 存储、按配置时区渲染。"""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo


def now_utc() -> datetime:
    """当前时间（带 UTC 时区）。"""
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    """转为存储用的 UTC ISO8601 字符串（秒精度，带 +00:00）。"""
    moment = dt or now_utc()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """解析 ISO8601 字符串为 aware datetime。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_zone(name: str) -> tzinfo:
    """按名称加载时区。

    绝不抛异常：Windows 上没有系统 tzdata（除非装了 ``tzdata`` 包），
    此时连 ``ZoneInfo("UTC")`` 都会失败。宁可退化成固定偏移也不能让
    记录时间这种基础操作把请求打挂。
    """
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - 任何加载失败都走回退
        pass
    try:
        return ZoneInfo("UTC")
    except Exception:  # noqa: BLE001 - 连 UTC 都没有时用 stdlib 固定偏移
        return timezone.utc
