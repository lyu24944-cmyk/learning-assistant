"""登录、注册、重置和改密码的频率限制。

计数在 rate_limits 表里：重启还在，多进程共享。固定窗口，允许边界突刺。
数据库出错时放行并记一条日志；不能因为限流表把主人锁在门外。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Hashable

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.db import session

LOGIN_FAILURE_WINDOW = 15 * 60
REGISTER_WINDOW = 60 * 60
RESET_WINDOW = 60 * 60
CHANGE_WINDOW = 15 * 60
_CLEANUP_SECONDS = 60
_RETENTION_SECONDS = 24 * 60 * 60

_last_cleanup = 0.0
_last_degraded = 0.0


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
LOGIN_IP_LIMIT = _positive_int("LEARNING_ASSISTANT_LOGIN_IP_LIMIT", 60)
REGISTER_LIMIT = _positive_int("LEARNING_ASSISTANT_REGISTER_LIMIT", 30)
RESET_LIMIT = _positive_int("LEARNING_ASSISTANT_RESET_LIMIT", 10)
CHANGE_PASSWORD_LIMIT = _positive_int("LEARNING_ASSISTANT_CHANGE_PASSWORD_LIMIT", 5)


def _key_text(key: Hashable) -> str:
    if isinstance(key, tuple):
        return ":".join(str(part) for part in key)
    return str(key)


def _check(key: Hashable, limit: int, window: float, now: float | None = None) -> bool:
    if not _enabled():
        return True
    stamp = time.time() if now is None else now
    try:
        with session() as db:
            row = db.execute(
                text("SELECT window_start, count FROM rate_limits WHERE bucket_key = :key"),
                {"key": _key_text(key)},
            ).first()
    except SQLAlchemyError:
        _degraded()
        return True
    if row is None:
        return True
    start, count = int(row[0]), int(row[1])
    if stamp - start >= window:
        return True
    return count < limit


def _hit(key: Hashable, window: float, now: float | None = None) -> None:
    global _last_cleanup
    if not _enabled():
        return
    stamp = int(time.time() if now is None else now)
    try:
        with session() as db:
            row = db.execute(
                text("SELECT window_start FROM rate_limits WHERE bucket_key = :key"),
                {"key": _key_text(key)},
            ).first()
            start = stamp
            if row is not None and stamp - int(row[0]) < window:
                # 窗口还没过期，沿用原来的起点；不能拿「当前秒」当起点，
                # 否则每秒都会重置计数，30 次注册限制永远打不到。
                start = int(row[0])
            db.execute(
                text(
                    """
                    INSERT INTO rate_limits (bucket_key, window_start, count)
                    VALUES (:key, :start, 1)
                    ON CONFLICT(bucket_key) DO UPDATE SET
                      count = CASE WHEN rate_limits.window_start = excluded.window_start
                                   THEN rate_limits.count + 1 ELSE 1 END,
                      window_start = excluded.window_start
                    """
                ),
                {"key": _key_text(key), "start": start},
            )
            if stamp - _last_cleanup >= _CLEANUP_SECONDS:
                db.execute(
                    text("DELETE FROM rate_limits WHERE window_start < :cutoff"),
                    {"cutoff": stamp - _RETENTION_SECONDS},
                )
                _last_cleanup = stamp
    except SQLAlchemyError:
        _degraded()


def _delete(key: Hashable) -> None:
    try:
        with session() as db:
            db.execute(
                text("DELETE FROM rate_limits WHERE bucket_key = :key"),
                {"key": _key_text(key)},
            )
    except SQLAlchemyError:
        _degraded()


def _degraded() -> None:
    global _last_degraded
    stamp = time.time()
    if stamp - _last_degraded < 60:
        return
    _last_degraded = stamp
    logging.getLogger("app.auth").warning(
        "rate_limit_degraded",
        extra={"event": "rate_limit_degraded"},
    )


def login_blocked(key: Hashable) -> bool:
    return not _check(key, LOGIN_FAILURE_LIMIT, LOGIN_FAILURE_WINDOW)


def note_login_failure(key: Hashable) -> None:
    _hit(key, LOGIN_FAILURE_WINDOW)


def clear_login_failures(key: Hashable) -> None:
    _delete(key)


def login_ip_blocked(client: str) -> bool:
    return not _check(f"login-ip:{client}", LOGIN_IP_LIMIT, LOGIN_FAILURE_WINDOW)


def note_login_ip(client: str) -> None:
    _hit(f"login-ip:{client}", LOGIN_FAILURE_WINDOW)


def register_blocked(client: str) -> bool:
    return not _check(f"register:{client}", REGISTER_LIMIT, REGISTER_WINDOW)


def note_registration(client: str) -> None:
    _hit(f"register:{client}", REGISTER_WINDOW)


def reset_blocked(client: str) -> bool:
    return not _check(f"reset:{client}", RESET_LIMIT, RESET_WINDOW)


def note_reset(client: str) -> None:
    _hit(f"reset:{client}", RESET_WINDOW)


def change_password_blocked(user_id: int) -> bool:
    return not _check(f"change:{user_id}", CHANGE_PASSWORD_LIMIT, CHANGE_WINDOW)


def note_change_password_failure(user_id: int) -> None:
    _hit(f"change:{user_id}", CHANGE_WINDOW)


def cleanup(now: float | None = None) -> int:
    stamp = int(time.time() if now is None else now)
    with session() as db:
        result = db.execute(
            text("DELETE FROM rate_limits WHERE window_start < :cutoff"),
            {"cutoff": stamp - _RETENTION_SECONDS},
        )
        return int(result.rowcount or 0)


def reset() -> None:
    """测试用：清空全部计数。"""
    with session() as db:
        db.execute(text("DELETE FROM rate_limits"))
