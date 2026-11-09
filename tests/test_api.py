import datetime as dt
import gc
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.api import app
from src.db import database_path


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("LEARNING_ASSISTANT_DB")
        os.environ["LEARNING_ASSISTANT_DB"] = str(Path(self.tmp.name) / "test.sqlite")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            close()
        if self.previous is None:
            os.environ.pop("LEARNING_ASSISTANT_DB", None)
        else:
            os.environ["LEARNING_ASSISTANT_DB"] = self.previous
        gc.collect()
        self.tmp.cleanup()

    def test_health_is_public(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_timeline_and_records_require_login(self) -> None:
        self.assertEqual(self.client.get("/api/timeline").status_code, 401)
        self.assertEqual(self.client.get("/api/records").status_code, 401)
        self.assertEqual(self.client.get("/api/reminders").status_code, 401)
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_reminders_match_the_seven_day_rule(self) -> None:
        self.assertEqual(self.client.post("/api/register", json=_account("remind@example.com")).status_code, 201)
        quiet = self.client.get("/api/reminders", params={"today": "2026-11-02"})
        self.assertEqual(quiet.status_code, 200)
        self.assertEqual(quiet.json()["message"], "今日不发送")
        self.assertEqual(quiet.json()["items"], [])
        self.assertEqual(quiet.json()["within_days"], 7)

        self.assertNotIn("已发送", quiet.json()["message"])
        window = self.client.get("/api/reminders", params={"today": "2026-11-08"})
        self.assertEqual(window.status_code, 200)
        body = window.json()
        self.assertEqual(
            [(item["标题"], item["截止日期"], item["标记"]) for item in body["items"]],
            [("计挑赛人工智能挑战赛-区域赛作品提交截止", "2026-11-13", "剩余5天")],
        )
        self.assertEqual(
            body["items"][0]["来源链接"],
            "https://www.ncccu.org.cn/index/Paper/case1.html",
        )
        self.assertIn("计挑赛人工智能挑战赛-区域赛作品提交截止", body["message"])

        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/reminders", params={"today": "2026-11-08"}).status_code, 401)

    def test_two_users_see_catalog_but_not_each_others_rows(self) -> None:
        alpha = TestClient(app)
        beta = TestClient(app)
        self.assertEqual(alpha.post("/api/register", json=_account("alpha@example.com")).status_code, 201)
        self.assertEqual(beta.post("/api/register", json=_account("beta@example.com")).status_code, 201)

        created = alpha.post("/api/records", json=_private_row())
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(beta.post("/api/records", json=_private_row("仅乙可见")).status_code, 201)

        alpha_records = alpha.get("/api/records").json()["records"]
        beta_records = beta.get("/api/records").json()["records"]
        self.assertIn("仅甲可见", [row["标题"] for row in alpha_records])
        self.assertNotIn("仅甲可见", [row["标题"] for row in beta_records])
        self.assertIn("仅乙可见", [row["标题"] for row in beta_records])
        self.assertNotIn("仅乙可见", [row["标题"] for row in alpha_records])
        for rows in (alpha_records, beta_records):
            exam = next(row for row in rows if row["标题"] == "计挑赛程序设计挑战赛-区域赛考试")
            self.assertEqual(exam["截止日期"], "2026-11-29")
            self.assertEqual(exam["来源链接"], "https://www.ncccu.org.cn/index/Paper/case4.html")
            self.assertEqual(exam["归属"], "共享目录")

        for client in (alpha, beta):
            timeline = client.get("/api/timeline", params={"today": "2026-11-02"})
            self.assertEqual(timeline.status_code, 200)
            self.assertEqual(
                [(item["标题"], item["截止日期"], item["标记"]) for item in timeline.json()["items"]],
                [
                    ("计挑赛人工智能挑战赛-区域赛作品提交截止", "2026-11-13", "剩余11天"),
                    ("APMCM2026英文赛项-报名截止", "2026-11-25", "剩余23天"),
                    ("计挑赛程序设计挑战赛-区域赛报名截止", "2026-11-25", "剩余23天"),
                    ("计挑赛程序设计挑战赛-区域赛考试", "2026-11-29", "剩余27天"),
                ],
            )

        self.assertEqual(alpha.post("/api/logout").status_code, 200)
        self.assertEqual(alpha.get("/api/timeline").status_code, 401)
        self.assertEqual(beta.get("/api/me").json()["email"], "beta@example.com")

    def test_password_is_stored_as_hash_and_login_rejects_a_wrong_one(self) -> None:
        self.assertEqual(self.client.post("/api/register", json=_account("one@example.com")).status_code, 201)
        connection = sqlite3.connect(database_path())
        try:
            stored = connection.execute("SELECT password_hash FROM users").fetchone()[0]
        finally:
            connection.close()
        self.assertNotIn("correct-horse", stored)
        self.assertTrue(stored.startswith("scrypt$"))
        self.client.post("/api/logout")
        bad = self.client.post("/api/login", json={"email": "one@example.com", "password": "wrong-password"})
        self.assertEqual(bad.status_code, 401)
        good = self.client.post("/api/login", json=_account("one@example.com"))
        self.assertEqual(good.status_code, 200)
        duplicate = self.client.post("/api/register", json=_account("one@example.com"))
        self.assertEqual(duplicate.status_code, 409)

    def test_post_private_record_accepts_not_found_and_rejects_a_relative_date(self) -> None:
        from src.records import REAL_RECORDS

        before = REAL_RECORDS.read_text(encoding="utf-8")
        self.assertEqual(self.client.post("/api/register", json=_account("paste@example.com")).status_code, 201)
        created = self.client.post(
            "/api/records",
            json={
                "标题": "手贴的相对日期",
                "类型": "竞赛",
                "截止日期": "未找到",
                "来源链接": "未找到",
                "更新时间": "2026-11-02",
                "适合专业": "未找到",
                "状态": "未处理",
                "核对状态": "已核对",
                "日期原句": "请于本周五前报名。",
                "是否参与提醒": "否",
                "报名条件": "未找到",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["归属"], "我的")
        self.assertEqual(created.json()["截止日期"], "未找到")
        titles = [row["标题"] for row in self.client.get("/api/records").json()["records"]]
        self.assertIn("手贴的相对日期", titles)
        timeline = self.client.get("/api/timeline", params={"today": "2026-11-02"}).json()
        self.assertNotIn("手贴的相对日期", [item["标题"] for item in timeline["items"]])

        rejected = self.client.post(
            "/api/records",
            json={
                "标题": "不该换算的本周五",
                "类型": "竞赛",
                "截止日期": "本周五",
                "来源链接": "未找到",
                "更新时间": "2026-11-02",
                "适合专业": "未找到",
                "状态": "未处理",
                "核对状态": "已核对",
                "日期原句": "请于本周五前报名。",
                "是否参与提醒": "否",
                "报名条件": "未找到",
            },
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("YYYY-MM-DD", rejected.json()["detail"])
        titles = [row["标题"] for row in self.client.get("/api/records").json()["records"]]
        self.assertNotIn("不该换算的本周五", titles)
        self.assertEqual(REAL_RECORDS.read_text(encoding="utf-8"), before)

    def test_reminder_prefs_change_the_window_and_do_not_send_mail(self) -> None:
        self.assertEqual(self.client.get("/api/reminder-settings").status_code, 401)
        self.assertEqual(self.client.post("/api/register", json=_account("prefs@example.com")).status_code, 201)
        other = TestClient(app)
        self.assertEqual(other.post("/api/register", json=_account("other-prefs@example.com")).status_code, 201)

        fresh = self.client.get("/api/reminder-settings")
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual(fresh.json()["基准日"], "")
        self.assertEqual(fresh.json()["提前几天"], 7)
        self.assertIs(fresh.json()["是否邮件"], False)
        self.assertEqual(fresh.json()["邮件触达"], "未接通")

        saved = self.client.put(
            "/api/reminder-settings",
            json={"基准日": "2026-11-08", "提前几天": 7, "是否邮件": True},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["邮件触达"], "未接通")
        again = self.client.get("/api/reminder-settings").json()
        self.assertEqual(again["基准日"], "2026-11-08")
        self.assertEqual(again["提前几天"], 7)
        self.assertIs(again["是否邮件"], True)
        self.assertEqual(other.get("/api/reminder-settings").json()["基准日"], "")
        self.assertTrue(again["updated_at"])

        timeline = self.client.get("/api/timeline")
        self.assertEqual(timeline.json()["today"], "2026-11-08")
        self.assertEqual(timeline.json()["within_days"], 7)
        marks = {item["标题"]: item["标记"] for item in timeline.json()["items"]}
        self.assertEqual(marks["计挑赛人工智能挑战赛-区域赛作品提交截止"], "剩余5天")
        self.assertEqual(marks["计挑赛程序设计挑战赛-区域赛考试"], "剩余21天")

        quiet = self.client.get("/api/reminders", params={"today": "2026-11-02"})
        self.assertEqual(quiet.json()["message"], "今日不发送")
        self.assertEqual(quiet.json()["within_days"], 7)
        window = self.client.get("/api/reminders")
        self.assertEqual(window.json()["today"], "2026-11-08")
        self.assertEqual(
            [(item["标题"], item["标记"]) for item in window.json()["items"]],
            [("计挑赛人工智能挑战赛-区域赛作品提交截止", "剩余5天")],
        )

        widened = self.client.put(
            "/api/reminder-settings",
            json={"基准日": "", "提前几天": 11, "是否邮件": False},
        )
        self.assertEqual(widened.status_code, 200)
        scoped = self.client.get("/api/reminders", params={"today": "2026-11-02"})
        self.assertEqual(scoped.json()["within_days"], 11)
        self.assertEqual(
            [item["标题"] for item in scoped.json()["items"]],
            ["计挑赛人工智能挑战赛-区域赛作品提交截止"],
        )
        self.assertEqual(other.get("/api/reminders", params={"today": "2026-11-02"}).json()["within_days"], 7)

        rejected = self.client.put(
            "/api/reminder-settings",
            json={"基准日": "本周五", "提前几天": 7, "是否邮件": False},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self.client.get("/api/reminder-settings").json()["基准日"], "")

    def test_mine_lists_only_private_rows_and_status_stays_after_update(self) -> None:
        self.assertEqual(self.client.get("/api/mine").status_code, 401)
        self.assertEqual(self.client.post("/api/register", json=_account("mine@example.com")).status_code, 201)
        other = TestClient(app)
        self.assertEqual(other.post("/api/register", json=_account("mine-other@example.com")).status_code, 201)
        created = self.client.post("/api/records", json=_private_row("手边的一条"))
        self.assertEqual(created.status_code, 201, created.text)
        self.assertIsInstance(created.json()["id"], int)
        mine = self.client.get("/api/mine")
        self.assertEqual(mine.status_code, 200)
        titles = [row["标题"] for row in mine.json()["records"]]
        self.assertEqual(titles, ["手边的一条"])
        self.assertNotIn("计挑赛程序设计挑战赛-区域赛考试", titles)
        record_id = mine.json()["records"][0]["id"]
        updated = self.client.patch(f"/api/mine/{record_id}", json={"状态": "已交"})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["状态"], "已交")
        self.assertEqual(self.client.get("/api/mine").json()["records"][0]["状态"], "已交")
        self.assertEqual(other.patch(f"/api/mine/{record_id}", json={"状态": "放弃"}).status_code, 404)
        self.assertEqual(self.client.get("/api/mine").json()["records"][0]["状态"], "已交")
        self.assertEqual(self.client.patch(f"/api/mine/{record_id}", json={"状态": "下周三"}).status_code, 400)
        edited = self.client.put(
            f"/api/mine/{record_id}",
            json={"标题": "改过的标题", "日期原句": "请于本周五前报名。"},
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()["标题"], "改过的标题")
        self.assertEqual(edited.json()["截止日期"], "未找到")
        self.assertEqual(self.client.get("/api/mine").json()["records"][0]["标题"], "改过的标题")
        self.assertEqual(other.put(f"/api/mine/{record_id}", json={"标题": "别人改的"}).status_code, 404)
        self.assertEqual(other.delete(f"/api/mine/{record_id}").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/mine/{record_id}").status_code, 200)
        self.assertEqual(self.client.get("/api/mine").json()["records"], [])
        kept = self.client.post("/api/records", json=_private_row("不能写成相对日期"))
        kept_id = kept.json()["id"]
        bad_edit = self.client.put(f"/api/mine/{kept_id}", json={"截止日期": "本周五"})
        self.assertEqual(bad_edit.status_code, 400)
        self.assertEqual(self.client.get("/api/mine").json()["records"][0]["截止日期"], "未找到")

    def test_past_deadline_is_marked_expired_without_rewriting_it(self) -> None:
        from unittest.mock import patch

        self.client.post("/api/register", json=_account("expired@example.com"))
        payload = _private_row("沿用已有截止日")
        payload["截止日期"] = "2026-11-13"
        payload["是否参与提醒"] = "是"
        payload["来源链接"] = "https://www.ncccu.org.cn/index/Paper/case1.html"
        payload["日期原句"] = "区域赛/省赛作品截止提交时间：11月13日23:59（报名时间：即日起—2026年11月13日）"
        created = self.client.post("/api/records", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        with patch("src.store.resolve_today", return_value=dt.date(2026, 11, 14)):
            listed = self.client.get("/api/mine")
        self.assertEqual(listed.status_code, 200)
        row = listed.json()["records"][0]
        self.assertEqual(row["截止日期"], "2026-11-13")
        self.assertEqual(row["展示"], "已过期")

    def test_weekly_review_echoes_the_user_and_persists(self) -> None:
        from src.config import load_settings, resolve_today

        self.assertEqual(self.client.get("/api/weekly-review").status_code, 401)
        self.assertEqual(self.client.post("/api/register", json=_account("review@example.com")).status_code, 201)
        other = TestClient(app)
        self.assertEqual(other.post("/api/register", json=_account("review-other@example.com")).status_code, 201)
        empty = self.client.put("/api/weekly-review", json={"done": "", "stuck": "", "next": ""})
        self.assertEqual(empty.status_code, 400)
        saved = self.client.put(
            "/api/weekly-review",
            json={"done": "对照了官网原文", "stuck": "时间不够", "next": "继续只抄原文"},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        body = saved.json()
        day = resolve_today(None, load_settings()["timezone"])
        year, week, _ = day.isocalendar()
        self.assertEqual(body["week"], f"{year}-W{week:02d}")
        self.assertIn("对照了官网原文", body["summary"])
        self.assertIn("时间不够", body["summary"])
        self.assertIn("继续只抄原文", body["plan"])
        self.assertIn("不要把截止日期改成原文里没有的日子", body["behind_hint"])
        self.assertNotIn("2026-12-01", body["summary"] + body["plan"])
        again = self.client.get("/api/weekly-review").json()
        self.assertEqual(again["done"], "对照了官网原文")
        self.assertEqual(other.get("/api/weekly-review").json()["done"], "")

    def test_courses_and_materials_are_private_indexes(self) -> None:
        self.assertEqual(self.client.get("/api/courses").status_code, 401)
        self.assertEqual(self.client.post("/api/register", json=_account("course@example.com")).status_code, 201)
        other = TestClient(app)
        self.assertEqual(other.post("/api/register", json=_account("course-other@example.com")).status_code, 201)
        created = self.client.post("/api/courses", json={"name": "我填的课", "term": "", "notes": "自己记的"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertIsInstance(created.json()["id"], int)
        self.assertEqual(self.client.post("/api/courses", json={"name": ""}).status_code, 400)
        self.assertEqual([row["name"] for row in self.client.get("/api/courses").json()["courses"]], ["我填的课"])
        self.assertEqual(other.get("/api/courses").json()["courses"], [])

        course_id = created.json()["id"]
        material = self.client.post(
            "/api/materials",
            json={"title": "我保存的链接", "course_id": course_id, "kind": "链接", "source_url": "", "page_hint": ""},
        )
        self.assertEqual(material.status_code, 201, material.text)
        self.assertEqual(other.post("/api/materials", json={"title": "别人的", "course_id": course_id}).status_code, 404)
        listed = self.client.get("/api/materials", params={"course_id": course_id})
        self.assertEqual([row["title"] for row in listed.json()["materials"]], ["我保存的链接"])
        self.assertEqual(other.get("/api/materials").json()["materials"], [])
        self.assertNotIn("2026-", material.json()["title"] + material.json()["page_hint"])

    def test_illegal_calendar_date_is_rejected_without_rewriting_old_rows(self) -> None:
        from src.db import connect

        self.assertEqual(self.client.post("/api/register", json=_account("calendar@example.com")).status_code, 201)
        bad = _private_row("非法日")
        bad["截止日期"] = "2026-02-31"
        bad["日期原句"] = "2026-02-31"
        rejected = self.client.post("/api/records", json=bad)
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertIn("真实公历", rejected.json()["detail"])
        leap = _private_row("闰日")
        leap["截止日期"] = "2024-02-29"
        leap["日期原句"] = "2024年2月29日"
        self.assertEqual(self.client.post("/api/records", json=leap).status_code, 201)
        not_leap = _private_row("平年二月")
        not_leap["截止日期"] = "2026-02-29"
        not_leap["日期原句"] = "2026-02-29"
        self.assertEqual(self.client.post("/api/records", json=not_leap).status_code, 400)
        relative = _private_row("相对日期")
        self.assertEqual(self.client.post("/api/records", json=relative).status_code, 201)
        self.assertEqual(relative["截止日期"], "未找到")

        connection = connect()
        try:
            user_id = connection.execute(
                "SELECT id FROM users WHERE email = ?",
                ("calendar@example.com",),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO user_records (
                    user_id, title, info_type, deadline, source_url, updated_on, major, status,
                    review_state, date_quote, remind, condition
                ) VALUES (?, '旧非法日', '竞赛', '2026-02-31', '未找到', '2026-11-02', '未找到', '未处理', '草稿', '旧记录', '否', '未找到')
                """,
                (user_id,),
            )
            connection.commit()
        finally:
            connection.close()
        listed = self.client.get("/api/mine").json()["records"]
        old = next(row for row in listed if row["标题"] == "旧非法日")
        self.assertEqual(old["截止日期"], "2026-02-31")
        edited = self.client.put(f"/api/mine/{old['id']}", json={"标题": "想改标题"})
        self.assertEqual(edited.status_code, 400, edited.text)
        again = next(row for row in self.client.get("/api/mine").json()["records"] if row["id"] == old["id"])
        self.assertEqual(again["标题"], "旧非法日")
        self.assertEqual(again["截止日期"], "2026-02-31")

    def test_bad_today_is_rejected_after_login(self) -> None:
        self.client.post("/api/register", json=_account("date@example.com"))
        response = self.client.get("/api/timeline", params={"today": "下周三"})
        self.assertEqual(response.status_code, 400)


def _account(email: str) -> dict[str, str]:
    return {"email": email, "password": "correct-horse"}


def _private_row(title: str = "仅甲可见") -> dict[str, str]:
    return {
        "标题": title,
        "类型": "竞赛",
        "截止日期": "未找到",
        "来源链接": "未找到",
        "更新时间": "2026-11-02",
        "适合专业": "未找到",
        "状态": "未处理",
        "核对状态": "已核对",
        "日期原句": "没有公历日期。",
        "是否参与提醒": "否",
        "报名条件": "未找到",
    }


if __name__ == "__main__":
    unittest.main()