"""登录失败和注册的频率限制。

计数只在当前进程内，重启后清零；多进程部署时每个进程各自计数。
阈值可以用环境变量调整，本地演示或压测可以用 LEARNING_ASSISTANT_RATE_LIMIT=0 关掉。
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable, Hashable

LOGIN_FAILURE_WINDOW = 15 * 60
REGISTER_WINDOW = 60 * 60
_SWEEP_THRESHOLD = 1000


def _enabled() -> bool:
    raw = os.environ.get("LEARNING_ASSISTANT_RATE_LIMIT", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


LOGIN_FAILURE_LIMIT = _positive_int("LEARNING_ASSISTANT_LOGIN_FAILURE_LIMIT", 10)
REGISTER_LIMIT = _positive_int("LEARNING_ASSISTANT_REGISTER_LIMIT", 30)


class SlidingWindow:
    """按键记录发生时间，窗口外的记录自动丢弃。

    只保留最近 limit 次，超过就不再放行。检查与记录分开，
    这样登录成功时可以先判断、失败时才计数。
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[Hashable, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: Hashable) -> bool:
        with self._lock:
            self._prune(key)
            return len(self._hits.get(key, ())) < self.limit

    def hit(self, key: Hashable) -> None:
        with self._lock:
            self._prune(key)
            self._hits.setdefault(key, deque()).append(self._clock())
            if len(self._hits) > _SWEEP_THRESHOLD:
                self._sweep()

    def reset(self, key: Hashable) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()

    def _prune(self, key: Hashable) -> None:
        bucket = self._hits.get(key)
        if not bucket:
            return
        cutoff = self._clock() - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            self._hits.pop(key, None)

    def _sweep(self) -> None:
        """键只增不减会慢慢吃内存。超过阈值时清掉整桶都过期的键。"""
        cutoff = self._clock() - self.window
        stale = [key for key, bucket in self._hits.items() if not bucket or bucket[-1] <= cutoff]
        for key in stale:
            self._hits.pop(key, None)


_failures = SlidingWindow(LOGIN_FAILURE_LIMIT, LOGIN_FAILURE_WINDOW)
_registrations = SlidingWindow(REGISTER_LIMIT, REGISTER_WINDOW)


def login_blocked(key: Hashable) -> bool:
    if not _enabled():
        return False
    return not _failures.allow(key)


def note_login_failure(key: Hashable) -> None:
    if _enabled():
        _failures.hit(key)


def clear_login_failures(key: Hashable) -> None:
    _failures.reset(key)


def register_blocked(client: str) -> bool:
    if not _enabled():
        return False
    return not _registrations.allow(client)


def note_registration(client: str) -> None:
    if _enabled():
        _registrations.hit(client)


def reset() -> None:
    """测试用：清空全部计数。"""
    _failures.clear()
    _registrations.clear()
