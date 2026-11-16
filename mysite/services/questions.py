"""Question bank and its review workflow:

draft -> editorial_review -> expert_review -> approved -> published -> retired
"""

import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.config import get_settings
from mysite.core.errors import conflict, forbidden, not_found, unprocessable
from mysite.db.models import (
    ContentReview,
    QuestionGroup,
    QuestionOption,
    QuestionStatus,
    QuestionVersion,
    RoleCode,
    Skill,
    User,
)
from mysite.db.schema.question import OptionIn, QuestionCreateIn, QuestionUpdateIn
from mysite.services import audit

S = QuestionStatus
EDITORS = {RoleCode.EDITOR, RoleCode.ADMIN}
EXPERTS = {RoleCode.EXPERT, RoleCode.ADMIN}
ADMINS = {RoleCode.ADMIN}

# (from, to) -> roles allowed to make the move. Anything not listed is forbidden.
TRANSITIONS: dict[tuple[str, str], set[str]] = {
    (S.DRAFT, S.EDITORIAL_REVIEW): EDITORS,
    (S.EDITORIAL_REVIEW, S.EXPERT_REVIEW): EDITORS,
    (S.EDITORIAL_REVIEW, S.DRAFT): EDITORS,
    (S.EXPERT_REVIEW, S.APPROVED): EXPERTS,
    (S.EXPERT_REVIEW, S.DRAFT): EXPERTS,
    (S.APPROVED, S.PUBLISHED): ADMINS,
    (S.APPROVED, S.DRAFT): ADMINS,
    (S.PUBLISHED, S.RETIRED): ADMINS,
}


def _options(version_id: uuid.UUID, options: list[OptionIn]) -> list[QuestionOption]:
    return [
        QuestionOption(
            question_version_id=version_id, key=o.key, text=o.text, is_correct=o.is_correct, order=i
        )
        for i, o in enumerate(options)
    ]


async def get_version(db: AsyncSession, version_id: uuid.UUID, *, lock: bool = False) -> QuestionVersion:
    query = select(QuestionVersion).where(QuestionVersion.id == version_id)
    if lock:
        query = query.with_for_update(of=QuestionVersion)
    version = (await db.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()
    if version is None:
        raise not_found("Question not found")
    return version


async def create(db: AsyncSession, data: QuestionCreateIn, actor: User) -> QuestionVersion:
    if data.group_id:
        group = await db.get(QuestionGroup, data.group_id)
        if group is None:
            raise unprocessable("invalid_group", "Question group not found")
        last = await db.scalar(
            select(func.max(QuestionVersion.version)).where(
                QuestionVersion.group_id == group.id, QuestionVersion.language == data.language
            )
        )
        if last is not None:
            raise conflict(
                "language_exists", "This task already has this language: create a new version of it"
            )
    else:
        if await db.get(Skill, data.skill_id) is None:
            raise unprocessable("invalid_skill", "Skill not found")
        group = QuestionGroup(
            id=uuid.uuid4(),
            code=f"Q-{secrets.token_hex(4).upper()}",
            skill_id=data.skill_id,
            created_by=actor.id,
        )
        db.add(group)
        await db.flush()

    version = QuestionVersion(
        id=uuid.uuid4(),
        group_id=group.id,
        version=1,
        language=data.language,
        author_id=actor.id,
        **data.model_dump(exclude={"skill_id", "group_id", "language", "options"}),
    )
    db.add(version)
    await db.flush()
    db.add_all(_options(version.id, data.options))
    await db.commit()
    return await get_version(db, version.id)


async def update_draft(
    db: AsyncSession, version_id: uuid.UUID, data: QuestionUpdateIn, actor: User
) -> QuestionVersion:
    version = await get_version(db, version_id, lock=True)
    if version.status != S.DRAFT:
        # Anything past draft is frozen: reviewers must see exactly what will be published.
        raise conflict("not_editable", "Only drafts can be edited")
    fields = data.model_dump(exclude_unset=True, exclude={"options"})
    for field, value in fields.items():
        if value is None and field in ("stem", "difficulty"):
            continue
        setattr(version, field, value)
    if data.options is not None:
        # Delete first, then insert: the (question, key) unique index forbids overlap.
        await db.execute(delete(QuestionOption).where(QuestionOption.question_version_id == version.id))
        db.add_all(_options(version.id, data.options))
    await db.commit()
    return await get_version(db, version.id)


async def publish_problems(db: AsyncSession, version: QuestionVersion) -> list[str]:
    """Why this question cannot be sent to review or published. Empty list = ready."""
    problems = []
    expected = get_settings().question_option_count
    if len(version.options) != expected:
        problems.append(f"Exactly {expected} options are required")
    if sum(o.is_correct for o in version.options) != 1:
        problems.append("Exactly one option must be correct")
    if not (version.solution or "").strip():
        problems.append("A verified step-by-step solution is required")
    if not (version.source or "").strip() or not (version.license or "").strip():
        problems.append("Source and license are required")
    skill = await db.get(Skill, version.group.skill_id)
    if skill is None or not skill.active:
        problems.append("The skill is missing or inactive")
    return problems


async def transition(
    db: AsyncSession, version_id: uuid.UUID, to: QuestionStatus, comment: str | None, actor: User
) -> QuestionVersion:
    version = await get_version(db, version_id, lock=True)
    current = version.status
    allowed = TRANSITIONS.get((current, to))
    if allowed is None:
        raise conflict("invalid_transition", f"Cannot move a question from {current} to {to}")
    if not (actor.role_codes & {str(r) for r in allowed}):
        raise forbidden()
    if to == S.DRAFT and not (comment or "").strip():
        raise unprocessable("comment_required", "Explain why the question is returned to draft")
    if (current, to) == (S.EXPERT_REVIEW, S.APPROVED) and version.author_id == actor.id:
        raise unprocessable("self_review_forbidden", "The author cannot approve their own question")
    if to in (S.EDITORIAL_REVIEW, S.PUBLISHED):
        problems = await publish_problems(db, version)
        if problems:
            raise unprocessable("question_incomplete", "The question is not ready", problems)

    now = datetime.now(UTC)
    if to == S.PUBLISHED:
        # A newer version replaces the one students currently get.
        await db.execute(
            update(QuestionVersion)
            .where(
                QuestionVersion.group_id == version.group_id,
                QuestionVersion.language == version.language,
                QuestionVersion.status == S.PUBLISHED,
            )
            .values(status=S.RETIRED, retired_at=now)
        )
        version.published_at = now
    if to == S.RETIRED:
        version.retired_at = now
    version.status = to

    db.add(
        ContentReview(
            question_version_id=version.id,
            from_status=current,
            to_status=to,
            actor_id=actor.id,
            comment=comment,
        )
    )
    if to in (S.PUBLISHED, S.RETIRED):
        audit.record(
            db, actor.id, f"question.{to}", "question_version", version.id, {"code": version.group.code}
        )
    await db.commit()
    return await get_version(db, version.id)


async def new_version(db: AsyncSession, version_id: uuid.UUID, actor: User) -> QuestionVersion:
    """Fixing a published question: copy it into a new draft, the published one stays untouched."""
    source = await get_version(db, version_id)
    if source.status not in (S.PUBLISHED, S.RETIRED):
        raise conflict("not_published", "Only published or retired questions get a new version")
    unfinished = await db.scalar(
        select(QuestionVersion.id).where(
            QuestionVersion.group_id == source.group_id,
            QuestionVersion.language == source.language,
            QuestionVersion.status.not_in([S.PUBLISHED, S.RETIRED]),
        )
    )
    if unfinished:
        raise conflict("version_in_progress", "A newer version of this question is already in progress")
    last = await db.scalar(
        select(func.max(QuestionVersion.version)).where(
            QuestionVersion.group_id == source.group_id, QuestionVersion.language == source.language
        )
    )
    copy = QuestionVersion(
        id=uuid.uuid4(),
        group_id=source.group_id,
        version=last + 1,
        language=source.language,
        stem=source.stem,
        solution=source.solution,
        difficulty=source.difficulty,
        estimated_time_sec=source.estimated_time_sec,
        source=source.source,
        license=source.license,
        author_id=actor.id,
    )
    db.add(copy)
    await db.flush()
    db.add_all(
        QuestionOption(
            question_version_id=copy.id, key=o.key, text=o.text, is_correct=o.is_correct, order=o.order
        )
        for o in source.options
    )
    await db.commit()
    return await get_version(db, copy.id)


async def list_versions(
    db: AsyncSession,
    *,
    status: QuestionStatus | None,
    language: str | None,
    skill_id: uuid.UUID | None,
    limit: int,
    offset: int,
) -> tuple[list[QuestionVersion], int]:
    query = select(QuestionVersion).join(QuestionGroup, QuestionGroup.id == QuestionVersion.group_id)
    if status:
        query = query.where(QuestionVersion.status == status)
    if language:
        query = query.where(QuestionVersion.language == language)
    if skill_id:
        query = query.where(QuestionGroup.skill_id == skill_id)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.scalars(query.order_by(QuestionVersion.created_at.desc()).limit(limit).offset(offset))
    return list(rows.unique().all()), total or 0


async def reviews(db: AsyncSession, version_id: uuid.UUID) -> list[ContentReview]:
    await get_version(db, version_id)
    rows = await db.scalars(
        select(ContentReview)
        .where(ContentReview.question_version_id == version_id)
        .order_by(ContentReview.created_at)
    )
    return list(rows.all())
