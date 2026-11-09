"""SQLite 连接。用户、会话和私人记录在这里。共享竞赛目录仍在 CSV。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from src.config import ROOT

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_prefs (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    within_days INTEGER NOT NULL,
    baseline_date TEXT NOT NULL DEFAULT '',
    email_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS weekly_reviews (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    week_key TEXT NOT NULL,
    done_text TEXT NOT NULL,
    stuck_text TEXT NOT NULL,
    next_text TEXT NOT NULL,
    summary TEXT NOT NULL,
    plan TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, week_key)
);

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    term TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    course_id INTEGER REFERENCES courses(id),
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    page_hint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS user_records (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    info_type TEXT NOT NULL,
    deadline TEXT NOT NULL,
    source_url TEXT NOT NULL,
    updated_on TEXT NOT NULL,
    major TEXT NOT NULL,
    status TEXT NOT NULL,
    review_state TEXT NOT NULL,
    date_quote TEXT NOT NULL,
    remind TEXT NOT NULL,
    condition TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS image_drafts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    original_rel TEXT NOT NULL,
    preview_rel TEXT NOT NULL,
    mime TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    ocr_text TEXT NOT NULL,
    ocr_engine TEXT NOT NULL,
    ocr_error TEXT NOT NULL DEFAULT '',
    candidate_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS image_links (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    draft_id INTEGER NOT NULL REFERENCES image_drafts(id),
    material_id INTEGER NOT NULL REFERENCES materials(id),
    record_id INTEGER NOT NULL REFERENCES user_records(id),
    UNIQUE (draft_id, record_id)
);

CREATE TABLE IF NOT EXISTS review_activities (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    week_key TEXT NOT NULL,
    text TEXT NOT NULL,
    draft_id INTEGER,
    material_id INTEGER,
    record_id INTEGER,
    created_at TEXT NOT NULL
);
"""


def database_path() -> Path:
    override = os.environ.get("LEARNING_ASSISTANT_DB")
    if override:
        return Path(override)
    return ROOT / "data" / "app.sqlite"


def _configure(connection: sqlite3.Connection) -> None:
    """并发写入时排队等待，而不是立刻报锁。

    没有切 WAL：在 Windows 上，多个连接同时执行建表语句再 BEGIN IMMEDIATE
    会偶发 "attempt to write a readonly database"。默认日志模式加 5 秒
    busy_timeout 已经能让并发写排队。需要多个写进程时按
    docs/stack-and-scope.md 换 Postgres，而不是靠换日志模式。
    """
    connection.execute("PRAGMA busy_timeout = 5000")


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        _configure(connection)
        connection.executescript(_SCHEMA)
        from src.migrate import migrate

        migrate(connection)
    except Exception:
        connection.close()
        raise
    return connection
