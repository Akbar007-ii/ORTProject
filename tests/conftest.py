"""Tests run against a real PostgreSQL (docker compose up -d), in a separate database."""

import asyncio
import os
import tempfile

os.environ["MEDIA_DIR"] = tempfile.mkdtemp(prefix="orto-media-")
os.environ["SMTP_HOST"] = ""  # never send real mail from tests

TEST_DB = "orto_test"
ADMIN_DSN = os.environ.get("TEST_ADMIN_DSN", "postgresql://orto:orto_dev@localhost:5433/postgres")
os.environ["DATABASE_URL"] = f"postgresql+asyncpg://orto:orto_dev@localhost:5433/{TEST_DB}"
os.environ["REDIS_URL"] = ""  # in-memory rate limiter
os.environ["ENVIRONMENT"] = "local"
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production-use-0123456789")

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from mysite.app import app  # noqa: E402
from mysite.core import mailer  # noqa: E402
from mysite.core.ratelimit import get_rate_limiter  # noqa: E402
from mysite.db.database import SessionLocal, engine  # noqa: E402
from mysite.db.models import Role, User, UserRole  # noqa: E402

PASSWORD = "correct-horse-battery"


async def _recreate_database() -> None:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def database():
    """Fresh database built by the real Alembic migrations, so they are tested too."""
    asyncio.run(_recreate_database())
    command.upgrade(Config("alembic.ini"), "head")
    yield


@pytest.fixture(autouse=True)
async def clean_state(database):
    yield
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE users, user_consents, user_roles, auth_sessions, password_reset_tokens, "
                "audit_logs, subjects, topics, skills, question_groups, question_versions, "
                "question_options, content_reviews, exam_formats, exam_format_versions, exam_sections, "
                "score_scales, score_scale_points, tests, test_questions, test_sessions, "
                "session_sections, session_questions, checkpoints, attempts, skill_progress, "
                "practice_sessions, practice_questions, mistake_cards, review_events, "
                "skill_section_weights, user_goals, goal_milestones, score_predictions, learning_plans, "
                "plan_items, user_achievements, tutor_explanations, ai_usage_logs, question_reports, "
                "telegram_accounts, telegram_link_tokens, notification_preferences, notification_log, "
                "universities, university_programs, media, import_jobs CASCADE"
            )
        )
    get_rate_limiter().clear()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def db():
    async with SessionLocal() as session:
        yield session


@pytest.fixture
def reset_tokens(monkeypatch) -> dict[str, str]:
    """Captures password reset tokens instead of 'sending' them: {email: token}."""
    sent: dict[str, str] = {}

    async def fake_send(email: str, token: str, locale: str) -> None:
        sent[email] = token

    monkeypatch.setattr(mailer, "send_password_reset", fake_send)
    return sent


def register_payload(email: str = "aibek@example.com", **overrides) -> dict:
    return {
        "email": email,
        "password": PASSWORD,
        "accept_terms": True,
        "accept_privacy": True,
        **overrides,
    }


async def register(client: AsyncClient, email: str = "aibek@example.com") -> dict:
    response = await client.post("/api/v1/auth/register", json=register_payload(email))
    assert response.status_code == 201, response.text
    return response.json()


def bearer(auth: dict) -> dict:
    return {"Authorization": f"Bearer {auth['tokens']['access_token']}"}


async def register_with_role(client: AsyncClient, email: str, role: str) -> dict:
    """Registers a user and grants a role directly in the DB (as the CLI does for the first admin)."""
    auth = await register(client, email)
    async with SessionLocal() as session:
        user_id = await session.scalar(select(User.id).where(User.email == email))
        role_id = await session.scalar(select(Role.id).where(Role.code == role))
        session.add(UserRole(user_id=user_id, role_id=role_id))
        await session.commit()
    return auth
