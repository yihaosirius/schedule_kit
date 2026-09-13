"""M0：统一配置文件与数据库迁移。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.config import Config, ConfigError
from app.db import Database
from app.migrations.runner import apply_migrations
from app.security import hash_password, verify_password


# --------------------------------------------------------------------------- #
# 配置：注释保留与原子写入
# --------------------------------------------------------------------------- #
def test_update_section_preserves_comments_and_other_sections(config_path: Path) -> None:
    before = config_path.read_text(encoding="utf-8")
    comments_before = [line for line in before.splitlines() if line.strip().startswith("#")]
    assert comments_before, "模板应含注释，否则此测试无意义"

    cfg = Config(config_path)
    cfg.update_section("llm", {"model": "changed-model", "temperature": 0.7})

    after = config_path.read_text(encoding="utf-8")
    comments_after = [line for line in after.splitlines() if line.strip().startswith("#")]
    assert comments_after == comments_before, "就地写入必须逐条保留注释"
    assert "[tls]" in after and "[backup]" in after, "其余段落必须保持不变"
    assert "changed-model" in after


def test_update_section_round_trips_through_reload(config_path: Path) -> None:
    cfg = Config(config_path)
    cfg.update_section("llm", {"model": "round-trip"})
    assert Config(config_path).llm.model == "round-trip"


def test_secrets_are_redacted_by_default(config_path: Path) -> None:
    cfg = Config(config_path)
    cfg.update_section("auth", {"secret_key": "super-secret"})
    cfg = Config(config_path)

    assert cfg.as_dict()["auth"]["secret_key"] == ""
    assert cfg.as_dict(include_secrets=True)["auth"]["secret_key"] == "super-secret"
    assert cfg.secret_presence()["auth.secret_key"] is True


def test_relative_data_dir_resolves_against_config_file(config_path: Path) -> None:
    cfg = Config(config_path)
    cfg.update_section("server", {"data_dir": "relative-data"})
    resolved = Config(config_path).server.data_dir
    assert resolved.is_absolute()
    assert resolved == config_path.parent / "relative-data"


def test_assert_ready_fails_closed_without_secret_key(config_path: Path) -> None:
    cfg = Config(config_path)
    cfg.update_section("auth", {"secret_key": "", "password_hash": ""})
    cfg = Config(config_path)
    with pytest.raises(ConfigError) as excinfo:
        cfg.assert_ready()
    message = str(excinfo.value)
    assert "secret_key" in message
    assert "password_hash" in message


def test_missing_config_file_raises_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config(tmp_path / "nope.toml")
    assert "cli init" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #
def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    first = apply_migrations(db)
    second = apply_migrations(db)
    assert first, "首次运行应当应用至少一个迁移"
    assert all(name.endswith(".sql") for name in first)
    assert second == [], "重复运行不应再次应用任何迁移"


def test_schema_contains_expected_tables(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    apply_migrations(db)
    with db.connect() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"items", "ingest_drafts", "api_keys", "courses", "course_sessions"} <= tables


def test_wal_mode_enabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    apply_migrations(db)
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


# --------------------------------------------------------------------------- #
# deadline / priority 严格二选一（数据库层硬约束）
# --------------------------------------------------------------------------- #
def _insert_item(conn: sqlite3.Connection, **overrides: object) -> None:
    row: dict[str, object] = {
        "title": "t",
        "category": "homework",
        "due_at": None,
        "priority": None,
        "client_uuid": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    conn.execute(
        "INSERT INTO items(title, category, due_at, priority, client_uuid, created_at, updated_at) "
        "VALUES (:title, :category, :due_at, :priority, :client_uuid, :created_at, :updated_at)",
        row,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"due_at": "2026-01-02T00:00:00+00:00", "priority": 3},  # 两者都给
        {"due_at": None, "priority": None},  # 两者都不给
    ],
)
def test_deadline_priority_xor_is_enforced(tmp_path: Path, overrides: dict[str, object]) -> None:
    db = Database(tmp_path / "t.db")
    apply_migrations(db)
    with db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_item(conn, **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"due_at": "2026-01-02T00:00:00+00:00", "priority": None},  # 只有 deadline
        {"due_at": None, "priority": 1},  # 只有优先级
        {"due_at": None, "priority": 5},
    ],
)
def test_either_deadline_or_priority_is_accepted(tmp_path: Path, overrides: dict[str, object]) -> None:
    db = Database(tmp_path / "t.db")
    apply_migrations(db)
    with db.transaction() as conn:
        _insert_item(conn, **overrides)
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_priority_out_of_range_rejected(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    apply_migrations(db)
    with db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_item(conn, priority=6)


def test_client_uuid_is_unique(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    apply_migrations(db)
    with db.connect() as conn:
        _insert_item(conn, priority=3, client_uuid="dup")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_item(conn, priority=3, client_uuid="dup")


# --------------------------------------------------------------------------- #
# 密码哈希
# --------------------------------------------------------------------------- #
def test_password_hash_round_trip() -> None:
    stored = hash_password("hunter2")
    assert stored.startswith("scrypt$")
    assert verify_password("hunter2", stored)
    assert not verify_password("hunter3", stored)


@pytest.mark.parametrize("stored", ["", "garbage", "scrypt$bad", "bcrypt$1$2$3$4$5"])
def test_verify_password_rejects_malformed_input(stored: str) -> None:
    assert verify_password("whatever", stored) is False


def test_password_hash_is_salted() -> None:
    assert hash_password("same") != hash_password("same")
