"""Shared learning history: every answered question passes through `record_attempts`.

It writes the attempt, refreshes the skill's progress window and, for a wrong answer,
creates or updates the mistake card. Tests, practice and review all use it, so the site
and the Telegram bot always see one history.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.db.models import (
    Attempt,
    AttemptSource,
    CardStatus,
    MistakeCard,
    SkillProgress,
    User,
    UserAchievement,
)
from mysite.services import mastery


@dataclass
class Answered:
    question_version_id: uuid.UUID
    question_group_id: uuid.UUID
    skill_id: uuid.UUID
    selected_option_id: uuid.UUID
    is_correct: bool
    time_spent_sec: int = 0


def local_today(user: User, now: datetime | None = None) -> date:
    """The student's calendar day: streaks and plans must not break at UTC midnight."""
    return (now or datetime.now(UTC)).astimezone(ZoneInfo(user.timezone)).date()


async def award(
    db: AsyncSession, user_id: uuid.UUID, code: str, ref: str = "", context: dict | None = None
) -> None:
    """Gives an achievement once; repeating the call changes nothing."""
    await db.execute(
        insert(UserAchievement)
        .values(id=uuid.uuid4(), user_id=user_id, code=code, ref=ref, context=context)
        .on_conflict_do_nothing(index_elements=["user_id", "code", "ref"])
    )


async def register_mistake(
    db: AsyncSession, user_id: uuid.UUID, item: Answered, source: AttemptSource, now: datetime
) -> None:
    """Creates the card, or sends an existing one back to the first interval."""
    due = now + timedelta(days=mastery.REVIEW_INTERVALS_DAYS[0])
    statement = insert(MistakeCard).values(
        id=uuid.uuid4(),
        user_id=user_id,
        question_group_id=item.question_group_id,
        question_version_id=item.question_version_id,
        skill_id=item.skill_id,
        status=CardStatus.NEW,
        source=source,
        last_selected_option_id=item.selected_option_id,
        due_at=due,
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id", "question_group_id"],
            set_={
                "error_count": MistakeCard.error_count + 1,
                "consecutive_successes": 0,
                "interval_index": 0,
                "question_version_id": item.question_version_id,
                "last_selected_option_id": item.selected_option_id,
                "due_at": due,
                # A card the student archived stays archived; anything else is being learned again.
                "status": func.coalesce(
                    func.nullif(MistakeCard.status, CardStatus.MASTERED), CardStatus.LEARNING
                ),
                "updated_at": now,
            },
        )
    )
    await db.execute(
        MistakeCard.__table__.update()
        .where(
            MistakeCard.user_id == user_id,
            MistakeCard.question_group_id == item.question_group_id,
            MistakeCard.status == CardStatus.NEW,
            MistakeCard.error_count > 1,
        )
        .values(status=CardStatus.LEARNING)
    )


async def record_attempts(
    db: AsyncSession,
    user: User,
    items: list[Answered],
    source: AttemptSource,
    source_id: uuid.UUID | None,
    now: datetime,
    *,
    make_cards: bool = True,
) -> list[Attempt]:
    """Runs inside the caller's transaction."""
    if not items:
        return []
    attempts = [
        Attempt(
            id=uuid.uuid4(),
            user_id=user.id,
            question_version_id=item.question_version_id,
            question_group_id=item.question_group_id,
            skill_id=item.skill_id,
            source=source,
            source_id=source_id,
            selected_option_id=item.selected_option_id,
            is_correct=item.is_correct,
            time_spent_sec=item.time_spent_sec,
            answered_at=now,
        )
        for item in items
    ]
    db.add_all(attempts)
    await db.flush()

    if make_cards:
        for item in items:
            if not item.is_correct:
                await register_mistake(db, user.id, item, source, now)

    for skill_id in {item.skill_id for item in items}:
        await _refresh_skill(db, user.id, skill_id, now)

    today = local_today(user, now)
    streak = mastery.current_streak(await active_days(db, user), today)
    for days in (7, 30):
        if streak >= days:
            await award(db, user.id, f"streak_{days}")
    return attempts


async def _refresh_skill(db: AsyncSession, user_id: uuid.UUID, skill_id: uuid.UUID, now: datetime) -> None:
    await db.execute(
        insert(SkillProgress).values(user_id=user_id, skill_id=skill_id).on_conflict_do_nothing()
    )
    progress = await db.scalar(
        select(SkillProgress)
        .where(SkillProgress.user_id == user_id, SkillProgress.skill_id == skill_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    totals = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(Attempt.is_correct),
                func.coalesce(func.sum(Attempt.time_spent_sec), 0),
            ).where(Attempt.user_id == user_id, Attempt.skill_id == skill_id)
        )
    ).one()
    recent = (
        await db.scalars(
            select(Attempt.is_correct)
            .where(Attempt.user_id == user_id, Attempt.skill_id == skill_id)
            .order_by(Attempt.answered_at.desc(), Attempt.id)
            .limit(mastery.WINDOW)
        )
    ).all()
    progress.attempts, progress.correct, progress.total_time_sec = totals
    progress.recent_total = len(recent)
    progress.recent_correct = sum(recent)
    progress.last_attempt_at = now
    if progress.mastered_at is None and mastery.is_mastered(progress.recent_correct, progress.recent_total):
        progress.mastered_at = now
        await award(db, user_id, "skill_mastered", str(skill_id))


async def active_days(db: AsyncSession, user: User, days_back: int = 400) -> set[date]:
    """Local calendar days on which the student answered at least one question."""
    since = datetime.now(UTC) - timedelta(days=days_back)
    local_day = func.date(func.timezone(user.timezone, Attempt.answered_at))
    rows = await db.scalars(
        select(local_day).where(Attempt.user_id == user.id, Attempt.answered_at >= since).distinct()
    )
    return set(rows.all())
