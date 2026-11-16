"""AI tutor, question reports and Telegram linking."""

from fastapi import APIRouter, Response, status

from mysite.core.deps import DB, CurrentUser
from mysite.db.schema.learning import LinkTokenOut, ReportIn, TutorIn, TutorOut
from mysite.services import telegram as telegram_service
from mysite.services import tutor as tutor_service

router = APIRouter()


@router.post("/tutor/explain", response_model=TutorOut, tags=["tutor"])
async def tutor_explain(data: TutorIn, user: CurrentUser, db: DB) -> TutorOut:
    """Explains the verified solution of a question the student has already answered.
    `source` says whether the text was written by the AI or is the verified solution itself."""
    return await tutor_service.explain(db, user, data.question_version_id, data.mode)


@router.post("/question-reports", status_code=status.HTTP_201_CREATED, tags=["tutor"])
async def report_question(data: ReportIn, user: CurrentUser, db: DB) -> dict:
    """ "This question or explanation is wrong or unclear": goes to the content team."""
    return {"id": await tutor_service.report(db, user, data.question_version_id, data.kind, data.comment)}


@router.post("/telegram/link-token", response_model=LinkTokenOut, tags=["telegram"])
async def telegram_link_token(user: CurrentUser, db: DB) -> LinkTokenOut:
    """A one-time, short-lived token. The student sends it to the bot as /start <token>."""
    return await telegram_service.create_link_token(db, user)


@router.delete("/telegram/link", status_code=status.HTTP_204_NO_CONTENT, tags=["telegram"])
async def telegram_unlink(user: CurrentUser, db: DB) -> Response:
    await telegram_service.unlink(db, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
