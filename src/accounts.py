"""邮箱、密码和会话。

密码只存 scrypt 哈希；登录态是 SQLite 里的 sessions 行。
批次 2 起这里用 SQLAlchemy 的 session()，不再直接写 sqlite3。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import os
import re
import secrets
import sys
from functools import lru_cache

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db import bootstrap, session
from src.db.models import User, UserSession
from src.passwords import PasswordError, validate_password
from src.security import RESET_TTL, TokenError, sign_reset_token, verify_reset_token

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SESSION_DAYS = 14
_IDLE_DAYS = 7
_TOUCH_SECONDS = 60


class AccountError(Exception):
    def __init__(self, message: str, status: int, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


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


def register(
    email: str,
    password: str,
    *,
    user_agent: str = "",
    ip_hash: str = "",
    previous_token: str | None = None,
) -> tuple[dict[str, str | int], str, str]:
    cleaned = _email(email)
    _validate_new_password(password, cleaned)
    now = _now_dt()
    try:
        with session() as db:
            exists = db.scalar(select(User.id).where(User.email == cleaned))
            if exists is not None:
                raise AccountError("这个邮箱已经注册。", 409)
            user = User(
                email=cleaned,
                password_hash=hash_password(password),
                created_at=now.isoformat(),
                password_changed_at=now.isoformat(),
            )
            db.add(user)
            db.flush()
            user_id = int(user.id)
            if previous_token:
                _delete_session(db, previous_token)
            token, csrf = _new_session(db, user_id, now, user_agent, ip_hash)
    except IntegrityError as exc:
        raise AccountError("这个邮箱已经注册。", 409) from exc
    return {"id": user_id, "email": cleaned}, token, csrf


def login(
    email: str,
    password: str,
    *,
    user_agent: str = "",
    ip_hash: str = "",
    previous_token: str | None = None,
) -> tuple[dict[str, str | int], str, str]:
    cleaned = email.strip().lower()
    now = _now_dt()
    with session() as db:
        user = db.scalar(select(User).where(User.email == cleaned))
        if user is None:
            _dummy_verify(password)
            raise AccountError("邮箱或密码不正确。", 401)
        if not verify_password(password, user.password_hash):
            raise AccountError("邮箱或密码不正确。", 401)
        if previous_token:
            _delete_session(db, previous_token)
        token, csrf = _new_session(db, int(user.id), now, user_agent, ip_hash)
        found = {"id": int(user.id), "email": str(user.email)}
    return found, token, csrf


def logout(token: str | None) -> None:
    if not token:
        return
    with session() as db:
        _delete_session(db, token)


def user_for_token(token: str | None, *, touch: bool = True) -> dict[str, str | int | bool] | None:
    if not token:
        return None
    now = _now_dt()
    with session() as db:
        row = db.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token == token)
        ).first()
        if row is None:
            return None
        current, user = row
        if _expired(current, now) or _idle(current, now):
            db.delete(current)
            return None
        _ensure_session_defaults(current, now)
        if touch and _needs_touch(current, now):
            current.last_seen_at = now.isoformat()
        return {
            "id": int(user.id),
            "email": str(user.email),
            "email_verified": user.email_verified_at is not None,
            "csrf_token": str(current.csrf_token),
            "session_id": str(current.public_id),
        }


def list_sessions(user_id: int, current_token: str | None = None) -> list[dict[str, str | bool]]:
    now = _now_dt()
    with session() as db:
        rows = db.scalars(select(UserSession).where(UserSession.user_id == user_id)).all()
        found: list[dict[str, str | bool]] = []
        for row in rows:
            _ensure_session_defaults(row, now)
            found.append(
                {
                    "id": str(row.public_id),
                    "created_at": row.created_at,
                    "last_seen_at": row.last_seen_at,
                    "user_agent": row.user_agent,
                    "current": bool(current_token and hmac.compare_digest(row.token, current_token)),
                }
            )
    found.sort(key=lambda item: str(item["last_seen_at"]), reverse=True)
    found.sort(key=lambda item: not bool(item["current"]))
    return found


def revoke_session(user_id: int, public_id: str) -> bool:
    with session() as db:
        row = db.scalar(
            select(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.public_id == public_id,
            )
        )
        if row is None:
            return False
        db.delete(row)
        return True


def delete_all_sessions(user_id: int) -> None:
    with session() as db:
        db.execute(delete(UserSession).where(UserSession.user_id == user_id))


def change_password(
    user_id: int,
    current_password: str,
    new_password: str,
    keep_public_id: str | None = None,
) -> None:
    with session() as db:
        user = db.get(User, user_id)
        if user is None:
            raise AccountError("找不到这个账号。", 404)
        if not verify_password(current_password, user.password_hash):
            raise AccountError("当前密码不正确。", 400, "current_password_invalid")
        _validate_new_password(new_password, user.email)
        user.password_hash = hash_password(new_password)
        user.password_changed_at = _now()
        statement = delete(UserSession).where(UserSession.user_id == user_id)
        if keep_public_id:
            statement = statement.where(UserSession.public_id != keep_public_id)
        db.execute(statement)


def reset_password_token(email: str) -> tuple[dict[str, str | int], str, str]:
    cleaned = email.strip().lower()
    with session() as db:
        user = db.scalar(select(User).where(User.email == cleaned))
        if user is None:
            raise LookupError("找不到这个邮箱。")
        token = sign_reset_token(int(user.id), user.password_changed_at or "")
        expires = (_now_dt() + dt.timedelta(seconds=RESET_TTL)).isoformat()
        return {"id": int(user.id), "email": str(user.email)}, token, expires


def reset_password(token: str, new_password: str) -> None:
    try:
        payload = verify_reset_token(token)
    except TokenError as exc:
        if exc.code == "token_expired":
            raise AccountError("链接已过期，请让管理员重新生成。", 400, "token_expired") from exc
        raise AccountError("链接已失效，请让管理员重新生成。", 400, "token_invalid") from exc
    with session() as db:
        user = db.get(User, int(payload["uid"]))
        if user is None or (user.password_changed_at or "") != payload["pwd"]:
            raise AccountError("链接已失效，请让管理员重新生成。", 400, "token_invalid")
        _validate_new_password(new_password, user.email)
        user.password_hash = hash_password(new_password)
        user.password_changed_at = _now()
        db.execute(delete(UserSession).where(UserSession.user_id == int(user.id)))


def mark_verified(email: str) -> str | None:
    cleaned = email.strip().lower()
    with session() as db:
        user = db.scalar(select(User).where(User.email == cleaned))
        if user is None:
            return None
        if user.email_verified_at:
            return str(user.email_verified_at)
        user.email_verified_at = _now()
        return str(user.email_verified_at)


def user_by_email(email: str) -> dict[str, str | int | bool] | None:
    cleaned = email.strip().lower()
    with session() as db:
        user = db.scalar(select(User).where(User.email == cleaned))
        if user is None:
            return None
        return {
            "id": int(user.id),
            "email": str(user.email),
            "email_verified": user.email_verified_at is not None,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.accounts", description="账号运维命令")
    sub = parser.add_subparsers(dest="command", required=True)

    reset = sub.add_parser("reset-password", help="生成 30 分钟有效的重置链接")
    reset.add_argument("email")
    mark = sub.add_parser("mark-verified", help="把邮箱标为已验证")
    mark.add_argument("email")
    sessions = sub.add_parser("sessions", help="列出某个邮箱的会话")
    sessions.add_argument("email")
    args = parser.parse_args(argv)

    bootstrap()
    if args.command == "reset-password":
        try:
            _user, token, expires = reset_password_token(args.email)
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        base = os.environ.get("LEARNING_ASSISTANT_WEB_BASE", "http://127.0.0.1:5173").rstrip("/")
        print(f"重置链接（{expires} 前有效，只能用一次）：")
        print(f"{base}/reset#token={token}")
        print("如果这是服务器，先设置 LEARNING_ASSISTANT_WEB_BASE 再生成链接。")
        return 0

    if args.command == "mark-verified":
        value = mark_verified(args.email)
        if value is None:
            print("找不到这个邮箱。", file=sys.stderr)
            return 2
        print(f"已验证：{args.email} @ {value}")
        return 0

    user = user_by_email(args.email)
    if user is None:
        print("找不到这个邮箱。", file=sys.stderr)
        return 2
    items = list_sessions(int(user["id"]), None)
    if not items:
        print("没有会话。")
        return 0
    for item in items:
        agent = str(item["user_agent"] or "")[:60]
        print(f"{item['id']}  创建 {item['created_at']}  最近 {item['last_seen_at']}  {agent}")
    return 0


def _email(email: str) -> str:
    cleaned = email.strip().lower()
    if not _EMAIL.match(cleaned):
        raise AccountError("邮箱格式不正确。", 400)
    return cleaned


def _validate_new_password(password: str, email: str) -> None:
    try:
        validate_password(password, email)
    except PasswordError as exc:
        raise AccountError(str(exc), 400, "password_too_weak") from exc


def _new_session(
    db: Session,
    user_id: int,
    now: dt.datetime,
    user_agent: str,
    ip_hash: str,
) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    stamp = now.isoformat()
    db.add(
        UserSession(
            token=token,
            user_id=user_id,
            created_at=stamp,
            public_id=_new_public_id(),
            csrf_token=csrf,
            last_seen_at=stamp,
            user_agent=(user_agent or "")[:200],
            ip_hash=ip_hash or "",
            expires_at=(now + dt.timedelta(days=_SESSION_DAYS)).isoformat(),
        )
    )
    return token, csrf


def _delete_session(db: Session, token: str) -> None:
    row = db.get(UserSession, token)
    if row is not None:
        db.delete(row)


def _new_public_id() -> str:
    return secrets.token_urlsafe(9)


def _ensure_session_defaults(current: UserSession, now: dt.datetime) -> None:
    if not current.public_id:
        current.public_id = _new_public_id()
    if not current.csrf_token:
        current.csrf_token = secrets.token_urlsafe(32)
    if not current.expires_at:
        current.expires_at = _expiry_dt(current).isoformat()
    if not current.last_seen_at:
        current.last_seen_at = current.created_at or now.isoformat()


def _expiry_dt(current: UserSession) -> dt.datetime:
    parsed = _parse_time(current.expires_at)
    if parsed is not None:
        return parsed
    created = _parse_time(current.created_at) or _now_dt()
    return created + dt.timedelta(days=_SESSION_DAYS)


def _expired(current: UserSession, now: dt.datetime) -> bool:
    return now >= _expiry_dt(current)


def _idle(current: UserSession, now: dt.datetime) -> bool:
    last = _parse_time(current.last_seen_at) or _parse_time(current.created_at) or now
    return now - last > dt.timedelta(days=_IDLE_DAYS)


def _needs_touch(current: UserSession, now: dt.datetime) -> bool:
    last = _parse_time(current.last_seen_at)
    return last is None or (now - last).total_seconds() >= _TOUCH_SECONDS


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def _dummy_verify(password: str) -> None:
    verify_password(password, _dummy_hash())


def _now_dt() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _now() -> str:
    return _now_dt().isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
