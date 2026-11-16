"""The student's account: data export, deletion, notification settings; plus universities,
exam strategy and media that students read."""

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from mysite.core.deps import DB, CurrentUser, enforce_rate_limit
from mysite.db.schema.auth import InputModel
from mysite.services import account as account_service
from mysite.services import media as media_service
from mysite.services import notifications as notification_service
from mysite.services import strategy as strategy_service
from mysite.services import universities as university_service

router = APIRouter()


class DeleteAccountIn(InputModel):
    password: str = Field(max_length=128)
    confirm: Annotated[str, Field(pattern="^DELETE$")]  # the client makes the user type it


class NotificationsIn(InputModel):
    telegram_enabled: bool | None = None
    reminder_hour: Annotated[int, Field(ge=0, le=23)] | None = None
    quiet_start: Annotated[int, Field(ge=0, le=23)] | None = None
    quiet_end: Annotated[int, Field(ge=0, le=23)] | None = None
    weekly_report: bool | None = None


class NotificationsOut(BaseModel):
    telegram_enabled: bool
    telegram_linked: bool
    reminder_hour: int
    quiet_start: int
    quiet_end: int
    weekly_report: bool


@router.get("/account/export", tags=["account"])
async def export_account(user: CurrentUser, db: DB) -> Response:
    """Everything stored about the student, as a downloadable JSON file."""
    await enforce_rate_limit(f"export:{user.id}", 5, 3600)
    data = await account_service.export(db, user)
    name = f"orto-export-{datetime.now(UTC):%Y%m%d}.json"
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


@router.delete("/account", status_code=status.HTTP_204_NO_CONTENT, tags=["account"])
async def delete_account(data: DeleteAccountIn, user: CurrentUser, db: DB) -> Response:
    """Deletes the account and all personal data at once. Cannot be undone."""
    await enforce_rate_limit(f"delete:{user.id}", 5, 3600)
    await account_service.delete_account(db, user, data.password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _notifications_out(db, user) -> NotificationsOut:
    from mysite.db.models import TelegramAccount

    prefs = await notification_service.get_preferences(db, user)
    return NotificationsOut(
        telegram_enabled=prefs.telegram_enabled,
        telegram_linked=await db.get(TelegramAccount, user.id) is not None,
        reminder_hour=prefs.reminder_hour,
        quiet_start=prefs.quiet_start,
        quiet_end=prefs.quiet_end,
        weekly_report=prefs.weekly_report,
    )


@router.get("/me/notifications", response_model=NotificationsOut, tags=["account"])
async def get_notifications(user: CurrentUser, db: DB) -> NotificationsOut:
    return await _notifications_out(db, user)


@router.patch("/me/notifications", response_model=NotificationsOut, tags=["account"])
async def update_notifications(data: NotificationsIn, user: CurrentUser, db: DB) -> NotificationsOut:
    """Reminders are off until the student turns them on here. Hours are in the student's timezone."""
    await notification_service.update_preferences(db, user, data.model_dump(exclude_unset=True))
    return await _notifications_out(db, user)


@router.get("/universities/programs", tags=["goal and plan"])
async def university_programs(
    db: DB,
    q: Annotated[str | None, Query(max_length=100)] = None,
    max_score: Annotated[float | None, Query(ge=0)] = None,
) -> list[dict]:
    """Past thresholds for state-funded places, with the year and the source. A guide, not a guarantee."""
    return await university_service.list_programs(db, q, max_score)


@router.get("/sessions/{session_id}/strategy", tags=["exams"])
async def session_strategy(session_id: uuid.UUID, user: CurrentUser, db: DB) -> dict:
    """Timing advice for a finished test: slow and rushed questions, sections that ran out of time."""
    return await strategy_service.analyze(db, user, session_id)


@router.get("/media/{media_id}", tags=["content"], include_in_schema=True)
async def get_media(media_id: uuid.UUID, db: DB) -> FileResponse:
    media, path = await media_service.get(db, media_id)
    return FileResponse(
        path,
        media_type=media.content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"},
    )
