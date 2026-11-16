"""Password hashing (Argon2id), opaque tokens and JWT access tokens."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from anyio import to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from mysite.config import get_settings

_hasher = PasswordHasher()  # Argon2id with library defaults
_ALGORITHM = "HS256"

# Verified when the user does not exist, so login takes the same time either way.
_DUMMY_HASH = _hasher.hash("timing-equalizer-not-a-real-password")


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# Argon2 is CPU-heavy on purpose: run it in a worker thread, not on the event loop.
async def hash_password(password: str) -> str:
    return await to_thread.run_sync(_hasher.hash, password)


async def verify_password(password_hash: str | None, password: str) -> bool:
    ok = await to_thread.run_sync(_verify, password_hash or _DUMMY_HASH, password)
    return ok and password_hash is not None


def new_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """Refresh/reset tokens are stored only as SHA-256 hashes."""
    return hashlib.sha256(token.encode()).hexdigest()


def create_access_token(user_id: uuid.UUID, session_id: uuid.UUID) -> tuple[str, int]:
    settings = get_settings()
    ttl = timedelta(minutes=settings.access_token_ttl_minutes)
    now = datetime.now(UTC)
    payload = {"sub": str(user_id), "sid": str(session_id), "iat": now, "exp": now + ttl}
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGORITHM), int(ttl.total_seconds())


def decode_access_token(token: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Returns (user_id, session_id) or None when the token is invalid or expired."""
    try:
        payload = jwt.decode(
            token,
            get_settings().secret_key,
            algorithms=[_ALGORITHM],
            options={"require": ["sub", "sid", "exp"]},
        )
        return uuid.UUID(payload["sub"]), uuid.UUID(payload["sid"])
    except (jwt.PyJWTError, ValueError):
        return None
