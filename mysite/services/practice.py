"""Topic practice: no timer, the verified answer and solution right after each question."""

import random
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.config import get_settings
from mysite.core.errors import conflict, not_found, unprocessable
from mysite.db.models import (
    Attempt,
    AttemptSource,
    LearningPlan,
    PlanItem,
    PlanItemStatus,
    PracticeQuestion,
    PracticeSession,
    QuestionGroup,
    QuestionStatus,
    QuestionVersion,
    Skill,
    Topic,
    User,
)
from mysite.db.schema.learning import FeedbackOut, PracticeOut, PracticeQuestionOut, PracticeStartIn
from mysite.services import learning


def options_out(version: QuestionVersion) -> list[dict]:
    """Options as the student sees them: never with the correct flag."""
    return [{"id": o.id, "key": o.key, "text": o.text} for o in version.options]


def correct_option_id(version: QuestionVersion) -> uuid.UUID:
    return next(o.id for o in version.options if o.is_correct)


async def _skill_ids(db: AsyncSession, data: PracticeStartIn) -> list[uuid.UUID]:
    if data.skill_id:
        skill = await db.get(Skill, data.skill_id)
        if skill is None or not skill.active:
            raise unprocessable("invalid_skill", "Skill not found")
        return [skill.id]
    topic = await db.get(Topic, data.topic_id)
    if topic is None or not topic.active:
        raise unprocessable("invalid_topic", "Topic not found")
    # The topic together with all its subtopics.
    siblings = (await db.scalars(select(Topic).where(Topic.subject_id == topic.subject_id))).all()
    wanted, frontier = {topic.id}, [topic.id]
    while frontier:
        parent = frontier.pop()
        for child in siblings:
            if child.parent_id == parent and child.active and child.id not in wanted:
                wanted.add(child.id)
                frontier.append(child.id)
    rows = await db.scalars(select(Skill.id).where(Skill.topic_id.in_(wanted), Skill.active.is_(True)))
    return list(rows.all())


async def start(db: AsyncSession, user: User, data: PracticeStartIn) -> PracticeOut:
    skill_ids = await _skill_ids(db, data)
    last_seen = (
        select(Attempt.question_group_id, func.max(Attempt.answered_at).label("seen_at"))
        .where(Attempt.user_id == user.id)
        .group_by(Attempt.question_group_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(QuestionVersion.id, last_seen.c.seen_at)
            .join(QuestionGroup, QuestionGroup.id == QuestionVersion.group_id)
            .outerjoin(last_seen, last_seen.c.question_group_id == QuestionGroup.id)
            .where(
                QuestionGroup.skill_id.in_(skill_ids),
                QuestionVersion.status == QuestionStatus.PUBLISHED,
                QuestionVersion.language == user.locale,
            )
        )
    ).all()
    if not rows:
        raise unprocessable("no_questions", "There are no questions for this topic yet")

    # New questions first, then the ones seen longest ago; random among equals.
    shuffled = random.sample(rows, len(rows))
    shuffled.sort(key=lambda row: (row.seen_at is not None, row.seen_at or datetime.min.replace(tzinfo=UTC)))
    chosen = [row.id for row in shuffled[: data.count]]

    if data.plan_item_id:
        await _own_plan_item(db, user.id, data.plan_item_id)
    session = PracticeSession(
        id=uuid.uuid4(),
        user_id=user.id,
        skill_id=data.skill_id,
        topic_id=data.topic_id,
        plan_item_id=data.plan_item_id,
    )
    db.add(session)
    await db.flush()
    db.add_all(
        PracticeQuestion(session_id=session.id, question_version_id=version_id, position=i)
        for i, version_id in enumerate(chosen, start=1)
    )
    await db.commit()
    return await get(db, user, session.id)


async def _own_plan_item(db: AsyncSession, user_id: uuid.UUID, item_id: uuid.UUID) -> PlanItem:
    item = await db.scalar(
        select(PlanItem)
        .join(LearningPlan, LearningPlan.id == PlanItem.plan_id)
        .where(PlanItem.id == item_id, LearningPlan.user_id == user_id)
    )
    if item is None:
        raise unprocessable("invalid_plan_item", "Plan item not found")
    return item


async def _load(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, *, lock: bool
) -> PracticeSession:
    query = select(PracticeSession).where(
        PracticeSession.id == session_id, PracticeSession.user_id == user_id
    )
    if lock:
        query = query.with_for_update()
    session = await db.scalar(query.execution_options(populate_existing=True))
    if session is None:
        raise not_found("Practice session not found")
    return session


async def _versions(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, QuestionVersion]:
    rows = await db.scalars(select(QuestionVersion).where(QuestionVersion.id.in_(ids)))
    return {v.id: v for v in rows.unique()}


def _feedback(question: PracticeQuestion, version: QuestionVersion) -> FeedbackOut:
    return FeedbackOut(
        is_correct=question.is_correct,
        selected_option_id=question.selected_option_id,
        correct_option_id=correct_option_id(version),
        solution=version.solution,
    )


async def get(db: AsyncSession, user: User, session_id: uuid.UUID) -> PracticeOut:
    session = await _load(db, user.id, session_id, lock=False)
    questions = (
        await db.scalars(
            select(PracticeQuestion)
            .where(PracticeQuestion.session_id == session.id)
            .order_by(PracticeQuestion.position)
            .execution_options(populate_existing=True)
        )
    ).all()
    versions = await _versions(db, [q.question_version_id for q in questions])
    answered = [q for q in questions if q.answered_at]
    return PracticeOut(
        id=session.id,
        status=session.status,
        skill_id=session.skill_id,
        topic_id=session.topic_id,
        total=len(questions),
        answered=len(answered),
        correct=sum(bool(q.is_correct) for q in answered),
        questions=[
            PracticeQuestionOut(
                id=q.id,
                position=q.position,
                question_version_id=q.question_version_id,
                skill_id=versions[q.question_version_id].group.skill_id,
                stem=versions[q.question_version_id].stem,
                options=options_out(versions[q.question_version_id]),
                # The key appears only for questions that are already answered.
                feedback=_feedback(q, versions[q.question_version_id]) if q.answered_at else None,
            )
            for q in questions
        ],
    )


async def answer(
    db: AsyncSession,
    user: User,
    session_id: uuid.UUID,
    question_id: uuid.UUID,
    option_id: uuid.UUID,
    time_spent: int,
) -> FeedbackOut:
    session = await _load(db, user.id, session_id, lock=True)
    question = await db.scalar(
        select(PracticeQuestion).where(
            PracticeQuestion.id == question_id, PracticeQuestion.session_id == session.id
        )
    )
    if question is None:
        raise not_found("Question not found in this session")
    version = (await _versions(db, [question.question_version_id]))[question.question_version_id]

    if question.answered_at:
        # One answer per question: a retry gets the same feedback and is not counted twice.
        stored = _feedback(question, version)
        await db.rollback()  # release the row lock; objects are unusable after this
        return stored
    if session.status != "in_progress":
        raise conflict("practice_finished", "This practice session is finished")
    if option_id not in {o.id for o in version.options}:
        raise unprocessable("invalid_option", "This option does not belong to the question")

    now = datetime.now(UTC)
    question.selected_option_id = option_id
    question.is_correct = option_id == correct_option_id(version)
    question.time_spent_sec = min(time_spent, get_settings().max_question_time_sec)
    question.answered_at = now
    await learning.record_attempts(
        db,
        user,
        [
            learning.Answered(
                question_version_id=version.id,
                question_group_id=version.group_id,
                skill_id=version.group.skill_id,
                selected_option_id=option_id,
                is_correct=question.is_correct,
                time_spent_sec=question.time_spent_sec,
            )
        ],
        AttemptSource.PRACTICE,
        session.id,
        now,
    )
    await db.commit()
    return _feedback(question, version)


async def finish(db: AsyncSession, user: User, session_id: uuid.UUID) -> PracticeOut:
    session = await _load(db, user.id, session_id, lock=True)
    if session.status == "in_progress":
        now = datetime.now(UTC)
        session.status = "finished"
        session.finished_at = now
        if session.plan_item_id:
            item = await db.get(PlanItem, session.plan_item_id)
            if item and item.status == PlanItemStatus.PENDING:
                item.status = PlanItemStatus.DONE
                item.completed_at = now
    await db.commit()
    return await get(db, user, session.id)
