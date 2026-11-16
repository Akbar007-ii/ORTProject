import uuid
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field

from mysite.db.models import GradeStatus, Locale, User


class InputModel(BaseModel):
    """Request bodies reject unknown fields, e.g. a client trying to send `roles`."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _valid_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Unknown timezone") from exc
    return value


Email = Annotated[EmailStr, AfterValidator(_normalize_email)]
Password = Annotated[str, Field(min_length=8, max_length=128)]
Timezone = Annotated[str, Field(max_length=64), AfterValidator(_valid_timezone)]
DisplayName = Annotated[str, Field(min_length=1, max_length=50)]


class RegisterIn(InputModel):
    model_config = ConfigDict(extra="forbid")  # do not strip whitespace from passwords

    email: Email
    password: Password
    display_name: DisplayName | None = None
    locale: Locale = Locale.KY
    timezone: Timezone = "Asia/Bishkek"
    # Must be literally true: registration is impossible without both consents.
    accept_terms: Literal[True]
    accept_privacy: Literal[True]


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Email
    password: str = Field(max_length=128)


class RefreshIn(InputModel):
    refresh_token: str = Field(max_length=256)


class PasswordResetRequestIn(InputModel):
    email: Email


class PasswordResetConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(max_length=256)
    new_password: Password


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str | None
    display_name: str | None
    locale: Locale
    timezone: str
    grade_status: GradeStatus | None
    roles: list[str]
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> "UserOut":
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            locale=Locale(user.locale),
            timezone=user.timezone,
            grade_status=GradeStatus(user.grade_status) if user.grade_status else None,
            roles=sorted(user.role_codes),
            created_at=user.created_at,
        )


class AuthOut(BaseModel):
    user: UserOut
    tokens: TokenOut


class MeUpdateIn(InputModel):
    display_name: DisplayName | None = None
    locale: Locale | None = None
    timezone: Timezone | None = None
    grade_status: GradeStatus | None = None
