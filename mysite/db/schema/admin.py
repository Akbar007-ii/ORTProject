import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from mysite.db.models import RoleCode, User, UserStatus
from mysite.db.schema.auth import InputModel


class UserRolesIn(InputModel):
    roles: list[RoleCode] = Field(min_length=1, max_length=len(RoleCode))


class AdminUserOut(BaseModel):
    id: uuid.UUID
    email: str | None
    display_name: str | None
    status: UserStatus
    roles: list[str]
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> "AdminUserOut":
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            status=UserStatus(user.status),
            roles=sorted(user.role_codes),
            created_at=user.created_at,
        )


class AdminUserPage(BaseModel):
    items: list[AdminUserOut]
    total: int
    limit: int
    offset: int
