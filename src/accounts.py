"""邮箱和密码。密码只存 scrypt 哈希，登录态是 SQLite 里的会话。

批次 2 起这里用 SQLAlchemy 的 session()，不再直接写 sqlite3。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db import session
from src.db.models import User, UserSession

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SESSION_DAYS = 14
MIN_PASSWORD = 8


class AccountError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=2**14,
            r=8,
            p=1,
            dklen=32,
        )
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


def register(email: str, password: str) -> tuple[dict[str, str | int], str]:
    cleaned = _email(email)
    _password(password)
    now = _now()
    with session() as db:
        exists = db.scalar(select(User.id).where(User.email == cleaned))
        if exists is not None:
            raise AccountError("这个邮箱已经注册。", 409)
        user = User(email=cleaned, password_hash=hash_password(password), created_at=now)
        db.add(user)
        db.flush()
        user_id = int(user.id)
        token = _new_session(db, user_id, now)
    return {"id": user_id, "email": cleaned}, token


def login(email: str, password: str) -> tuple[dict[str, str | int], str]:
    cleaned = email.strip().lower()
    with session() as db:
        user = db.scalar(select(User).where(User.email == cleaned))
        if user is None or not verify_password(password, user.password_hash):
            raise AccountError("邮箱或密码不正确。", 401)
        token = _new_session(db, int(user.id), _now())
        found = {"id": int(user.id), "email": str(user.email)}
    return found, token


def logout(token: str | None) -> None:
    if not token:
        return
    with session() as db:
        row = db.get(UserSession, token)
        if row is not None:
            db.delete(row)


def user_for_token(token: str | None) -> dict[str, str | int] | None:
    if not token:
        return None
    with session() as db:
        row = db.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token == token)
        ).first()
        if row is None:
            return None
        current, user = row
        try:
            created = dt.datetime.fromisoformat(current.created_at)
        except ValueError:
            db.delete(current)
            return None
        if _now_dt() - created > dt.timedelta(days=_SESSION_DAYS):
            db.delete(current)
            return None
        return {"id": int(user.id), "email": str(user.email)}


def _email(email: str) -> str:
    cleaned = email.strip().lower()
    if not _EMAIL.match(cleaned):
        raise AccountError("邮箱格式不正确。", 400)
    return cleaned


def _password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"密码至少 {MIN_PASSWORD} 位。", 400)


def _new_session(db: Session, user_id: int, now: str) -> str:
    token = secrets.token_urlsafe(32)
    db.add(UserSession(token=token, user_id=user_id, created_at=now))
    return token


def _now_dt() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _now() -> str:
    return _now_dt().isoformat()
