"""Learning loop tables: attempts, skill progress, practice, mistake cards, review,
goals, plans, predictions, achievements, tutor and Telegram."""

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from mysite.db.database import Base


class AttemptSource(StrEnum):
    TEST = "test"
    PRACTICE = "practice"
    REVIEW = "review"


class CardStatus(StrEnum):
    NEW = "new"
    LEARNING = "learning"
    MASTERED = "mastered"
    SUSPENDED = "suspended"


class MistakeReason(StrEnum):
    UNKNOWN_TOPIC = "unknown_topic"
    CALCULATION = "calculation"
    INATTENTION = "inattention"
    MISREAD = "misread"
    NO_TIME = "no_time"
    GUESSED = "guessed"
    OTHER = "other"


class ReviewOutcome(StrEnum):
    SUCCESS = "success"
    FAIL = "fail"
    SNOOZE = "snooze"


class PlanItemKind(StrEnum):
    PRACTICE = "practice"
    REVIEW = "review"
    MOCK = "mock"


class PlanItemStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"


class TutorMode(StrEnum):
    SHORT = "short"
    DETAILED = "detailed"
    EXAMPLE = "example"


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _user() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("users.id", ondelete="CASCADE"))


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Attempt(Base):
    """One answered question, from any source. The single history all statistics read."""

    __tablename__ = "attempts"
    __table_args__ = (
        Index("ix_attempts_user_answered", "user_id", "answered_at"),
        Index("ix_attempts_user_skill", "user_id", "skill_id", "answered_at"),
    )

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user()
    question_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question_versions.id", ondelete="RESTRICT")
    )
    question_group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question_groups.id", ondelete="RESTRICT")
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skills.id", ondelete="RESTRICT"))
    source: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # test session / practice session / card
    selected_option_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    is_correct: Mapped[bool]
    time_spent_sec: Mapped[int] = mapped_column(default=0)
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SkillProgress(Base):
    """Rolling window of the student's last answers on a skill (see services/mastery.py)."""

    __tablename__ = "skill_progress"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="RESTRICT"), primary_key=True
    )
    attempts: Mapped[int] = mapped_column(default=0)
    correct: Mapped[int] = mapped_column(default=0)
    recent_total: Mapped[int] = mapped_column(default=0)  # size of the window (max 10)
    recent_correct: Mapped[int] = mapped_column(default=0)
    total_time_sec: Mapped[int] = mapped_column(default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mastered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PracticeSession(Base):
    __tablename__ = "practice_sessions"
    __table_args__ = (Index("ix_practice_sessions_user", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user()
    skill_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("skills.id", ondelete="RESTRICT"))
    topic_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("topics.id", ondelete="RESTRICT"))
    plan_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(16), default="in_progress")
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PracticeQuestion(Base):
    __tablename__ = "practice_questions"
    __table_args__ = (UniqueConstraint("session_id", "position"),)

    id: Mapped[uuid.UUID] = _id()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("practice_sessions.id", ondelete="CASCADE"))
    question_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question_versions.id", ondelete="RESTRICT")
    )
    position: Mapped[int]
    selected_option_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    is_correct: Mapped[bool | None]
    time_spent_sec: Mapped[int] = mapped_column(default=0)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MistakeCard(Base):
    """One card per student and task. Repeated mistakes update it instead of duplicating it."""

    __tablename__ = "mistake_cards"
    __table_args__ = (
        UniqueConstraint("user_id", "question_group_id"),
        Index("ix_mistake_cards_user_status_due", "user_id", "status", "due_at"),
    )

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user()
    question_group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question_groups.id", ondelete="RESTRICT")
    )
    question_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question_versions.id", ondelete="RESTRICT")
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skills.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(16), default=CardStatus.NEW)
    source: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(16))  # the student's own guess, never shown publicly
    error_count: Mapped[int] = mapped_column(default=1)
    consecutive_successes: Mapped[int] = mapped_column(default=0)
    interval_index: Mapped[int] = mapped_column(default=0)
    last_selected_option_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    last_success_on: Mapped[date | None] = mapped_column(Date)  # in the student's timezone
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # UTC; NULL = not scheduled
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReviewEvent(Base):
    __tablename__ = "review_events"
    __table_args__ = (
        UniqueConstraint("card_id", "idempotency_key"),
        Index("ix_review_events_card_reviewed", "card_id", "reviewed_at"),
    )

    id: Mapped[uuid.UUID] = _id()
    card_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mistake_cards.id", ondelete="CASCADE"))
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("attempts.id", ondelete="SET NULL"))
    outcome: Mapped[str] = mapped_column(String(16))
    interval_before: Mapped[int]  # days
    interval_after: Mapped[int]
    policy_version: Mapped[int]
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict | None] = mapped_column(JSONB)  # response replayed for a repeated request
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SkillSectionWeight(Base):
    """How many questions of an exam section a skill is expected to bring (set by experts)."""

    __tablename__ = "skill_section_weights"

    skill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True)
    exam_section_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam_sections.id", ondelete="CASCADE"), primary_key=True
    )
    expected_questions: Mapped[float] = mapped_column(Float)


class UserGoal(Base):
    __tablename__ = "user_goals"
    __table_args__ = (
        Index("uq_user_goals_active", "user_id", unique=True, postgresql_where=text("status = 'active'")),
    )

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user()
    format_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exam_format_versions.id", ondelete="RESTRICT")
    )
    start_score: Mapped[float] = mapped_column(Float)  # score when the goal was set
    target_score: Mapped[float] = mapped_column(Float)
    exam_date: Mapped[date] = mapped_column(Date)
    minutes_per_day: Mapped[int]
    university_program_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("university_programs.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = _ts()


class GoalMilestone(Base):
    __tablename__ = "goal_milestones"
    __table_args__ = (UniqueConstraint("goal_id", "score"),)

    id: Mapped[uuid.UUID] = _id()
    goal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_goals.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float)
    reached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScorePrediction(Base):
    """Stored forecasts, so they can later be compared with real mock results."""

    __tablename__ = "score_predictions"
    __table_args__ = (Index("ix_score_predictions_user_computed", "user_id", "computed_at"),)

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user()
    low: Mapped[float] = mapped_column(Float)
    point: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    confidence: Mapped[str] = mapped_column(String(8))
    algorithm_version: Mapped[int]
    computed_on: Mapped[date] = mapped_column(Date)
    computed_at: Mapped[datetime] = _ts()


class LearningPlan(Base):
    __tablename__ = "learning_plans"
    __table_args__ = (
        Index("uq_learning_plans_active", "goal_id", unique=True, postgresql_where=text("status = 'active'")),
    )

    id: Mapped[uuid.UUID] = _id()
    goal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_goals.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = _user()
    version: Mapped[int]
    status: Mapped[str] = mapped_column(String(16), default="active")
    inputs_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()


class PlanItem(Base):
    __tablename__ = "plan_items"
    __table_args__ = (Index("ix_plan_items_plan_day", "plan_id", "day"),)

    id: Mapped[uuid.UUID] = _id()
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("learning_plans.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)  # in the student's timezone
    position: Mapped[int]
    kind: Mapped[str] = mapped_column(String(16))
    skill_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("skills.id", ondelete="RESTRICT"))
    minutes: Mapped[int]
    expected_gain: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(16), default=PlanItemStatus.PENDING)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserAchievement(Base):
    __tablename__ = "user_achievements"
    __table_args__ = (UniqueConstraint("user_id", "code", "ref"),)

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user()
    code: Mapped[str] = mapped_column(String(32))
    ref: Mapped[str] = mapped_column(String(64), default="")  # e.g. skill id or milestone score
    context: Mapped[dict | None] = mapped_column(JSONB)
    earned_at: Mapped[datetime] = _ts()


class TutorExplanation(Base):
    """Cache of AI explanations. The key holds no personal data."""

    __tablename__ = "tutor_explanations"
    __table_args__ = (
        UniqueConstraint("question_version_id", "locale", "mode", "wrong_option_key", "prompt_version"),
    )

    id: Mapped[uuid.UUID] = _id()
    question_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question_versions.id", ondelete="CASCADE")
    )
    locale: Mapped[str] = mapped_column(String(2))
    mode: Mapped[str] = mapped_column(String(16))
    wrong_option_key: Mapped[str] = mapped_column(String(1), default="")
    prompt_version: Mapped[int]
    model: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class AiUsageLog(Base):
    __tablename__ = "ai_usage_logs"
    __table_args__ = (Index("ix_ai_usage_logs_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user()
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[int]
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    latency_ms: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16))  # ok | error
    created_at: Mapped[datetime] = _ts()


class QuestionReport(Base):
    """A student's complaint about a question or an explanation. Goes to the content queue."""

    __tablename__ = "question_reports"
    __table_args__ = (Index("ix_question_reports_status", "status", "created_at"),)

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    question_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question_versions.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16))  # question | explanation
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = _ts()


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    linked_at: Mapped[datetime] = _ts()


class TelegramLinkToken(Base):
    __tablename__ = "telegram_link_tokens"

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = _user()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
