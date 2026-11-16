"""Admin: question import, image upload, universities."""

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile, status
from pydantic import Field

from mysite.config import get_settings
from mysite.core.deps import DB, AdminUser, ContentEditor
from mysite.core.errors import AppError
from mysite.db.schema.auth import InputModel
from mysite.db.schema.catalog import Code
from mysite.services import importer
from mysite.services import media as media_service
from mysite.services import universities as university_service

router = APIRouter(tags=["admin: content"])
MAX_IMPORT_BYTES = 5 * 1024 * 1024


async def _read(upload: UploadFile, limit: int) -> bytes:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise AppError(413, "file_too_large", f"The file is larger than {limit // 1024} KB")
    return data


@router.post("/questions/import")
async def import_questions(
    user: ContentEditor,
    db: DB,
    file: Annotated[UploadFile, File(description=".csv or .json, see services/importer.py for the columns")],
    dry_run: Annotated[bool, Query()] = True,
) -> dict:
    """Checks every row first. With dry_run=false and no errors, creates the questions as drafts.
    A file with any bad row imports nothing; re-sending the same rows skips them."""
    report = await importer.run(db, await _read(file, MAX_IMPORT_BYTES), file.filename or "", dry_run, user)
    return {
        "job_id": report.job_id,
        "dry_run": report.dry_run,
        "ok": report.ok,
        "total": report.total,
        "created": report.created,  # with dry_run: how many would be created
        "skipped": report.skipped,
        "errors": report.errors,
    }


@router.post("/media", status_code=status.HTTP_201_CREATED)
async def upload_media(user: ContentEditor, db: DB, file: Annotated[UploadFile, File()]) -> dict:
    """Upload a picture for a question; put the returned `markdown` into the question text."""
    media = await media_service.upload(db, await _read(file, get_settings().media_max_bytes), user)
    url = f"/api/v1/media/{media.id}"
    return {
        "id": media.id,
        "url": url,
        "markdown": f"![]({url})",
        "content_type": media.content_type,
        "size": media.size,
    }


class UniversityIn(InputModel):
    code: Code
    title_ky: Annotated[str, Field(min_length=1, max_length=200)]
    title_ru: Annotated[str, Field(min_length=1, max_length=200)]
    city: Annotated[str, Field(max_length=80)] | None = None
    active: bool = True


class ProgramIn(InputModel):
    code: Code
    title_ky: Annotated[str, Field(min_length=1, max_length=200)]
    title_ru: Annotated[str, Field(min_length=1, max_length=200)]
    year: Annotated[int, Field(ge=2000, le=2100)]
    threshold_score: Annotated[float, Field(ge=0, le=1000)]
    source: Annotated[str, Field(min_length=3, max_length=255)]  # required: no number without a source
    active: bool = True


class ProgramUpdateIn(InputModel):
    threshold_score: Annotated[float, Field(ge=0, le=1000)] | None = None
    source: Annotated[str, Field(min_length=3, max_length=255)] | None = None
    active: bool | None = None


@router.put("/universities")
async def upsert_university(data: UniversityIn, user: AdminUser, db: DB) -> dict:
    university = await university_service.upsert_university(db, data.model_dump(), user)
    return {"id": university.id, "code": university.code}


@router.post("/universities/{university_id}/programs", status_code=status.HTTP_201_CREATED)
async def add_program(university_id: uuid.UUID, data: ProgramIn, user: AdminUser, db: DB) -> dict:
    program = await university_service.add_program(db, university_id, data.model_dump(), user)
    return {"id": program.id}


@router.patch("/university-programs/{program_id}")
async def update_program(program_id: uuid.UUID, data: ProgramUpdateIn, user: AdminUser, db: DB) -> dict:
    changes = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    program = await university_service.update_program(db, program_id, changes, user)
    return {"id": program.id, "threshold_score": program.threshold_score, "active": program.active}
