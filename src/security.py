"""密钥、签名令牌、脱敏。

这一层不碰数据库；`accounts.py` 负责查用户和比对密码版本。
令牌是「盖了章的一次性介绍信」：payload 里写 uid、exp、pwd，章是 HMAC。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from functools import lru_cache
from pathlib import Path

from src.db.paths import database_path

RESET_TTL = 30 * 60
_MIN_SECRET = 32


class TokenError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def secret() -> bytes:
    explicit = os.environ.get("LEARNING_ASSISTANT_SECRET", "").strip()
    if explicit:
        if len(explicit) < _MIN_SECRET:
            raise RuntimeError(f"LEARNING_ASSISTANT_SECRET 至少 {_MIN_SECRET} 个字符。")
        return explicit.encode("utf-8")
    path = (database_path().parent / "secret.key").resolve()
    return _read_or_create_secret(str(path))


@lru_cache(maxsize=8)
def _read_or_create_secret(path_text: str) -> bytes:
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _checked(path.read_bytes())
    except FileNotFoundError:
        pass
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _checked(path.read_bytes())
    try:
        value = secrets.token_bytes(32)
        os.write(handle, value)
    finally:
        os.close(handle)
    return value


def _checked(value: bytes) -> bytes:
    if len(value) < _MIN_SECRET:
        raise RuntimeError(f"密钥文件至少要有 {_MIN_SECRET} 字节。")
    return value


def reset_for_tests() -> None:
    _read_or_create_secret.cache_clear()


def hash_ip(ip: str) -> str:
    digest = hmac.new(secret(), b"ip:" + ip.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:16]


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{local[:1] or '*'}***@{domain}"


def sign_reset_token(user_id: int, password_changed_at: str, ttl: int = RESET_TTL) -> str:
    payload = {
        "uid": int(user_id),
        "exp": int(time.time()) + int(ttl),
        "pwd": password_changed_at,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _b64(raw)
    return f"{body}.{_sign(body)}"


def verify_reset_token(token: str) -> dict:
    if not token or token.count(".") != 1:
        raise TokenError("token_invalid")
    body, signature = token.split(".", 1)
    try:
        expected = _sign(body)
    except (UnicodeEncodeError, ValueError) as exc:
        raise TokenError("token_invalid") from exc
    if not hmac.compare_digest(signature, expected):
        raise TokenError("token_invalid")
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError) as exc:
        raise TokenError("token_invalid") from exc
    if not isinstance(payload, dict):
        raise TokenError("token_invalid")
    uid = payload.get("uid")
    exp = payload.get("exp")
    pwd = payload.get("pwd")
    if not isinstance(uid, int) or not isinstance(exp, int) or not isinstance(pwd, str):
        raise TokenError("token_invalid")
    if int(time.time()) >= exp:
        raise TokenError("token_expired")
    return {"uid": uid, "exp": exp, "pwd": pwd}


def _sign(body: str) -> str:
    message = b"reset:" + body.encode("ascii")
    return _b64(hmac.new(secret(), message, hashlib.sha256).digest())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)
