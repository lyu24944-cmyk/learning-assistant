"""常用查询的索引。只加现有列；新列的索引跟各自批次一起加。"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_sessions_user_id", "sessions", ["user_id"]),
    ("ix_user_records_user_id_id", "user_records", ["user_id", "id"]),
    ("ix_user_records_user_id_status", "user_records", ["user_id", "status"]),
    ("ix_user_records_user_id_deadline", "user_records", ["user_id", "deadline"]),
    ("ix_user_records_user_id_review_state", "user_records", ["user_id", "review_state"]),
    ("ix_materials_user_id_course_id", "materials", ["user_id", "course_id"]),
    ("ix_materials_user_id_id", "materials", ["user_id", "id"]),
    ("ix_image_drafts_user_id_id", "image_drafts", ["user_id", "id"]),
    ("ix_image_drafts_user_id_sha256", "image_drafts", ["user_id", "sha256"]),
    ("ix_review_activities_user_id_week_key", "review_activities", ["user_id", "week_key"]),
    ("ix_image_links_user_id_draft_id", "image_links", ["user_id", "draft_id"]),
    ("ix_image_links_user_id_record_id", "image_links", ["user_id", "record_id"]),
)


def upgrade() -> None:
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _columns in reversed(INDEXES):
        op.drop_index(name, table_name=table)
