"""SQLAlchemy engine 与每个进程一次的初始化。

旧模块继续用 sqlite_adapter.connect()；新模块用 session()。
DDL 只在 init_db() 里跑，connect() 不再建表。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from src.db import sqlite_adapter
from src.db.migrations import upgrade_to_head
from src.db.paths import database_path, database_url

_initialized: set[str] = set()
_lock = threading.Lock()
_engines: dict[str, Engine] = {}
_factories: dict[str, sessionmaker[Session]] = {}


def get_engine(url: str | None = None) -> Engine:
    resolved = url or database_url()
    engine = _engines.get(resolved)
    if engine is None:
        kwargs: dict = {"future": True, "pool_pre_ping": True}
        if resolved.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            kwargs["poolclass"] = NullPool
        engine = create_engine(resolved, **kwargs)
        if resolved.startswith("sqlite"):
            # 批次 1 只给 sqlite_adapter.connect() 配了连接参数，ORM 连接漏了。
            # 放在这里，session() 和 connect() 的锁等待、外键行为才一致。
            event.listen(engine, "connect", _configure_sqlite)
        _engines[resolved] = engine
    return engine


def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    sqlite_adapter.configure_dbapi(dbapi_connection)


def _session_factory() -> sessionmaker[Session]:
    url = database_url()
    factory = _factories.get(url)
    if factory is None:
        factory = sessionmaker(bind=get_engine(url), expire_on_commit=False)
        _factories[url] = factory
    return factory


@contextmanager
def session() -> Iterator[Session]:
    # 和 connect() 一样，第一次用之前保证库已初始化；每个进程只跑一次。
    if database_url().startswith("sqlite"):
        ensure_initialized(database_path())
    current = _session_factory()()
    try:
        yield current
        current.commit()
    except Exception:
        current.rollback()
        raise
    finally:
        current.close()


def bootstrap() -> None:
    """FastAPI lifespan 和 CLI 入口调用；重复调用是安全的。"""
    if database_url().startswith("sqlite"):
        ensure_initialized(database_path())
    else:
        upgrade_to_head(database_url())


def ensure_initialized(path: Path) -> None:
    key = str(Path(path).resolve())
    if key in _initialized:
        return
    with _lock:
        if key in _initialized:
            return
        init_db()
        _initialized.add(key)


def init_db() -> None:
    url = database_url()
    if not url.startswith("sqlite"):
        upgrade_to_head(url)
        return
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tables = sqlite_adapter.table_names(path)
    if tables and "alembic_version" not in tables:
        sqlite_adapter.backup_database(path)
        sqlite_adapter.run_legacy_migration(path)
    upgrade_to_head(url)
    sqlite_adapter.set_journal_mode(path, sqlite_adapter.journal_mode())


def reset_for_tests() -> None:
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
    _factories.clear()
    _initialized.clear()
