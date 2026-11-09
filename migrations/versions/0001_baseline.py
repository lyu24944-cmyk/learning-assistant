"""baseline：当前 11 张表的结构。

用 checkfirst=True 建表：老库已经有部分表时只补缺的，不碰已有表。
老表的列由 src/migrate.py 的 legacy 迁移补齐，之后 Alembic 才接管。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

metadata = sa.MetaData()

users = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("email", sa.Text, nullable=False, unique=True),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
)

sessions = sa.Table(
    "sessions",
    metadata,
    sa.Column("token", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
)

reminder_prefs = sa.Table(
    "reminder_prefs",
    metadata,
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), primary_key=True),
    sa.Column("within_days", sa.Integer, nullable=False),
    sa.Column("baseline_date", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("email_enabled", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("updated_at", sa.Text, nullable=False, server_default=sa.text("''")),
)

weekly_reviews = sa.Table(
    "weekly_reviews",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("week_key", sa.Text, nullable=False),
    sa.Column("done_text", sa.Text, nullable=False),
    sa.Column("stuck_text", sa.Text, nullable=False),
    sa.Column("next_text", sa.Text, nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("plan", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
    sa.UniqueConstraint("user_id", "week_key", name="uq_weekly_reviews_user_week"),
)

courses = sa.Table(
    "courses",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("term", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("notes", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("created_at", sa.Text, nullable=False, server_default=sa.text("''")),
)

materials = sa.Table(
    "materials",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("course_id", sa.Integer, sa.ForeignKey("courses.id"), nullable=True),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("source_url", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("page_hint", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("created_at", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("image_rel", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("draft_id", sa.Integer, nullable=True),
)

user_records = sa.Table(
    "user_records",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("info_type", sa.Text, nullable=False),
    sa.Column("deadline", sa.Text, nullable=False),
    sa.Column("source_url", sa.Text, nullable=False),
    sa.Column("updated_on", sa.Text, nullable=False),
    sa.Column("major", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("review_state", sa.Text, nullable=False),
    sa.Column("date_quote", sa.Text, nullable=False),
    sa.Column("remind", sa.Text, nullable=False),
    sa.Column("condition", sa.Text, nullable=False),
)

image_drafts = sa.Table(
    "image_drafts",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("storage_key", sa.Text, nullable=False),
    sa.Column("original_rel", sa.Text, nullable=False),
    sa.Column("preview_rel", sa.Text, nullable=False),
    sa.Column("mime", sa.Text, nullable=False),
    sa.Column("byte_size", sa.Integer, nullable=False),
    sa.Column("width", sa.Integer, nullable=False),
    sa.Column("height", sa.Integer, nullable=False),
    sa.Column("sha256", sa.Text, nullable=False),
    sa.Column("ocr_text", sa.Text, nullable=False),
    sa.Column("ocr_engine", sa.Text, nullable=False),
    sa.Column("ocr_error", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("candidate_json", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
)

image_links = sa.Table(
    "image_links",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("draft_id", sa.Integer, sa.ForeignKey("image_drafts.id"), nullable=False),
    sa.Column("material_id", sa.Integer, sa.ForeignKey("materials.id"), nullable=False),
    sa.Column("record_id", sa.Integer, sa.ForeignKey("user_records.id"), nullable=False),
    sa.UniqueConstraint("draft_id", "record_id", name="uq_image_links_draft_record"),
)

review_activities = sa.Table(
    "review_activities",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("week_key", sa.Text, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("draft_id", sa.Integer, nullable=True),
    sa.Column("material_id", sa.Integer, nullable=True),
    sa.Column("record_id", sa.Integer, nullable=True),
    sa.Column("created_at", sa.Text, nullable=False),
)


def upgrade() -> None:
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # baseline 不删表：老库里是用户数据，回滚只回到“没有版本记录”的状态。
    pass
