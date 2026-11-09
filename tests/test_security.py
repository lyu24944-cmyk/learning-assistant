"""签名令牌、密钥和脱敏的单元测试。不启动服务。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import security


@pytest.fixture(autouse=True)
def _clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LEARNING_ASSISTANT_DB", str(tmp_path / "test.sqlite"))
    monkeypatch.delenv("LEARNING_ASSISTANT_SECRET", raising=False)
    security.reset_for_tests()
    yield
    security.reset_for_tests()


def test_secret_file_is_created_next_to_the_database(tmp_path: Path) -> None:
    first = security.secret()
    second = security.secret()
    assert first == second
    assert len(first) == 32
    assert (tmp_path / "secret.key").is_file()


def test_secret_can_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_ASSISTANT_SECRET", "x" * 32)
    assert security.secret() == b"x" * 32


def test_short_environment_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_ASSISTANT_SECRET", "short")
    with pytest.raises(RuntimeError):
        security.secret()


def test_reset_token_round_trip() -> None:
    token = security.sign_reset_token(7, "2026-11-01T00:00:00+00:00")
    payload = security.verify_reset_token(token)
    assert payload["uid"] == 7
    assert payload["pwd"] == "2026-11-01T00:00:00+00:00"


def test_tampered_token_is_rejected() -> None:
    token = security.sign_reset_token(7, "2026-11-01T00:00:00+00:00")
    body, signature = token.split(".", 1)
    tampered = f"{body}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    with pytest.raises(security.TokenError) as caught:
        security.verify_reset_token(tampered)
    assert caught.value.code == "token_invalid"


def test_expired_token_is_rejected() -> None:
    token = security.sign_reset_token(7, "2026-11-01T00:00:00+00:00", ttl=-1)
    with pytest.raises(security.TokenError) as caught:
        security.verify_reset_token(token)
    assert caught.value.code == "token_expired"


def test_token_signed_with_another_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_ASSISTANT_SECRET", "a" * 32)
    token = security.sign_reset_token(7, "2026-11-01T00:00:00+00:00")
    monkeypatch.setenv("LEARNING_ASSISTANT_SECRET", "b" * 32)
    with pytest.raises(security.TokenError) as caught:
        security.verify_reset_token(token)
    assert caught.value.code == "token_invalid"


def test_garbage_token_is_rejected() -> None:
    for value in ("", "abc", "a.b.c", "...."):
        with pytest.raises(security.TokenError):
            security.verify_reset_token(value)


def test_hash_ip_is_stable_and_not_the_raw_ip() -> None:
    first = security.hash_ip("127.0.0.1")
    assert first == security.hash_ip("127.0.0.1")
    assert first != security.hash_ip("127.0.0.2")
    assert "127.0.0.1" not in first
    assert len(first) == 16


def test_mask_email_keeps_the_domain() -> None:
    assert security.mask_email("alice@example.com") == "a***@example.com"
    assert security.mask_email("not-an-email") == "***"
