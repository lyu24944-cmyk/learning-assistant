"""所有 SQLite 方言都留在这个文件里。

其他模块只允许调用 connect()/session()，不直接写 PRAGMA、sqlite_master、
INSERT OR REPLACE、strftime 或 AUTOINCREMENT。tests/test_dialect.py 会检查。
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import TypeVar

from src.db.paths import database_path, database_url

T = TypeVar("T")


def journal_mode() -> str:
    """默认 DELETE。

    2026-11-09 实测：4 写 4 读 × 3 轮，WAL 每轮都出现
    `attempt to write a readonly database`；DELETE 0 错误、800 行全写入。
    即使 DDL 已经移出连接，Windows 上 WAL 仍不稳定，不要改默认值。
    """
    raw = os.environ.get("LEARNING_ASSISTANT_SQLITE_JOURNAL", "").strip().upper()
    return raw if raw in {"WAL", "DELETE"} else "DELETE"


def configure(connection: sqlite3.Connection) -> None:
    """并发写入排队等待，而不是立刻报锁。"""
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = NORMAL")


def connect() -> sqlite3.Connection:
    if not database_url().startswith("sqlite"):
        raise RuntimeError("当前运行时仍使用 sqlite3 连接；Postgres 只用于迁移和模型测试。")
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    from src.db.engine import ensure_initialized

    ensure_initialized(path)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        configure(connection)
    except Exception:
        connection.close()
        raise
    return connection


def table_names(path: Path) -> set[str]:
    if not path.exists() or path.stat().st_size == 0:
        return set()
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()


def set_journal_mode(path: Path, mode: str) -> str:
    if mode not in {"WAL", "DELETE"}:
        raise ValueError("journal_mode 只能是 WAL 或 DELETE。")
    connection = sqlite3.connect(path)
    try:
        return str(connection.execute(f"PRAGMA journal_mode = {mode}").fetchone()[0])
    finally:
        connection.close()


def backup_database(path: Path, out_dir: Path | None = None, prefix: str = "pre-migration") -> Path:
    """迁移前备份。默认放在数据库同级的 backups/ 下，测试库不会写到项目目录。"""
    target_dir = out_dir or (path.parent / "backups")
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"{prefix}-{stamp}.sqlite"
    suffix = 1
    while target.exists():
        target = target_dir / f"{prefix}-{stamp}-{suffix}.sqlite"
        suffix += 1
    source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(target)
    integrity = "failed"
    try:
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        destination.close()
        source.close()
    if integrity != "ok":
        target.unlink(missing_ok=True)
        raise RuntimeError(f"迁移前备份完整性校验失败：{integrity}")
    return target


def run_legacy_migration(path: Path) -> None:
    """把 2026-11-07 之前的老结构补齐。只跑一次，由 init_db 判断。"""
    from src.migrate import migrate

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        migrate(connection)
    finally:
        connection.close()


def rollback_quiet(connection: sqlite3.Connection) -> None:
    """BEGIN IMMEDIATE 没拿到锁时没有事务可回滚。

    直接 ROLLBACK 会抛 "cannot rollback - no transaction is active"，
    把真正的异常（例如库被占用）盖掉。
    """
    try:
        connection.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


@contextmanager
def write_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE 的短事务。重试由 write_retry 包住整个函数。"""
    previous = connection.isolation_level
    connection.isolation_level = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        else:
            connection.execute("COMMIT")
    finally:
        connection.isolation_level = previous


def write_retry(func: Callable[..., T]) -> Callable[..., T]:
    """数据库被占用时重跑整个函数，最多 3 次。"""

    @wraps(func)
    def wrapper(*args, **kwargs) -> T:
        last: sqlite3.OperationalError | None = None
        for attempt in range(3):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                last = exc
                if "locked" not in str(exc).lower() or attempt == 2:
                    raise
                time.sleep(0.05 * (2**attempt))
        raise last  # pragma: no cover

    return wrapper
