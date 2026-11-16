"""Mistake journal and Smart Review (spaced repetition of the student's own mistakes)."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.config import get_settings
from mysite.core.errors import conflict, not_found, unprocessable
from mysite.db.models import (
    AttemptSource,
    CardStatus,
    MistakeCard,
    QuestionStatus,
    QuestionVersion,
    ReviewEvent,
    ReviewOutcome,
    User,
)
from mysite.db.schema.learning import (
    CardOut,
    CardUpdateIn,
    ReviewCardOut,
    ReviewQueueOut,
    ReviewResultOut,
)
from mysite.services import learning, mastery
from mysite.services.practice import correct_option_id, options_out

ACTIVE = (CardStatus.NEW, CardStatus.LEARNING)
MINUTES_PER_CARD = 1.5


async def _version_for(db: AsyncSession, card: MistakeCard, locale: str) -> QuestionVersion | None:
    """The version to show now. A withdrawn question is replaced by its published successor;
    if there is none, the card silently drops out of review."""
    version = await db.scalar(select(QuestionVersion).where(QuestionVersion.id == card.question_version_id))
    if version is not None and version.status == QuestionStatus.PUBLISHED:
        return version
    language = version.language if version else locale
    return await db.scalar(
        select(QuestionVersion).where(
            QuestionVersion.group_id == card.question_group_id,
            QuestionVersion.language == language,
            QuestionVersion.status == QuestionStatus.PUBLISHED,
        )
    )


async def due_count(db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(MistakeCard)
            .where(
                MistakeCard.user_id == user_id,
                MistakeCard.status.in_(ACTIVE),
                MistakeCard.due_at <= (now or datetime.now(UTC)),
            )
        )
        or 0
    )


async def queue(db: AsyncSession, user: User, limit: int = 50) -> ReviewQueueOut:
    """Cards whose time has come, oldest first. Overdue cards stay available."""
    now = datetime.now(UTC)
    cards = (
        await db.scalars(
            select(MistakeCard)
            .where(MistakeCard.user_id == user.id, MistakeCard.status.in_(ACTIVE), MistakeCard.due_at <= now)
            .order_by(MistakeCard.due_at, MistakeCard.error_count.desc())
            .limit(limit)
        )
    ).all()
    items = []
    for card in cards:
        version = await _version_for(db, card, user.locale)
        if version is None:
            continue
        items.append(
            ReviewCardOut(
                card_id=card.id,
                skill_id=card.skill_id,
                stem=version.stem,
                options=options_out(version),
                error_count=card.error_count,
                due_at=card.due_at,
                interval_days=mastery.REVIEW_INTERVALS_DAYS[card.interval_index],
            )
        )
    return ReviewQueueOut(due=len(items), estimated_minutes=round(len(items) * MINUTES_PER_CARD), items=items)


async def _load(db: AsyncSession, user_id: uuid.UUID, card_id: uuid.UUID) -> MistakeCard:
    card = await db.scalar(
        select(MistakeCard)
        .where(MistakeCard.id == card_id, MistakeCard.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if card is None:
        raise not_found("Card not found")
    return card


async def answer(
    db: AsyncSession,
    user: User,
    card_id: uuid.UUID,
    option_id: uuid.UUID,
    time_spent: int,
    idempotency_key: str | None,
) -> ReviewResultOut:
    card = await _load(db, user.id, card_id)
    if idempotency_key:
        previous = await db.scalar(
            select(ReviewEvent).where(
                ReviewEvent.card_id == card.id, ReviewEvent.idempotency_key == idempotency_key
            )
        )
        if previous is not None:
            # The same request again (lost response, double tap): same answer, no second step.
            stored = ReviewResultOut.model_validate(previous.result)
            await db.rollback()  # release the row lock; objects are unusable after this
            return stored

    now = datetime.now(UTC)
    if card.status not in ACTIVE or card.due_at is None or card.due_at > now:
        raise conflict("card_not_due", "This card is not due for review now")
    version = await _version_for(db, card, user.locale)
    if version is None:
        raise conflict("card_unavailable", "The question of this card was withdrawn")
    if option_id not in {o.id for o in version.options}:
        raise unprocessable("invalid_option", "This option does not belong to the question")

    is_correct = option_id == correct_option_id(version)
    interval_before = mastery.REVIEW_INTERVALS_DAYS[card.interval_index]
    if is_correct:
        state = mastery.after_success(
            card.interval_index,
            card.consecutive_successes,
            card.last_success_on,
            now,
            learning.local_today(user, now),
        )
    else:
        state = mastery.after_failure(card.last_success_on, now)
        card.error_count += 1

    card.interval_index = state.interval_index
    card.consecutive_successes = state.consecutive_successes
    card.last_success_on = state.last_success_on
    card.due_at = state.due_at
    card.status = CardStatus.MASTERED if state.mastered else CardStatus.LEARNING
    card.question_version_id = version.id
    card.last_selected_option_id = option_id
    card.updated_at = now

    attempts = await learning.record_attempts(
        db,
        user,
        [
            learning.Answered(
                question_version_id=version.id,
                question_group_id=version.group_id,
                skill_id=version.group.skill_id,
                selected_option_id=option_id,
                is_correct=is_correct,
                time_spent_sec=min(time_spent, get_settings().max_question_time_sec),
            )
        ],
        AttemptSource.REVIEW,
        card.id,
        now,
        make_cards=False,  # the card is updated right here, by the review rules
    )
    if state.mastered:
        mastered_total = await db.scalar(
            select(func.count())
            .select_from(MistakeCard)
            .where(MistakeCard.user_id == user.id, MistakeCard.status == CardStatus.MASTERED)
        )
        for threshold in (1, 10, 50):
            if (mastered_total or 0) >= threshold:  # the count already includes this card
                await learning.award(db, user.id, f"cards_mastered_{threshold}")

    result = ReviewResultOut(
        is_correct=is_correct,
        selected_option_id=option_id,
        correct_option_id=correct_option_id(version),
        solution=version.solution,
        card_id=card.id,
        status=CardStatus(card.status),
        consecutive_successes=card.consecutive_successes,
        next_due_at=card.due_at,
        interval_days=state.interval_days,
    )
    db.add(
        ReviewEvent(
            card_id=card.id,
            attempt_id=attempts[0].id,
            outcome=ReviewOutcome.SUCCESS if is_correct else ReviewOutcome.FAIL,
            interval_before=interval_before,
            interval_after=state.interval_days,
            policy_version=mastery.REVIEW_POLICY_VERSION,
            idempotency_key=idempotency_key,
            result=result.model_dump(mode="json"),
            reviewed_at=now,
        )
    )
    await db.commit()
    return result


async def snooze(db: AsyncSession, user: User, card_id: uuid.UUID) -> datetime:
    """Postpones a card by a day. It is recorded, but never counts as a success."""
    card = await _load(db, user.id, card_id)
    if card.status not in ACTIVE:
        raise conflict("card_not_active", "This card is not in review")
    now = datetime.now(UTC)
    interval = mastery.REVIEW_INTERVALS_DAYS[card.interval_index]
    card.due_at = now + timedelta(days=1)
    db.add(
        ReviewEvent(
            card_id=card.id,
            outcome=ReviewOutcome.SNOOZE,
            interval_before=interval,
            interval_after=interval,
            policy_version=mastery.REVIEW_POLICY_VERSION,
            reviewed_at=now,
        )
    )
    await db.commit()
    return card.due_at


# ---------- journal ----------


async def _card_out(db: AsyncSession, card: MistakeCard) -> CardOut:
    version = await db.scalar(select(QuestionVersion).where(QuestionVersion.id == card.question_version_id))
    return CardOut(
        id=card.id,
        status=CardStatus(card.status),
        source=card.source,
        reason=card.reason,
        skill_id=card.skill_id,
        question_code=version.group.code,
        stem=version.stem,
        options=options_out(version),
        selected_option_id=card.last_selected_option_id,
        correct_option_id=correct_option_id(version),
        solution=version.solution,
        error_count=card.error_count,
        consecutive_successes=card.consecutive_successes,
        due_at=card.due_at,
        created_at=card.created_at,
    )


async def list_cards(
    db: AsyncSession,
    user: User,
    *,
    status: CardStatus | None,
    skill_id: uuid.UUID | None,
    source: str | None,
    limit: int,
    offset: int,
) -> tuple[list[CardOut], int]:
    query = select(MistakeCard).where(MistakeCard.user_id == user.id)
    if status:
        query = query.where(MistakeCard.status == status)
    if skill_id:
        query = query.where(MistakeCard.skill_id == skill_id)
    if source:
        query = query.where(MistakeCard.source == source)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    cards = (
        await db.scalars(query.order_by(MistakeCard.updated_at.desc()).limit(limit).offset(offset))
    ).all()
    return [await _card_out(db, card) for card in cards], total or 0


async def update_card(db: AsyncSession, user: User, card_id: uuid.UUID, data: CardUpdateIn) -> CardOut:
    card = await _load(db, user.id, card_id)
    fields = data.model_fields_set
    if "reason" in fields:
        card.reason = data.reason
    if data.archived is True and card.status != CardStatus.MASTERED:
        card.status = CardStatus.SUSPENDED
    if data.archived is False and card.status == CardStatus.SUSPENDED:
        card.status = CardStatus.LEARNING
        card.due_at = datetime.now(UTC)
    await db.commit()
    return await _card_out(db, card)
