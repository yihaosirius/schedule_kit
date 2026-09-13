"""迁移执行器：按文件名顺序应用 ``*.sql``，已应用的记入 ``schema_version``。

每个迁移在单个事务内执行（脚本自带 ``BEGIN``/``COMMIT``，因为
``sqlite3.executescript`` 不做隐式事务控制），失败则整体回滚。
"""

from __future__ import annotations

from pathlib import Path

from app.db import Database
from app.timeutil import utc_iso

MIGRATIONS_DIR = Path(__file__).resolve().parent


def applied_versions(db: Database) -> set[str]:
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  version    TEXT PRIMARY KEY,"
            "  applied_at TEXT NOT NULL"
            ")"
        )
        return {row["version"] for row in conn.execute("SELECT version FROM schema_version")}


def apply_migrations(db: Database, migrations_dir: Path | None = None) -> list[str]:
    """应用尚未执行的迁移，返回本次应用的版本列表。"""
    directory = migrations_dir or MIGRATIONS_DIR
    done = applied_versions(db)
    newly_applied: list[str] = []

    for path in sorted(directory.glob("*.sql")):
        version = path.name
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        timestamp = utc_iso().replace("'", "''")
        safe_version = version.replace("'", "''")
        script = (
            "BEGIN;\n"
            f"{sql}\n"
            "INSERT INTO schema_version(version, applied_at) "
            f"VALUES ('{safe_version}', '{timestamp}');\n"
            "COMMIT;\n"
        )
        with db.connect() as conn:
            conn.executescript(script)
        newly_applied.append(version)

    return newly_applied
