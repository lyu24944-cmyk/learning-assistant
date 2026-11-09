"""把已有 SQLite 升到当前表结构。

旧库来自本机脱敏 DDL：weekly_reviews 用 week_start/created_at，
courses 和 materials 的 created_at 是非空且没有默认值。
CREATE TABLE IF NOT EXISTS 不会改已有表，所以要在这里迁移。
失败则回滚，不留下半张新表。同一库重复打开不再改数据。
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3

_WEEK_KEY = re.compile(r"^\d{4}-W\d{2}$")
_NEW_WEEKLY = """
CREATE TABLE weekly_reviews__new (
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
)
"""


def migrate(connection: sqlite3.Connection) -> None:
    previous = connection.isolation_level
    connection.isolation_level = None
    connection.execute("BEGIN")
    try:
        _migrate_weekly_reviews(connection)
        _ensure_created_at(connection, "courses")
        _ensure_created_at(connection, "materials")
        _ensure_column(connection, "reminder_prefs", "updated_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "materials", "image_rel", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "materials", "draft_id", "INTEGER")
        _migration_checkpoint(connection)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        # 迁移结束后恢复调用方原来的事务模式，注册和确认草稿才不会留下半条数据。
        connection.isolation_level = previous


def _migration_checkpoint(connection: sqlite3.Connection) -> None:
    """测试可以在结构改完、提交之前打断，用来确认回滚。"""
    return None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _migrate_weekly_reviews(connection: sqlite3.Connection) -> None:
    columns = _columns(connection, "weekly_reviews")
    if not columns or "week_key" in columns and "week_start" not in columns and "updated_at" in columns:
        return
    rows = connection.execute("SELECT * FROM weekly_reviews").fetchall()
    names = rows[0].keys() if rows else columns
    merged: dict[tuple[int, str], dict[str, str]] = {}
    order: list[tuple[int, str]] = []
    for row in rows:
        item = {key: row[key] for key in names}
        user_id = int(item["user_id"])
        week_key = _week_key(str(item.get("week_key") or item.get("week_start") or ""))
        stamp = str(item.get("updated_at") or item.get("created_at") or "")
        payload = {
            "id": str(item["id"]),
            "done_text": str(item.get("done_text") or ""),
            "stuck_text": str(item.get("stuck_text") or ""),
            "next_text": str(item.get("next_text") or ""),
            "summary": str(item.get("summary") or ""),
            "plan": str(item.get("plan") or ""),
            "updated_at": stamp,
        }
        key = (user_id, week_key)
        if key not in merged:
            merged[key] = payload
            order.append(key)
            continue
        current = merged[key]
        if stamp >= current["updated_at"]:
            base, extra = payload, current
        else:
            base, extra = current, payload
        for field in ("done_text", "stuck_text", "next_text", "summary", "plan"):
            base[field] = _join_text(base[field], extra[field])
        merged[key] = base
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(_NEW_WEEKLY)
    for user_id, week_key in order:
        item = merged[(user_id, week_key)]
        connection.execute(
            """
            INSERT INTO weekly_reviews__new (
                id, user_id, week_key, done_text, stuck_text, next_text, summary, plan, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(item["id"]),
                user_id,
                week_key,
                item["done_text"],
                item["stuck_text"],
                item["next_text"],
                item["summary"],
                item["plan"],
                item["updated_at"],
            ),
        )
    connection.execute("DROP TABLE weekly_reviews")
    connection.execute("ALTER TABLE weekly_reviews__new RENAME TO weekly_reviews")
    connection.execute("PRAGMA foreign_keys = ON")


def _ensure_created_at(connection: sqlite3.Connection, table: str) -> None:
    if table not in {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}:
        return
    if "created_at" not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if table not in tables or column in _columns(connection, table):
        return
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _week_key(value: str) -> str:
    text = value.strip()
    if _WEEK_KEY.match(text):
        return text
    try:
        day = dt.date.fromisoformat(text[:10])
    except ValueError:
        return text or "legacy"
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _join_text(later: str, earlier: str) -> str:
    if not earlier or earlier == later:
        return later
    if not later:
        return earlier
    return earlier + "\n" + later
