"""The student's own data: export everything, or delete the account."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.core import mailer, security
from mysite.core.errors import unauthorized, unprocessable
from mysite.db.models import (
    AiUsageLog,
    Attempt,
    Checkpoint,
    GoalMilestone,
    MistakeCard,
    QuestionGroup,
    RoleCode,
    SkillProgress,
    TelegramAccount,
    TestSession,
    User,
    UserAchievement,
    UserConsent,
    UserGoal,
)
from mysite.db.models.extra import NotificationPreference
from mysite.services import audit


def _row(obj: Any, *fields: str) -> dict:
    out = {}
    for field in fields:
        value = getattr(obj, field)
        out[field] = (
            value.isoformat()
            if hasattr(value, "isoformat")
            else (str(value) if isinstance(value, uuid.UUID) else value)
        )
    return out


async def export(db: AsyncSession, user: User) -> dict:
    """Everything stored about the student, in one JSON document."""

    async def rows(model, *where, order=None):
        query = select(model).where(*where)
        if order is not None:
            query = query.order_by(order)
        return (await db.scalars(query)).all()

    codes = dict((await db.execute(select(QuestionGroup.id, QuestionGroup.code))).all())
    goals = await rows(UserGoal, UserGoal.user_id == user.id, order=UserGoal.created_at)
    goal_ids = [g.id for g in goals]
    milestones = await rows(GoalMilestone, GoalMilestone.goal_id.in_(goal_ids), order=GoalMilestone.score)
    prefs = await db.get(NotificationPreference, user.id)
    telegram = await db.get(TelegramAccount, user.id)

    data = {
        "exported_at": datetime.now(UTC).isoformat(),
        "profile": _row(
            user, "id", "email", "display_name", "locale", "timezone", "grade_status", "created_at"
        ),
        "roles": sorted(user.role_codes),
        "consents": [
            _row(c, "type", "document_version", "accepted_at", "revoked_at")
            for c in await rows(UserConsent, UserConsent.user_id == user.id, order=UserConsent.accepted_at)
        ],
        "test_sessions": [
            _row(
                s,
                "id",
                "test_id",
                "status",
                "finish_reason",
                "started_at",
                "submitted_at",
                "raw_score",
                "max_score",
                "scaled_score",
            )
            for s in await rows(TestSession, TestSession.user_id == user.id, order=TestSession.started_at)
        ],
        "checkpoints": [
            _row(c, "kind", "total_raw", "max_raw", "total_scaled", "sections", "taken_at")
            for c in await rows(Checkpoint, Checkpoint.user_id == user.id, order=Checkpoint.taken_at)
        ],
        "answers": [
            {
                **_row(a, "source", "is_correct", "time_spent_sec", "answered_at"),
                "question_code": codes.get(a.question_group_id),
            }
            for a in await rows(Attempt, Attempt.user_id == user.id, order=Attempt.answered_at)
        ],
        "mistake_cards": [
            {
                **_row(
                    c,
                    "status",
                    "source",
                    "reason",
                    "error_count",
                    "consecutive_successes",
                    "due_at",
                    "created_at",
                ),
                "question_code": codes.get(c.question_group_id),
            }
            for c in await rows(MistakeCard, MistakeCard.user_id == user.id, order=MistakeCard.created_at)
        ],
        "skill_progress": [
            _row(p, "skill_id", "attempts", "correct", "last_attempt_at", "mastered_at")
            for p in await rows(SkillProgress, SkillProgress.user_id == user.id)
        ],
        "goals": [
            {
                **_row(
                    g, "start_score", "target_score", "exam_date", "minutes_per_day", "status", "created_at"
                ),
                "milestones": [_row(m, "score", "reached_at") for m in milestones if m.goal_id == g.id],
            }
            for g in goals
        ],
        "achievements": [
            _row(a, "code", "ref", "earned_at")
            for a in await rows(
                UserAchievement, UserAchievement.user_id == user.id, order=UserAchievement.earned_at
            )
        ],
        "ai_tutor_requests": await db.scalar(
            select(func.count()).select_from(AiUsageLog).where(AiUsageLog.user_id == user.id)
        ),
        "telegram_linked": telegram is not None,
        "notifications": _row(
            prefs, "telegram_enabled", "reminder_hour", "quiet_start", "quiet_end", "weekly_report"
        )
        if prefs
        else None,
    }
    audit.record(db, user.id, "account.export", "user", user.id)
    await db.commit()
    return data


async def delete_account(db: AsyncSession, user: User, password: str) -> None:
    """Erases the account and everything tied to it. Cannot be undone.

    Answers, cards, goals, sessions and the Telegram link are removed with the user row
    (ON DELETE CASCADE); content the user wrote as staff keeps existing without an author.
    """
    if not await security.verify_password(user.password_hash, password):
        raise unauthorized("invalid_credentials", "Wrong password")
    if RoleCode.ADMIN in user.role_codes:
        raise unprocessable("admin_cannot_delete", "Remove your admin role before deleting the account")

    email, locale, user_id = user.email, user.locale, user.id
    audit.record(db, user_id, "account.delete", "user", user_id)
    await db.flush()
    await db.execute(delete(User).where(User.id == user_id))
    await db.commit()
    if email:
        await mailer.send_account_deleted(email, locale)
