"""11 张表的 ORM 模型。列名保持英文，中文 API 字段的映射留在各业务模块。

索引写在这里，给 autogenerate 和结构测试用；Alembic 的 0001 基线
不直接使用这份 metadata，而是自己建表，避免历史库被新列污染。
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    email_verified_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_changed_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))


class UserSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        Index("ux_sessions_public_id", "public_id", unique=True),
        Index("ix_sessions_expires_at", "expires_at"),
    )

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    public_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    csrf_token: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    last_seen_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    user_agent: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    ip_hash: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    expires_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))


class RateLimit(Base):
    __tablename__ = "rate_limits"

    bucket_key: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[int] = mapped_column(Integer, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)


class ReminderPref(Base):
    __tablename__ = "reminder_prefs"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), primary_key=True)
    within_days: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_date: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    email_enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))


class WeeklyReview(Base):
    __tablename__ = "weekly_reviews"
    __table_args__ = (UniqueConstraint("user_id", "week_key", name="uq_weekly_reviews_user_week"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    week_key: Mapped[str] = mapped_column(Text, nullable=False)
    done_text: Mapped[str] = mapped_column(Text, nullable=False)
    stuck_text: Mapped[str] = mapped_column(Text, nullable=False)
    next_text: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    term: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))


class Material(Base):
    __tablename__ = "materials"
    __table_args__ = (
        Index("ix_materials_user_id_course_id", "user_id", "course_id"),
        Index("ix_materials_user_id_id", "user_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    course_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("courses.id"), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    source_url: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    page_hint: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    image_rel: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    draft_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class UserRecord(Base):
    __tablename__ = "user_records"
    __table_args__ = (
        Index("ix_user_records_user_id_id", "user_id", "id"),
        Index("ix_user_records_user_id_status", "user_id", "status"),
        Index("ix_user_records_user_id_deadline", "user_id", "deadline"),
        Index("ix_user_records_user_id_review_state", "user_id", "review_state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    info_type: Mapped[str] = mapped_column(Text, nullable=False)
    deadline: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    updated_on: Mapped[str] = mapped_column(Text, nullable=False)
    major: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    review_state: Mapped[str] = mapped_column(Text, nullable=False)
    date_quote: Mapped[str] = mapped_column(Text, nullable=False)
    remind: Mapped[str] = mapped_column(Text, nullable=False)
    condition: Mapped[str] = mapped_column(Text, nullable=False)


class ImageDraft(Base):
    __tablename__ = "image_drafts"
    __table_args__ = (
        Index("ix_image_drafts_user_id_id", "user_id", "id"),
        Index("ix_image_drafts_user_id_sha256", "user_id", "sha256"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    original_rel: Mapped[str] = mapped_column(Text, nullable=False)
    preview_rel: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    ocr_text: Mapped[str] = mapped_column(Text, nullable=False)
    ocr_engine: Mapped[str] = mapped_column(Text, nullable=False)
    ocr_error: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    candidate_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    # 识别任务状态：queued / running / done / failed。和 status 是两件事：
    # status 管草稿的生死，ocr_state 管识别跑到哪一步。
    # 默认 'done' 只服务于「0003 的老行」和「回滚到批次 2」；新代码必须显式写 'queued'，
    # 依赖默认值会让任务以已完成的姿态静静躺着，永远没人识别。
    ocr_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'done'"))
    ocr_queued_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    ocr_started_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    ocr_finished_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    ocr_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ocr_provider: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))


class ImageLink(Base):
    __tablename__ = "image_links"
    __table_args__ = (
        UniqueConstraint("draft_id", "record_id", name="uq_image_links_draft_record"),
        Index("ix_image_links_user_id_draft_id", "user_id", "draft_id"),
        Index("ix_image_links_user_id_record_id", "user_id", "record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    draft_id: Mapped[int] = mapped_column(Integer, ForeignKey("image_drafts.id"), nullable=False)
    material_id: Mapped[int] = mapped_column(Integer, ForeignKey("materials.id"), nullable=False)
    record_id: Mapped[int] = mapped_column(Integer, ForeignKey("user_records.id"), nullable=False)


class ReviewActivity(Base):
    __tablename__ = "review_activities"
    __table_args__ = (Index("ix_review_activities_user_id_week_key", "user_id", "week_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    week_key: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    draft_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    material_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
