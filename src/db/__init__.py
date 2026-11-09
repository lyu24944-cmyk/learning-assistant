"""数据库入口。

- connect(): sqlite3 连接，给尚未迁移的旧模块用；不再执行 DDL。
- session(): SQLAlchemy Session，给新模块用。
- bootstrap(): 建表/迁移，每个进程一次。
"""

from __future__ import annotations

from src.db.engine import (
    bootstrap,
    ensure_initialized,
    get_engine,
    init_db,
    reset_for_tests,
    session,
)
from src.db.paths import database_path, database_url
from src.db.sqlite_adapter import connect, write_retry, write_transaction

__all__ = [
    "bootstrap",
    "connect",
    "database_path",
    "database_url",
    "ensure_initialized",
    "get_engine",
    "init_db",
    "reset_for_tests",
    "session",
    "write_retry",
    "write_transaction",
]
