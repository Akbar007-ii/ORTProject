"""Demo data for trying the API by hand. NOT the real exam: sizes, timings and the scale are
made up. The real format and scale are entered through the admin API once they are confirmed."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.db.models import (
    ExamFormat,
    ExamFormatVersion,
    ExamSection,
    PublishStatus,
    QuestionGroup,
    QuestionOption,
    QuestionStatus,
    QuestionVersion,
    ScaleKind,
    ScoreScale,
    ScoreScalePoint,
    Skill,
    Subject,
    Test,
    TestKind,
    TestQuestion,
    Topic,
)

FORMAT_CODE = "demo"

# (skill, stem, options A-D, correct key, solution)
QUESTIONS = [
    ("algebra", "Теңдеменин тамырларын тап: $x^2 - 5x + 6 = 0$", ["1 жана 6", "2 жана 3", "−2 жана −3", "−1 жана −6"], "B",
     "1. D = 25 − 24 = 1\n2. x = (5 ± 1) / 2\n3. x₁ = 2, x₂ = 3"),
    ("algebra", "Теңдемени чыгар: $2x + 6 = 14$", ["3", "4", "5", "10"], "B", "1. 2x = 14 − 6 = 8\n2. x = 4"),
    ("arithmetic", "200дүн 15% канча?", ["15", "20", "30", "35"], "C", "200 · 0,15 = 30"),
    ("algebra", "Дискриминантты тап: $3x^2 + 2x - 1 = 0$", ["4", "8", "16", "−8"], "C",
     "1. D = b² − 4ac\n2. D = 4 − 4 · 3 · (−1) = 4 + 12 = 16"),
    ("arithmetic", "Эсепте: $\\frac{3}{4} + \\frac{1}{8}$", ["4/12", "7/8", "1/2", "5/8"], "B", "3/4 = 6/8; 6/8 + 1/8 = 7/8"),
    ("arithmetic", "$\\sqrt{144}$ канчага барабар?", ["11", "12", "14", "72"], "B", "12 · 12 = 144"),
    ("arithmetic", "Үч бурчтуктун эки бурчу 50° жана 60°. Үчүнчү бурчу канча?", ["60°", "70°", "80°", "90°"], "B",
     "Бурчтардын суммасы 180°: 180 − 50 − 60 = 70"),
    ("arithmetic", "$2^5$ канчага барабар?", ["10", "16", "25", "32"], "D", "2 · 2 · 2 · 2 · 2 = 32"),
]  # fmt: skip


async def seed_demo(db: AsyncSession) -> str:
    if await db.scalar(select(ExamFormat.id).where(ExamFormat.code == FORMAT_CODE)):
        return "Demo data already exists, nothing changed"
    now = datetime.now(UTC)

    subject = await db.scalar(select(Subject).where(Subject.code == "math"))
    if subject is None:
        subject = Subject(id=uuid.uuid4(), code="math", title_ky="Математика", title_ru="Математика", order=1)
        db.add(subject)
        await db.flush()
    skills = {}
    for order, (code, ky, ru) in enumerate(
        [("algebra", "Алгебра", "Алгебра"), ("arithmetic", "Арифметика", "Арифметика")]
    ):
        topic = Topic(id=uuid.uuid4(), subject_id=subject.id, title_ky=ky, title_ru=ru, order=order)
        db.add(topic)
        await db.flush()
        skills[code] = Skill(
            id=uuid.uuid4(), topic_id=topic.id, code=f"demo.{code}", title_ky=ky, title_ru=ru
        )
        db.add(skills[code])
    await db.flush()

    exam_format = ExamFormat(
        id=uuid.uuid4(),
        code=FORMAT_CODE,
        title_ky="ДЕМО формат (чыныгы ЖРТ эмес)",
        title_ru="ДЕМО формат (не настоящий ОРТ)",
    )
    db.add(exam_format)
    await db.flush()
    fv = ExamFormatVersion(
        id=uuid.uuid4(), format_id=exam_format.id, version=1, status=PublishStatus.PUBLISHED, rules={}
    )
    db.add(fv)
    await db.flush()
    sections = [
        ExamSection(
            id=uuid.uuid4(), format_version_id=fv.id, code=f"part{i + 1}", title_ky=f"{i + 1}-бөлүм",
            title_ru=f"Часть {i + 1}", question_count=4, time_limit_sec=480, order=i,
        )
        for i in range(2)
    ]  # fmt: skip
    db.add_all(sections)
    scale = ScoreScale(
        id=uuid.uuid4(), format_version_id=fv.id, version=1, kind=ScaleKind.ESTIMATED,
        status=PublishStatus.PUBLISHED, source="demo, invented",
    )  # fmt: skip
    db.add(scale)
    await db.flush()
    for section in sections:
        db.add_all(
            ScoreScalePoint(scale_id=scale.id, section_id=section.id, raw_value=raw, scaled_value=scaled)
            for raw, scaled in ((0, 0), (4, 40))
        )

    test = Test(
        id=uuid.uuid4(), code="demo-mock", version=1, title_ky="Демо сыноо тест", title_ru="Демо пробный тест",
        kind=TestKind.MOCK, language="ky", subject_id=subject.id, format_version_id=fv.id,
        status=PublishStatus.PUBLISHED, published_at=now,
    )  # fmt: skip
    db.add(test)
    await db.flush()

    for index, (skill, stem, options, correct, solution) in enumerate(QUESTIONS):
        group = QuestionGroup(id=uuid.uuid4(), code=f"Q-DEMO{index + 1:02d}", skill_id=skills[skill].id)
        db.add(group)
        await db.flush()
        version = QuestionVersion(
            id=uuid.uuid4(), group_id=group.id, version=1, language="ky", stem=stem, solution=solution,
            difficulty=2, estimated_time_sec=90, source="ORTO demo", license="own",
            status=QuestionStatus.PUBLISHED, published_at=now,
        )  # fmt: skip
        db.add(version)
        await db.flush()
        db.add_all(
            QuestionOption(
                question_version_id=version.id, key=key, text=text, order=i, is_correct=key == correct
            )
            for i, (key, text) in enumerate(zip("ABCD", options, strict=True))
        )
        db.add(
            TestQuestion(
                test_id=test.id,
                question_version_id=version.id,
                section_id=sections[index // 4].id,
                position=index + 1,
            )
        )
    await db.commit()
    return "Demo data created: subject 'math', format 'demo', mock exam 'demo-mock' (8 questions, 2 sections)"
