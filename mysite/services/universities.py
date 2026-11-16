"""Universities and their past thresholds for state-funded places."""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.core.errors import conflict, not_found
from mysite.db.models import University, UniversityProgram, User
from mysite.services import audit


async def list_programs(db: AsyncSession, query: str | None, max_score: float | None) -> list[dict]:
    """Active programs, newest year per program first. `query` matches university or program names."""
    stmt = (
        select(UniversityProgram, University)
        .join(University, University.id == UniversityProgram.university_id)
        .where(UniversityProgram.active.is_(True), University.active.is_(True))
        .order_by(University.title_ru, UniversityProgram.title_ru, UniversityProgram.year.desc())
    )
    if query:
        like = f"%{query.strip()}%"
        stmt = stmt.where(
            University.title_ky.ilike(like)
            | University.title_ru.ilike(like)
            | UniversityProgram.title_ky.ilike(like)
            | UniversityProgram.title_ru.ilike(like)
        )
    if max_score is not None:
        stmt = stmt.where(UniversityProgram.threshold_score <= max_score)
    return [
        {
            "id": p.id,
            "university_id": u.id,
            "university_code": u.code,
            "university_ky": u.title_ky,
            "university_ru": u.title_ru,
            "city": u.city,
            "code": p.code,
            "title_ky": p.title_ky,
            "title_ru": p.title_ru,
            "year": p.year,
            "threshold_score": p.threshold_score,
            "source": p.source,
        }
        for p, u in (await db.execute(stmt.limit(500))).all()
    ]


async def upsert_university(db: AsyncSession, data: dict, actor: User) -> University:
    university = await db.scalar(select(University).where(University.code == data["code"]))
    if university is None:
        university = University(id=uuid.uuid4(), **data)
        db.add(university)
    else:
        for field, value in data.items():
            setattr(university, field, value)
    audit.record(db, actor.id, "university.upsert", "university", university.id, {"code": data["code"]})
    await db.commit()
    return university


async def add_program(
    db: AsyncSession, university_id: uuid.UUID, data: dict, actor: User
) -> UniversityProgram:
    if await db.get(University, university_id) is None:
        raise not_found("University not found")
    program = UniversityProgram(id=uuid.uuid4(), university_id=university_id, **data)
    db.add(program)
    audit.record(db, actor.id, "university_program.create", "university_program", program.id)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise conflict("program_exists", "This program already has a threshold for this year") from None
    return program


async def update_program(
    db: AsyncSession, program_id: uuid.UUID, data: dict, actor: User
) -> UniversityProgram:
    program = await db.get(UniversityProgram, program_id)
    if program is None:
        raise not_found("Program not found")
    for field, value in data.items():
        setattr(program, field, value)
    audit.record(
        db, actor.id, "university_program.update", "university_program", program.id, {"fields": list(data)}
    )
    await db.commit()
    return program
