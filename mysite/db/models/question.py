import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mysite.db.database import Base


class QuestionStatus(StrEnum):
    DRAFT = "draft"
    EDITORIAL_REVIEW = "editorial_review"
    EXPERT_REVIEW = "expert_review"
    APPROVED = "approved"
    PUBLISHED = "published"
    RETIRED = "retired"


class QuestionGroup(Base):
    """Identity of one task. Its language versions and revisions hang off this row."""

    __tablename__ = "question_groups"
    __table_args__ = (Index("ix_question_groups_skill_id", "skill_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    # Id of the task in the source file it was imported from: re-importing the file skips it.
    external_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    skill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skills.id", ondelete="RESTRICT"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuestionVersion(Base):
    """One revision of a task in one language. Immutable once it leaves draft."""

    __tablename__ = "question_versions"
    __table_args__ = (
        UniqueConstraint("group_id", "language", "version"),
        CheckConstraint("difficulty BETWEEN 1 AND 5", name="difficulty"),
        Index("ix_question_versions_status_language", "status", "language"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("question_groups.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(default=1)
    language: Mapped[str] = mapped_column(String(2))
    stem: Mapped[str] = mapped_column(Text)  # Markdown/LaTeX, rendered safely by the client
    solution: Mapped[str | None] = mapped_column(Text)  # verified step-by-step solution
    difficulty: Mapped[int] = mapped_column(SmallInteger, default=3)
    estimated_time_sec: Mapped[int | None]
    source: Mapped[str | None] = mapped_column(String(255))
    license: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default=QuestionStatus.DRAFT)
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    group: Mapped[QuestionGroup] = relationship(lazy="joined")
    options: Mapped[list["QuestionOption"]] = relationship(
        lazy="selectin", order_by="QuestionOption.order", cascade="all, delete-orphan"
    )


class QuestionOption(Base):
    __tablename__ = "question_options"
    __table_args__ = (UniqueConstraint("question_version_id", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    question_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question_versions.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(String(1))  # A, B, C, D
    text: Mapped[str] = mapped_column(Text)
    order: Mapped[int] = mapped_column(default=0)
    is_correct: Mapped[bool] = mapped_column(default=False)


class ContentReview(Base):
    """Every workflow transition: who moved the question, from where to where, and why."""

    __tablename__ = "content_reviews"
    __table_args__ = (Index("ix_content_reviews_question_version_id", "question_version_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    question_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question_versions.id", ondelete="CASCADE")
    )
    from_status: Mapped[str] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
