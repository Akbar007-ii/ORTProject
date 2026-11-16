"""FastAPI dependencies: current user, role checks, rate limiting helpers."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.core import security
from mysite.core.errors import forbidden, too_many_requests, unauthorized
from mysite.core.ratelimit import get_rate_limiter
from mysite.db.database import get_db
from mysite.db.models import AuthSession, RoleCode, User, UserStatus

DB = Annotated[AsyncSession, Depends(get_db)]
_bearer = HTTPBearer(auto_error=False)


@dataclass
class AuthContext:
    user: User
    session_id: object


async def get_auth_context(
    db: DB, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> AuthContext:
    if credentials is None:
        raise unauthorized()
    decoded = security.decode_access_token(credentials.credentials)
    if decoded is None:
        raise unauthorized("invalid_token", "Token is invalid or expired")
    user_id, session_id = decoded

    # The session row is checked on every request, so logout and blocking act immediately.
    user = await db.scalar(
        select(User)
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(
            User.id == user_id,
            User.status == UserStatus.ACTIVE,
            AuthSession.id == session_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > datetime.now(UTC),
        )
    )
    if user is None:
        raise unauthorized("invalid_token", "Token is invalid or expired")
    return AuthContext(user=user, session_id=session_id)


async def get_current_user(ctx: Annotated[AuthContext, Depends(get_auth_context)]) -> User:
    return ctx.user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: RoleCode) -> Callable:
    """Dependency factory: the user must hold at least one of the given roles."""
    allowed = {str(r) for r in roles}

    async def checker(user: CurrentUser) -> User:
        if not (user.role_codes & allowed):
            raise forbidden()
        return user

    return checker


AdminUser = Annotated[User, Depends(require_roles(RoleCode.ADMIN))]
ContentEditor = Annotated[User, Depends(require_roles(RoleCode.EDITOR, RoleCode.ADMIN))]


def client_ip(request: Request) -> str:
    # Behind a reverse proxy, configure uvicorn --proxy-headers so this is the real client.
    return request.client.host if request.client else "unknown"


async def enforce_rate_limit(key: str, limit: int, window: int) -> None:
    retry_after = await get_rate_limiter().hit(key, limit, window)
    if retry_after is not None:
        raise too_many_requests(retry_after)
