"""Auth domain logic. Routes stay thin; the Telegram bot will reuse these functions."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.config import get_settings
from mysite.core import mailer, security
from mysite.core.errors import conflict, unauthorized, unprocessable
from mysite.db.models import (
    AuthSession,
    ConsentType,
    PasswordResetToken,
    Role,
    RoleCode,
    User,
    UserConsent,
    UserRole,
    UserStatus,
)
from mysite.db.schema.auth import RegisterIn, TokenOut


def _now() -> datetime:
    return datetime.now(UTC)


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Fresh copy of the user with roles loaded."""
    result = await db.execute(
        select(User).where(User.id == user_id).execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _open_session(db: AsyncSession, user: User, user_agent: str | None) -> TokenOut:
    refresh_token = security.new_opaque_token()
    session = AuthSession(
        id=uuid.uuid4(),
        user_id=user.id,
        refresh_token_hash=security.hash_token(refresh_token),
        user_agent=(user_agent or "")[:255] or None,
        expires_at=_now() + timedelta(days=get_settings().refresh_token_ttl_days),
    )
    db.add(session)
    access_token, expires_in = security.create_access_token(user.id, session.id)
    return TokenOut(access_token=access_token, expires_in=expires_in, refresh_token=refresh_token)


async def register(db: AsyncSession, data: RegisterIn, user_agent: str | None) -> tuple[User, TokenOut]:
    settings = get_settings()
    if await db.scalar(select(User.id).where(User.email == data.email)):
        raise conflict("email_taken", "This email is already registered")

    student_role_id = await db.scalar(select(Role.id).where(Role.code == RoleCode.STUDENT))
    if student_role_id is None:
        raise RuntimeError("Role 'student' is missing: run migrations")

    user = User(
        id=uuid.uuid4(),
        email=data.email,
        password_hash=await security.hash_password(data.password),
        display_name=data.display_name,
        locale=data.locale,
        timezone=data.timezone,
    )
    db.add(user)
    try:
        # Insert the user first: the rows below reference it by foreign key.
        await db.flush()
    except IntegrityError:
        # Two registrations with the same email raced; the unique index decided.
        await db.rollback()
        raise conflict("email_taken", "This email is already registered") from None
    db.add(UserRole(user_id=user.id, role_id=student_role_id))
    db.add(UserConsent(user_id=user.id, type=ConsentType.TERMS, document_version=settings.terms_version))
    db.add(UserConsent(user_id=user.id, type=ConsentType.PRIVACY, document_version=settings.privacy_version))
    tokens = await _open_session(db, user, user_agent)
    await db.commit()

    return await get_user(db, user.id), tokens


async def login(db: AsyncSession, email: str, password: str, user_agent: str | None) -> tuple[User, TokenOut]:
    user = await db.scalar(select(User).where(User.email == email))
    # Always verify (against a dummy hash if needed) so timing does not reveal existing emails.
    password_ok = await security.verify_password(user.password_hash if user else None, password)
    if user is None or not password_ok or user.status != UserStatus.ACTIVE:
        raise unauthorized("invalid_credentials", "Invalid email or password")

    tokens = await _open_session(db, user, user_agent)
    await db.commit()
    return user, tokens


async def refresh(db: AsyncSession, refresh_token: str) -> TokenOut:
    """Issues a new access token. The refresh token is not rotated on purpose:
    on a weak connection a lost response would otherwise log the student out."""
    row = (
        await db.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.refresh_token_hash == security.hash_token(refresh_token))
        )
    ).first()
    if row is None:
        raise unauthorized("invalid_refresh_token", "Session expired, sign in again")
    session, user = row
    if session.revoked_at or session.expires_at <= _now() or user.status != UserStatus.ACTIVE:
        raise unauthorized("invalid_refresh_token", "Session expired, sign in again")

    access_token, expires_in = security.create_access_token(user.id, session.id)
    return TokenOut(access_token=access_token, expires_in=expires_in, refresh_token=refresh_token)


async def logout(db: AsyncSession, refresh_token: str) -> None:
    """Idempotent: an unknown or already revoked token is not an error."""
    await db.execute(
        update(AuthSession)
        .where(
            AuthSession.refresh_token_hash == security.hash_token(refresh_token),
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=_now())
    )
    await db.commit()


async def revoke_all_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


async def request_password_reset(db: AsyncSession, email: str) -> None:
    """Never reveals whether the email exists: the caller always answers 202."""
    user = await db.scalar(select(User).where(User.email == email, User.status == UserStatus.ACTIVE))
    if user is None:
        return
    token = security.new_opaque_token()
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=security.hash_token(token),
            expires_at=_now() + timedelta(minutes=get_settings().password_reset_ttl_minutes),
        )
    )
    await db.commit()
    await mailer.send_password_reset(email, token, user.locale)


async def confirm_password_reset(db: AsyncSession, token: str, new_password: str) -> None:
    password_hash = await security.hash_password(new_password)
    now = _now()
    # Marking the token used in one UPDATE makes it single-use even under concurrent requests.
    user_id = await db.scalar(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.token_hash == security.hash_token(token),
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(PasswordResetToken.user_id)
    )
    if user_id is None:
        await db.rollback()
        raise unprocessable("invalid_reset_token", "Reset link is invalid or expired")

    await db.execute(update(User).where(User.id == user_id).values(password_hash=password_hash))
    # Other unused reset links for this user die too, and every device is signed out.
    await db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
    )
    await revoke_all_sessions(db, user_id)
    await db.commit()
