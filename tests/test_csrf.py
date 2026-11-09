"""CSRF 与 Origin 校验。裸 TestClient 才能控制「不带」和「带错」。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.db import reset_for_tests


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LEARNING_ASSISTANT_DB", str(tmp_path / "test.sqlite"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LEARNING_ASSISTANT_CSRF", raising=False)
    reset_for_tests()
    yield
    reset_for_tests()


def _client() -> TestClient:
    return TestClient(app)


def _account(email: str = "a@example.com") -> dict[str, str]:
    return {"email": email, "password": "correct-horse"}


def _row(title: str) -> dict[str, str]:
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


def _token(client: TestClient) -> str:
    response = client.get("/api/me")
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def test_write_without_token_is_rejected() -> None:
    client = _client()
    assert client.post("/api/register", json=_account()).status_code == 201

    rejected = client.post("/api/records", json=_row("没有口令"))
    assert rejected.status_code == 403
    assert rejected.json()["code"] == "csrf_failed"

    accepted = client.post(
        "/api/records",
        json=_row("带口令"),
        headers={"X-CSRF-Token": _token(client)},
    )
    assert accepted.status_code == 201, accepted.text


def test_get_does_not_need_a_token() -> None:
    client = _client()
    assert client.post("/api/register", json=_account()).status_code == 201
    assert client.get("/api/records").status_code == 200


def test_login_and_register_are_exempt() -> None:
    client = _client()
    assert client.post("/api/register", json=_account()).status_code == 201
    assert client.post("/api/login", json=_account()).status_code == 200


def test_wrong_token_is_rejected() -> None:
    client = _client()
    assert client.post("/api/register", json=_account()).status_code == 201
    response = client.post(
        "/api/records",
        json=_row("错口令"),
        headers={"X-CSRF-Token": "not-the-token"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"


def test_origin_mismatch_is_rejected() -> None:
    client = _client()
    rejected = client.post(
        "/api/login",
        json=_account(),
        headers={"Origin": "http://evil.example"},
    )
    assert rejected.status_code == 403
    assert rejected.json()["code"] == "origin_rejected"

    allowed = client.post(
        "/api/register",
        json=_account(),
        headers={"Origin": "http://127.0.0.1:5173"},
    )
    assert allowed.status_code == 201, allowed.text


def test_same_origin_is_allowed() -> None:
    client = _client()
    response = client.post(
        "/api/register",
        json=_account(),
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 201, response.text


def test_csrf_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_ASSISTANT_CSRF", "0")
    client = _client()
    assert client.post("/api/register", json=_account()).status_code == 201
    assert client.post("/api/records", json=_row("开关关了")).status_code == 201


def test_me_exposes_token_and_verification() -> None:
    client = _client()
    assert client.post("/api/register", json=_account()).status_code == 201
    body = client.get("/api/me").json()
    assert body["csrf_token"]
    assert body["session_id"]
    assert body["email_verified"] is False


def test_file_upload_is_also_checked() -> None:
    client = _client()
    assert client.post("/api/register", json=_account()).status_code == 201
    rejected = client.post(
        "/api/image-drafts",
        files={"file": ("x.png", b"not-an-image", "image/png")},
    )
    assert rejected.status_code == 403
    reached = client.post(
        "/api/image-drafts",
        files={"file": ("x.png", b"not-an-image", "image/png")},
        headers={"X-CSRF-Token": _token(client)},
    )
    assert reached.status_code != 403
