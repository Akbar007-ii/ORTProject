import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from mysite.db.models import AuditLog


def record(
    db: AsyncSession,
    actor_id: uuid.UUID,
    action: str,
    object_type: str,
    object_id: uuid.UUID | str | None = None,
    meta: dict | None = None,
) -> None:
    """Adds an audit row to the current transaction: it commits or rolls back with the action."""
    db.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            object_type=object_type,
            object_id=str(object_id) if object_id else None,
            meta=meta,
        )
    )
