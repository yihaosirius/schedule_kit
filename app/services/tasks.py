"""任务的排序、筛选与二选一约束落实。

两条领域规则在这里强制执行，不依赖调用方自觉：

1. **deadline 与 priority 严格二选一**（数据库 CHECK 是最后一道防线）。
2. **提供 deadline 时，deadline 胜出**——更新时把 ``due_at`` 设为非空
   会自动清空 ``priority``，反之亦然。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Iterable

from app.logging import get_logger, kv
from app.timeutil import load_zone, utc_iso

log = get_logger("tasks")

STATUS_ORDER = ("open", "done", "cancelled")


class TaskNotFound(LookupError):
    """指定 id 的任务不存在。"""


class TaskConflict(RuntimeError):
    """请求与现有状态冲突（例如唯一键重复）。"""


def normalize_due_at(value: datetime | str | None, timezone: str) -> str | None:
    """把入参时间统一成 UTC ISO8601 字符串。

    无时区信息的时间按**配置时区**解释——这是 LLM 输出最常见的情况，
    也让 "下周五 23:59" 这类输入有确定含义。

    接受 ``str`` 是为了让 service 层也能被非 HTTP 调用方（草稿确认、
    测试、未来的 MCP 工具）直接使用，不必先构造 datetime。
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"无法解析时间：{value}") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=load_zone(timezone))
    return utc_iso(value)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def list_items(
    db,
    *,
    view: str = "ordered",
    status: str | None = None,
    category: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """按视图返回任务。

    * ``ordered``：有 deadline，按时间升序（最紧迫的在前）
    * ``unordered``：只有优先级，先按 Ⅰ→Ⅴ，同档再按创建时间
    """
    if view == "ordered":
        where = ["due_at IS NOT NULL"]
        order = "ORDER BY due_at ASC, id ASC"
    elif view == "unordered":
        where = ["priority IS NOT NULL"]
        order = "ORDER BY priority ASC, created_at ASC, id ASC"
    else:
        raise ValueError(f"未知视图：{view}")

    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if category:
        where.append("category = ?")
        params.append(category)

    sql = f"SELECT * FROM items WHERE {' AND '.join(where)} {order}"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))

    with db.connect() as conn:
        return list(conn.execute(sql, params))


#: 已完成面板最多显示多少条。渲染端与 refresh() 必须一致，
#: 所以它由页面路由传给模板、再由模板交给 JS，不在前端另写一个常数。
COMPLETED_LIMIT = 30


def list_completed(db, *, limit: int = COMPLETED_LIMIT) -> list[sqlite3.Row]:
    """已完成的任务，**最近完成的在前**。

    单独一个函数而不是复用 :func:`list_items`：那两个视图是按 ``due_at`` /
    ``priority`` 排的，对已完成的任务没有意义 —— 这里唯一有用的顺序是
    "最近完成的"。

    排序键 ``completed_at`` 是 UTC ISO8601 字符串，所以字典序即时间序；
    前端 ``tasks.js`` 的 ``byCompletedDesc`` 用的是同一条规则
    （同格式字符串直接比较），两边结果一致。改一个要改两个。
    """
    with db.connect() as conn:
        return list(
            conn.execute(
                "SELECT * FROM items WHERE status = 'done'"
                " ORDER BY completed_at DESC, id DESC LIMIT ?",
                (int(limit),),
            )
        )


def get_item(db, item_id: int) -> sqlite3.Row | None:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def find_by_client_uuid(db, client_uuid: str) -> sqlite3.Row | None:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM items WHERE client_uuid = ?", (client_uuid,)).fetchone()


def counts(db) -> dict[str, int]:
    with db.connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status")
        return {row["status"]: row["n"] for row in rows}


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def create_item(db, payload, *, timezone: str, source: str = "web") -> tuple[sqlite3.Row, bool]:
    """创建任务。带 ``client_uuid`` 时具备幂等性。

    返回 ``(row, created)``；``created=False`` 表示命中了既有的 client_uuid，
    调用方应返回 200 而不是 201。
    """
    client_uuid = getattr(payload, "client_uuid", None)
    if client_uuid:
        existing = find_by_client_uuid(db, client_uuid)
        if existing is not None:
            log.info("task.create_idempotent %s", kv(client_uuid=client_uuid, item_id=existing["id"]))
            return existing, False

    due_at = normalize_due_at(payload.due_at, timezone)
    priority = payload.priority
    if due_at is not None and priority is not None:
        # 理论上被 schema 拦住，这里是纵深防御
        log.warning("task.create_priority_dropped %s", kv(reason="deadline-wins", priority=priority))
        priority = None

    now = utc_iso()
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO items(title, notes, category, due_at, priority, status, source,"
            " client_uuid, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
            (
                payload.title,
                payload.notes,
                payload.category,
                due_at,
                priority,
                source,
                client_uuid,
                now,
                now,
            ),
        )
        item_id = int(cursor.lastrowid)
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    log.info(
        "task.created %s",
        kv(item_id=item_id, category=payload.category, has_due=due_at is not None,
           priority=priority, source=source),
    )
    return row, True


def update_item(db, item_id: int, payload, *, timezone: str) -> sqlite3.Row:
    """局部更新。未提供的字段保持不变。"""
    existing = get_item(db, item_id)
    if existing is None:
        raise TaskNotFound(str(item_id))

    provided = payload.model_fields_set
    values: dict[str, Any] = {}

    if "title" in provided and payload.title is not None:
        values["title"] = payload.title
    if "notes" in provided and payload.notes is not None:
        values["notes"] = payload.notes
    if "category" in provided and payload.category is not None:
        values["category"] = payload.category

    current_due = existing["due_at"]
    current_priority = existing["priority"]

    # deadline 胜出：显式给出其中一个就清掉另一个
    if "due_at" in provided:
        values["due_at"] = normalize_due_at(payload.due_at, timezone)
        if payload.due_at is None and "priority" not in provided:
            # 显式清空 deadline 却没给优先级 → 会破坏二选一，必须补一个
            raise TaskConflict(
                "清空截止时间时必须同时给出优先级，否则任务既无时间也无优先级"
            )
        if values["due_at"] is not None:
            if current_priority is not None:
                log.info(
                    "task.update_priority_dropped %s",
                    kv(item_id=item_id, reason="deadline-wins", dropped_priority=current_priority),
                )
            values["priority"] = None
    if "priority" in provided:
        values["priority"] = payload.priority
        if payload.priority is not None:
            if current_due is not None and "due_at" not in provided:
                log.info(
                    "task.update_due_dropped %s",
                    kv(item_id=item_id, reason="priority-wins", dropped_due=current_due),
                )
            values["due_at"] = None
        elif "due_at" not in provided:
            raise TaskConflict(
                "清空优先级时必须同时给出截止时间，否则任务既无时间也无优先级"
            )

    if "status" in provided and payload.status is not None:
        values["status"] = payload.status
        values["completed_at"] = utc_iso() if payload.status == "done" else None

    if not values:
        return existing

    values["updated_at"] = utc_iso()
    assignments = ", ".join(f"{column} = ?" for column in values)
    with db.transaction() as conn:
        conn.execute(
            f"UPDATE items SET {assignments} WHERE id = ?", (*values.values(), item_id)
        )
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    log.info("task.updated %s", kv(item_id=item_id, fields=",".join(sorted(values))))
    return row


def delete_item(db, item_id: int) -> bool:
    with db.transaction() as conn:
        cursor = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        deleted = cursor.rowcount > 0
    if deleted:
        log.info("task.deleted %s", kv(item_id=item_id))
    else:
        log.warning("task.delete_missing %s", kv(item_id=item_id))
    return deleted


def bulk_insert(db, drafts: Iterable[dict[str, Any]]) -> list[int]:
    """在单个事务里插入多条任务，返回新建 id 列表（供草稿确认使用）。"""
    created: list[int] = []
    now = utc_iso()
    with db.transaction() as conn:
        for draft in drafts:
            cursor = conn.execute(
                "INSERT INTO items(title, notes, category, due_at, priority, status, source,"
                " client_uuid, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                (
                    draft["title"],
                    draft.get("notes", ""),
                    draft.get("category", "other"),
                    draft.get("due_at"),
                    draft.get("priority"),
                    draft.get("source", "llm"),
                    draft.get("client_uuid"),
                    now,
                    now,
                ),
            )
            created.append(int(cursor.lastrowid))
    log.info("task.bulk_inserted %s", kv(count=len(created), ids=created))
    return created
