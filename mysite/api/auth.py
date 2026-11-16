from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from mysite.config import get_settings
from mysite.core.deps import DB, AuthContext, client_ip, enforce_rate_limit, get_auth_context
from mysite.core.ratelimit import get_rate_limiter
from mysite.db.schema.auth import (
    AuthOut,
    LoginIn,
    PasswordResetConfirmIn,
    PasswordResetRequestIn,
    RefreshIn,
    RegisterIn,
    TokenOut,
    UserOut,
)
from mysite.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])
HOUR = 3600


@router.post("/register", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
async def register(data: RegisterIn, request: Request, db: DB) -> AuthOut:
    await enforce_rate_limit(f"register:{client_ip(request)}", get_settings().register_max_per_hour, HOUR)
    user, tokens = await auth_service.register(db, data, request.headers.get("user-agent"))
    return AuthOut(user=UserOut.from_user(user), tokens=tokens)


@router.post("/login", response_model=AuthOut)
async def login(data: LoginIn, request: Request, db: DB) -> AuthOut:
    settings = get_settings()
    ip = client_ip(request)
    key = f"login:{data.email}:{ip}"
    # Per account+IP stops guessing one password; per IP stops spraying many accounts.
    await enforce_rate_limit(key, settings.login_max_attempts, settings.login_window_seconds)
    await enforce_rate_limit(
        f"login-ip:{ip}", settings.login_max_attempts * 10, settings.login_window_seconds
    )
    user, tokens = await auth_service.login(db, data.email, data.password, request.headers.get("user-agent"))
    await get_rate_limiter().reset(key)
    return AuthOut(user=UserOut.from_user(user), tokens=tokens)


@router.post("/refresh", response_model=TokenOut)
async def refresh(data: RefreshIn, db: DB) -> TokenOut:
    return await auth_service.refresh(db, data.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: RefreshIn, db: DB) -> Response:
    await auth_service.logout(db, data.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(ctx: Annotated[AuthContext, Depends(get_auth_context)], db: DB) -> Response:
    await auth_service.revoke_all_sessions(db, ctx.user.id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
async def password_reset_request(data: PasswordResetRequestIn, request: Request, db: DB) -> dict:
    await enforce_rate_limit(f"reset:{client_ip(request)}", get_settings().password_reset_max_per_hour, HOUR)
    await auth_service.request_password_reset(db, data.email)
    # Same answer whether or not the email exists.
    return {"status": "accepted"}


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def password_reset_confirm(data: PasswordResetConfirmIn, request: Request, db: DB) -> Response:
    await enforce_rate_limit(f"reset-confirm:{client_ip(request)}", 20, HOUR)
    await auth_service.confirm_password_reset(db, data.token, data.new_password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
