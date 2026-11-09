"""账号模块与运维命令的测试。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import select

from src import accounts, security
from src.db import reset_for_tests, session
from src.db.models import User, UserSession


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LEARNING_ASSISTANT_DB", str(tmp_path / "test.sqlite"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LEARNING_ASSISTANT_SECRET", raising=False)
    reset_for_tests()
    security.reset_for_tests()
    yield
    reset_for_tests()
    security.reset_for_tests()


def _register(email: str = "a@example.com", password: str = "correct-horse") -> tuple[dict, str, str]:
    return accounts.register(email, password)


def test_register_rejects_a_weak_password() -> None:
    with pytest.raises(accounts.AccountError) as caught:
        accounts.register("a@example.com", "password12")
    assert caught.value.code == "password_too_weak"
    assert caught.value.status == 400


def test_old_eight_character_password_still_logs_in() -> None:
    now = dt.datetime.now(dt.UTC).isoformat()
    with session() as db:
        db.add(
            User(
                email="old@example.com",
                password_hash=accounts.hash_password("12345678"),
                created_at=now,
                password_changed_at=now,
            )
        )
    _user, token, _csrf = accounts.login("old@example.com", "12345678")
    assert token
    assert accounts.user_for_token(token) is not None


def test_login_rotates_the_session() -> None:
    _user, token_a, _csrf = _register()
    _user, token_b, _csrf = accounts.login(
        "a@example.com", "correct-horse", previous_token=token_a
    )
    assert token_a != token_b
    assert accounts.user_for_token(token_a) is None
    assert accounts.user_for_token(token_b) is not None


def test_change_password_keeps_current_and_drops_others() -> None:
    user, token_a, _csrf = _register()
    _user, token_b, _csrf = accounts.login("a@example.com", "correct-horse")
    current = accounts.user_for_token(token_a)
    assert current is not None

    accounts.change_password(
        int(user["id"]),
        "correct-horse",
        "new-password-123",
        keep_public_id=str(current["session_id"]),
    )

    assert accounts.user_for_token(token_a) is not None
    assert accounts.user_for_token(token_b) is None
    assert accounts.login("a@example.com", "new-password-123")[0]["email"] == "a@example.com"
    with pytest.raises(accounts.AccountError):
        accounts.login("a@example.com", "correct-horse")


def test_reset_token_changes_password_and_is_single_use() -> None:
    _user, token, _csrf = _register()
    _user, reset, _expires = accounts.reset_password_token("a@example.com")

    accounts.reset_password(reset, "new-password-123")
    assert accounts.user_for_token(token) is None
    assert accounts.login("a@example.com", "new-password-123")[0]["email"] == "a@example.com"

    with pytest.raises(accounts.AccountError) as caught:
        accounts.reset_password(reset, "another-password-123")
    assert caught.value.code == "token_invalid"


def test_expired_reset_token_is_rejected() -> None:
    _register()
    with session() as db:
        user = db.scalar(select(User).where(User.email == "a@example.com"))
        assert user is not None
        token = security.sign_reset_token(int(user.id), user.password_changed_at, ttl=-1)
    with pytest.raises(accounts.AccountError) as caught:
        accounts.reset_password(token, "new-password-123")
    assert caught.value.code == "token_expired"


def test_mark_verified_sets_the_flag() -> None:
    _register()
    assert accounts.user_by_email("a@example.com")["email_verified"] is False
    assert accounts.mark_verified("a@example.com")
    assert accounts.user_by_email("a@example.com")["email_verified"] is True
    assert accounts.mark_verified("nobody@example.com") is None


def test_list_and_revoke_sessions() -> None:
    user, token_a, _csrf = _register()
    _user, token_b, _csrf = accounts.login("a@example.com", "correct-horse")

    items = accounts.list_sessions(int(user["id"]), token_a)
    assert len(items) == 2
    assert items[0]["current"] is True
    other = next(item for item in items if not item["current"])
    assert accounts.revoke_session(int(user["id"]), str(other["id"])) is True
    assert accounts.user_for_token(token_b) is None
    assert accounts.user_for_token(token_a) is not None
    assert accounts.revoke_session(int(user["id"]), "missing") is False


def test_expired_and_idle_sessions_are_deleted() -> None:
    _user, token, _csrf = _register()
    with session() as db:
        row = db.get(UserSession, token)
        assert row is not None
        row.expires_at = "2020-01-01T00:00:00+00:00"
    assert accounts.user_for_token(token) is None
    with session() as db:
        assert db.get(UserSession, token) is None

    _user, token, _csrf = _register("b@example.com")
    with session() as db:
        row = db.get(UserSession, token)
        assert row is not None
        row.last_seen_at = (dt.datetime.now(dt.UTC) - dt.timedelta(days=8)).isoformat()
    assert accounts.user_for_token(token) is None


def test_last_seen_is_throttled() -> None:
    _user, token, _csrf = _register()
    with session() as db:
        row = db.get(UserSession, token)
        assert row is not None
        before = row.last_seen_at

    accounts.user_for_token(token)
    with session() as db:
        row = db.get(UserSession, token)
        assert row is not None
        assert row.last_seen_at == before

    old = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=2)).isoformat()
    with session() as db:
        row = db.get(UserSession, token)
        assert row is not None
        row.last_seen_at = old
    accounts.user_for_token(token)
    with session() as db:
        row = db.get(UserSession, token)
        assert row is not None
        assert row.last_seen_at != old


def test_cli_prints_a_fragment_link(capsys: pytest.CaptureFixture[str]) -> None:
    _register()
    assert accounts.main(["reset-password", "a@example.com"]) == 0
    out = capsys.readouterr().out
    assert "/reset#token=" in out
    assert "只能用一次" in out

    assert accounts.main(["mark-verified", "a@example.com"]) == 0
    assert "已验证" in capsys.readouterr().out

    assert accounts.main(["reset-password", "nobody@example.com"]) == 2
