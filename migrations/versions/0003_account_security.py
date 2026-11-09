"""账号安全：会话过期、CSRF、数据库限流、邮箱验证记录。

新增列都带默认值或可空，回滚到批次 1 的代码时旧 INSERT 仍能成功。
`public_id` 特意保持可空：唯一索引允许多个 NULL，旧代码连开多个会话不会撞约束。

降级只丢这一批新列里的值（验证时间、会话元数据），不动用户、记录、草稿。
"""

from __future__ import annotations

import datetime as dt
import secrets

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SESSION_DAYS = 14


def _public_id(seen: set[str]) -> str:
    while True:
        value = secrets.token_urlsafe(9)
        if value not in seen:
            seen.add(value)
            return value


def _plus_days(value: str, days: int) -> str:
    try:
        start = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        start = dt.datetime.now(dt.UTC)
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.UTC)
    return (start + dt.timedelta(days=days)).isoformat()


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column("password_changed_at", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column("sessions", sa.Column("public_id", sa.Text(), nullable=True))
    op.add_column(
        "sessions",
        sa.Column("csrf_token", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "sessions",
        sa.Column("last_seen_at", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "sessions",
        sa.Column("user_agent", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "sessions",
        sa.Column("ip_hash", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "sessions",
        sa.Column("expires_at", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.create_table(
        "rate_limits",
        sa.Column("bucket_key", sa.Text(), primary_key=True),
        sa.Column("window_start", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
    )

    connection = op.get_bind()
    connection.execute(sa.text("UPDATE users SET password_changed_at = created_at"))
    rows = connection.execute(sa.text("SELECT token, created_at FROM sessions")).fetchall()
    seen: set[str] = set()
    for token, created_at in rows:
        connection.execute(
            sa.text(
                "UPDATE sessions SET public_id = :public_id, csrf_token = :csrf_token, "
                "last_seen_at = :created_at, expires_at = :expires_at WHERE token = :token"
            ),
            {
                "public_id": _public_id(seen),
                "csrf_token": secrets.token_urlsafe(32),
                "created_at": created_at,
                "expires_at": _plus_days(created_at, SESSION_DAYS),
                "token": token,
            },
        )

    op.create_index("ux_sessions_public_id", "sessions", ["public_id"], unique=True)
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ux_sessions_public_id", table_name="sessions")
    op.drop_table("rate_limits")
    op.drop_column("sessions", "expires_at")
    op.drop_column("sessions", "ip_hash")
    op.drop_column("sessions", "user_agent")
    op.drop_column("sessions", "last_seen_at")
    op.drop_column("sessions", "csrf_token")
    op.drop_column("sessions", "public_id")
    op.drop_column("users", "password_changed_at")
    op.drop_column("users", "email_verified_at")
