"""频率限制的单元测试。不启动服务，不写数据库。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src import ratelimit
from src.ratelimit import SlidingWindow


class SlidingWindowTest(unittest.TestCase):
    def test_limit_reset_and_independent_keys(self) -> None:
        window = SlidingWindow(limit=2, window_seconds=60, clock=lambda: 0.0)
        self.assertTrue(window.allow("k"))
        window.hit("k")
        window.hit("k")
        self.assertFalse(window.allow("k"))
        self.assertTrue(window.allow("other"))
        window.reset("k")
        self.assertTrue(window.allow("k"))

    def test_entries_expire_after_the_window(self) -> None:
        now = [0.0]
        window = SlidingWindow(limit=1, window_seconds=10, clock=lambda: now[0])
        window.hit("k")
        now[0] = 9.0
        self.assertFalse(window.allow("k"))
        now[0] = 10.0
        self.assertTrue(window.allow("k"))

    def test_sweep_keeps_the_key_map_bounded(self) -> None:
        now = [0.0]
        window = SlidingWindow(limit=5, window_seconds=10, clock=lambda: now[0])
        with patch.object(ratelimit, "_SWEEP_THRESHOLD", 2):
            window.hit("a")
            window.hit("b")
            window.hit("c")
        self.assertEqual(len(window._hits), 3)
        now[0] = 100.0
        with patch.object(ratelimit, "_SWEEP_THRESHOLD", 2):
            window.hit("d")
        self.assertEqual(list(window._hits), ["d"])


class ModuleLimitTest(unittest.TestCase):
    def setUp(self) -> None:
        ratelimit.reset()

    def tearDown(self) -> None:
        ratelimit.reset()

    def test_login_failures_block_only_that_account_and_ip(self) -> None:
        key = ("127.0.0.1", "a@example.com")
        self.assertFalse(ratelimit.login_blocked(key))
        for _ in range(ratelimit.LOGIN_FAILURE_LIMIT):
            ratelimit.note_login_failure(key)
        self.assertTrue(ratelimit.login_blocked(key))
        self.assertFalse(ratelimit.login_blocked(("127.0.0.1", "b@example.com")))
        self.assertFalse(ratelimit.login_blocked(("10.0.0.2", "a@example.com")))
        ratelimit.clear_login_failures(key)
        self.assertFalse(ratelimit.login_blocked(key))

    def test_register_limit_and_off_switch(self) -> None:
        client = "127.0.0.1"
        for _ in range(ratelimit.REGISTER_LIMIT):
            ratelimit.note_registration(client)
        self.assertTrue(ratelimit.register_blocked(client))
        self.assertFalse(ratelimit.register_blocked("10.0.0.2"))

        ratelimit.reset()
        with patch.dict(os.environ, {"LEARNING_ASSISTANT_RATE_LIMIT": "0"}):
            for _ in range(ratelimit.REGISTER_LIMIT + 5):
                ratelimit.note_registration(client)
            self.assertFalse(ratelimit.register_blocked(client))


if __name__ == "__main__":
    unittest.main()
