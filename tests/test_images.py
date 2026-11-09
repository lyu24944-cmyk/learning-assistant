"""识图上传、真实本地识别和确认。合成图只在测试里绘制，并标明不是真实通知。"""

from __future__ import annotations

import gc
import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from src.api import app
from src.db import connect
from src.image_drafts import confirm_draft
from src.ocr import OcrUnavailable, describe_recognizer_languages, engine_status, transcribe
from src.ocr_windows import dimension_exceeds, prepare_ocr_image
from src.poster import parse_poster
from tests.csrf_client import CsrfClient

SYNTHETIC_MARK = "合成样例，不是真实通知"
POSTER_LINES = [
    "全国大学生数学建模竞赛",
    "报名截止：2026年11月25日",
    "作品提交截止：2026年11月30日",
    "报名条件：全日制在校本科生",
    "主办方：中国工业与应用数学学会",
    "来源：https://www.example.com/mcm",
    SYNTHETIC_MARK,
]


class ImageDraftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_db = os.environ.get("LEARNING_ASSISTANT_DB")
        self.previous_private = os.environ.get("LEARNING_ASSISTANT_PRIVATE")
        self.previous_ocr = os.environ.get("LEARNING_ASSISTANT_TESSERACT")
        self.previous_provider = os.environ.get("LEARNING_ASSISTANT_OCR_PROVIDER")
        self.previous_font = os.environ.get("LEARNING_ASSISTANT_CJK_FONT")
        self.previous_sync = os.environ.get("LEARNING_ASSISTANT_OCR_SYNC")
        os.environ["LEARNING_ASSISTANT_DB"] = str(Path(self.tmp.name) / "test.sqlite")
        os.environ["LEARNING_ASSISTANT_PRIVATE"] = str(Path(self.tmp.name) / "private")
        # 这一组测的是识别和确认的语义，不是队列。同步模式让上传当场出结果，
        # 队列本身由 tests/test_ocr_queue.py 覆盖。
        os.environ["LEARNING_ASSISTANT_OCR_SYNC"] = "1"
        os.environ.pop("LEARNING_ASSISTANT_TESSERACT", None)
        os.environ.pop("LEARNING_ASSISTANT_OCR_PROVIDER", None)
        self.client = CsrfClient(TestClient(app))
        self.client.post("/api/register", json={"email": "ocr@example.com", "password": "secret-pass-1"})

    def tearDown(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            close()
        self._restore("LEARNING_ASSISTANT_DB", self.previous_db)
        self._restore("LEARNING_ASSISTANT_PRIVATE", self.previous_private)
        self._restore("LEARNING_ASSISTANT_TESSERACT", self.previous_ocr)
        self._restore("LEARNING_ASSISTANT_OCR_PROVIDER", self.previous_provider)
        self._restore("LEARNING_ASSISTANT_CJK_FONT", self.previous_font)
        self._restore("LEARNING_ASSISTANT_OCR_SYNC", self.previous_sync)
        gc.collect()
        self.tmp.cleanup()

    def test_real_ocr_matches_candidates_until_the_user_confirms(self) -> None:
        poster = _synthetic_poster()
        created = self.client.post(
            "/api/image-drafts",
            files={"file": ("../../合成海报.png", poster, "image/png")},
        )
        self.assertEqual(created.status_code, 202, created.text)
        body = created.json()
        chosen = engine_status()
        self.assertTrue(chosen["available"], chosen["detail"])
        self.assertEqual(body["provider"], chosen["provider"])
        self.assertEqual(body["mode"], "识别草稿")
        self.assertTrue(body["ocr_text"].strip())
        self.assertIn(SYNTHETIC_MARK, "\n".join(POSTER_LINES))
        flat = "".join(body["ocr_text"].split())
        self.assertEqual(body["candidate"]["事项"], parse_poster(body["ocr_text"])["事项"])
        for item in body["candidate"]["事项"]:
            self.assertIn(item["标注"], ("待核对", "未找到"))
            if item["截止日期"] != "未找到":
                quoted = [row["截止日期"] for row in parse_poster(item["日期原句"])["事项"]]
                self.assertIn(item["截止日期"], quoted)
        self.assertEqual(body["candidate"]["识别原文"], body["ocr_text"])
        self.assertEqual(_without_spaces(body["ocr_text"]), _without_spaces(body["candidate"]["空白规范化"]))
        normalized = body["candidate"]["空白规范化"]
        by_matter = {item["事项"]: item for item in body["candidate"]["事项"]}
        if "报名截止" in normalized:
            self.assertIn("报名截止", by_matter)
        if "作品提交截止" in normalized:
            self.assertIn("作品提交截止", by_matter)
        if chosen["provider"] == "tesseract":
            self.assertIn("报名截止", flat)
            self.assertIn("2026年11月25日", flat)
            self.assertIn("作品提交截止", flat)
            self.assertIn("2026年11月30日", flat)
            self.assertIn("https://www.example.com/mcm", flat)
            self.assertEqual(by_matter["报名截止"]["截止日期"], "2026-11-25")
            self.assertEqual(by_matter["作品提交截止"]["截止日期"], "2026-11-30")
        self.assertNotIn("..", str(Path(os.environ["LEARNING_ASSISTANT_PRIVATE"]).resolve()))
        stored = list(Path(os.environ["LEARNING_ASSISTANT_PRIVATE"]).rglob("original.png"))
        self.assertEqual(len(stored), 1)
        self.assertNotIn("..", stored[0].parts)

        self.assertEqual(self.client.get("/api/mine").json()["records"], [])
        review = self.client.put(
            "/api/weekly-review",
            json={"done": "手写完成", "stuck": "手写卡住", "next": "手写计划"},
        )
        self.assertEqual(review.status_code, 200, review.text)

        payload = _confirm_items(body, verified=False)
        saved = self.client.post(f"/api/image-drafts/{body['id']}/confirm", json={"items": payload})
        self.assertEqual(saved.status_code, 200, saved.text)
        again = self.client.post(f"/api/image-drafts/{body['id']}/confirm", json={"items": payload})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["record_ids"], saved.json()["record_ids"])
        self.assertTrue(again.json()["idempotent"])
        records = self.client.get("/api/mine").json()["records"]
        self.assertEqual(len(records), len(saved.json()["record_ids"]))
        self.assertTrue(all(row["状态"] == "未处理" for row in records))
        self.assertTrue(all(row["是否参与提醒"] == "否" for row in records))
        self.assertTrue(all(row["核对状态"] == "草稿" for row in records))
        self.assertEqual(
            {row["截止日期"] for row in records},
            {item["截止日期"] for item in body["candidate"]["事项"]},
        )
        self.assertTrue(all(row["资料id"] == saved.json()["material_id"] for row in records))

        reminders = self.client.get("/api/reminders", params={"today": "2026-11-08"}).json()
        titles = [item["标题"] for item in reminders["items"]]
        self.assertTrue(all("数学建模" not in title for title in titles))

        current = self.client.get("/api/weekly-review").json()
        self.assertEqual(current["done"], "手写完成")
        self.assertEqual(current["stuck"], "手写卡住")
        self.assertEqual(current["next"], "手写计划")
        self.assertTrue(current["activities"])
        self.assertIn("识图新增", current["activities"][0]["text"])
        self.assertIn("整理了资料", current["activities"][0]["text"])
        self.assertNotIn("识图新增", current["summary"])

        material = self.client.get("/api/materials").json()["materials"]
        linked = next(item for item in material if item["id"] == saved.json()["material_id"])
        self.assertTrue(linked["has_image"])
        self.assertEqual(sorted(linked["record_ids"]), sorted(saved.json()["record_ids"]))
        if "https://www.example.com/mcm" in body["candidate"]["空白规范化"]:
            self.assertIn("https://www.example.com/mcm", records[0]["来源链接"])
            self.assertNotIn("没有官网", linked["page_hint"])
        else:
            self.assertEqual(records[0]["来源链接"], "未找到")
            self.assertIn("没有官网", linked["page_hint"])
        original = self.client.get(f"/api/image-drafts/{body['id']}/original")
        self.assertEqual(original.status_code, 200)
        self.assertEqual(original.content, stored[0].read_bytes())
        self.assertEqual(stored[0].read_bytes(), poster)
        preview = self.client.get(f"/api/image-drafts/{body['id']}/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertNotEqual(preview.content, original.content)

        other = CsrfClient(TestClient(app))
        other.post("/api/register", json={"email": "other-ocr@example.com", "password": "secret-pass-1"})
        self.assertEqual(other.get(f"/api/image-drafts/{body['id']}/original").status_code, 404)
        self.assertEqual(other.get(f"/api/image-drafts/{body['id']}/preview").status_code, 404)
        self.assertEqual(other.get(f"/api/materials/{linked['id']}/image").status_code, 404)
        self.assertEqual(other.get("/api/mine").json()["records"], [])
        other.close()

    def test_cancel_invalid_file_and_missing_engine_do_not_invent_results(self) -> None:
        poster = self.client.post(
            "/api/image-drafts",
            files={"file": ("poster.png", _tiny_png(), "image/png")},
        )
        self.assertEqual(poster.status_code, 202, poster.text)
        draft_id = poster.json()["id"]
        cancelled = self.client.post(f"/api/image-drafts/{draft_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        rejected = self.client.post(f"/api/image-drafts/{draft_id}/confirm", json={"items": _confirm_items(poster.json(), False)})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self.client.get("/api/mine").json()["records"], [])
        self.assertEqual(self.client.get("/api/weekly-review").json()["activities"], [])

        svg = self.client.post(
            "/api/image-drafts",
            files={"file": ("x.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/svg+xml")},
        )
        self.assertEqual(svg.status_code, 400)
        huge = b"\xff\xd8\xff" + b"0" * (8 * 1024 * 1024)
        oversized = self.client.post("/api/image-drafts", files={"file": ("big.jpg", huge, "image/jpeg")})
        self.assertEqual(oversized.status_code, 400)
        self.assertIn("8MB", oversized.json()["detail"])

        os.environ["LEARNING_ASSISTANT_OCR_PROVIDER"] = "tesseract"
        os.environ["LEARNING_ASSISTANT_TESSERACT"] = str(Path(self.tmp.name) / "missing-tesseract")
        manual = self.client.post(
            "/api/image-drafts",
            files={"file": ("note.png", _tiny_png(), "image/png")},
        )
        self.assertEqual(manual.status_code, 202, manual.text)
        self.assertEqual(manual.json()["mode"], "人工补录")
        self.assertEqual(manual.json()["ocr_text"], "")
        self.assertEqual(manual.json()["candidate"]["事项"], [])
        self.assertIn("不可用", manual.json()["ocr_error"])
        self.assertNotIn("数学建模", manual.json()["candidate"]["标题"])
        saved = self.client.post(
            f"/api/image-drafts/{manual.json()['id']}/confirm",
            json={
                "items": [
                    {
                        "事项": "报名截止",
                        "标题": "手写补录",
                        "类型": "竞赛",
                        "截止日期": "未找到",
                        "日期原句": "海报上看不清",
                        "来源链接": "未找到",
                        "报名条件": "未找到",
                        "主办方": "未找到",
                        "适合专业": "未找到",
                        "核对状态": "草稿",
                        "是否参与提醒": "是",
                    }
                ]
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        row = self.client.get("/api/mine").json()["records"][0]
        self.assertEqual(row["标题"], "手写补录-报名截止")
        self.assertEqual(row["状态"], "未处理")
        self.assertEqual(row["是否参与提醒"], "否")
        activity = self.client.get("/api/weekly-review").json()["activities"][0]["text"]
        self.assertIn("不是识别结果", activity)

        os.environ["LEARNING_ASSISTANT_OCR_PROVIDER"] = "tesseract"
        os.environ["LEARNING_ASSISTANT_TESSERACT"] = str(Path(self.tmp.name) / "missing-tesseract")
        checked = self.client.post(
            "/api/image-drafts",
            files={"file": ("note2.png", _tiny_png(), "image/png")},
        )
        self.assertEqual(checked.status_code, 202, checked.text)
        unfinished = {
            "事项": "作品提交截止",
            "标题": "手写日期",
            "类型": "竞赛",
            "截止日期": "未找到",
            "日期原句": "11月30日",
            "来源链接": "未找到",
            "报名条件": "未找到",
            "主办方": "未找到",
            "适合专业": "未找到",
            "核对状态": "已核对",
            "是否参与提醒": "是",
        }
        denied = self.client.post(f"/api/image-drafts/{checked.json()['id']}/confirm", json={"items": [unfinished]})
        self.assertEqual(denied.status_code, 400)
        unfinished["截止日期"] = "2026-11-10"
        unfinished["日期原句"] = "作品提交截止：2026年11月10日"
        accepted = self.client.post(f"/api/image-drafts/{checked.json()['id']}/confirm", json={"items": [unfinished]})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        reminded = self.client.get("/api/reminders", params={"today": "2026-11-08"}).json()
        matched = [item for item in reminded["items"] if item["标题"] == "手写日期-作品提交截止"]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["截止日期"], "2026-11-10")
        stored = next(row for row in self.client.get("/api/mine").json()["records"] if row["标题"] == "手写日期-作品提交截止")
        self.assertEqual(stored["状态"], "未处理")
        self.assertEqual(stored["核对状态"], "已核对")
        illegal = dict(unfinished)
        illegal["标题"] = "非法公历"
        illegal["截止日期"] = "2026-02-31"
        illegal["日期原句"] = "2026-02-31"
        another = self.client.post(
            "/api/image-drafts",
            files={"file": ("note3.png", _tiny_png(), "image/png")},
        )
        self.assertEqual(another.status_code, 202, another.text)
        denied_day = self.client.post(
            f"/api/image-drafts/{another.json()['id']}/confirm",
            json={"items": [illegal]},
        )
        self.assertEqual(denied_day.status_code, 400)
        self.assertNotIn("非法公历", [row["标题"] for row in self.client.get("/api/mine").json()["records"]])

    def test_anonymous_and_cross_user_cannot_touch_drafts(self) -> None:
        anon = CsrfClient(TestClient(app))
        self.assertEqual(anon.get("/api/ocr/status").status_code, 401)
        self.assertEqual(
            anon.post("/api/image-drafts", files={"file": ("a.png", _tiny_png(), "image/png")}).status_code,
            401,
        )
        anon.close()
        created = self.client.post(
            "/api/image-drafts",
            files={"file": ("a.png", _tiny_png(), "image/png")},
        )
        self.assertEqual(created.status_code, 202, created.text)
        draft_id = created.json()["id"]
        other = CsrfClient(TestClient(app))
        self.assertEqual(other.post("/api/register", json={"email": "other-auth@example.com", "password": "secret-pass-1"}).status_code, 201)
        self.assertEqual(other.get(f"/api/image-drafts/{draft_id}").status_code, 404)
        self.assertEqual(other.post(f"/api/image-drafts/{draft_id}/confirm", json={"items": [_manual_item()]}).status_code, 404)
        self.assertEqual(other.post(f"/api/image-drafts/{draft_id}/cancel").status_code, 404)
        self.assertEqual(other.get(f"/api/image-drafts/{draft_id}/original").status_code, 404)
        self.assertEqual(other.get(f"/api/image-drafts/{draft_id}/preview").status_code, 404)
        saved = self.client.post(f"/api/image-drafts/{draft_id}/confirm", json={"items": [_manual_item()]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(other.get(f"/api/materials/{saved.json()['material_id']}/image").status_code, 404)
        self.assertEqual(other.get("/api/mine").json()["records"], [])
        other.close()

    def test_multiframe_and_pixel_limits_are_rejected(self) -> None:
        frames = self.client.post(
            "/api/image-drafts",
            files={"file": ("anim.gif", _animated_gif(), "image/gif")},
        )
        self.assertEqual(frames.status_code, 400)
        self.assertIn("静止", frames.json()["detail"])
        wide = self.client.post(
            "/api/image-drafts",
            files={"file": ("wide.png", _png_size(6001, 16), "image/png")},
        )
        self.assertEqual(wide.status_code, 400)
        self.assertIn("尺寸", wide.json()["detail"])
        pixels = self.client.post(
            "/api/image-drafts",
            files={"file": ("big.png", _png_size(3000, 3000), "image/png")},
        )
        self.assertEqual(pixels.status_code, 400)
        self.assertTrue("尺寸" in pixels.json()["detail"] or "解码" in pixels.json()["detail"])

    def test_second_record_failure_rolls_back_everything(self) -> None:
        created = self.client.post("/api/image-drafts", files={"file": ("a.png", _tiny_png(), "image/png")})
        self.assertEqual(created.status_code, 202, created.text)
        draft_id = created.json()["id"]
        calls = {"n": 0}
        real = confirm_draft.__globals__["_insert_record"]

        def boom(connection, user_id, record):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("second insert failed")
            return real(connection, user_id, record)

        user_id = _only_user_id()
        with patch("src.image_drafts._insert_record", boom):
            with self.assertRaises(RuntimeError):
                confirm_draft(user_id, draft_id, [_manual_item("甲"), _manual_item("乙", "作品提交截止")])
        connection = connect()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM image_links").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM user_records").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM materials").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_activities").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT status FROM image_drafts WHERE id = ?", (draft_id,)).fetchone()[0],
                "pending",
            )
        finally:
            connection.close()
        self.assertEqual(self.client.get("/api/weekly-review").json()["activities"], [])

    def test_concurrent_confirm_keeps_one_record(self) -> None:
        created = self.client.post("/api/image-drafts", files={"file": ("a.png", _tiny_png(), "image/png")})
        self.assertEqual(created.status_code, 202, created.text)
        draft_id = created.json()["id"]
        user_id = _only_user_id()
        barrier = threading.Barrier(2)
        results: list[dict] = []
        errors: list[BaseException] = []

        def run() -> None:
            barrier.wait()
            try:
                results.append(confirm_draft(user_id, draft_id, [_manual_item("并发")]))
            except BaseException as exc:  # noqa: BLE001 - the test records worker failures
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["record_ids"], results[1]["record_ids"])
        self.assertEqual(len(results[0]["record_ids"]), 1)
        self.assertTrue(any(item["idempotent"] for item in results))
        connection = connect()
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM image_links WHERE draft_id = ?", (draft_id,)).fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_activities").fetchone()[0], 1)
        finally:
            connection.close()

    def test_pending_and_cancelled_drafts_do_not_fill_review_done(self) -> None:
        saved = self.client.put(
            "/api/weekly-review",
            json={"done": "手写完成", "stuck": "", "next": ""},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        created = self.client.post("/api/image-drafts", files={"file": ("a.png", _tiny_png(), "image/png")})
        self.assertEqual(created.status_code, 202, created.text)
        midway = self.client.get("/api/weekly-review").json()
        self.assertEqual(midway["done"], "手写完成")
        self.assertEqual(midway["activities"], [])
        cancelled = self.client.post(f"/api/image-drafts/{created.json()['id']}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        after = self.client.get("/api/weekly-review").json()
        self.assertEqual(after["done"], "手写完成")
        self.assertEqual(after["activities"], [])
        self.assertNotIn("识图新增", after["summary"])

    def test_language_limit_and_font_faces_are_explicit(self) -> None:
        chinese_only = describe_recognizer_languages(["zh-Hans-CN"])
        self.assertTrue(chinese_only["available"])
        self.assertEqual(chinese_only["languages"], ["zh-Hans-CN"])
        self.assertFalse(chinese_only["english_independent"])
        self.assertIn("不保证", chinese_only["detail"])
        english_only = describe_recognizer_languages(["en-US", "en-GB"])
        self.assertFalse(english_only["available"])
        self.assertIn("zh-Hans-CN", english_only["detail"])
        self.assertEqual(
            simplified_face_index(["Noto Sans CJK JP", "Noto Sans CJK KR", "Noto Sans CJK SC"], collection=True),
            2,
        )
        self.assertEqual(simplified_face_index(["Noto Sans SC"], collection=False), 0)
        with self.assertRaises(LookupError):
            simplified_face_index(["Noto Sans CJK JP"], collection=True)
        os.environ["LEARNING_ASSISTANT_CJK_FONT"] = str(Path(self.tmp.name) / "missing.ttf")
        with self.assertRaises(AssertionError) as caught:
            _synthetic_poster()
        self.assertIn("LEARNING_ASSISTANT_CJK_FONT", str(caught.exception))
        os.environ.pop("LEARNING_ASSISTANT_CJK_FONT", None)

    def test_engine_limit_is_numeric_not_text_order(self) -> None:
        self.assertFalse(dimension_exceeds(1700, 1200, 10000))
        self.assertFalse(dimension_exceeds(16, 16, 10000))
        self.assertFalse(dimension_exceeds("1700", "1200", "10000"))
        self.assertFalse(dimension_exceeds(1700, 1200, None))
        self.assertTrue(dimension_exceeds(12000, 10, 10000))
        self.assertGreater("1700", "10000")

    def test_windows_provider_is_not_faked_from_here(self) -> None:
        os.environ["LEARNING_ASSISTANT_OCR_PROVIDER"] = "windows"
        status = engine_status()
        if sys.platform == "win32":
            self.assertEqual(status["provider"], "windows")
            self.assertIn("zh-Hans-CN", status["languages"])
            self.assertFalse(status["english_independent"])
            self.assertTrue(status["available"], status["detail"])
        else:
            self.assertEqual(status["provider"], "windows")
            self.assertFalse(status["available"])
            self.assertNotIn("数学建模", status["detail"])
            with self.assertRaises(OcrUnavailable):
                transcribe(Image.new("RGB", (32, 32), "white"), status)
        original = Image.new("RGB", (800, 4000), "white")
        prepared = prepare_ocr_image(original, 10000)
        self.assertEqual(original.size, (800, 4000))
        self.assertEqual(prepared.size, (1600, 8000))
        tall = Image.new("L", (12000, 10), 255)
        capped = prepare_ocr_image(tall, 10000)
        self.assertLessEqual(max(capped.size), 10000)
        self.assertEqual(tall.size, (12000, 10))
        poster = Image.new("L", (1700, 1200), 255)
        self.assertEqual(prepare_ocr_image(poster, 10000).size, (1700, 1200))

    def _restore(self, name: str, previous: str | None) -> None:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def _confirm_items(body: dict, verified: bool) -> list[dict]:
    candidate = body["candidate"]
    return [
        {
            "事项": item["事项"],
            "标题": candidate["标题"],
            "类型": "竞赛",
            "截止日期": item["截止日期"],
            "日期原句": item["日期原句"],
            "来源链接": candidate["来源链接"],
            "报名条件": candidate["报名条件"],
            "主办方": candidate["主办方"],
            "适合专业": candidate["适合专业"],
            "核对状态": "已核对" if verified else "草稿",
            "是否参与提醒": "是" if verified else "否",
        }
        for item in candidate["事项"]
    ]


def _without_spaces(text: str) -> str:
    return "".join(char for char in text if char not in {" ", "\u3000"})


def _manual_item(title: str = "手写事项", matter: str = "报名截止") -> dict:
    return {
        "事项": matter,
        "标题": title,
        "类型": "竞赛",
        "截止日期": "未找到",
        "日期原句": "海报上看不清",
        "来源链接": "未找到",
        "报名条件": "未找到",
        "主办方": "未找到",
        "适合专业": "未找到",
        "核对状态": "草稿",
        "是否参与提醒": "否",
    }


def _only_user_id() -> int:
    connection = connect()
    try:
        return int(connection.execute("SELECT id FROM users").fetchone()[0])
    finally:
        connection.close()


def _tiny_png() -> bytes:
    return _png_size(16, 16)


def _png_size(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _animated_gif() -> bytes:
    frames = [Image.new("RGB", (16, 16), color) for color in ("red", "blue")]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
    return buffer.getvalue()


def simplified_face_index(names: list[str], *, collection: bool) -> int:
    if not collection:
        return 0
    for index, name in enumerate(names):
        lowered = name.lower()
        if any(marker in lowered for marker in ("cjk sc", "sans sc", "serif sc", "simplified", "简体")):
            return index
    raise LookupError("字体集合里没有简体面")


def _synthetic_poster() -> bytes:
    font = _cjk_font(64)
    image = Image.new("L", (1700, 1200), 255)
    draw = ImageDraw.Draw(image)
    top = 70
    for line in POSTER_LINES:
        draw.text((70, top), line, fill=0, font=font)
        top += 140
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _cjk_font(size: int) -> ImageFont.FreeTypeFont:
    override = os.environ.get("LEARNING_ASSISTANT_CJK_FONT", "").strip()
    if override:
        path = Path(override)
        if not path.is_file():
            raise AssertionError(
                f"LEARNING_ASSISTANT_CJK_FONT 指向的字体不存在：{path}。识图测试失败，没有跳过，也没有改用预设文字。"
            )
        return _open_cjk_face(path, size)
    for path in _font_files():
        try:
            return _open_cjk_face(path, size)
        except (AssertionError, OSError):
            continue
    raise AssertionError(
        "找不到真实中文字体。可设置 LEARNING_ASSISTANT_CJK_FONT 指向 Noto Sans SC、Noto Serif SC 或系统简体字体。"
        "识图测试失败，没有跳过，也没有改用预设文字。"
    )


def _font_files():
    names = (
        "NotoSansSC-VF.ttf",
        "NotoSerifSC-VF.ttf",
        "NotoSansCJKsc-Regular.otf",
        "NotoSansCJK-Regular.ttc",
        "NotoSansCJK-Regular.otf",
        "msyh.ttc",
        "msyh.ttf",
        "simhei.ttf",
        "simsun.ttc",
    )
    roots = [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".local/share/fonts",
    ]
    windows = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windows:
        roots.append(Path(windows) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for name in names:
            for path in root.rglob(name):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield resolved


def _open_cjk_face(path: Path, size: int) -> ImageFont.FreeTypeFont:
    collection = path.suffix.lower() in {".ttc", ".otc"}
    if not collection:
        font = ImageFont.truetype(str(path), size, index=0)
    else:
        names: list[str] = []
        faces: list[ImageFont.FreeTypeFont] = []
        for index in range(16):
            try:
                face = ImageFont.truetype(str(path), size, index=index)
            except OSError:
                break
            faces.append(face)
            names.append(" ".join(face.getname()))
        if not faces:
            raise AssertionError(f"无法读取字体集合：{path}")
        font = faces[simplified_face_index(names, collection=True)]
    mask = font.getmask("国")
    if mask.size[0] < 8:
        raise AssertionError(f"{path} 画不出「国」，不能当作中文字体。")
    return font


if __name__ == "__main__":
    unittest.main()
