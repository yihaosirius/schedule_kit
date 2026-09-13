"""SQLite 访问层。

* 每个操作一个短连接（单用户低流量，创建开销可忽略，且天然线程安全）
* WAL + ``synchronous=NORMAL`` + ``foreign_keys=ON`` + ``busy_timeout``
* :meth:`Database.transaction` 提供 ``BEGIN IMMEDIATE`` 显式事务
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class Database:
    """SQLite 连接工厂。"""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """只读或自管事务的连接。"""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：异常时回滚。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - 连接已失效
                pass
            raise
        finally:
            conn.close()
