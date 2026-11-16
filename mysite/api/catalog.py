import uuid

from fastapi import APIRouter

from mysite.core.deps import DB
from mysite.db.schema.catalog import SubjectOut, TopicNode
from mysite.services import catalog as catalog_service

router = APIRouter(tags=["catalog"])


@router.get("/subjects", response_model=list[SubjectOut])
async def list_subjects(db: DB) -> list:
    return await catalog_service.list_subjects(db)


@router.get("/subjects/{subject_id}/topics", response_model=list[TopicNode])
async def subject_topics(subject_id: uuid.UUID, db: DB) -> list[TopicNode]:
    """Topic tree of a subject with skills. Only active items."""
    return await catalog_service.topic_tree(db, subject_id)
