"""密码策略的单元测试。"""

from __future__ import annotations

import pytest

from src.passwords import MAX_LENGTH, MIN_LENGTH, PasswordError, validate_password


def _filler(length: int) -> str:
    return ("z9" * 100)[:length]


def test_length_boundaries() -> None:
    with pytest.raises(PasswordError):
        validate_password(_filler(MIN_LENGTH - 1))
    validate_password(_filler(MIN_LENGTH))
    validate_password(_filler(MAX_LENGTH))
    with pytest.raises(PasswordError):
        validate_password(_filler(MAX_LENGTH + 1))


def test_common_password_is_rejected() -> None:
    with pytest.raises(PasswordError):
        validate_password("password12")


def test_email_local_part_is_rejected() -> None:
    with pytest.raises(PasswordError):
        validate_password("alicealice", "alicealice@example.com")
    validate_password("not-the-local-part", "alicealice@example.com")


def test_a_reasonable_password_passes() -> None:
    validate_password("moon-light-42", "alice@example.com")
