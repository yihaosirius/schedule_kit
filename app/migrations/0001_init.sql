-- ScheduleKit 初始 schema（PLAN.md §6）
--
-- 单租户：无 users 表、无 user_id 外键。
-- 时间一律存 UTC ISO8601 字符串。
-- items 的 deadline / priority 严格二选一，由 CHECK 约束保证。

CREATE TABLE items (
    id          INTEGER PRIMARY KEY,
    title       TEXT    NOT NULL,
    notes       TEXT    NOT NULL DEFAULT '',
    category    TEXT    NOT NULL CHECK (category IN ('homework','practice','exam','appointment','other')),
    due_at      TEXT,                       -- ISO8601 UTC；NULL ⇒ 无序表
    priority    INTEGER,                    -- 1..5 即 Ⅰ..Ⅴ；NULL ⇒ 有序表
    status      TEXT    NOT NULL CHECK (status IN ('open','done','cancelled')) DEFAULT 'open',
    source      TEXT    NOT NULL CHECK (source IN ('web','shortcut','llm','api')) DEFAULT 'web',
    client_uuid TEXT    UNIQUE,             -- 客户端幂等键，可空
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    completed_at TEXT,
    CHECK ((due_at IS NULL) <> (priority IS NULL)),
    CHECK (priority IS NULL OR (priority BETWEEN 1 AND 5))
);

CREATE INDEX idx_items_due ON items(status, due_at);
CREATE INDEX idx_items_pri ON items(status, priority, created_at);


CREATE TABLE ingest_drafts (
    id              INTEGER PRIMARY KEY,
    status          TEXT    NOT NULL CHECK (status IN ('pending','confirmed','discarded','failed')) DEFAULT 'pending',
    channel         TEXT    NOT NULL CHECK (channel IN ('image','text')),
    image_path      TEXT,
    image_sha256    TEXT,
    input_text      TEXT,
    context_snapshot TEXT,                  -- 本次识别注入的时间上下文，供确认页核对与排障
    llm_provider    TEXT,
    llm_model       TEXT,
    llm_raw         TEXT,
    draft_json      TEXT    NOT NULL,       -- {"items":[...]}，支持一图多事项
    created_item_ids TEXT,                  -- JSON 数组：一次确认可产生多条 items
    error           TEXT,
    created_at      TEXT    NOT NULL,
    expires_at      TEXT    NOT NULL,
    confirmed_at    TEXT
);

CREATE INDEX idx_drafts_status ON ingest_drafts(status, expires_at);


CREATE TABLE api_keys (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    key_hash     TEXT    NOT NULL UNIQUE,   -- SHA-256，仅创建时明文展示一次
    read_only    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);


CREATE TABLE courses (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    teacher    TEXT    NOT NULL DEFAULT '',
    location   TEXT    NOT NULL DEFAULT '',
    note       TEXT    NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0
);


CREATE TABLE course_sessions (
    id          INTEGER PRIMARY KEY,
    course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    weekday     INTEGER NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    start_time  TEXT    NOT NULL,           -- "HH:MM"
    end_time    TEXT    NOT NULL,           -- "HH:MM"
    start_week  INTEGER NOT NULL DEFAULT 1,
    end_week    INTEGER NOT NULL DEFAULT 18,
    week_parity TEXT    NOT NULL CHECK (week_parity IN ('all','odd','even')) DEFAULT 'all'
);

CREATE INDEX idx_sessions_course ON course_sessions(course_id);
