"""Alembic 基线、老库接管和 downgrade。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.db import database_url, init_db, reset_for_tests
from src.db.migrations import current_revision, downgrade, upgrade_to_head

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "schema-only-20261107.json"
ORDER = ("users", "courses", "reminder_prefs", "user_records", "weekly_reviews", "materials")


@pytest.fixture(autouse=True)
def _clean_state():
    reset_for_tests()
    yield
    reset_for_tests()


def _use(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_ASSISTANT_DB", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _legacy_database(path: Path) -> None:
    schema = json.loads(FIXTURE.read_text(encoding="utf-8"))
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        for name in ORDER:
            connection.execute(schema["tables"][name]["create_table_sql"])
        connection.execute(
            "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (1, "legacy@example.com", "synthetic-hash", "2026-11-01T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()


def test_fresh_database_reaches_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "fresh.sqlite"
    _use(db, monkeypatch)
    init_db()
    assert current_revision(database_url()) == "0002"
    connection = sqlite3.connect(db)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "image_drafts" in tables
        assert "alembic_version" in tables
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_legacy_fixture_is_adopted_without_losing_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "legacy.sqlite"
    _legacy_database(db)
    _use(db, monkeypatch)
    init_db()

    assert current_revision(database_url()) == "0002"
    assert list((tmp_path / "backups").glob("pre-migration-*.sqlite"))
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        weekly_columns = {row[1] for row in connection.execute("PRAGMA table_info(weekly_reviews)")}
        assert "week_key" in weekly_columns
        assert "week_start" not in weekly_columns
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in ("image_drafts", "image_links", "review_activities", "sessions"):
            assert table in tables
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_downgrade_drops_indexes_and_upgrade_restores_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "roundtrip.sqlite"
    _use(db, monkeypatch)
    init_db()
    url = database_url()

    downgrade(url, "-1")
    assert current_revision(url) == "0001"
    connection = sqlite3.connect(db)
    try:
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "ix_user_records_user_id_id" not in indexes
    finally:
        connection.close()

    upgrade_to_head(url)
    assert current_revision(url) == "0002"
    connection = sqlite3.connect(db)
    try:
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "ix_user_records_user_id_id" in indexes
    finally:
        connection.close()
