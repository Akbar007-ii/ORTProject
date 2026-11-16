"""Notifications, universities, media and content import."""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from mysite.db.database import Base


class NotificationPreference(Base):
    """Off until the student turns it on (no mailings without consent)."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    telegram_enabled: Mapped[bool] = mapped_column(default=False)
    reminder_hour: Mapped[int] = mapped_column(default=18)  # local hour of the daily reminder
    quiet_start: Mapped[int] = mapped_column(default=22)  # local hours when nothing is sent
    quiet_end: Mapped[int] = mapped_column(default=8)
    weekly_report: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NotificationLog(Base):
    """One row per message per local day: the worker can run any number of times safely."""

    __tablename__ = "notification_log"
    __table_args__ = (UniqueConstraint("user_id", "kind", "local_date"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # daily | weekly
    local_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | sent | failed | blocked
    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class University(Base):
    __tablename__ = "universities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title_ky: Mapped[str] = mapped_column(String(200))
    title_ru: Mapped[str] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(default=True)


class UniversityProgram(Base):
    """A specialty and its past threshold for a state-funded place: a guide, never a guarantee."""

    __tablename__ = "university_programs"
    __table_args__ = (
        UniqueConstraint("university_id", "code", "year"),
        Index("ix_university_programs_university", "university_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    university_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("universities.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(32))
    title_ky: Mapped[str] = mapped_column(String(200))
    title_ru: Mapped[str] = mapped_column(String(200))
    year: Mapped[int]
    threshold_score: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(255))  # where the number comes from
    active: Mapped[bool] = mapped_column(default=True)


class Media(Base):
    """An uploaded image. Stored on disk under its id; referenced from question text as /media/<id>."""

    __tablename__ = "media"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    content_type: Mapped[str] = mapped_column(String(32))
    size: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportJob(Base):
    __tablename__ = "import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    file_hash: Mapped[str] = mapped_column(String(64))
    dry_run: Mapped[bool]
    status: Mapped[str] = mapped_column(String(16))  # ok | failed
    total: Mapped[int] = mapped_column(default=0)
    created: Mapped[int] = mapped_column(default=0)
    skipped: Mapped[int] = mapped_column(default=0)
    errors: Mapped[list] = mapped_column(JSONB, default=list)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
