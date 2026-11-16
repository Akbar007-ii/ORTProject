import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import delete, func, select

from mysite.core.deps import DB, AdminUser
from mysite.core.errors import not_found, unprocessable
from mysite.db.models import Role, RoleCode, User, UserRole
from mysite.db.schema.admin import AdminUserOut, AdminUserPage, UserRolesIn
from mysite.services import audit
from mysite.services import auth as auth_service

router = APIRouter(prefix="/users", tags=["admin: users"])


@router.get("", response_model=AdminUserPage)
async def list_users(
    admin: AdminUser,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminUserPage:
    total = await db.scalar(select(func.count()).select_from(User))
    users = (
        await db.scalars(select(User).order_by(User.created_at.desc()).limit(limit).offset(offset))
    ).all()
    return AdminUserPage(
        items=[AdminUserOut.from_user(u) for u in users], total=total or 0, limit=limit, offset=offset
    )


@router.put("/{user_id}/roles", response_model=AdminUserOut)
async def set_user_roles(user_id: uuid.UUID, data: UserRolesIn, admin: AdminUser, db: DB) -> AdminUserOut:
    """Replaces the user's roles. Only an admin can do it; every change is audited."""
    target = await auth_service.get_user(db, user_id)
    if target is None:
        raise not_found("User not found")

    new_codes = {str(code) for code in data.roles}
    if target.id == admin.id and RoleCode.ADMIN not in new_codes:
        # Otherwise the last admin could lock everyone out of administration.
        raise unprocessable("cannot_remove_own_admin", "You cannot remove your own admin role")

    old_codes = target.role_codes
    roles = (await db.scalars(select(Role).where(Role.code.in_(new_codes)))).all()
    await db.execute(delete(UserRole).where(UserRole.user_id == target.id))
    for role in roles:
        db.add(UserRole(user_id=target.id, role_id=role.id, assigned_by=admin.id))
    audit.record(
        db,
        admin.id,
        "user.roles.set",
        "user",
        target.id,
        {"before": sorted(old_codes), "after": sorted(new_codes)},
    )
    await db.commit()
    return AdminUserOut.from_user(await auth_service.get_user(db, target.id))
