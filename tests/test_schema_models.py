"""模型与迁移后的真实结构必须一致。

SQLite 和 Postgres 都跑：CI 的 Postgres job 只跑这个文件和迁移测试。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import UniqueConstraint, inspect

from src.db import database_url, get_engine, init_db, reset_for_tests
from src.db.migrations import current_revision
from src.db.models import Base

HEAD = "0002"


@pytest.fixture(autouse=True)
def _clean_state():
    reset_for_tests()
    yield
    reset_for_tests()


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    if os.environ.get("DATABASE_URL"):
        init_db()
        return database_url()
    db = tmp_path / "schema.sqlite"
    monkeypatch.setenv("LEARNING_ASSISTANT_DB", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    init_db()
    return database_url()


def _type_name(value: object) -> str:
    return str(value).split("(")[0].upper()


def test_tables_columns_and_keys_match_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = _prepare(tmp_path, monkeypatch)
    inspector = inspect(get_engine(url))
    tables = set(inspector.get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)

    for name, table in Base.metadata.tables.items():
        actual_columns = {column["name"]: column for column in inspector.get_columns(name)}
        assert set(actual_columns) == {column.name for column in table.columns}, name
        for column in table.columns:
            actual = actual_columns[column.name]
            assert bool(actual["nullable"]) == bool(column.nullable), f"{name}.{column.name} nullable"
            assert _type_name(actual["type"]) == _type_name(column.type), f"{name}.{column.name} type"

        primary_key = inspector.get_pk_constraint(name)["constrained_columns"]
        assert primary_key == [column.name for column in table.primary_key.columns], name

        expected_fks = {
            (fk.parent.name, fk.column.table.name, fk.column.name) for fk in table.foreign_keys
        }
        actual_fks = {
            (fk["constrained_columns"][0], fk["referred_table"], fk["referred_columns"][0])
            for fk in inspector.get_foreign_keys(name)
        }
        assert actual_fks == expected_fks, name

        expected_indexes = {index.name for index in table.indexes}
        actual_indexes = {index["name"] for index in inspector.get_indexes(name)}
        assert expected_indexes <= actual_indexes, f"{name} 缺少索引：{expected_indexes - actual_indexes}"

        expected_unique = {
            tuple(sorted(column.name for column in constraint.columns))
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        actual_unique = {
            tuple(sorted(constraint["column_names"])) for constraint in inspector.get_unique_constraints(name)
        }
        assert expected_unique <= actual_unique, f"{name} 缺少唯一约束：{expected_unique - actual_unique}"


def test_database_is_at_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = _prepare(tmp_path, monkeypatch)
    assert current_revision(url) == HEAD
