"""Staff side of exams: formats (blueprints), score scales and test building."""

import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.core.errors import conflict, not_found, unprocessable
from mysite.db.models import (
    ExamFormat,
    ExamFormatVersion,
    ExamSection,
    PublishStatus,
    QuestionStatus,
    QuestionVersion,
    ScoreScale,
    ScoreScalePoint,
    Subject,
    Test,
    TestKind,
    TestQuestion,
    User,
)
from mysite.db.schema.exam import (
    ExamFormatIn,
    ExamFormatVersionOut,
    ScoreScaleIn,
    SectionOut,
    TestAdminOut,
    TestIn,
)
from mysite.services import audit, scoring

P = PublishStatus


def format_out(fv: ExamFormatVersion) -> ExamFormatVersionOut:
    return ExamFormatVersionOut(
        id=fv.id,
        code=fv.format.code,
        title_ky=fv.format.title_ky,
        title_ru=fv.format.title_ru,
        version=fv.version,
        status=P(fv.status),
        rules=fv.rules,
        sections=[
            SectionOut(
                id=s.id,
                code=s.code,
                title_ky=s.title_ky,
                title_ru=s.title_ru,
                question_count=s.question_count,
                time_limit_sec=s.time_limit_sec,
                order=s.order,
            )
            for s in fv.sections
        ],
    )


async def get_format_version(db: AsyncSession, fv_id: uuid.UUID, *, lock: bool = False) -> ExamFormatVersion:
    query = select(ExamFormatVersion).where(ExamFormatVersion.id == fv_id)
    if lock:
        query = query.with_for_update(of=ExamFormatVersion)
    fv = (await db.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()
    if fv is None:
        raise not_found("Exam format version not found")
    return fv


async def list_format_versions(db: AsyncSession) -> list[ExamFormatVersion]:
    rows = await db.scalars(
        select(ExamFormatVersion).join(ExamFormat).order_by(ExamFormat.code, ExamFormatVersion.version.desc())
    )
    return list(rows.unique().all())


async def create_format_version(db: AsyncSession, data: ExamFormatIn, actor: User) -> ExamFormatVersion:
    exam_format = await db.scalar(select(ExamFormat).where(ExamFormat.code == data.code).with_for_update())
    if exam_format is None:
        exam_format = ExamFormat(
            id=uuid.uuid4(), code=data.code, title_ky=data.title_ky, title_ru=data.title_ru
        )
        db.add(exam_format)
        await db.flush()
    else:
        exam_format.title_ky, exam_format.title_ru = data.title_ky, data.title_ru
    last = await db.scalar(
        select(func.max(ExamFormatVersion.version)).where(ExamFormatVersion.format_id == exam_format.id)
    )
    fv = ExamFormatVersion(
        id=uuid.uuid4(), format_id=exam_format.id, version=(last or 0) + 1, rules=data.rules
    )
    db.add(fv)
    await db.flush()
    db.add_all(
        ExamSection(format_version_id=fv.id, order=i, **section.model_dump())
        for i, section in enumerate(data.sections)
    )
    audit.record(db, actor.id, "exam_format.create", "exam_format_version", fv.id, {"code": data.code})
    await db.commit()
    return await get_format_version(db, fv.id)


async def publish_format_version(db: AsyncSession, fv_id: uuid.UUID, actor: User) -> ExamFormatVersion:
    fv = await get_format_version(db, fv_id, lock=True)
    if fv.status != P.DRAFT:
        raise conflict("not_draft", "Only a draft can be published")
    # The previous blueprint stays in history: old tests and results still point to it.
    await db.execute(
        update(ExamFormatVersion)
        .where(ExamFormatVersion.format_id == fv.format_id, ExamFormatVersion.status == P.PUBLISHED)
        .values(status=P.RETIRED)
    )
    fv.status = P.PUBLISHED
    audit.record(db, actor.id, "exam_format.publish", "exam_format_version", fv.id)
    await db.commit()
    return await get_format_version(db, fv.id)


async def create_scale(db: AsyncSession, fv_id: uuid.UUID, data: ScoreScaleIn, actor: User) -> ScoreScale:
    fv = await get_format_version(db, fv_id, lock=True)
    sections = {s.code: s for s in fv.sections}
    tables: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for point in data.points:
        if point.section_code not in sections:
            raise unprocessable("invalid_scale", f"Unknown section: {point.section_code}")
        tables[point.section_code].append((point.raw, point.scaled))

    problems = []
    for code, section in sections.items():
        raws = [raw for raw, _ in tables[code]]
        if len(set(raws)) != len(raws):
            problems.append(f"{code}: duplicate raw values")
        problems += [f"{code}: {p}" for p in scoring.scale_problems(tables[code], section.question_count)]
    if problems:
        raise unprocessable("invalid_scale", "The scale is not valid", problems)

    last = await db.scalar(select(func.max(ScoreScale.version)).where(ScoreScale.format_version_id == fv.id))
    scale = ScoreScale(
        id=uuid.uuid4(), format_version_id=fv.id, version=(last or 0) + 1, kind=data.kind, source=data.source
    )
    db.add(scale)
    await db.flush()
    db.add_all(
        ScoreScalePoint(scale_id=scale.id, section_id=sections[code].id, raw_value=raw, scaled_value=scaled)
        for code, table in tables.items()
        for raw, scaled in table
    )
    audit.record(db, actor.id, "score_scale.create", "score_scale", scale.id, {"kind": data.kind})
    await db.commit()
    return scale


async def publish_scale(db: AsyncSession, scale_id: uuid.UUID, actor: User) -> ScoreScale:
    scale = await db.scalar(select(ScoreScale).where(ScoreScale.id == scale_id).with_for_update())
    if scale is None:
        raise not_found("Score scale not found")
    if scale.status != P.DRAFT:
        raise conflict("not_draft", "Only a draft can be published")
    # Finished results keep pointing at the scale they were scored with: nothing is recalculated.
    await db.execute(
        update(ScoreScale)
        .where(ScoreScale.format_version_id == scale.format_version_id, ScoreScale.status == P.PUBLISHED)
        .values(status=P.RETIRED)
    )
    scale.status = P.PUBLISHED
    audit.record(db, actor.id, "score_scale.publish", "score_scale", scale.id, {"kind": scale.kind})
    await db.commit()
    return scale


# ---------- tests ----------


def test_out(test: Test) -> TestAdminOut:
    return TestAdminOut(
        id=test.id,
        code=test.code,
        version=test.version,
        title_ky=test.title_ky,
        title_ru=test.title_ru,
        kind=TestKind(test.kind),
        language=test.language,
        subject_id=test.subject_id,
        format_version_id=test.format_version_id,
        time_limit_sec=test.time_limit_sec,
        status=P(test.status),
        question_count=len(test.questions),
    )


async def get_test(db: AsyncSession, test_id: uuid.UUID, *, lock: bool = False) -> Test:
    query = select(Test).where(Test.id == test_id)
    if lock:
        query = query.with_for_update(of=Test)
    test = (await db.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()
    if test is None:
        raise not_found("Test not found")
    return test


async def list_tests(db: AsyncSession) -> list[Test]:
    return list((await db.scalars(select(Test).order_by(Test.created_at.desc()))).all())


async def _test_questions(db: AsyncSession, test_id: uuid.UUID, data: TestIn) -> list[TestQuestion]:
    """Checks references only. Completeness is checked separately, before publishing."""
    sections: dict[str, ExamSection] = {}
    if data.format_version_id:
        sections = {s.code: s for s in (await get_format_version(db, data.format_version_id)).sections}
    if data.subject_id and await db.get(Subject, data.subject_id) is None:
        raise unprocessable("invalid_subject", "Subject not found")

    ids = [q.question_version_id for q in data.questions]
    known = set((await db.scalars(select(QuestionVersion.id).where(QuestionVersion.id.in_(ids)))).all())
    rows = []
    for position, item in enumerate(data.questions, start=1):
        if item.question_version_id not in known:
            raise unprocessable("invalid_question", f"Question not found: {item.question_version_id}")
        if item.section_code and item.section_code not in sections:
            raise unprocessable("invalid_section", f"Unknown section: {item.section_code}")
        rows.append(
            TestQuestion(
                test_id=test_id,
                question_version_id=item.question_version_id,
                section_id=sections[item.section_code].id if item.section_code else None,
                position=position,
                points=item.points,
            )
        )
    return rows


async def create_test(db: AsyncSession, data: TestIn, actor: User) -> Test:
    last = await db.scalar(select(func.max(Test.version)).where(Test.code == data.code))
    test = Test(
        id=uuid.uuid4(),
        version=(last or 0) + 1,
        created_by=actor.id,
        **data.model_dump(exclude={"questions"}),
    )
    questions = await _test_questions(db, test.id, data)
    db.add(test)
    await db.flush()
    db.add_all(questions)
    await db.commit()
    return await get_test(db, test.id)


async def replace_test(db: AsyncSession, test_id: uuid.UUID, data: TestIn, actor: User) -> Test:
    test = await get_test(db, test_id, lock=True)
    if test.status != P.DRAFT:
        raise conflict("not_editable", "A published test is frozen: create a new version instead")
    if data.code != test.code:
        raise unprocessable("code_immutable", "The test code cannot be changed")
    questions = await _test_questions(db, test.id, data)
    for field, value in data.model_dump(exclude={"questions", "code"}).items():
        setattr(test, field, value)
    test.questions = []
    await db.flush()  # delete old rows before inserting: positions are unique per test
    db.add_all(questions)
    await db.commit()
    return await get_test(db, test.id)


async def test_problems(db: AsyncSession, test: Test) -> list[str]:
    """Everything that stops a test from being published. Empty list = ready."""
    problems: list[str] = []
    if not test.questions:
        return ["The test has no questions"]

    versions = {
        v.id: v
        for v in (
            await db.scalars(
                select(QuestionVersion).where(
                    QuestionVersion.id.in_([q.question_version_id for q in test.questions])
                )
            )
        ).unique()
    }
    for tq in test.questions:
        version = versions[tq.question_version_id]
        label = f"#{tq.position} {version.group.code}"
        if version.status != QuestionStatus.PUBLISHED:
            problems.append(f"{label}: question is {version.status}, not published")
        if version.language != test.language:
            problems.append(f"{label}: language {version.language} differs from the test ({test.language})")

    if test.kind == TestKind.MOCK:
        if test.format_version_id is None:
            return [*problems, "A mock exam needs an exam format"]
        fv = await get_format_version(db, test.format_version_id)
        if fv.status != P.PUBLISHED:
            problems.append("The exam format version is not published")
        per_section = Counter(tq.section_id for tq in test.questions)
        if per_section.get(None):
            problems.append(f"{per_section[None]} question(s) have no section")
        for section in fv.sections:
            have = per_section.get(section.id, 0)
            if have != section.question_count:
                problems.append(
                    f"Section {section.code}: {have} questions, the format requires {section.question_count}"
                )
        if any(tq.points != 1 for tq in test.questions):
            problems.append(
                "Mock exam questions must be worth 1 point each (the scale counts correct answers)"
            )
        order = {s.id: s.order for s in fv.sections}
        sequence = [order.get(tq.section_id, -1) for tq in test.questions]
        if sequence != sorted(sequence):
            problems.append("Questions must be grouped by section, in the order of the format")
    else:
        if test.format_version_id is not None or any(tq.section_id for tq in test.questions):
            problems.append("Only mock exams use an exam format and sections")
        if not test.time_limit_sec:
            problems.append("A time limit is required")
    return problems


async def publish_test(db: AsyncSession, test_id: uuid.UUID, actor: User) -> Test:
    test = await get_test(db, test_id, lock=True)
    if test.status != P.DRAFT:
        raise conflict("not_draft", "Only a draft can be published")
    problems = await test_problems(db, test)
    if problems:
        raise unprocessable("test_incomplete", "The test is not ready", problems)
    # Students get the newest version; sessions already started on the old one finish on it.
    await db.execute(
        update(Test).where(Test.code == test.code, Test.status == P.PUBLISHED).values(status=P.RETIRED)
    )
    test.status = P.PUBLISHED
    test.published_at = datetime.now(UTC)
    audit.record(db, actor.id, "test.publish", "test", test.id, {"code": test.code, "version": test.version})
    await db.commit()
    return await get_test(db, test.id)


async def retire_test(db: AsyncSession, test_id: uuid.UUID, actor: User) -> Test:
    test = await get_test(db, test_id, lock=True)
    if test.status != P.PUBLISHED:
        raise conflict("not_published", "Only a published test can be retired")
    test.status = P.RETIRED
    audit.record(db, actor.id, "test.retire", "test", test.id, {"code": test.code})
    await db.commit()
    return await get_test(db, test.id)
