"""有界队列：派活、满、异常不死、关闭、恢复、统计。"""

from __future__ import annotations

import os
import threading
import unittest

from src import ocr_queue


def _clean_env() -> None:
    for name in ("LEARNING_ASSISTANT_OCR_QUEUE_SIZE", "LEARNING_ASSISTANT_OCR_WORKERS"):
        os.environ.pop(name, None)


class OcrQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        ocr_queue.reset()
        _clean_env()

    def tearDown(self) -> None:
        ocr_queue.reset()
        _clean_env()

    def test_submitted_job_runs(self) -> None:
        seen: list[int] = []
        done = threading.Event()

        def job(draft_id: int) -> None:
            seen.append(draft_id)
            done.set()

        ocr_queue.start(job)
        self.assertTrue(ocr_queue.submit(7))
        self.assertTrue(done.wait(2.0))
        self.assertEqual(seen, [7])

    def test_full_queue_returns_false_instead_of_blocking(self) -> None:
        os.environ["LEARNING_ASSISTANT_OCR_QUEUE_SIZE"] = "1"
        os.environ["LEARNING_ASSISTANT_OCR_WORKERS"] = "1"
        started = threading.Event()
        release = threading.Event()

        def job(draft_id: int) -> None:
            started.set()
            release.wait(2.0)

        ocr_queue.start(job)
        self.assertTrue(ocr_queue.submit(1))
        self.assertTrue(started.wait(2.0))
        self.assertTrue(ocr_queue.submit(2))
        self.assertFalse(ocr_queue.submit(3))
        release.set()

    def test_a_crashing_job_does_not_kill_the_worker(self) -> None:
        os.environ["LEARNING_ASSISTANT_OCR_WORKERS"] = "1"
        calls: list[int] = []
        second = threading.Event()

        def job(draft_id: int) -> None:
            calls.append(draft_id)
            if draft_id == 1:
                raise RuntimeError("synthetic crash")
            second.set()

        ocr_queue.start(job)
        self.assertTrue(ocr_queue.submit(1))
        self.assertTrue(ocr_queue.submit(2))
        self.assertTrue(second.wait(2.0))
        self.assertEqual(calls, [1, 2])

    def test_submit_before_start_is_rejected(self) -> None:
        self.assertFalse(ocr_queue.submit(1))

    def test_shutdown_rejects_new_work(self) -> None:
        ocr_queue.start(lambda _draft_id: None)
        ocr_queue.shutdown()
        self.assertFalse(ocr_queue.submit(1))

    def test_start_is_idempotent(self) -> None:
        runs: list[int] = []
        done = threading.Event()

        def job(draft_id: int) -> None:
            runs.append(draft_id)
            done.set()

        ocr_queue.start(job)
        ocr_queue.start(job)
        self.assertTrue(ocr_queue.submit(5))
        self.assertTrue(done.wait(2.0))
        self.assertEqual(runs, [5])
        self.assertEqual(ocr_queue.stats()["max_workers"], 2)

    def test_stats_reports_limits_and_state(self) -> None:
        os.environ["LEARNING_ASSISTANT_OCR_QUEUE_SIZE"] = "3"
        os.environ["LEARNING_ASSISTANT_OCR_WORKERS"] = "1"
        ocr_queue.start(lambda _draft_id: None)
        stats = ocr_queue.stats()
        self.assertEqual(stats["depth"], 0)
        self.assertEqual(stats["running"], 0)
        self.assertEqual(stats["max_queue"], 3)
        self.assertEqual(stats["max_workers"], 1)
        self.assertTrue(stats["started"])

    def test_requeue_reports_what_it_could_not_take(self) -> None:
        result = ocr_queue.requeue([1, 2, 3])
        self.assertEqual(result, {"requeued": 0, "rejected": 3})

    def test_requeue_accepts_when_started(self) -> None:
        received: list[int] = []
        done = threading.Event()

        def job(draft_id: int) -> None:
            received.append(draft_id)
            if len(received) == 2:
                done.set()

        ocr_queue.start(job)
        result = ocr_queue.requeue([11, 12])
        self.assertEqual(result, {"requeued": 2, "rejected": 0})
        self.assertTrue(done.wait(2.0))
        self.assertEqual(sorted(received), [11, 12])

    def test_reset_clears_everything(self) -> None:
        ocr_queue.start(lambda _draft_id: None)
        ocr_queue.reset()
        self.assertFalse(ocr_queue.submit(1))
        self.assertFalse(ocr_queue.stats()["started"])


if __name__ == "__main__":
    unittest.main()
