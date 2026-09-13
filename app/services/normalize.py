"""服务端强制后处理：**不信任模型输出**。

结构化输出（function calling）保证的是"格式符合 schema"，
保证不了"语义正确"。例如模型很可能同时给出 ``due_at`` 与 ``priority``，
而"有 deadline 就忽略优先级"是业务规则——没有任何解码器能替我们执行。

因此每条规则都在这里再落实一遍，并且**每次改动模型输出都打一条日志**，
否则事后无法判断是模型错了还是后处理改的（见 docs/dev-notes.md §1）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.llm.tools import CATEGORY_ENUM, MAX_ITEMS, NOTES_MAX, QUOTE_MAX, TITLE_MAX
from app.logging import get_logger, kv
from app.services.tasks import normalize_due_at

log = get_logger("normalize")

DEFAULT_PRIORITY = 3
TITLE_HARD_MAX = 200
NOTES_HARD_MAX = 2000
QUOTE_HARD_MAX = 200


def _text(value: Any, *, limit: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _coerce_priority(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(5, number))


def normalize_items(
    raw_items: list[Any], *, timezone: str, limit: int = MAX_ITEMS
) -> list[dict[str, Any]]:
    """把模型输出规整成可直接入库的形状。

    返回的每一项都带 ``adjustments``，记录这一条被后处理改动过什么，
    便于在确认页和日志里解释"为什么和模型说的不一样"。
    """
    normalized: list[dict[str, Any]] = []

    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            log.warning("normalize.dropped %s", kv(index=index, reason="not-an-object"))
            continue

        adjustments: list[str] = []

        title = _text(raw.get("title"), limit=TITLE_HARD_MAX)
        if not title:
            log.warning("normalize.dropped %s", kv(index=index, reason="empty-title"))
            continue

        category = _text(raw.get("category"), limit=40) or "other"
        if category not in CATEGORY_ENUM:
            adjustments.append("category-downgraded")
            log.warning(
                "normalize.category_downgraded %s", kv(index=index, from_value=category, to_value="other")
            )
            category = "other"

        # deadline 优先：只要能解析出时间，就丢弃模型给的优先级
        due_at: str | None = None
        raw_due = raw.get("due_at")
        if raw_due:
            try:
                due_at = normalize_due_at(str(raw_due), timezone)
            except ValueError:
                adjustments.append("due-unparsable")
                log.warning("normalize.due_unparsable %s", kv(index=index, raw_due=str(raw_due)[:60]))
                due_at = None

        model_priority = _coerce_priority(raw.get("priority"))
        if raw.get("priority") is not None and model_priority != raw.get("priority"):
            if model_priority is None:
                adjustments.append("priority-unparsable")
                log.warning(
                    "normalize.priority_unparsable %s", kv(index=index, raw_priority=str(raw.get("priority"))[:40])
                )
            else:
                adjustments.append("priority-clamped")

        priority: int | None
        needs_priority = False
        if due_at is not None:
            if model_priority is not None:
                adjustments.append("priority-dropped")
                log.info(
                    "normalize.priority_dropped %s",
                    kv(index=index, reason="deadline-wins", dropped_priority=model_priority, due_at=due_at),
                )
            priority = None
        elif model_priority is None:
            # 既没时间也没优先级：给个默认档，并标记出来让确认页高亮
            priority = DEFAULT_PRIORITY
            needs_priority = True
            adjustments.append("priority-defaulted")
            log.info("normalize.needs_priority %s", kv(index=index, defaulted=DEFAULT_PRIORITY))
        else:
            priority = model_priority

        item = {
            "title": title[:TITLE_MAX] if len(title) > TITLE_MAX else title,
            "category": category,
            "due_at": due_at,
            "priority": priority,
            "notes": _text(raw.get("notes"), limit=min(NOTES_MAX, NOTES_HARD_MAX)),
            "source_quote": _text(raw.get("source_quote"), limit=min(QUOTE_MAX, QUOTE_HARD_MAX)),
            "needs_priority": needs_priority,
            "adjustments": adjustments,
        }
        normalized.append(item)

        if len(normalized) >= limit:
            remaining = len(raw_items) - index - 1
            if remaining > 0:
                log.warning("normalize.truncated %s", kv(limit=limit, dropped=remaining))
            break

    log.info(
        "normalize.done %s",
        kv(input=len(raw_items), kept=len(normalized), adjusted=sum(1 for i in normalized if i["adjustments"])),
    )
    return normalized


def items_payload(normalized: list[dict[str, Any]]) -> dict[str, Any]:
    """草稿存库用的外形。"""
    return {"items": normalized}
