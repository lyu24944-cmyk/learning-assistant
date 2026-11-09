"""频率限制的测试。计数在临时库里，不碰真实数据。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src import ratelimit
from src.db import reset_for_tests, session


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LEARNING_ASSISTANT_DB", str(tmp_path / "test.sqlite"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_for_tests()
    ratelimit.reset()
    yield
    ratelimit.reset()
    reset_for_tests()


def test_login_failures_block_only_that_key() -> None:
    key = ("127.0.0.1", "a@example.com")
    assert ratelimit.login_blocked(key) is False
    for _ in range(ratelimit.LOGIN_FAILURE_LIMIT):
        ratelimit.note_login_failure(key)
    assert ratelimit.login_blocked(key) is True
    assert ratelimit.login_blocked(("127.0.0.1", "b@example.com")) is False
    assert ratelimit.login_blocked(("10.0.0.2", "a@example.com")) is False
    ratelimit.clear_login_failures(key)
    assert ratelimit.login_blocked(key) is False


def test_counts_survive_a_new_engine() -> None:
    key = ("127.0.0.1", "a@example.com")
    for _ in range(ratelimit.LOGIN_FAILURE_LIMIT):
        ratelimit.note_login_failure(key)
    reset_for_tests()  # 模拟进程重启：清掉 engine 缓存，库还在
    assert ratelimit.login_blocked(key) is True


def test_register_limit_and_off_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    client = "127.0.0.1"
    for _ in range(ratelimit.REGISTER_LIMIT):
        ratelimit.note_registration(client)
    assert ratelimit.register_blocked(client) is True
    assert ratelimit.register_blocked("10.0.0.2") is False

    ratelimit.reset()
    monkeypatch.setenv("LEARNING_ASSISTANT_RATE_LIMIT", "0")
    for _ in range(ratelimit.REGISTER_LIMIT + 5):
        ratelimit.note_registration(client)
    assert ratelimit.register_blocked(client) is False


def test_window_rolls_over() -> None:
    key = "login:127.0.0.1:a@example.com"
    with session() as db:
        db.execute(
            text(
                "INSERT INTO rate_limits (bucket_key, window_start, count) VALUES (:key, :start, 99)"
            ),
            {"key": key, "start": int(time.time()) - ratelimit.LOGIN_FAILURE_WINDOW - 1},
        )
    assert ratelimit.login_blocked(key) is False


def test_cleanup_removes_old_rows() -> None:
    now = int(time.time())
    with session() as db:
        db.execute(
            text("INSERT INTO rate_limits (bucket_key, window_start, count) VALUES (:key, :start, 1)"),
            {"key": "old", "start": now - 25 * 60 * 60},
        )
        db.execute(
            text("INSERT INTO rate_limits (bucket_key, window_start, count) VALUES (:key, :start, 1)"),
            {"key": "fresh", "start": now},
        )
    assert ratelimit.cleanup(now=now) == 1
    with session() as db:
        keys = {row[0] for row in db.execute(text("SELECT bucket_key FROM rate_limits"))}
    assert keys == {"fresh"}


def test_reset_and_change_buckets() -> None:
    for _ in range(ratelimit.RESET_LIMIT):
        ratelimit.note_reset("127.0.0.1")
    assert ratelimit.reset_blocked("127.0.0.1") is True

    for _ in range(ratelimit.CHANGE_PASSWORD_LIMIT):
        ratelimit.note_change_password_failure(7)
    assert ratelimit.change_password_blocked(7) is True
    assert ratelimit.change_password_blocked(8) is False


def test_database_error_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def __enter__(self):
            raise SQLAlchemyError("boom")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(ratelimit, "session", lambda: Broken())
    assert ratelimit.login_blocked(("127.0.0.1", "a@example.com")) is False
    ratelimit.note_login_failure(("127.0.0.1", "a@example.com"))
    monkeypatch.undo()
