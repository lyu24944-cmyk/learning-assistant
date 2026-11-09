"""识图异步路径：202、后台完成、确认守卫、去重、重试、重启恢复、队列满、取消。

这一组测的是「任务状态」和「队列」，不测识别准确率——那是 test_images.py 的事。
识别本身被替换成固定文本，所以这里跑得快，也不依赖本机装没装中文识别包。
"""

from __future__ import annotations

import datetime as dt
import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from src import image_drafts, ocr_queue
from src.api import app
from src.bitmap import write_private
from src.db import connect
from src.ocr import OcrFailed
from tests.csrf_client import CsrfClient

TEXT = "全国大学生数学建模竞赛\n报名截止：2026年11月25日"
STATUS = {
    "available": True,
    "provider": "windows",
    "engine": "Windows.Media.Ocr",
    "languages": ["zh-Hans-CN"],
    "english_independent": True,
    "detail": "合成状态，不是真实识别。",
}

ITEM = {
    "事项": "报名截止",
    "标题": "全国大学生数学建模竞赛",
    "类型": "竞赛",
    "截止日期": "2026-11-25",
    "日期原句": "报名截止：2026年11月25日",
    "来源链接": "未找到",
    "报名条件": "未找到",
    "主办方": "未找到",
    "适合专业": "未找到",
    "核对状态": "草稿",
    "是否参与提醒": "否",
}

_INSERT = """
INSERT INTO image_drafts (
    user_id, status, storage_key, original_rel, preview_rel, mime, byte_size,
    width, height, sha256, ocr_text, ocr_engine, ocr_error, candidate_json, created_at,
    ocr_state, ocr_queued_at, ocr_started_at, ocr_finished_at, ocr_attempts, ocr_provider
) VALUES (?, 'pending', ?, ?, ?, 'image/png', 100, 32, 32, ?, '', '', '', '{}', ?, ?, ?, ?, '', 1, '')
"""


def _png(seed: int = 0) -> bytes:
    """每次给不同的像素，sha256 才不同——否则会被「在途复用」直接命中。"""
    colour = (seed % 256, (seed * 7) % 256, (seed * 13) % 256)
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), colour).save(buffer, format="PNG")
    return buffer.getvalue()


class AsyncDraftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {
            name: os.environ.get(name)
            for name in (
                "LEARNING_ASSISTANT_DB",
                "LEARNING_ASSISTANT_PRIVATE",
                "LEARNING_ASSISTANT_OCR_SYNC",
                "LEARNING_ASSISTANT_OCR_QUEUE_SIZE",
                "LEARNING_ASSISTANT_OCR_WORKERS",
                "LEARNING_ASSISTANT_OCR_UNFINISHED_LIMIT",
                "LEARNING_ASSISTANT_OCR_REAP_INTERVAL",
            )
        }
        os.environ["LEARNING_ASSISTANT_DB"] = str(Path(self.tmp.name) / "test.sqlite")
        os.environ["LEARNING_ASSISTANT_PRIVATE"] = str(Path(self.tmp.name) / "private")
        os.environ.pop("LEARNING_ASSISTANT_OCR_SYNC", None)
        os.environ.pop("LEARNING_ASSISTANT_OCR_QUEUE_SIZE", None)
        os.environ.pop("LEARNING_ASSISTANT_OCR_WORKERS", None)
        os.environ.pop("LEARNING_ASSISTANT_OCR_UNFINISHED_LIMIT", None)
        # 看门狗在测试里手动触发；后台线程别插进来，否则 reap 计数不确定。
        os.environ["LEARNING_ASSISTANT_OCR_REAP_INTERVAL"] = "3600"
        ocr_queue.reset()
        ocr_queue.start(image_drafts.run_ocr, image_drafts.reap_stale)
        self._seq = 0
        self.client = CsrfClient(TestClient(app))
        self.client.post(
            "/api/register", json={"email": "async@example.com", "password": "secret-pass-1"}
        )

    def tearDown(self) -> None:
        ocr_queue.reset()
        close = getattr(self.client, "close", None)
        if close is not None:
            close()
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def _upload(self, payload: bytes | None = None):
        if payload is None:
            payload = _png(self._seq)
            self._seq += 1
        return self.client.post(
            "/api/image-drafts",
            files={"file": ("poster.png", payload, "image/png")},
        )

    def _wait(self, draft_id: int, state: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.client.get(f"/api/image-drafts/{draft_id}").json()
            if last["ocr_state"] == state:
                return last
            time.sleep(0.02)
        self.fail(f"等不到 {state}，最后是 {last}")

    def _draft_rows(self) -> int:
        connection = connect()
        try:
            return int(connection.execute("SELECT COUNT(*) FROM image_drafts").fetchone()[0])
        finally:
            connection.close()

    def _insert_draft(self, state: str, started_at: str = "", sha256: str = "f" * 64) -> int:
        storage_key = f"key{state}{sha256[:6]}"
        original_rel, preview_rel = write_private(1, storage_key, ".png", _png(), b"synthetic-preview")
        connection = connect()
        try:
            cursor = connection.execute(
                _INSERT,
                (
                    1,
                    storage_key,
                    original_rel,
                    preview_rel,
                    sha256,
                    dt.datetime.now(dt.UTC).isoformat(),
                    state,
                    dt.datetime.now(dt.UTC).isoformat(),
                    started_at,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)
        finally:
            connection.close()

    def test_upload_returns_202_and_finishes_in_background(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_transcribe(_image, _status=None) -> str:
            started.set()
            release.wait(5.0)
            return TEXT

        with (
            patch.object(image_drafts, "engine_status_cached", return_value=STATUS),
            patch.object(image_drafts, "transcribe", side_effect=slow_transcribe),
        ):
            created = self._upload()
            self.assertEqual(created.status_code, 202, created.text)
            body = created.json()
            self.assertIn(body["ocr_state"], {"queued", "running"})
            self.assertEqual(body["candidate"]["事项"], [])
            self.assertTrue(started.wait(3.0))

            running = self.client.get(f"/api/image-drafts/{body['id']}").json()
            self.assertEqual(running["ocr_state"], "running")
            self.assertGreater(running["poll_after_ms"], 0)

            release.set()
            done = self._wait(body["id"], "done")

        self.assertTrue(done["ocr_text"].strip())
        self.assertEqual(done["mode"], "识别草稿")
        self.assertEqual(done["poll_after_ms"], 0)
        self.assertEqual(done["provider"], "windows")
        self.assertTrue(done["candidate"]["事项"])

    def test_confirm_is_rejected_while_running_and_writes_nothing(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_transcribe(_image, _status=None) -> str:
            started.set()
            release.wait(5.0)
            return TEXT

        with (
            patch.object(image_drafts, "engine_status_cached", return_value=STATUS),
            patch.object(image_drafts, "transcribe", side_effect=slow_transcribe),
        ):
            created = self._upload()
            self.assertEqual(created.status_code, 202, created.text)
            draft_id = created.json()["id"]
            self.assertTrue(started.wait(3.0))

            denied = self.client.post(
                f"/api/image-drafts/{draft_id}/confirm", json={"items": [ITEM]}
            )
            self.assertEqual(denied.status_code, 409, denied.text)
            self.assertEqual(denied.json()["code"], "ocr_not_ready")

            self.assertEqual(self.client.get("/api/mine").json()["records"], [])
            self.assertEqual(self.client.get("/api/materials").json()["materials"], [])
            self.assertEqual(self.client.get("/api/weekly-review").json()["activities"], [])

            release.set()
            self._wait(draft_id, "done")

        accepted = self.client.post(f"/api/image-drafts/{draft_id}/confirm", json={"items": [ITEM]})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(len(self.client.get("/api/mine").json()["records"]), 1)

    def test_same_image_is_reused_without_running_ocr_again(self) -> None:
        payload = _png()
        with (
            patch.object(image_drafts, "engine_status_cached", return_value=STATUS),
            patch.object(image_drafts, "transcribe", return_value=TEXT) as transcribe,
        ):
            first = self._upload(payload)
            self.assertEqual(first.status_code, 202, first.text)
            self.assertFalse(first.json()["reused"])
            self._wait(first.json()["id"], "done")

            second = self._upload(payload)
            self.assertEqual(second.status_code, 202, second.text)
            body = second.json()
            self.assertTrue(body["reused"])
            self.assertEqual(body["id"], first.json()["id"])
            self.assertEqual(body["ocr_state"], "done")
            self.assertEqual(body["poll_after_ms"], 0)

        self.assertEqual(transcribe.call_count, 1)

    def test_retry_requeues_a_failed_draft(self) -> None:
        with (
            patch.object(image_drafts, "engine_status_cached", return_value=STATUS),
            patch.object(image_drafts, "transcribe", side_effect=OcrFailed("识别超时，没有得到竞赛信息。")),
        ):
            created = self._upload()
            self.assertEqual(created.status_code, 202, created.text)
            draft_id = created.json()["id"]
            failed = self._wait(draft_id, "failed")

        self.assertEqual(failed["ocr_attempts"], 1)
        self.assertIn("超时", failed["ocr_error"])
        self.assertEqual(failed["mode"], "人工补录")

        with (
            patch.object(image_drafts, "engine_status_cached", return_value=STATUS),
            patch.object(image_drafts, "transcribe", return_value=TEXT),
        ):
            retried = self.client.post(f"/api/image-drafts/{draft_id}/retry")
            self.assertEqual(retried.status_code, 200, retried.text)
            self.assertEqual(retried.json()["ocr_state"], "queued")
            done = self._wait(draft_id, "done")

        self.assertEqual(done["ocr_attempts"], 2)
        self.assertEqual(done["ocr_error"], "")

    def test_recover_marks_every_running_row_failed_and_requeues_queued(self) -> None:
        long_ago = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)).isoformat()
        stale = self._insert_draft("running", started_at=long_ago, sha256="a" * 64)
        # 刚崩掉时那条 running 才几秒。启动恢复不按时间筛，它也必须被收掉。
        fresh = self._insert_draft(
            "running", started_at=dt.datetime.now(dt.UTC).isoformat(), sha256="e" * 64
        )
        waiting = self._insert_draft("queued", sha256="b" * 64)

        with (
            patch.object(image_drafts, "engine_status_cached", return_value=STATUS),
            patch.object(image_drafts, "transcribe", return_value=TEXT),
        ):
            result = image_drafts.recover_interrupted()
            self.assertEqual(result["failed"], 2)
            self.assertEqual(result["requeued"], 1)
            self.assertEqual(result["dropped"], 0)
            self._wait(waiting, "done")

        connection = connect()
        try:
            rows = connection.execute(
                "SELECT id, ocr_state, ocr_error FROM image_drafts WHERE id IN (?, ?)",
                (stale, fresh),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual({row["ocr_state"] for row in rows}, {"failed"})
        for row in rows:
            self.assertIn("重启", row["ocr_error"])

    def test_full_queue_returns_503_without_leaving_an_orphan_row(self) -> None:
        os.environ["LEARNING_ASSISTANT_OCR_QUEUE_SIZE"] = "1"
        os.environ["LEARNING_ASSISTANT_OCR_WORKERS"] = "1"
        # 放开单人在途上限，否则第 3 张先被那道闸拦成 429，走不到队列满的分支。
        os.environ["LEARNING_ASSISTANT_OCR_UNFINISHED_LIMIT"] = "10"
        ocr_queue.reset()
        ocr_queue.start(image_drafts.run_ocr, image_drafts.reap_stale)
        started = threading.Event()
        release = threading.Event()

        def slow_transcribe(_image, _status=None) -> str:
            started.set()
            release.wait(5.0)
            return TEXT

        with (
            patch.object(image_drafts, "engine_status_cached", return_value=STATUS),
            patch.object(image_drafts, "transcribe", side_effect=slow_transcribe),
        ):
            first = self._upload()
            self.assertEqual(first.status_code, 202, first.text)
            self.assertTrue(started.wait(3.0))
            second = self._upload()
            self.assertEqual(second.status_code, 202, second.text)
            third = self._upload()
            self.assertEqual(third.status_code, 503, third.text)
            self.assertEqual(third.json()["code"], "ocr_queue_full")
            release.set()

        self.assertEqual(self._draft_rows(), 2)

    def test_third_unfinished_upload_is_refused(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_transcribe(_image, _status=None) -> str:
            started.set()
            release.wait(5.0)
            return TEXT

        with (
            patch.object(image_drafts, "engine_status_cached", return_value=STATUS),
            patch.object(image_drafts, "transcribe", side_effect=slow_transcribe),
        ):
            first = self._upload()
            self.assertEqual(first.status_code, 202, first.text)
            self.assertTrue(started.wait(3.0))
            second = self._upload()
            self.assertEqual(second.status_code, 202, second.text)
            third = self._upload()
            self.assertEqual(third.status_code, 429, third.text)
            self.assertEqual(third.json()["code"], "draft_busy")
            release.set()

        self.assertEqual(self._draft_rows(), 2)

    def test_upload_rate_limit_refuses_after_the_quota(self) -> None:
        from src import ratelimit

        with patch.object(ratelimit, "UPLOAD_LIMIT", 2):
            with (
                patch.object(image_drafts, "engine_status_cached", return_value=STATUS),
                patch.object(image_drafts, "transcribe", return_value=TEXT),
            ):
                for _ in range(2):
                    created = self._upload()
                    self.assertEqual(created.status_code, 202, created.text)
                    self._wait(created.json()["id"], "done")
                denied = self._upload()
        self.assertEqual(denied.status_code, 429, denied.text)
        self.assertEqual(denied.json()["code"], "upload_rate_limited")

    def test_reaper_marks_a_stuck_running_row_failed(self) -> None:
        long_ago = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)).isoformat()
        stuck = self._insert_draft("running", started_at=long_ago, sha256="d" * 64)
        self.assertEqual(ocr_queue.reap_once(), 1)
        connection = connect()
        try:
            row = connection.execute(
                "SELECT ocr_state, ocr_error FROM image_drafts WHERE id = ?", (stuck,)
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row["ocr_state"], "failed")
        self.assertIn("超时", row["ocr_error"])

    def test_queue_position_counts_the_tasks_ahead(self) -> None:
        first = self._insert_draft("queued", sha256="1" * 64)
        second = self._insert_draft("queued", sha256="2" * 64)
        third = self._insert_draft("queued", sha256="3" * 64)
        self.assertEqual(image_drafts.queue_position(first), 0)
        self.assertEqual(image_drafts.queue_position(second), 1)
        self.assertEqual(image_drafts.queue_position(third), 2)

        running = self._insert_draft("running", sha256="4" * 64)
        self.assertEqual(image_drafts.queue_position(running), 0)

    def test_in_flight_image_is_reused_instead_of_queued_twice(self) -> None:
        payload = _png()
        started = threading.Event()
        release = threading.Event()

        def slow_transcribe(_image, _status=None) -> str:
            started.set()
            release.wait(5.0)
            return TEXT

        with (
            patch.object(image_drafts, "engine_status_cached", return_value=STATUS),
            patch.object(image_drafts, "transcribe", side_effect=slow_transcribe) as transcribe,
        ):
            first = self._upload(payload)
            self.assertEqual(first.status_code, 202, first.text)
            self.assertTrue(started.wait(3.0))
            second = self._upload(payload)
            self.assertEqual(second.status_code, 202, second.text)
            self.assertTrue(second.json()["reused"])
            self.assertEqual(second.json()["id"], first.json()["id"])
            self.assertEqual(second.json()["ocr_state"], "running")
            release.set()
            self._wait(first.json()["id"], "done")

        self.assertEqual(transcribe.call_count, 1)
        self.assertEqual(self._draft_rows(), 1)

    def test_cancel_while_running_discards_the_result(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_transcribe(_image, _status=None) -> str:
            started.set()
            release.wait(5.0)
            return TEXT

        with (
            patch.object(image_drafts, "engine_status_cached", return_value=STATUS),
            patch.object(image_drafts, "transcribe", side_effect=slow_transcribe),
        ):
            created = self._upload()
            self.assertEqual(created.status_code, 202, created.text)
            draft_id = created.json()["id"]
            self.assertTrue(started.wait(3.0))

            cancelled = self.client.post(f"/api/image-drafts/{draft_id}/cancel")
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            release.set()
            settled = self._wait(draft_id, "done")

        self.assertEqual(settled["status"], "cancelled")
        self.assertEqual(settled["ocr_text"], "")
        self.assertEqual(self.client.get("/api/mine").json()["records"], [])

    def test_retry_is_refused_when_attempts_run_out(self) -> None:
        draft_id = self._insert_draft("failed", sha256="c" * 64)
        connection = connect()
        try:
            connection.execute("UPDATE image_drafts SET ocr_attempts = 3 WHERE id = ?", (draft_id,))
            connection.commit()
        finally:
            connection.close()
        denied = self.client.post(f"/api/image-drafts/{draft_id}/retry")
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual(denied.json()["code"], "ocr_retry_exhausted")


if __name__ == "__main__":
    unittest.main()
