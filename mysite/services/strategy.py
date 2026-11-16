"""Exam strategy: what the student's timing in a finished test says, beyond right and wrong."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.core.errors import conflict
from mysite.db.models import (
    ExamFormatVersion,
    ExamSection,
    QuestionVersion,
    SessionQuestion,
    SessionSection,
    SessionStatus,
    Test,
    User,
)
from mysite.services import sessions as session_service

DEFAULT_QUESTION_SEC = 90
SLOW_FACTOR = 2.0  # took more than twice the expected time
RUSHED_SHARE = 0.25  # answered in under a quarter of the expected time

TIPS = {
    "too_slow": {
        "ky": "{count} суроого күтүлгөндөн 2 эседен көп убакыт кетти. Бир суроого токтоп калбай, белгилеп өтүп, кийин кайтыңыз.",
        "ru": "На {count} вопр. ушло более чем вдвое больше ожидаемого времени. Не застревайте: отметьте вопрос и вернитесь к нему позже.",
    },
    "rushed": {
        "ky": "{count} суроого шашып жооп берип, ката кетирдиңиз. Шартын аягына чейин окуңуз.",
        "ru": "На {count} вопр. вы ответили слишком быстро и ошиблись. Дочитывайте условие до конца.",
    },
    "ran_out": {
        "ky": "«{section}» бөлүмүндө убакыт жетпей, {count} суроо жоопсуз калды. Ар бир суроого ≈{pace} сек бөлүңүз.",
        "ru": "В разделе «{section}» не хватило времени: {count} вопр. без ответа. Рассчитывайте ≈{pace} сек на вопрос.",
    },
    "guess_blanks": {
        "ky": "{count} суроо бош калды. Туура эмес жооп үчүн балл кемибесе, бош калтырбай, божомол менен белгилеңиз.",
        "ru": "{count} вопр. остались без ответа. Если за неверный ответ баллы не снимают, лучше отметить догадку, чем оставить пустым.",
    },
    "left_early": {
        "ky": "«{section}» бөлүмүн {minutes} мүнөт эрте бүтүрүп, {count} суроону бош калтырдыңыз. Убакыт калса, бош суроолорго кайтыңыз.",
        "ru": "Раздел «{section}» закончен на {minutes} мин раньше, а {count} вопр. остались пустыми. Если время есть, вернитесь к ним.",
    },
    "time_left": {
        "ky": "«{section}» бөлүмүн {minutes} мүнөт эрте бүтүрдүңүз. Калган убакытта белгиленген суроолорду текшериңиз.",
        "ru": "Раздел «{section}» закончен на {minutes} мин раньше. Используйте остаток времени, чтобы проверить отмеченные вопросы.",
    },
}


def _tip(code: str, locale: str, **values) -> dict:
    return {"code": code, "text": TIPS[code].get(locale, TIPS[code]["ru"]).format(**values), **values}


async def analyze(db: AsyncSession, user: User, session_id: uuid.UUID) -> dict:
    # Reuses the owner check and the lazy deadline handling of the exam engine.
    state = await session_service.get_state(db, user.id, session_id)
    if state.status != SessionStatus.FINISHED:
        raise conflict("session_in_progress", "Strategy advice is available after the test is finished")

    test = await db.get(Test, state.test_id)
    rules = {}
    if test.format_version_id:
        fv = (
            (
                await db.scalars(
                    select(ExamFormatVersion).where(ExamFormatVersion.id == test.format_version_id)
                )
            )
            .unique()
            .one()
        )
        rules = fv.rules or {}
    questions = (
        await db.scalars(
            select(SessionQuestion)
            .where(SessionQuestion.session_id == session_id)
            .order_by(SessionQuestion.position)
        )
    ).all()
    expected = dict(
        (
            await db.execute(
                select(QuestionVersion.id, QuestionVersion.estimated_time_sec).where(
                    QuestionVersion.id.in_([q.question_version_id for q in questions])
                )
            )
        ).all()
    )
    sections = (
        await db.scalars(
            select(SessionSection)
            .where(SessionSection.session_id == session_id)
            .order_by(SessionSection.order)
        )
    ).all()
    names = {
        s.id: (s.title_ky if user.locale == "ky" else s.title_ru)
        for s in (
            await db.scalars(select(ExamSection).where(ExamSection.id.in_([x.section_id for x in sections])))
        ).all()
    }

    slow, rushed, blank = [], [], []
    for q in questions:
        norm = expected.get(q.question_version_id) or DEFAULT_QUESTION_SEC
        if q.selected_option_id is None:
            blank.append(q.position)
        elif q.time_spent_sec > norm * SLOW_FACTOR:
            slow.append(q.position)
        elif q.is_correct is False and 0 < q.time_spent_sec < norm * RUSHED_SHARE:
            rushed.append(q.position)

    tips = []
    section_rows = []
    for section in sections:
        own = [q for q in questions if q.section_id == section.section_id]
        used = (
            int((section.closed_at - section.started_at).total_seconds())
            if section.closed_at and section.started_at
            else 0
        )
        unanswered = sum(q.selected_option_id is None for q in own)
        timed_out = bool(
            section.deadline_at and section.closed_at and section.closed_at >= section.deadline_at
        )
        section_rows.append(
            {
                "title": names.get(section.section_id),
                "time_limit_sec": section.time_limit_sec,
                "time_used_sec": used,
                "questions": len(own),
                "unanswered": unanswered,
                "ran_out_of_time": timed_out,
            }
        )
        if timed_out and unanswered:
            pace = section.time_limit_sec // max(1, len(own))
            tips.append(
                _tip(
                    "ran_out", user.locale, section=names.get(section.section_id), count=unanswered, pace=pace
                )
            )
        elif not timed_out and unanswered and section.time_limit_sec - used >= 60:
            # Ending a section early with blanks is a different mistake from running out of time.
            tips.append(
                _tip(
                    "left_early",
                    user.locale,
                    section=names.get(section.section_id),
                    minutes=(section.time_limit_sec - used) // 60,
                    count=unanswered,
                )
            )
        elif not timed_out and section.time_limit_sec - used >= 300:
            tips.append(
                _tip(
                    "time_left",
                    user.locale,
                    section=names.get(section.section_id),
                    minutes=(section.time_limit_sec - used) // 60,
                )
            )
    if slow:
        tips.append(_tip("too_slow", user.locale, count=len(slow)))
    if rushed:
        tips.append(_tip("rushed", user.locale, count=len(rushed)))
    # Only when the exam rules say a wrong answer costs nothing (set in the format, not guessed here).
    if blank and rules.get("wrong_answer_penalty") is False:
        tips.append(_tip("guess_blanks", user.locale, count=len(blank)))

    return {
        "session_id": session_id,
        "slow_questions": slow,
        "rushed_questions": rushed,
        "unanswered_questions": blank,
        "sections": section_rows,
        "tips": tips,
    }
