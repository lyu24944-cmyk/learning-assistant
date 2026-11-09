"""邮箱和密码。密码只存 scrypt 哈希，登录态是 SQLite 里的会话。"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import secrets

from src.db import connect

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
    connection = connect()
    try:
        exists = connection.execute("SELECT id FROM users WHERE email = ?", (cleaned,)).fetchone()
        if exists is not None:
            raise AccountError("这个邮箱已经注册。", 409)
        cursor = connection.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (cleaned, hash_password(password), now),
        )
        user_id = int(cursor.lastrowid)
        token = _new_session(connection, user_id, now)
        connection.commit()
    finally:
        connection.close()
    return {"id": user_id, "email": cleaned}, token


def login(email: str, password: str) -> tuple[dict[str, str | int], str]:
    cleaned = email.strip().lower()
    connection = connect()
    try:
        row = connection.execute(
            "SELECT id, email, password_hash FROM users WHERE email = ?",
            (cleaned,),
        ).fetchone()
        if row is None or not verify_password(password, row["password_hash"]):
            raise AccountError("邮箱或密码不正确。", 401)
        user = {"id": int(row["id"]), "email": str(row["email"])}
        token = _new_session(connection, int(user["id"]), _now())
        connection.commit()
    finally:
        connection.close()
    return user, token


def logout(token: str | None) -> None:
    if not token:
        return
    connection = connect()
    try:
        connection.execute("DELETE FROM sessions WHERE token = ?", (token,))
        connection.commit()
    finally:
        connection.close()


def user_for_token(token: str | None) -> dict[str, str | int] | None:
    if not token:
        return None
    connection = connect()
    try:
        row = connection.execute(
            """
            SELECT users.id AS id, users.email AS email, sessions.created_at AS created_at
            FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = ?
            """,
            (token,),
        ).fetchone()
        if row is None:
            return None
        user = {"id": int(row["id"]), "email": str(row["email"])}
        created = dt.datetime.fromisoformat(row["created_at"])
        if _now_dt() - created > dt.timedelta(days=_SESSION_DAYS):
            connection.execute("DELETE FROM sessions WHERE token = ?", (token,))
            connection.commit()
            return None
        return user
    finally:
        connection.close()


def _email(email: str) -> str:
    cleaned = email.strip().lower()
    if not _EMAIL.match(cleaned):
        raise AccountError("邮箱格式不正确。", 400)
    return cleaned


def _password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"密码至少 {MIN_PASSWORD} 位。", 400)


def _new_session(connection, user_id: int, now: str) -> str:
    token = secrets.token_urlsafe(32)
    connection.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
        (token, user_id, now),
    )
    return token


def _now_dt() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()
