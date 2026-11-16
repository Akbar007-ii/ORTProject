"""Exam Engine.

Rules that hold everywhere in this module:
- The server clock decides time. Deadlines are applied lazily, on any access to a session,
  as if they had fired exactly on time (see `_advance`).
- Every change runs under a row lock on the session, so parallel requests are serialized.
- Correct answers leave the server only in the result of a finished session.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.config import get_settings
from mysite.core.errors import conflict, not_found, unprocessable
from mysite.db.models import (
    AttemptSource,
    Checkpoint,
    CheckpointKind,
    ExamSection,
    FinishReason,
    PlanItemKind,
    PublishStatus,
    QuestionOption,
    QuestionStatus,
    QuestionVersion,
    ScaleKind,
    ScoreScale,
    SessionQuestion,
    SessionSection,
    SessionStatus,
    Test,
    TestKind,
    TestQuestion,
    TestSession,
    User,
)
from mysite.db.schema.exam import (
    AnswerIn,
    AnswerOut,
    CheckpointOut,
    ResultOut,
    ResultQuestionOut,
    ResultSectionOut,
    SessionListItem,
    SessionOut,
    SessionQuestionOut,
    SessionSectionOut,
    TestOut,
    TestSectionOut,
)
from mysite.services import goals, learning, scoring


def _now() -> datetime:
    return datetime.now(UTC)


# ---------- catalog ----------


async def _published_scale(db: AsyncSession, format_version_id: uuid.UUID | None) -> ScoreScale | None:
    if format_version_id is None:
        return None
    return await db.scalar(
        select(ScoreScale).where(
            ScoreScale.format_version_id == format_version_id, ScoreScale.status == PublishStatus.PUBLISHED
        )
    )


async def _exam_sections(db: AsyncSession, format_version_id: uuid.UUID | None) -> list[ExamSection]:
    if format_version_id is None:
        return []
    rows = await db.scalars(
        select(ExamSection)
        .where(ExamSection.format_version_id == format_version_id)
        .order_by(ExamSection.order)
    )
    return list(rows.all())


async def _test_out(db: AsyncSession, test: Test) -> TestOut:
    sections = await _exam_sections(db, test.format_version_id)
    scale = await _published_scale(db, test.format_version_id)
    return TestOut(
        id=test.id,
        code=test.code,
        title_ky=test.title_ky,
        title_ru=test.title_ru,
        kind=TestKind(test.kind),
        language=test.language,
        subject_id=test.subject_id,
        question_count=len(test.questions),
        time_limit_sec=sum(s.time_limit_sec for s in sections) if sections else test.time_limit_sec,
        sections=[
            TestSectionOut(
                code=s.code,
                title_ky=s.title_ky,
                title_ru=s.title_ru,
                question_count=s.question_count,
                time_limit_sec=s.time_limit_sec,
            )
            for s in sections
        ],
        scale_kind=ScaleKind(scale.kind) if scale else None,
    )


async def list_tests(
    db: AsyncSession, *, kind: TestKind | None, subject_id: uuid.UUID | None, language: str | None
) -> list[TestOut]:
    query = select(Test).where(Test.status == PublishStatus.PUBLISHED).order_by(Test.published_at.desc())
    if kind:
        query = query.where(Test.kind == kind)
    if subject_id:
        query = query.where(Test.subject_id == subject_id)
    if language:
        query = query.where(Test.language == language)
    return [await _test_out(db, test) for test in (await db.scalars(query)).all()]


async def get_test(db: AsyncSession, test_id: uuid.UUID) -> TestOut:
    test = await db.get(Test, test_id)
    if test is None or test.status != PublishStatus.PUBLISHED:
        raise not_found("Test not found")
    return await _test_out(db, test)


# ---------- loading and time ----------


async def _load(db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> TestSession:
    """Locks the session row. Someone else's session looks exactly like a missing one."""
    session = await db.scalar(
        select(TestSession)
        .where(TestSession.id == session_id, TestSession.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if session is None:
        raise not_found("Session not found")
    return session


async def _sections(db: AsyncSession, session_id: uuid.UUID) -> list[SessionSection]:
    rows = await db.scalars(
        select(SessionSection).where(SessionSection.session_id == session_id).order_by(SessionSection.order)
    )
    return list(rows.all())


async def _questions(db: AsyncSession, session_id: uuid.UUID) -> list[SessionQuestion]:
    rows = await db.scalars(
        select(SessionQuestion)
        .where(SessionQuestion.session_id == session_id)
        .order_by(SessionQuestion.position)
    )
    return list(rows.all())


def _open_section(sections: list[SessionSection]) -> SessionSection | None:
    return next((s for s in sections if s.closed_at is None), None)


async def _advance(db: AsyncSession, session: TestSession, now: datetime) -> list[SessionSection]:
    """Brings the session to the state it must have at `now`.

    A section whose deadline passed is closed at that deadline and the next one starts at the
    same moment; when nothing is left the session is finalized. So it does not matter whether
    the student kept the tab open: the outcome is the same as with a timer firing on time.
    """
    sections = await _sections(db, session.id)
    if session.status != SessionStatus.IN_PROGRESS:
        return sections

    if not sections:
        if now >= session.deadline_at:
            await _finalize(db, session, sections, session.deadline_at, FinishReason.DEADLINE)
        return sections

    previous_close = session.started_at
    for section in sections:
        if section.closed_at is None:
            if section.started_at is None:
                section.started_at = previous_close
                section.deadline_at = previous_close + timedelta(seconds=section.time_limit_sec)
            if now < section.deadline_at:
                return sections  # this is the open section
            section.closed_at = section.deadline_at
        previous_close = section.closed_at

    last = sections[-1]
    reason = FinishReason.DEADLINE if last.closed_at == last.deadline_at else FinishReason.SUBMITTED
    await _finalize(db, session, sections, last.closed_at, reason)
    return sections


# ---------- start ----------


async def start(
    db: AsyncSession, user_id: uuid.UUID, test_id: uuid.UUID, idempotency_key: str | None
) -> tuple[SessionOut, bool]:
    """Returns (session, created). Repeating the request never creates a second session."""
    test = await db.get(Test, test_id)
    if test is None or test.status != PublishStatus.PUBLISHED:
        raise not_found("Test not found")

    if idempotency_key:
        previous = await db.scalar(
            select(TestSession).where(
                TestSession.user_id == user_id, TestSession.idempotency_key == idempotency_key
            )
        )
        if previous is not None:
            if previous.test_id != test_id:
                raise conflict("idempotency_key_reused", "This Idempotency-Key was used for another test")
            return await get_state(db, user_id, previous.id), False

    existing_id = await _in_progress_id(db, user_id, test_id)
    if existing_id:
        state = await get_state(db, user_id, existing_id)
        if state.status == SessionStatus.IN_PROGRESS:
            return state, False
        # It had just expired and is finalized now: fall through and start a fresh attempt.

    rows = (
        (
            await db.execute(
                select(TestQuestion, QuestionVersion)
                .join(QuestionVersion, QuestionVersion.id == TestQuestion.question_version_id)
                .where(TestQuestion.test_id == test.id)
                .order_by(TestQuestion.position)
            )
        )
        .unique()
        .all()
    )
    if any(version.status != QuestionStatus.PUBLISHED for _, version in rows):
        # A question was withdrawn after the test was published: do not serve a broken test.
        raise conflict("test_unavailable", "This test is being updated, try again later")

    now = _now()
    exam_sections = await _exam_sections(db, test.format_version_id)
    total_seconds = sum(s.time_limit_sec for s in exam_sections) if exam_sections else test.time_limit_sec
    session = TestSession(
        id=uuid.uuid4(),
        user_id=user_id,
        test_id=test.id,
        started_at=now,
        deadline_at=now + timedelta(seconds=total_seconds),
        idempotency_key=idempotency_key,
    )
    db.add(session)
    try:
        await db.flush()
    except IntegrityError:
        # A parallel "start" won the race: return the session it created.
        await db.rollback()
        existing_id = await _in_progress_id(db, user_id, test_id)
        if existing_id is None:
            raise conflict("start_conflict", "Could not start the test, try again") from None
        return await get_state(db, user_id, existing_id), False

    for index, section in enumerate(exam_sections):
        first = index == 0
        db.add(
            SessionSection(
                session_id=session.id,
                section_id=section.id,
                order=index,
                time_limit_sec=section.time_limit_sec,
                started_at=now if first else None,
                deadline_at=now + timedelta(seconds=section.time_limit_sec) if first else None,
            )
        )
    for test_question, version in rows:
        db.add(
            SessionQuestion(
                session_id=session.id,
                question_version_id=version.id,
                section_id=test_question.section_id,
                position=test_question.position,
                points=test_question.points,
                # Frozen copy of what the student sees. `is_correct` is deliberately left out.
                snapshot={
                    "stem": version.stem,
                    "options": [{"id": str(o.id), "key": o.key, "text": o.text} for o in version.options],
                },
            )
        )
    await db.commit()
    return await get_state(db, user_id, session.id), True


async def _in_progress_id(db: AsyncSession, user_id: uuid.UUID, test_id: uuid.UUID) -> uuid.UUID | None:
    return await db.scalar(
        select(TestSession.id).where(
            TestSession.user_id == user_id,
            TestSession.test_id == test_id,
            TestSession.status == SessionStatus.IN_PROGRESS,
        )
    )


# ---------- state ----------


async def get_state(db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> SessionOut:
    session = await _load(db, user_id, session_id)
    sections = await _advance(db, session, _now())
    await db.commit()
    return await _state(db, session, sections)


async def _state(db: AsyncSession, session: TestSession, sections: list[SessionSection]) -> SessionOut:
    test = await db.get(Test, session.test_id)
    exam_sections = {s.id: s for s in await _exam_sections(db, test.format_version_id)}
    current = _open_section(sections) if session.status == SessionStatus.IN_PROGRESS else None
    in_progress = session.status == SessionStatus.IN_PROGRESS

    questions = []
    for q in await _questions(db, session.id):
        section = exam_sections.get(q.section_id)
        # With sections, only the open one is readable: closed ones cannot be reviewed and
        # future ones cannot be previewed. A finished session returns no question text at all.
        visible = in_progress and (
            not sections or (current is not None and q.section_id == current.section_id)
        )
        if not visible:
            continue
        questions.append(
            SessionQuestionOut(
                id=q.id,
                position=q.position,
                section_code=section.code if section else None,
                stem=q.snapshot["stem"],
                options=q.snapshot["options"],
                selected_option_id=q.selected_option_id,
                marked=q.marked,
                seq=q.seq,
            )
        )

    return SessionOut(
        id=session.id,
        test_id=test.id,
        test_code=test.code,
        title_ky=test.title_ky,
        title_ru=test.title_ru,
        kind=TestKind(test.kind),
        status=session.status,
        finish_reason=session.finish_reason,
        started_at=session.started_at,
        deadline_at=current.deadline_at if current else session.deadline_at,
        submitted_at=session.submitted_at,
        server_time=_now(),
        current_section_code=exam_sections[current.section_id].code if current else None,
        sections=[
            SessionSectionOut(
                code=exam_sections[s.section_id].code,
                title_ky=exam_sections[s.section_id].title_ky,
                title_ru=exam_sections[s.section_id].title_ru,
                time_limit_sec=s.time_limit_sec,
                started_at=s.started_at,
                deadline_at=s.deadline_at,
                closed_at=s.closed_at,
            )
            for s in sections
        ],
        questions=questions,
    )


async def list_sessions(
    db: AsyncSession, user_id: uuid.UUID, limit: int, offset: int
) -> list[SessionListItem]:
    # Close anything that ran out of time first, so the list never shows a stale "in progress".
    overdue = await db.scalars(
        select(TestSession.id).where(
            TestSession.user_id == user_id,
            TestSession.status == SessionStatus.IN_PROGRESS,
            TestSession.deadline_at <= _now(),
        )
    )
    for session_id in overdue.all():
        await get_state(db, user_id, session_id)

    rows = await db.execute(
        select(TestSession, Test)
        .join(Test, Test.id == TestSession.test_id)
        .where(TestSession.user_id == user_id)
        .order_by(TestSession.started_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [
        SessionListItem(
            id=s.id,
            test_id=t.id,
            test_code=t.code,
            title_ky=t.title_ky,
            title_ru=t.title_ru,
            kind=TestKind(t.kind),
            status=s.status,
            started_at=s.started_at,
            submitted_at=s.submitted_at,
            raw_score=s.raw_score,
            max_score=s.max_score,
            scaled_score=s.scaled_score,
        )
        for s, t in rows.all()
    ]


# ---------- answering ----------


async def answer(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, question_id: uuid.UUID, data: AnswerIn
) -> AnswerOut:
    session = await _load(db, user_id, session_id)
    now = _now()
    sections = await _advance(db, session, now)
    if session.status != SessionStatus.IN_PROGRESS:
        await db.commit()  # keep the finalization that `_advance` may have just done
        raise conflict("session_finished", "The test is finished, answers can no longer be changed")

    question = await db.scalar(
        select(SessionQuestion).where(
            SessionQuestion.id == question_id, SessionQuestion.session_id == session.id
        )
    )
    if question is None:
        raise not_found("Question not found in this session")
    if sections and question.section_id != _open_section(sections).section_id:
        raise conflict("section_closed", "This question belongs to a section that is not open")

    applied = data.seq > question.seq
    if applied:
        if data.option_id is not None and str(data.option_id) not in {
            o["id"] for o in question.snapshot["options"]
        }:
            raise unprocessable("invalid_option", "This option does not belong to the question")
        question.selected_option_id = data.option_id
        question.seq = data.seq
        question.answered_at = now
        if data.marked is not None:
            question.marked = data.marked
        if data.time_spent_sec is not None:
            # Time only grows, and a client cannot report an absurd value.
            capped = min(data.time_spent_sec, get_settings().max_question_time_sec)
            question.time_spent_sec = max(question.time_spent_sec, capped)
    await db.commit()

    return AnswerOut(
        question_id=question.id,
        option_id=question.selected_option_id,
        marked=question.marked,
        seq=question.seq,
        applied=applied,
        answered_at=question.answered_at,
        server_time=now,
    )


async def close_section(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, section_code: str
) -> SessionOut:
    """The student finishes a section early. The next one starts immediately."""
    session = await _load(db, user_id, session_id)
    now = _now()
    sections = await _advance(db, session, now)
    test = await db.get(Test, session.test_id)
    by_code = {s.code: s.id for s in await _exam_sections(db, test.format_version_id)}
    if section_code not in by_code:
        raise not_found("Section not found in this test")

    target = next(s for s in sections if s.section_id == by_code[section_code])
    if target.closed_at is None:
        if session.status != SessionStatus.IN_PROGRESS or target is not _open_section(sections):
            raise conflict("section_not_open", "Only the section that is open now can be closed")
        target.closed_at = now
        sections = await _advance(db, session, now)
    # Closing an already closed section is a no-op, so a retried request is safe.
    await db.commit()
    return await _state(db, session, sections)


# ---------- finishing ----------


async def submit(db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> ResultOut:
    """Idempotent: a second submit returns the same result and scores nothing again."""
    session = await _load(db, user_id, session_id)
    now = _now()
    sections = await _advance(db, session, now)
    if session.status == SessionStatus.IN_PROGRESS:
        for section in sections:
            if section.closed_at is None:
                section.closed_at = now
        await _finalize(db, session, sections, now, FinishReason.SUBMITTED)
    await db.commit()
    return await _result(db, session, sections)


async def _finalize(
    db: AsyncSession,
    session: TestSession,
    sections: list[SessionSection],
    finished_at: datetime,
    reason: FinishReason,
) -> None:
    """Scores the session and closes it. Runs inside the caller's transaction and row lock."""
    test = await db.get(Test, session.test_id)
    questions = await _questions(db, session.id)
    correct_ids = set(
        (
            await db.scalars(
                select(QuestionOption.id).where(
                    QuestionOption.question_version_id.in_([q.question_version_id for q in questions]),
                    QuestionOption.is_correct.is_(True),
                )
            )
        ).all()
    )

    for question in questions:
        # NULL means skipped: it is neither right nor wrong in the statistics.
        question.is_correct = (
            None if question.selected_option_id is None else question.selected_option_id in correct_ids
        )
    session.raw_score = sum(q.points for q in questions if q.is_correct)
    session.max_score = sum(q.points for q in questions)

    scale = await _published_scale(db, test.format_version_id) if sections else None
    tables: dict[uuid.UUID, list[tuple[int, float]]] = {}
    if scale:
        for point in scale.points:
            tables.setdefault(point.section_id, []).append((point.raw_value, point.scaled_value))

    for section in sections:
        own = [q for q in questions if q.section_id == section.section_id]
        section.raw_score = sum(q.points for q in own if q.is_correct)
        section.max_score = sum(q.points for q in own)
        if scale:
            section.scaled_score = scoring.scale_lookup(tables[section.section_id], section.raw_score)
    if scale:
        session.scaled_score = round(sum(s.scaled_score for s in sections), 1)
        session.score_scale_id = scale.id

    session.status = SessionStatus.FINISHED
    session.finish_reason = reason
    session.submitted_at = finished_at

    # Feed the learning loop: attempts, skill progress and a mistake card per wrong answer.
    user = await db.get(User, session.user_id)
    answered = [q for q in questions if q.selected_option_id is not None]
    versions = {
        v.id: v
        for v in (
            await db.scalars(
                select(QuestionVersion).where(
                    QuestionVersion.id.in_([q.question_version_id for q in answered])
                )
            )
        ).unique()
    }
    await learning.record_attempts(
        db,
        user,
        [
            learning.Answered(
                question_version_id=q.question_version_id,
                question_group_id=versions[q.question_version_id].group_id,
                skill_id=versions[q.question_version_id].group.skill_id,
                selected_option_id=q.selected_option_id,
                is_correct=bool(q.is_correct),
                time_spent_sec=q.time_spent_sec,
            )
            for q in answered
        ],
        AttemptSource.TEST,
        session.id,
        finished_at,
    )

    if test.kind == TestKind.MOCK:
        exam_sections = {s.id: s for s in await _exam_sections(db, test.format_version_id)}
        has_previous = await db.scalar(
            select(func.count()).select_from(Checkpoint).where(Checkpoint.user_id == session.user_id)
        )
        checkpoint = Checkpoint(
            user_id=session.user_id,
            session_id=session.id,
            format_version_id=test.format_version_id,
            # The first measured mock exam is the starting point of the student's path.
            kind=CheckpointKind.CONTROL if has_previous else CheckpointKind.BASELINE,
            total_raw=session.raw_score,
            max_raw=session.max_score,
            total_scaled=session.scaled_score,
            sections=[
                {
                    "code": exam_sections[s.section_id].code,
                    "raw": s.raw_score,
                    "max": s.max_score,
                    "scaled": s.scaled_score,
                }
                for s in sections
            ],
            score_scale_id=session.score_scale_id,
            taken_at=finished_at,
        )
        db.add(checkpoint)
        await db.flush()
        await goals.on_checkpoint(db, session.user_id, checkpoint)
        await goals.mark_today_done(db, user, PlanItemKind.MOCK, finished_at)
    await db.flush()


# ---------- result ----------


def checkpoint_out(checkpoint: Checkpoint) -> CheckpointOut:
    return CheckpointOut(
        id=checkpoint.id,
        kind=checkpoint.kind,
        session_id=checkpoint.session_id,
        total_raw=checkpoint.total_raw,
        max_raw=checkpoint.max_raw,
        total_scaled=checkpoint.total_scaled,
        sections=checkpoint.sections,
        taken_at=checkpoint.taken_at,
    )


async def get_result(db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> ResultOut:
    session = await _load(db, user_id, session_id)
    sections = await _advance(db, session, _now())
    await db.commit()
    if session.status != SessionStatus.FINISHED:
        # The answer key stays hidden until the attempt is over.
        raise conflict("session_in_progress", "The result is available after the test is finished")
    return await _result(db, session, sections)


def _counts(questions: list[SessionQuestion]) -> dict[str, int]:
    return {
        "correct": sum(q.is_correct is True for q in questions),
        "wrong": sum(q.is_correct is False for q in questions),
        "skipped": sum(q.is_correct is None for q in questions),
    }


async def _result(db: AsyncSession, session: TestSession, sections: list[SessionSection]) -> ResultOut:
    test = await db.get(Test, session.test_id)
    questions = await _questions(db, session.id)
    exam_sections = {s.id: s for s in await _exam_sections(db, test.format_version_id)}
    versions = {
        v.id: v
        for v in (
            await db.scalars(
                select(QuestionVersion).where(
                    QuestionVersion.id.in_([q.question_version_id for q in questions])
                )
            )
        ).unique()
    }
    scale = await db.get(ScoreScale, session.score_scale_id) if session.score_scale_id else None
    checkpoint = await db.scalar(select(Checkpoint).where(Checkpoint.session_id == session.id))

    return ResultOut(
        session_id=session.id,
        test_id=test.id,
        test_code=test.code,
        kind=TestKind(test.kind),
        finish_reason=session.finish_reason,
        started_at=session.started_at,
        submitted_at=session.submitted_at,
        duration_sec=int((session.submitted_at - session.started_at).total_seconds()),
        raw_score=session.raw_score,
        max_score=session.max_score,
        scaled_score=session.scaled_score,
        scale_kind=ScaleKind(scale.kind) if scale else None,
        score_is_estimated=bool(scale and scale.kind == ScaleKind.ESTIMATED),
        **_counts(questions),
        sections=[
            ResultSectionOut(
                code=exam_sections[s.section_id].code,
                title_ky=exam_sections[s.section_id].title_ky,
                title_ru=exam_sections[s.section_id].title_ru,
                raw_score=s.raw_score,
                max_score=s.max_score,
                scaled_score=s.scaled_score,
                **_counts([q for q in questions if q.section_id == s.section_id]),
            )
            for s in sections
        ],
        questions=[
            ResultQuestionOut(
                id=q.id,
                position=q.position,
                section_code=exam_sections[q.section_id].code if q.section_id else None,
                question_code=versions[q.question_version_id].group.code,
                skill_id=versions[q.question_version_id].group.skill_id,
                stem=q.snapshot["stem"],
                options=q.snapshot["options"],
                selected_option_id=q.selected_option_id,
                correct_option_id=next(o.id for o in versions[q.question_version_id].options if o.is_correct),
                is_correct=q.is_correct,
                solution=versions[q.question_version_id].solution,
                time_spent_sec=q.time_spent_sec,
            )
            for q in questions
        ],
        checkpoint=checkpoint_out(checkpoint) if checkpoint else None,
    )


async def list_checkpoints(db: AsyncSession, user_id: uuid.UUID) -> list[CheckpointOut]:
    rows = await db.scalars(
        select(Checkpoint).where(Checkpoint.user_id == user_id).order_by(Checkpoint.taken_at)
    )
    return [checkpoint_out(c) for c in rows.all()]


async def expire_overdue(db: AsyncSession, batch: int = 200) -> int:
    """For a periodic job: finalizes sessions whose time ran out while nobody was looking.

    Not required for correctness (deadlines are applied on access anyway), but it keeps
    statistics and checkpoints up to date for students who simply closed the tab.
    """
    rows = await db.execute(
        select(TestSession.id, TestSession.user_id)
        .where(TestSession.status == SessionStatus.IN_PROGRESS, TestSession.deadline_at <= _now())
        .limit(batch)
    )
    pending = rows.all()
    for session_id, user_id in pending:
        await get_state(db, user_id, session_id)
    return len(pending)
