"""API Key 的创建、列出与吊销。

密钥只存 SHA-256，明文仅在创建时返回一次。这里没有"作用域"概念，
只有一个 ``read_only`` 布尔——悬浮窗需要勾选任务所以要读写，
只读留给未来的 Scriptable 小组件（PLAN.md §7）。
"""

from __future__ import annotations

import sqlite3

from app.logging import get_logger, kv
from app.security import generate_api_key, hash_api_key
from app.timeutil import utc_iso

log = get_logger("apikeys")


def list_keys(db, *, include_revoked: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT id, name, read_only, created_at, last_used_at, revoked_at FROM api_keys"
    if not include_revoked:
        sql += " WHERE revoked_at IS NULL"
    sql += " ORDER BY created_at DESC, id DESC"
    with db.connect() as conn:
        return list(conn.execute(sql))


def create_key(db, *, name: str, read_only: bool = False) -> tuple[int, str]:
    """创建密钥，返回 ``(id, 明文)``。明文只此一次，之后无法再取回。"""
    plaintext = generate_api_key()
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO api_keys(name, key_hash, read_only, created_at) VALUES (?, ?, ?, ?)",
            (name.strip()[:60] or "未命名", hash_api_key(plaintext), 1 if read_only else 0, utc_iso()),
        )
        key_id = int(cursor.lastrowid)
    log.info("apikey.created %s", kv(key_id=key_id, name=name, read_only=read_only))
    return key_id, plaintext


def revoke_key(db, key_id: int) -> bool:
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (utc_iso(), key_id),
        )
        revoked = cursor.rowcount > 0
    if revoked:
        log.info("apikey.revoked %s", kv(key_id=key_id))
    else:
        log.warning("apikey.revoke_missing %s", kv(key_id=key_id))
    return revoked


def counts(db) -> dict[str, int]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT CASE WHEN revoked_at IS NULL THEN 'active' ELSE 'revoked' END AS state,"
            " COUNT(*) AS n FROM api_keys GROUP BY state"
        )
        result = {"active": 0, "revoked": 0}
        for row in rows:
            result[row["state"]] = row["n"]
        return result
