import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mysite.db.database import Base


class PublishStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


class ScaleKind(StrEnum):
    ESTIMATED = "estimated"  # working estimate, UI must say "approximate"
    OFFICIAL = "official"  # confirmed against the exam authority


class TestKind(StrEnum):
    __test__ = False  # not a pytest test class
    MOCK = "mock"  # full exam built strictly by an exam format
    SECTION = "section"
    MINI = "mini"
    DIAGNOSTIC = "diagnostic"
    SUBJECT = "subject"


class SessionStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    FINISHED = "finished"


class FinishReason(StrEnum):
    SUBMITTED = "submitted"
    DEADLINE = "deadline"


class CheckpointKind(StrEnum):
    BASELINE = "baseline"
    CONTROL = "control"
    FINAL = "final"
    SELF_REPORTED = "self_reported"


_PUBLISHED = text("status = 'published'")


class ExamFormat(Base):
    __tablename__ = "exam_formats"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title_ky: Mapped[str] = mapped_column(String(120))
    title_ru: Mapped[str] = mapped_column(String(120))


class ExamFormatVersion(Base):
    """Blueprint of the exam: sections, question counts, time limits. Configurable, versioned."""

    __tablename__ = "exam_format_versions"
    __table_args__ = (
        UniqueConstraint("format_id", "version"),
        Index("uq_exam_format_versions_published", "format_id", unique=True, postgresql_where=_PUBLISHED),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    format_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exam_formats.id", ondelete="RESTRICT"))
    version: Mapped[int]
    status: Mapped[str] = mapped_column(String(16), default=PublishStatus.DRAFT)
    rules: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    format: Mapped[ExamFormat] = relationship(lazy="joined")
    sections: Mapped[list["ExamSection"]] = relationship(lazy="selectin", order_by="ExamSection.order")


class ExamSection(Base):
    __tablename__ = "exam_sections"
    __table_args__ = (UniqueConstraint("format_version_id", "code"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    format_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam_format_versions.id", ondelete="CASCADE")
    )
    code: Mapped[str] = mapped_column(String(32))
    title_ky: Mapped[str] = mapped_column(String(120))
    title_ru: Mapped[str] = mapped_column(String(120))
    question_count: Mapped[int]
    time_limit_sec: Mapped[int]
    order: Mapped[int] = mapped_column(default=0)


class ScoreScale(Base):
    """Conversion of raw section scores to exam points. Results keep the scale they were scored with."""

    __tablename__ = "score_scales"
    __table_args__ = (
        UniqueConstraint("format_version_id", "version"),
        Index("uq_score_scales_published", "format_version_id", unique=True, postgresql_where=_PUBLISHED),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    format_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam_format_versions.id", ondelete="RESTRICT")
    )
    version: Mapped[int]
    kind: Mapped[str] = mapped_column(String(16), default=ScaleKind.ESTIMATED)
    status: Mapped[str] = mapped_column(String(16), default=PublishStatus.DRAFT)
    source: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    points: Mapped[list["ScoreScalePoint"]] = relationship(
        lazy="selectin", order_by="ScoreScalePoint.raw_value", cascade="all, delete-orphan"
    )


class ScoreScalePoint(Base):
    __tablename__ = "score_scale_points"
    __table_args__ = (UniqueConstraint("scale_id", "section_id", "raw_value"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scale_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("score_scales.id", ondelete="CASCADE"))
    section_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exam_sections.id", ondelete="CASCADE"))
    raw_value: Mapped[int]
    scaled_value: Mapped[float] = mapped_column(Float)


class Test(Base):
    """A fixed set of questions. Published tests are frozen; a fix is a new version."""

    __test__ = False  # not a pytest test class

    __tablename__ = "tests"
    __table_args__ = (
        UniqueConstraint("code", "version"),
        Index("ix_tests_status_kind", "status", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(default=1)
    title_ky: Mapped[str] = mapped_column(String(160))
    title_ru: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(16))
    language: Mapped[str] = mapped_column(String(2))
    subject_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("subjects.id", ondelete="RESTRICT"))
    format_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("exam_format_versions.id", ondelete="RESTRICT")
    )
    time_limit_sec: Mapped[int | None]  # for tests without sections
    status: Mapped[str] = mapped_column(String(16), default=PublishStatus.DRAFT)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    questions: Mapped[list["TestQuestion"]] = relationship(
        lazy="selectin", order_by="TestQuestion.position", cascade="all, delete-orphan"
    )


class TestQuestion(Base):
    __test__ = False  # not a pytest test class
    __tablename__ = "test_questions"
    __table_args__ = (
        UniqueConstraint("test_id", "position"),
        UniqueConstraint("test_id", "question_version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    test_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"))
    question_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question_versions.id", ondelete="RESTRICT")
    )
    section_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("exam_sections.id", ondelete="RESTRICT"))
    position: Mapped[int]
    points: Mapped[int] = mapped_column(default=1)


class TestSession(Base):
    """One attempt of one user at one test. The server owns its time and its score."""

    __test__ = False  # not a pytest test class

    __tablename__ = "test_sessions"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key"),
        # At most one unfinished attempt per test, so a repeated "start" cannot create a duplicate.
        Index(
            "uq_test_sessions_in_progress",
            "user_id",
            "test_id",
            unique=True,
            postgresql_where=text("status = 'in_progress'"),
        ),
        Index("ix_test_sessions_user_started", "user_id", "started_at"),
        Index("ix_test_sessions_status_deadline", "status", "deadline_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    test_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tests.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(16), default=SessionStatus.IN_PROGRESS)
    finish_reason: Mapped[str | None] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # hard upper bound
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_score: Mapped[int | None]
    max_score: Mapped[int | None]
    scaled_score: Mapped[float | None] = mapped_column(Float)
    score_scale_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("score_scales.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64))


class SessionSection(Base):
    __tablename__ = "session_sections"
    __table_args__ = (UniqueConstraint("session_id", "section_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("test_sessions.id", ondelete="CASCADE"))
    section_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("exam_sections.id", ondelete="RESTRICT"))
    order: Mapped[int]
    time_limit_sec: Mapped[int]
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_score: Mapped[int | None]
    max_score: Mapped[int | None]
    scaled_score: Mapped[float | None] = mapped_column(Float)


class SessionQuestion(Base):
    """Snapshot of a question inside a session plus the student's answer to it."""

    __tablename__ = "session_questions"
    __table_args__ = (UniqueConstraint("session_id", "position"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("test_sessions.id", ondelete="CASCADE"))
    question_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question_versions.id", ondelete="RESTRICT")
    )
    section_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("exam_sections.id", ondelete="RESTRICT"))
    position: Mapped[int]
    points: Mapped[int] = mapped_column(default=1)
    snapshot: Mapped[dict] = mapped_column(JSONB)  # stem + options, never the correct answer
    selected_option_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("question_options.id", ondelete="RESTRICT")
    )
    marked: Mapped[bool] = mapped_column(Boolean, default=False)
    seq: Mapped[int] = mapped_column(default=0)  # last applied client sequence number
    time_spent_sec: Mapped[int] = mapped_column(default=0)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_correct: Mapped[bool | None]  # set at finalization; NULL = skipped


class Checkpoint(Base):
    """A measured point on the student's score trajectory (mock exam result)."""

    __tablename__ = "checkpoints"
    __table_args__ = (Index("ix_checkpoints_user_taken", "user_id", "taken_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_sessions.id", ondelete="CASCADE"), unique=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    format_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("exam_format_versions.id", ondelete="RESTRICT")
    )
    total_raw: Mapped[int]
    max_raw: Mapped[int]
    total_scaled: Mapped[float | None] = mapped_column(Float)
    sections: Mapped[list] = mapped_column(JSONB, default=list)
    score_scale_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("score_scales.id", ondelete="RESTRICT")
    )
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
