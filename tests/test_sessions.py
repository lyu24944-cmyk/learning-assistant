"""会话过期、设备列表、改密码和重置密码的 HTTP 测试。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import accounts
from src.api import app
from src.db import reset_for_tests, session
from src.db.models import UserSession
from tests.csrf_client import CsrfClient


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LEARNING_ASSISTANT_DB", str(tmp_path / "test.sqlite"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LEARNING_ASSISTANT_SECRET", raising=False)
    reset_for_tests()
    yield
    reset_for_tests()


def _client() -> CsrfClient:
    return CsrfClient(TestClient(app))


def _account(email: str = "a@example.com", password: str = "correct-horse") -> dict[str, str]:
    return {"email": email, "password": password}


def test_two_logins_list_and_revoke_one() -> None:
    first = _client()
    second = _client()
    assert first.post("/api/register", json=_account()).status_code == 201
    assert second.post("/api/login", json=_account()).status_code == 200

    items = first.get("/api/sessions").json()["sessions"]
    assert len(items) == 2
    assert items[0]["current"] is True
    other = next(item for item in items if not item["current"])
    assert first.delete(f"/api/sessions/{other['id']}").status_code == 200
    assert second.get("/api/me").status_code == 401
    assert first.get("/api/me").status_code == 200


def test_logout_all_kills_every_session() -> None:
    first = _client()
    second = _client()
    assert first.post("/api/register", json=_account()).status_code == 201
    assert second.post("/api/login", json=_account()).status_code == 200
    assert first.post("/api/auth/logout-all").status_code == 200
    assert first.get("/api/me").status_code == 401
    assert second.get("/api/me").status_code == 401


def test_change_password_keeps_current_and_drops_others() -> None:
    first = _client()
    second = _client()
    assert first.post("/api/register", json=_account()).status_code == 201
    assert second.post("/api/login", json=_account()).status_code == 200

    changed = first.post(
        "/api/auth/change-password",
        json={"current_password": "correct-horse", "new_password": "new-password-123"},
    )
    assert changed.status_code == 200, changed.text
    assert first.get("/api/me").status_code == 200
    assert second.get("/api/me").status_code == 401

    assert second.post("/api/login", json=_account(password="new-password-123")).status_code == 200
    assert second.post("/api/login", json=_account()).status_code == 401


def test_change_password_rejects_a_wrong_current_password() -> None:
    client = _client()
    assert client.post("/api/register", json=_account()).status_code == 201
    response = client.post(
        "/api/auth/change-password",
        json={"current_password": "wrong-password", "new_password": "new-password-123"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "current_password_invalid"


def test_expired_session_returns_401() -> None:
    client = _client()
    assert client.post("/api/register", json=_account()).status_code == 201
    token = client.cookies.get("session")
    with session() as db:
        row = db.get(UserSession, token)
        assert row is not None
        row.expires_at = "2020-01-01T00:00:00+00:00"
    assert client.get("/api/me").status_code == 401


def test_idle_session_returns_401() -> None:
    client = _client()
    assert client.post("/api/register", json=_account()).status_code == 201
    token = client.cookies.get("session")
    with session() as db:
        row = db.get(UserSession, token)
        assert row is not None
        row.last_seen_at = (dt.datetime.now(dt.UTC) - dt.timedelta(days=8)).isoformat()
    assert client.get("/api/me").status_code == 401


def test_login_rotates_the_cookie() -> None:
    client = _client()
    assert client.post("/api/register", json=_account()).status_code == 201
    old = client.cookies.get("session")
    assert client.post("/api/login", json=_account()).status_code == 200
    new = client.cookies.get("session")
    assert old != new

    stale = TestClient(app)
    stale.cookies.set("session", old)
    assert stale.get("/api/me").status_code == 401


def test_reset_password_endpoint_and_reuse() -> None:
    client = _client()
    assert client.post("/api/register", json=_account()).status_code == 201
    _user, reset, _expires = accounts.reset_password_token("a@example.com")

    raw = TestClient(app)
    changed = raw.post(
        "/api/auth/reset-password",
        json={"token": reset, "password": "new-password-123"},
    )
    assert changed.status_code == 200, changed.text
    assert client.get("/api/me").status_code == 401

    again = raw.post(
        "/api/auth/reset-password",
        json={"token": reset, "password": "another-password-123"},
    )
    assert again.status_code == 400
    assert again.json()["code"] == "token_invalid"


def test_reset_password_rejects_a_weak_password() -> None:
    _client().post("/api/register", json=_account())
    _user, reset, _expires = accounts.reset_password_token("a@example.com")
    raw = TestClient(app)
    response = raw.post(
        "/api/auth/reset-password",
        json={"token": reset, "password": "password12"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "password_too_weak"
