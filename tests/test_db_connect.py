"""connect() 只建立连接，不再执行建表或迁移。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.db import connect, init_db, reset_for_tests


@pytest.fixture(autouse=True)
def _clean_state():
    reset_for_tests()
    yield
    reset_for_tests()


def test_connect_does_not_run_ddl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "no-ddl.sqlite"
    monkeypatch.setenv("LEARNING_ASSISTANT_DB", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    init_db()

    statements: list[str] = []
    real_connect = sqlite3.connect

    def tracked(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr("src.db.sqlite_adapter.sqlite3.connect", tracked)
    connection = connect()
    try:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    finally:
        connection.close()

    ddl = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("CREATE", "ALTER", "DROP"))
    ]
    assert not ddl, f"connect() 不应执行 DDL：{ddl}"
