"""录入草稿的状态机与确认事务。

核心不变量：**草稿阶段 items 表零写入**。LLM 识别出的一切都先落在
``ingest_drafts``，只有人工确认后才在单个事务里落库。这样模型出错、
网络中断、用户改主意，都不会留下脏数据（PLAN.md §11）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.llm.tools import CATEGORY_ENUM
from app.logging import get_logger, kv
from app.services import tasks as task_service
from app.timeutil import now_utc, utc_iso

log = get_logger("drafts")

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_DISCARDED = "discarded"
STATUS_FAILED = "failed"

#: 全部状态，顺序即 UI 中的展示顺序。
STATUSES = (STATUS_PENDING, STATUS_CONFIRMED, STATUS_DISCARDED, STATUS_FAILED)


class DraftNotFound(LookupError):
    """草稿不存在或已过期清理。"""


class DraftNotPending(RuntimeError):
    """草稿已被确认/丢弃/失败，不能再操作。"""


class DraftValidationError(ValueError):
    """客户端提交的草稿条目不合规。"""


@dataclass(frozen=True)
class ConfirmResult:
    draft_id: int
    item_ids: list[int]
    already_confirmed: bool = False


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def get_draft(db, draft_id: int) -> sqlite3.Row | None:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM ingest_drafts WHERE id = ?", (draft_id,)).fetchone()


def list_drafts(
    db,
    *,
    status: str | None = None,
    channel: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[sqlite3.Row], int]:
    """按条件列出草稿，返回 ``(rows, total)``。新的在前。

    ``total`` 是**过滤后**的总数，供分页用；与 ``rows`` 的长度无关。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if channel:
        clauses.append("channel = ?")
        params.append(channel)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    with db.connect() as conn:
        total = int(
            conn.execute(f"SELECT COUNT(*) FROM ingest_drafts{where}", params).fetchone()[0]
        )
        rows = conn.execute(
            f"SELECT * FROM ingest_drafts{where}"
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return list(rows), total


def status_counts(db) -> dict[str, int]:
    """各状态的草稿数。

    刻意返回**完整的四个键**（没有就是 0），而不是 :mod:`app.status` 那种
    只含实际存在状态的稀疏字典——调用方在这里不该还要记得 ``.get(k, 0)``。
    """
    counts = {name: 0 for name in STATUSES}
    with db.connect() as conn:
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM ingest_drafts GROUP BY status"
        ):
            counts[str(row["status"])] = int(row["n"])
    return counts


def draft_items(row: sqlite3.Row) -> list[dict[str, Any]]:
    try:
        payload = json.loads(row["draft_json"])
    except (ValueError, TypeError):
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    return items if isinstance(items, list) else []


def _public(row: sqlite3.Row) -> dict[str, Any]:
    """给客户端的草稿表示。"""
    return {
        "draft_id": row["id"],
        "status": row["status"],
        "channel": row["channel"],
        "items": draft_items(row),
        "context_snapshot": row["context_snapshot"],
        "image_path": row["image_path"],
        "input_text": row["input_text"],
        "error": row["error"],
        "llm_provider": row["llm_provider"],
        "llm_model": row["llm_model"],
        "llm_path": row["llm_path"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "created_item_ids": json.loads(row["created_item_ids"]) if row["created_item_ids"] else [],
    }


def to_public(row: sqlite3.Row) -> dict[str, Any]:
    return _public(row)


def to_summary(row: sqlite3.Row) -> dict[str, Any]:
    """草稿箱列表用的**轻量**表示。

    刻意不含 ``items`` 与 ``context_snapshot``：一次列 50 条的话那些字段会
    把响应撑到几百 KB，而列表只需要"这是什么、多大、什么时候过期"。
    要看全文走 ``GET /api/ingest/{draft_id}``。
    """
    items = draft_items(row)
    first_title = str(items[0].get("title") or "") if items else ""
    input_text = (row["input_text"] or "").strip()
    return {
        "draft_id": row["id"],
        "status": row["status"],
        "channel": row["channel"],
        "item_count": len(items),
        # 列表预览：优先第一个事项的标题，没有就用输入文本的开头
        "preview": first_title or input_text[:120] or "（无内容）",
        "input_text": input_text[:200] or None,
        "error": row["error"],
        "has_image": bool(row["image_path"]),
        "llm_provider": row["llm_provider"],
        "llm_model": row["llm_model"],
        "llm_path": row["llm_path"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "confirmed_at": row["confirmed_at"],
        "created_item_ids": json.loads(row["created_item_ids"]) if row["created_item_ids"] else [],
    }


# --------------------------------------------------------------------------- #
# 创建
# --------------------------------------------------------------------------- #
def create_draft(
    db,
    *,
    channel: str,
    items: list[dict[str, Any]],
    ttl_hours: int,
    image_path: str | None = None,
    image_sha256: str | None = None,
    input_text: str | None = None,
    context_snapshot: str | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_raw: str | None = None,
    llm_path: str | None = None,
) -> sqlite3.Row:
    created_at = now_utc()
    expires_at = created_at + timedelta(hours=max(1, ttl_hours))
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO ingest_drafts(status, channel, image_path, image_sha256, input_text,"
            " context_snapshot, llm_provider, llm_model, llm_raw, llm_path, draft_json,"
            " created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                STATUS_PENDING,
                channel,
                image_path,
                image_sha256,
                input_text,
                context_snapshot,
                llm_provider,
                llm_model,
                llm_raw,
                llm_path,
                json.dumps({"items": items}, ensure_ascii=False),
                utc_iso(created_at),
                utc_iso(expires_at),
            ),
        )
        draft_id = int(cursor.lastrowid)
        row = conn.execute("SELECT * FROM ingest_drafts WHERE id = ?", (draft_id,)).fetchone()

    log.info(
        "draft.created %s",
        kv(draft_id=draft_id, channel=channel, items=len(items), ttl_hours=ttl_hours,
           image=bool(image_path), provider=llm_provider, path=llm_path),
    )
    return row


def create_failed_draft(
    db,
    *,
    channel: str,
    error: str,
    ttl_hours: int,
    image_path: str | None = None,
    image_sha256: str | None = None,
    input_text: str | None = None,
    context_snapshot: str | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_raw: str | None = None,
    llm_path: str | None = None,
) -> sqlite3.Row:
    """识别失败也要留档：保留 llm_raw 才能在事后判断是模型的问题还是我们的。"""
    created_at = now_utc()
    expires_at = created_at + timedelta(hours=max(1, ttl_hours))
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO ingest_drafts(status, channel, image_path, image_sha256, input_text,"
            " context_snapshot, llm_provider, llm_model, llm_raw, llm_path, draft_json, error,"
            " created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                STATUS_FAILED,
                channel,
                image_path,
                image_sha256,
                input_text,
                context_snapshot,
                llm_provider,
                llm_model,
                llm_raw,
                llm_path,
                json.dumps({"items": []}, ensure_ascii=False),
                error,
                utc_iso(created_at),
                utc_iso(expires_at),
            ),
        )
        draft_id = int(cursor.lastrowid)
        row = conn.execute("SELECT * FROM ingest_drafts WHERE id = ?", (draft_id,)).fetchone()

    log.warning("draft.failed %s", kv(draft_id=draft_id, channel=channel, error=error[:200]))
    return row


# --------------------------------------------------------------------------- #
# 客户端提交的条目校验
# --------------------------------------------------------------------------- #
def validate_client_items(raw_items: list[Any], *, timezone: str) -> list[dict[str, Any]]:
    """校验并规整客户端回传/修改后的条目。

    与 :mod:`app.services.normalize` 的区别：那里处理的是**模型输出**，
    容错优先（坏条目悄悄丢掉）；这里处理的是**人确认过的输入**，必须报错，
    绝不能默默丢弃用户改好的内容。
    """
    if not isinstance(raw_items, list):
        raise DraftValidationError("items 必须是数组")
    if not raw_items:
        raise DraftValidationError("至少要有一条事项，全部删除请改用「丢弃」")

    cleaned: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise DraftValidationError(f"第 {index} 条不是对象")

        title = str(raw.get("title") or "").strip()
        if not title:
            raise DraftValidationError(f"第 {index} 条标题为空")

        category = str(raw.get("category") or "").strip() or "other"
        if category not in CATEGORY_ENUM:
            raise DraftValidationError(f"第 {index} 条分类非法：{category}")

        due_raw = raw.get("due_at")
        due_at: str | None = None
        if due_raw not in (None, ""):
            try:
                due_at = task_service.normalize_due_at(str(due_raw), timezone)
            except ValueError as exc:
                raise DraftValidationError(f"第 {index} 条截止时间无法解析：{due_raw}") from exc

        priority_raw = raw.get("priority")
        priority: int | None = None
        if priority_raw not in (None, ""):
            try:
                priority = int(priority_raw)
            except (TypeError, ValueError) as exc:
                raise DraftValidationError(f"第 {index} 条优先级不是整数：{priority_raw}") from exc
            if not 1 <= priority <= 5:
                raise DraftValidationError(f"第 {index} 条优先级越界（应为 1–5）：{priority}")

        if (due_at is None) == (priority is None):
            raise DraftValidationError(
                f"第 {index} 条必须二选一：有截止时间就不给优先级，没有截止时间才给优先级"
            )

        cleaned.append(
            {
                "title": title[:200],
                "category": category,
                "due_at": due_at,
                "priority": priority,
                "notes": str(raw.get("notes") or "").strip()[:2000],
                "source_quote": str(raw.get("source_quote") or "").strip()[:200],
                "needs_priority": False,
                "adjustments": list(raw.get("adjustments") or []),
            }
        )

    return cleaned


# --------------------------------------------------------------------------- #
# 状态变更
# --------------------------------------------------------------------------- #
def update_draft_items(db, draft_id: int, items: list[dict[str, Any]]) -> sqlite3.Row:
    """就地替换草稿条目。

    当前没有单独的路由暴露它——确认接口支持直接带上修改后的 items
    （一次请求原子完成"改 + 确认"，比 PATCH 再 confirm 少一次往返，
    也不会出现"改完了但没确认"的中间态）。保留此函数是为了让
    未来的 MCP 工具或脚本调用方也能走同一条路径。
    """
    row = get_draft(db, draft_id)
    if row is None:
        raise DraftNotFound(str(draft_id))
    if row["status"] != STATUS_PENDING:
        raise DraftNotPending(f"草稿当前状态为 {row['status']}，不能修改")

    with db.transaction() as conn:
        conn.execute(
            "UPDATE ingest_drafts SET draft_json = ? WHERE id = ?",
            (json.dumps({"items": items}, ensure_ascii=False), draft_id),
        )
    log.info("draft.updated %s", kv(draft_id=draft_id, items=len(items)))
    return get_draft(db, draft_id)


def confirm_draft(
    db, draft_id: int, *, timezone: str, items: list[dict[str, Any]] | None = None
) -> ConfirmResult:
    """确认入库。幂等：已确认的草稿再确认会明确报错而不是重复插入。"""
    row = get_draft(db, draft_id)
    if row is None:
        raise DraftNotFound(str(draft_id))

    if row["status"] == STATUS_CONFIRMED:
        existing = json.loads(row["created_item_ids"]) if row["created_item_ids"] else []
        log.warning("draft.confirm_repeat %s", kv(draft_id=draft_id, item_ids=existing))
        raise DraftNotPending(f"草稿 {draft_id} 已确认过（任务 {existing}）")

    if row["status"] != STATUS_PENDING:
        raise DraftNotPending(f"草稿 {draft_id} 状态为 {row['status']}，无法确认")

    final_items = items if items is not None else draft_items(row)
    if not final_items:
        raise DraftValidationError("草稿里没有可入库的事项")

    # 落库前再校验一次：客户端可能改过内容
    validated = validate_client_items(final_items, timezone=timezone)

    with db.transaction() as conn:
        created_ids: list[int] = []
        now = utc_iso()
        for item in validated:
            cursor = conn.execute(
                "INSERT INTO items(title, notes, category, due_at, priority, status, source,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'open', 'llm', ?, ?)",
                (item["title"], item["notes"], item["category"], item["due_at"], item["priority"], now, now),
            )
            created_ids.append(int(cursor.lastrowid))

        conn.execute(
            "UPDATE ingest_drafts SET status = ?, draft_json = ?, created_item_ids = ?,"
            " confirmed_at = ? WHERE id = ?",
            (
                STATUS_CONFIRMED,
                json.dumps({"items": validated}, ensure_ascii=False),
                json.dumps(created_ids),
                now,
                draft_id,
            ),
        )

    log.info("draft.confirmed %s", kv(draft_id=draft_id, items=len(created_ids), item_ids=created_ids))
    return ConfirmResult(draft_id=draft_id, item_ids=created_ids)


def discard_draft(db, draft_id: int) -> sqlite3.Row:
    row = get_draft(db, draft_id)
    if row is None:
        raise DraftNotFound(str(draft_id))
    if row["status"] != STATUS_PENDING:
        raise DraftNotPending(f"草稿当前状态为 {row['status']}，不能丢弃")

    with db.transaction() as conn:
        conn.execute("UPDATE ingest_drafts SET status = ? WHERE id = ?", (STATUS_DISCARDED, draft_id))
    log.info("draft.discarded %s", kv(draft_id=draft_id))
    return get_draft(db, draft_id)


# --------------------------------------------------------------------------- #
# 清理
# --------------------------------------------------------------------------- #
def cleanup_expired(db, *, now: datetime | None = None) -> int:
    """清掉过期且未确认的草稿。已确认的草稿保留作为识别历史。"""
    moment = utc_iso(now or now_utc())
    with db.transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM ingest_drafts WHERE status IN (?, ?, ?) AND expires_at < ?",
            (STATUS_PENDING, STATUS_DISCARDED, STATUS_FAILED, moment),
        )
        removed = cursor.rowcount or 0
    if removed:
        log.info("draft.cleanup %s", kv(removed=removed, before=moment))
    return removed


def purge_drafts(db, ids: list[int]) -> tuple[list[int], list[int]]:
    """**永久**删除指定草稿行，返回 ``(已删除, 未找到)``。

    与 :func:`discard_draft` 的区别：丢弃只改状态、留痕；这里是真删行。
    手工清理用，所以调用方应当把"不可恢复"写在最显眼的地方。

    两件**绝不能**顺手做的事：

    1. **不删图片文件。** ``media.store_image`` 按内容哈希命名
       （``sha256[:16]``）且已存在就不重写，所以多个草稿可能共用同一个文件；
       已确认的草稿更是永久保留 ``image_path``。图片的生命周期只由
       :func:`app.housekeeping._purge_old_uploads` 按目录日期管。
       在这里 unlink 会把别人的图删掉。
    2. **不碰 items 表。** ``created_item_ids`` 只是个 JSON 数组，
       ``items`` 与 ``ingest_drafts`` 之间没有外键——删草稿不会、也不该
       删掉已经确认入库的任务。
    """
    wanted = sorted({int(value) for value in ids})
    if not wanted:
        return [], []

    placeholders = ",".join("?" * len(wanted))
    with db.transaction() as conn:
        existing = {
            int(row["id"])
            for row in conn.execute(
                f"SELECT id FROM ingest_drafts WHERE id IN ({placeholders})", wanted
            )
        }
        if existing:
            conn.execute(
                f"DELETE FROM ingest_drafts WHERE id IN ({placeholders})", sorted(existing)
            )

    deleted = sorted(existing)
    missing = [value for value in wanted if value not in existing]
    log.warning(
        "draft.purged %s",
        kv(deleted=len(deleted), missing=len(missing), ids=deleted),
    )
    return deleted, missing
