"""用脱敏旧 DDL 建临时库，不读取真实用户数据。"""

from __future__ import annotations

import gc
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config import load_settings, resolve_today
from src.courses import add_course, add_material, list_courses, list_materials
from src.db import connect
from src.migrate import _week_key
from src.prefs import get as get_prefs
from src.prefs import update as update_prefs
from src.review import get_current, save_current
from src.store import list_own_records, update_own_record

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "schema-only-20261107.json"
ORDER = ("users", "courses", "reminder_prefs", "user_records", "weekly_reviews", "materials")


class OldSchemaMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "legacy.sqlite"
        self.previous = os.environ.get("LEARNING_ASSISTANT_DB")
        os.environ["LEARNING_ASSISTANT_DB"] = str(self.path)
        self._create_legacy()

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop("LEARNING_ASSISTANT_DB", None)
        else:
            os.environ["LEARNING_ASSISTANT_DB"] = self.previous
        gc.collect()
        self.tmp.cleanup()

    def test_upgrade_keeps_semantics_and_can_be_repeated(self) -> None:
        monday = _monday(resolve_today(None, load_settings()["timezone"]))
        other_week = monday.replace(year=monday.year - 1)
        self._seed(monday, other_week)
        connect().close()

        review = get_current(1)
        self.assertEqual(review["week"], _week_key(monday.isoformat()))
        self.assertEqual(review["done"], "synthetic-done-user-1")
        self.assertNotIn("synthetic-done-user-2", review["summary"])
        self.assertEqual(get_current(2)["done"], "")

        saved = save_current(1, {"done": "synthetic-done-user-1-edited", "stuck": "", "next": "synthetic-next"})
        self.assertIn("synthetic-done-user-1-edited", saved["summary"])
        self.assertIn("synthetic-next", saved["plan"])
        self.assertNotIn("2026-12-01", saved["summary"] + saved["plan"])
        self.assertEqual(get_current(1)["done"], "synthetic-done-user-1-edited")
        self.assertEqual(get_current(2)["done"], "")

        prefs = get_prefs(1)
        self.assertEqual(prefs["提前几天"], 9)
        self.assertEqual(prefs["基准日"], "2026-11-08")
        self.assertIs(prefs["是否邮件"], False)
        self.assertEqual(prefs["邮件触达"], "未接通")
        updated = update_prefs(1, {"基准日": "", "提前几天": 11, "是否邮件": True})
        self.assertEqual(updated["提前几天"], 11)
        self.assertEqual(updated["邮件触达"], "未接通")
        self.assertEqual(get_prefs(2)["提前几天"], 4)

        courses = list_courses(1)
        self.assertEqual([row["name"] for row in courses], ["synthetic-course"])
        self.assertEqual(list_courses(2), [])
        materials = list_materials(1, courses[0]["id"])
        self.assertEqual([row["title"] for row in materials], ["synthetic-material"])
        self.assertEqual(materials[0]["course_id"], courses[0]["id"])
        added = add_material(1, {"title": "synthetic-unlinked", "course_id": None, "kind": "", "source_url": "", "page_hint": ""})
        self.assertIsNone(added["course_id"])
        with self.assertRaises(LookupError):
            add_material(2, {"title": "nope", "course_id": courses[0]["id"]})
        new_course = add_course(2, {"name": "synthetic-course-user-2", "term": "", "notes": ""})
        self.assertEqual(list_courses(1)[0]["id"], courses[0]["id"])
        self.assertNotEqual(new_course["id"], courses[0]["id"])

        records = list_own_records(1)
        self.assertEqual([row["标题"] for row in records], ["synthetic-record"])
        self.assertEqual(records[0]["截止日期"], "未找到")
        changed = update_own_record(1, records[0]["id"], {"标题": "synthetic-record-edited"})
        self.assertEqual(changed["标题"], "synthetic-record-edited")
        self.assertEqual(changed["截止日期"], "未找到")
        self.assertIsNone(update_own_record(2, records[0]["id"], {"标题": "other"}))

        raw = sqlite3.connect(self.path)
        try:
            preserved = raw.execute(
                "SELECT done_text FROM weekly_reviews WHERE user_id = 2"
            ).fetchone()[0]
            self.assertEqual(preserved, "synthetic-done-user-2")
            self.assertNotIn("week_start", {row[1] for row in raw.execute("PRAGMA table_info(weekly_reviews)")})
        finally:
            raw.close()

        connect().close()
        raw = sqlite3.connect(self.path)
        try:
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM weekly_reviews").fetchone()[0], 2)
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM courses").fetchone()[0], 2)
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM materials").fetchone()[0], 2)
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM user_records").fetchone()[0], 1)
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM reminder_prefs").fetchone()[0], 2)
            self.assertIn("image_rel", {row[1] for row in raw.execute("PRAGMA table_info(materials)")})
            self.assertEqual(
                raw.execute("SELECT title, user_id FROM materials WHERE id = 8").fetchone(),
                ("synthetic-material", 1),
            )
            for table in ("image_drafts", "image_links", "review_activities"):
                self.assertIn(table, {row[0] for row in raw.execute("SELECT name FROM sqlite_master")})
                self.assertEqual(raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
        finally:
            raw.close()

    def test_failed_upgrade_rolls_back(self) -> None:
        monday = _monday(resolve_today(None, load_settings()["timezone"]))
        self._seed(monday, monday.replace(year=monday.year - 1))
        with patch("src.migrate._migration_checkpoint", side_effect=RuntimeError("stop")):
            with self.assertRaises(RuntimeError):
                connect().close()
        raw = sqlite3.connect(self.path)
        try:
            columns = {row[1] for row in raw.execute("PRAGMA table_info(weekly_reviews)")}
            self.assertIn("week_start", columns)
            self.assertNotIn("week_key", columns)
            self.assertEqual(
                raw.execute("SELECT done_text FROM weekly_reviews WHERE user_id = 1").fetchone()[0],
                "synthetic-done-user-1",
            )
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM courses").fetchone()[0], 1)
        finally:
            raw.close()

    def test_failed_connect_closes_the_sqlite_handle(self) -> None:
        closed: list[str] = []
        real_connect = sqlite3.connect

        class Tracking:
            def __init__(self, inner, path):
                self._inner = inner
                self._path = path

            def close(self):
                closed.append(str(self._path))
                return self._inner.close()

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def __setattr__(self, name, value):
                if name in {"_inner", "_path"}:
                    object.__setattr__(self, name, value)
                else:
                    setattr(self._inner, name, value)

        def tracking(path, *args, **kwargs):
            return Tracking(real_connect(path, *args, **kwargs), path)

        with patch("src.db.sqlite_adapter.backup_database", return_value=Path("backup.sqlite")):
            with patch("src.db.sqlite_adapter.sqlite3.connect", tracking):
                with patch("src.migrate._migration_checkpoint", side_effect=RuntimeError("stop")):
                    with self.assertRaises(RuntimeError):
                        connect()
        self.assertTrue(closed, "迁移失败时必须关掉 SQLite 连接，否则 Windows 无法删除测试库")
        self.path.unlink()

    def test_new_database_still_creates(self) -> None:
        fresh = Path(self.tmp.name) / "fresh.sqlite"
        os.environ["LEARNING_ASSISTANT_DB"] = str(fresh)
        connect().close()
        raw = sqlite3.connect(fresh)
        try:
            columns = {row[1] for row in raw.execute("PRAGMA table_info(weekly_reviews)")}
            self.assertIn("week_key", columns)
            self.assertNotIn("week_start", columns)
        finally:
            raw.close()
        os.environ["LEARNING_ASSISTANT_DB"] = str(self.path)

    def _create_legacy(self) -> None:
        schema = json.loads(FIXTURE.read_text(encoding="utf-8"))
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            for name in ORDER:
                connection.execute(schema["tables"][name]["create_table_sql"])
            connection.commit()
        finally:
            connection.close()

    def _seed(self, current_monday, older_monday) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executemany(
                "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (
                    (1, "synthetic-user-1@example.com", "synthetic-hash-1", "2026-11-01T00:00:00+00:00"),
                    (2, "synthetic-user-2@example.com", "synthetic-hash-2", "2026-11-01T00:00:00+00:00"),
                ),
            )
            connection.execute(
                """
                INSERT INTO weekly_reviews (
                    id, user_id, week_start, done_text, stuck_text, next_text, summary, plan, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    1,
                    current_monday.isoformat(),
                    "synthetic-done-user-1",
                    "",
                    "",
                    "synthetic-summary-user-1",
                    "synthetic-plan-user-1",
                    "2026-11-02T00:00:00+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO weekly_reviews (
                    id, user_id, week_start, done_text, stuck_text, next_text, summary, plan, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    2,
                    2,
                    older_monday.isoformat(),
                    "synthetic-done-user-2",
                    "synthetic-stuck-user-2",
                    "synthetic-next-user-2",
                    "synthetic-summary-user-2",
                    "synthetic-plan-user-2",
                    "2026-10-01T00:00:00+00:00",
                ),
            )
            connection.executemany(
                """
                INSERT INTO reminder_prefs (user_id, within_days, baseline_date, email_enabled, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (1, 9, "2026-11-08", 0, "2026-11-02T00:00:00+00:00"),
                    (2, 4, "", 1, "2026-11-02T00:00:00+00:00"),
                ),
            )
            connection.execute(
                """
                INSERT INTO courses (id, user_id, name, term, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (7, 1, "synthetic-course", "", "synthetic-note", "2026-11-01T00:00:00+00:00"),
            )
            connection.execute(
                """
                INSERT INTO materials (
                    id, user_id, course_id, title, kind, source_url, page_hint, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (8, 1, 7, "synthetic-material", "note", "", "", "2026-11-01T00:00:00+00:00"),
            )
            connection.execute(
                """
                INSERT INTO user_records (
                    id, user_id, title, info_type, deadline, source_url, updated_on, major, status,
                    review_state, date_quote, remind, condition
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    3,
                    1,
                    "synthetic-record",
                    "竞赛",
                    "未找到",
                    "未找到",
                    "2026-11-02",
                    "未找到",
                    "未处理",
                    "已核对",
                    "请于本周五前报名。",
                    "否",
                    "未找到",
                ),
            )
            connection.commit()
        finally:
            connection.close()


def _monday(day):
    return day.fromordinal(day.toordinal() - day.weekday())


if __name__ == "__main__":
    unittest.main()
