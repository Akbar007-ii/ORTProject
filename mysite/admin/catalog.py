import uuid

from fastapi import APIRouter, status

from mysite.core.deps import DB, ContentEditor
from mysite.db.schema.catalog import (
    SkillCreateIn,
    SkillOut,
    SkillUpdateIn,
    SubjectCreateIn,
    SubjectOut,
    SubjectUpdateIn,
    TopicCreateIn,
    TopicNode,
    TopicOut,
    TopicUpdateIn,
)
from mysite.services import catalog as catalog_service

# Nothing is deleted here: content is switched off with active=false so history stays intact.
router = APIRouter(tags=["admin: catalog"])


@router.get("/subjects", response_model=list[SubjectOut])
async def list_subjects(user: ContentEditor, db: DB) -> list:
    return await catalog_service.list_subjects(db, include_inactive=True)


@router.post("/subjects", response_model=SubjectOut, status_code=status.HTTP_201_CREATED)
async def create_subject(data: SubjectCreateIn, user: ContentEditor, db: DB):
    return await catalog_service.create_subject(db, data, user.id)


@router.patch("/subjects/{subject_id}", response_model=SubjectOut)
async def update_subject(subject_id: uuid.UUID, data: SubjectUpdateIn, user: ContentEditor, db: DB):
    return await catalog_service.update_subject(db, subject_id, data, user.id)


@router.get("/subjects/{subject_id}/topics", response_model=list[TopicNode])
async def subject_topics(subject_id: uuid.UUID, user: ContentEditor, db: DB) -> list[TopicNode]:
    return await catalog_service.topic_tree(db, subject_id, include_inactive=True)


@router.post("/topics", response_model=TopicOut, status_code=status.HTTP_201_CREATED)
async def create_topic(data: TopicCreateIn, user: ContentEditor, db: DB):
    return await catalog_service.create_topic(db, data, user.id)


@router.patch("/topics/{topic_id}", response_model=TopicOut)
async def update_topic(topic_id: uuid.UUID, data: TopicUpdateIn, user: ContentEditor, db: DB):
    return await catalog_service.update_topic(db, topic_id, data, user.id)


@router.post("/skills", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
async def create_skill(data: SkillCreateIn, user: ContentEditor, db: DB):
    return await catalog_service.create_skill(db, data, user.id)


@router.patch("/skills/{skill_id}", response_model=SkillOut)
async def update_skill(skill_id: uuid.UUID, data: SkillUpdateIn, user: ContentEditor, db: DB):
    return await catalog_service.update_skill(db, skill_id, data, user.id)
