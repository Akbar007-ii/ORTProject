"""Subjects -> topics (tree) -> skills."""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.core.errors import conflict, not_found, unprocessable
from mysite.db.models import Skill, Subject, Topic
from mysite.db.schema.catalog import (
    SkillCreateIn,
    SkillOut,
    SkillUpdateIn,
    SubjectCreateIn,
    SubjectUpdateIn,
    TopicCreateIn,
    TopicNode,
    TopicOut,
    TopicUpdateIn,
)
from mysite.services import audit


async def list_subjects(db: AsyncSession, *, include_inactive: bool = False) -> list[Subject]:
    query = select(Subject).order_by(Subject.order, Subject.code)
    if not include_inactive:
        query = query.where(Subject.active.is_(True))
    return list((await db.scalars(query)).all())


async def topic_tree(
    db: AsyncSession, subject_id: uuid.UUID, *, include_inactive: bool = False
) -> list[TopicNode]:
    subject = await db.get(Subject, subject_id)
    if subject is None or (not subject.active and not include_inactive):
        raise not_found("Subject not found")

    topics = (
        await db.scalars(
            select(Topic).where(Topic.subject_id == subject_id).order_by(Topic.order, Topic.title_ru)
        )
    ).all()

    nodes: dict[uuid.UUID, TopicNode] = {}
    for topic in topics:
        if not topic.active and not include_inactive:
            continue
        node = TopicNode.model_validate(TopicOut.model_validate(topic).model_dump())
        node.skills = [
            SkillOut.model_validate(s)
            for s in sorted(topic.skills, key=lambda s: (s.order, s.code))
            if s.active or include_inactive
        ]
        nodes[topic.id] = node

    roots: list[TopicNode] = []
    for node in nodes.values():
        if node.parent_id is None:
            roots.append(node)
        elif node.parent_id in nodes:
            nodes[node.parent_id].children.append(node)
        # A child of a hidden (inactive) parent stays hidden together with it.
    return roots


async def _commit_unique(db: AsyncSession, code: str, message: str) -> None:
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise conflict(code, message) from None


async def create_subject(db: AsyncSession, data: SubjectCreateIn, actor_id: uuid.UUID) -> Subject:
    subject = Subject(id=uuid.uuid4(), **data.model_dump())
    db.add(subject)
    audit.record(db, actor_id, "subject.create", "subject", subject.id, {"code": data.code})
    await _commit_unique(db, "code_taken", "Subject code already exists")
    return subject


async def update_subject(
    db: AsyncSession, subject_id: uuid.UUID, data: SubjectUpdateIn, actor_id: uuid.UUID
) -> Subject:
    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise not_found("Subject not found")
    changes = _apply(subject, data)
    audit.record(db, actor_id, "subject.update", "subject", subject.id, {"fields": changes})
    await db.commit()
    return subject


def _apply(obj: object, data: object, *, nullable: tuple[str, ...] = ()) -> list[str]:
    """Copies fields the client sent. An explicit null is only kept for nullable fields."""
    changed = []
    for field, value in data.model_dump(exclude_unset=True).items():
        if value is None and field not in nullable:
            continue
        setattr(obj, field, value)
        changed.append(field)
    return changed


async def _check_parent(
    db: AsyncSession, subject_id: uuid.UUID, parent_id: uuid.UUID, topic_id: uuid.UUID | None
) -> None:
    """Parent must be in the same subject and must not be the topic itself or its descendant."""
    current_id: uuid.UUID | None = parent_id
    first = True
    while current_id is not None:
        if current_id == topic_id:
            raise unprocessable("topic_cycle", "A topic cannot be its own ancestor")
        parent = await db.get(Topic, current_id)
        if parent is None or (first and parent.subject_id != subject_id):
            raise unprocessable("invalid_parent", "Parent topic not found in this subject")
        first = False
        current_id = parent.parent_id


async def create_topic(db: AsyncSession, data: TopicCreateIn, actor_id: uuid.UUID) -> Topic:
    if await db.get(Subject, data.subject_id) is None:
        raise unprocessable("invalid_subject", "Subject not found")
    if data.parent_id:
        await _check_parent(db, data.subject_id, data.parent_id, None)
    topic = Topic(id=uuid.uuid4(), **data.model_dump())
    db.add(topic)
    audit.record(db, actor_id, "topic.create", "topic", topic.id)
    await db.commit()
    return topic


async def update_topic(
    db: AsyncSession, topic_id: uuid.UUID, data: TopicUpdateIn, actor_id: uuid.UUID
) -> Topic:
    topic = await db.get(Topic, topic_id)
    if topic is None:
        raise not_found("Topic not found")
    if data.parent_id:
        await _check_parent(db, topic.subject_id, data.parent_id, topic.id)
    changes = _apply(topic, data, nullable=("parent_id",))
    audit.record(db, actor_id, "topic.update", "topic", topic.id, {"fields": changes})
    await db.commit()
    return topic


async def create_skill(db: AsyncSession, data: SkillCreateIn, actor_id: uuid.UUID) -> Skill:
    if await db.get(Topic, data.topic_id) is None:
        raise unprocessable("invalid_topic", "Topic not found")
    skill = Skill(id=uuid.uuid4(), **data.model_dump())
    db.add(skill)
    audit.record(db, actor_id, "skill.create", "skill", skill.id, {"code": data.code})
    await _commit_unique(db, "code_taken", "Skill code already exists")
    return skill


async def update_skill(
    db: AsyncSession, skill_id: uuid.UUID, data: SkillUpdateIn, actor_id: uuid.UUID
) -> Skill:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise not_found("Skill not found")
    if data.topic_id and await db.get(Topic, data.topic_id) is None:
        raise unprocessable("invalid_topic", "Topic not found")
    changes = _apply(skill, data)
    audit.record(db, actor_id, "skill.update", "skill", skill.id, {"fields": changes})
    await db.commit()
    return skill
