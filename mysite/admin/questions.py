import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from mysite.core.deps import DB, ContentEditor, require_roles
from mysite.core.errors import not_found
from mysite.db.models import Locale, QuestionReport, QuestionStatus, RoleCode, User
from mysite.db.schema.question import (
    QuestionAdminOut,
    QuestionCreateIn,
    QuestionPage,
    QuestionUpdateIn,
    ReviewOut,
    TransitionIn,
)
from mysite.services import questions as question_service

router = APIRouter(prefix="/questions", tags=["admin: questions"])

# Editors, experts and admins can read the bank; who may change what is decided per action.
ContentStaff = Annotated[User, Depends(require_roles(RoleCode.EDITOR, RoleCode.EXPERT, RoleCode.ADMIN))]


@router.get("", response_model=QuestionPage)
async def list_questions(
    user: ContentStaff,
    db: DB,
    status_: Annotated[QuestionStatus | None, Query(alias="status")] = None,
    language: Locale | None = None,
    skill_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> QuestionPage:
    items, total = await question_service.list_versions(
        db, status=status_, language=language, skill_id=skill_id, limit=limit, offset=offset
    )
    return QuestionPage(
        items=[QuestionAdminOut.from_version(v) for v in items], total=total, limit=limit, offset=offset
    )


@router.post("", response_model=QuestionAdminOut, status_code=status.HTTP_201_CREATED)
async def create_question(data: QuestionCreateIn, user: ContentEditor, db: DB) -> QuestionAdminOut:
    return QuestionAdminOut.from_version(await question_service.create(db, data, user))


@router.get("/versions/{version_id}", response_model=QuestionAdminOut)
async def get_question(version_id: uuid.UUID, user: ContentStaff, db: DB) -> QuestionAdminOut:
    return QuestionAdminOut.from_version(await question_service.get_version(db, version_id))


@router.patch("/versions/{version_id}", response_model=QuestionAdminOut)
async def update_question(
    version_id: uuid.UUID, data: QuestionUpdateIn, user: ContentEditor, db: DB
) -> QuestionAdminOut:
    return QuestionAdminOut.from_version(await question_service.update_draft(db, version_id, data, user))


@router.post("/versions/{version_id}/transition", response_model=QuestionAdminOut)
async def transition_question(
    version_id: uuid.UUID, data: TransitionIn, user: ContentStaff, db: DB
) -> QuestionAdminOut:
    version = await question_service.transition(db, version_id, data.to, data.comment, user)
    return QuestionAdminOut.from_version(version)


@router.post(
    "/versions/{version_id}/new-version", response_model=QuestionAdminOut, status_code=status.HTTP_201_CREATED
)
async def new_question_version(version_id: uuid.UUID, user: ContentEditor, db: DB) -> QuestionAdminOut:
    return QuestionAdminOut.from_version(await question_service.new_version(db, version_id, user))


@router.get("/versions/{version_id}/reviews", response_model=list[ReviewOut])
async def question_reviews(version_id: uuid.UUID, user: ContentStaff, db: DB) -> list:
    rows = await question_service.reviews(db, version_id)
    return [
        ReviewOut(
            from_status=r.from_status,
            to_status=r.to_status,
            actor_id=r.actor_id,
            comment=r.comment,
            created_at=r.created_at,
        )
        for r in rows
    ]


reports_router = APIRouter(prefix="/question-reports", tags=["admin: questions"])


@reports_router.get("")
async def list_reports(
    user: ContentStaff, db: DB, status_: Annotated[str, Query(alias="status")] = "open"
) -> list[dict]:
    """Complaints from students about questions and explanations, oldest first."""
    rows = await db.scalars(
        select(QuestionReport)
        .where(QuestionReport.status == status_)
        .order_by(QuestionReport.created_at)
        .limit(200)
    )
    return [
        {
            "id": r.id,
            "question_version_id": r.question_version_id,
            "kind": r.kind,
            "comment": r.comment,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in rows.all()
    ]


@reports_router.post("/{report_id}/resolve")
async def resolve_report(report_id: uuid.UUID, user: ContentStaff, db: DB) -> dict:
    report = await db.get(QuestionReport, report_id)
    if report is None:
        raise not_found("Report not found")
    report.status = "resolved"
    await db.commit()
    return {"id": report.id, "status": report.status}
