"""Question images. The file type is detected from its bytes, never trusted from the client."""

import hashlib
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.config import get_settings
from mysite.core.errors import AppError, not_found, unprocessable
from mysite.db.models import Media, User
from mysite.services import audit

# Raster formats only: SVG can carry scripts, so it is not accepted.
SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def detect_type(data: bytes) -> str | None:
    for signature, content_type in SIGNATURES:
        if data.startswith(signature):
            return content_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def path_for(media_id: uuid.UUID) -> Path:
    return Path(get_settings().media_dir) / media_id.hex


async def upload(db: AsyncSession, data: bytes, actor: User) -> Media:
    settings = get_settings()
    if len(data) > settings.media_max_bytes:
        raise AppError(
            413, "file_too_large", f"The file is larger than {settings.media_max_bytes // 1024} KB"
        )
    content_type = detect_type(data)
    if content_type is None:
        raise unprocessable("unsupported_file", "Only PNG, JPEG, GIF and WebP images are accepted")

    digest = hashlib.sha256(data).hexdigest()
    existing = await db.scalar(select(Media).where(Media.sha256 == digest))
    if existing is not None:
        return existing  # the same picture uploaded twice is stored once

    media = Media(
        id=uuid.uuid4(), content_type=content_type, size=len(data), sha256=digest, uploaded_by=actor.id
    )
    path = path_for(media.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    db.add(media)
    audit.record(db, actor.id, "media.upload", "media", media.id, {"size": len(data)})
    await db.commit()
    return media


async def get(db: AsyncSession, media_id: uuid.UUID) -> tuple[Media, Path]:
    media = await db.get(Media, media_id)
    path = path_for(media_id)
    if media is None or not path.is_file():
        raise not_found("File not found")
    return media, path
