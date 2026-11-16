"""Operator commands.

    python -m mysite.cli create-admin admin@example.com
    python -m mysite.cli seed-demo          # demo subject, format and mock exam
    python -m mysite.cli expire-sessions    # finalize sessions whose time ran out (for cron)
    python -m mysite.cli seed-staging --domain example.com   # demo data + test account per role

The password is asked interactively, so it never lands in shell history.
"""

import argparse
import asyncio
import getpass
import secrets
import uuid

from sqlalchemy import select

from mysite.core import security
from mysite.db.database import SessionLocal, engine
from mysite.db.models import Role, RoleCode, User, UserRole


async def create_admin(email: str, password: str) -> None:
    email = email.strip().lower()
    async with SessionLocal() as db:
        if await db.scalar(select(User.id).where(User.email == email)):
            raise SystemExit(f"User {email} already exists")
        role_ids = (
            await db.scalars(select(Role.id).where(Role.code.in_([RoleCode.ADMIN, RoleCode.STUDENT])))
        ).all()
        if len(role_ids) != 2:
            raise SystemExit("Roles are missing: run `alembic upgrade head` first")
        user = User(id=uuid.uuid4(), email=email, password_hash=await security.hash_password(password))
        db.add(user)
        await db.flush()  # user row must exist before its role rows
        for role_id in role_ids:
            db.add(UserRole(user_id=user.id, role_id=role_id))
        await db.commit()
    await engine.dispose()
    print(f"Admin {email} created")


TEST_ACCOUNTS = (
    ("admin", (RoleCode.ADMIN, RoleCode.STUDENT)),
    ("editor", (RoleCode.EDITOR,)),
    ("expert", (RoleCode.EXPERT,)),
    ("student", (RoleCode.STUDENT,)),
)


async def seed_staging(domain: str) -> list[tuple[str, str, str]]:
    """Test accounts for the frontend team, one per role, with fresh random passwords.

    Returns (role, email, password-or-note). Existing accounts are left untouched.
    """
    from mysite.config import get_settings

    if get_settings().environment == "production":
        raise SystemExit("Test accounts are not created in production")
    results = []
    async with SessionLocal() as db:
        roles = {r.code: r.id for r in (await db.scalars(select(Role))).all()}
        if not roles:
            raise SystemExit("Roles are missing: run `alembic upgrade head` first")
        for name, role_codes in TEST_ACCOUNTS:
            email = f"test-{name}@{domain}".lower()
            if await db.scalar(select(User.id).where(User.email == email)):
                results.append((name, email, "(already exists, password unchanged)"))
                continue
            password = secrets.token_urlsafe(12)
            user = User(
                id=uuid.uuid4(),
                email=email,
                display_name=f"Test {name}",
                password_hash=await security.hash_password(password),
            )
            db.add(user)
            await db.flush()
            for code in role_codes:
                db.add(UserRole(user_id=user.id, role_id=roles[code]))
            results.append((name, email, password))
        await db.commit()
    return results


async def run_seed_staging(domain: str) -> None:
    from mysite.seed import seed_demo

    accounts = await seed_staging(domain)
    async with SessionLocal() as db:
        print(await seed_demo(db))
    await engine.dispose()
    print("\nTest accounts (passwords are shown only now, pass them to the team privately):")
    for role, email, password in accounts:
        print(f"  {role:8} {email:40} {password}")


async def run_seed_demo() -> None:
    from mysite.seed import seed_demo

    async with SessionLocal() as db:
        print(await seed_demo(db))
    await engine.dispose()


async def run_expire_sessions() -> None:
    from mysite.services.sessions import expire_overdue

    async with SessionLocal() as db:
        print(f"Finalized sessions: {await expire_overdue(db)}")
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="mysite.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="Create the first administrator")
    admin.add_argument("email")
    sub.add_parser("seed-demo", help="Create demo data for trying the API")
    sub.add_parser("expire-sessions", help="Finalize sessions whose time ran out")
    staging = sub.add_parser("seed-staging", help="Demo data plus one test account per role")
    staging.add_argument("--domain", default="example.com", help="email domain of the test accounts")
    args = parser.parse_args()

    if args.command == "seed-staging":
        asyncio.run(run_seed_staging(args.domain))
    if args.command == "seed-demo":
        asyncio.run(run_seed_demo())
    if args.command == "expire-sessions":
        asyncio.run(run_expire_sessions())

    if args.command == "create-admin":
        password = getpass.getpass("Password (min 8 chars): ")
        if len(password) < 8 or password != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords do not match or are shorter than 8 characters")
        asyncio.run(create_admin(args.email, password))


if __name__ == "__main__":
    main()
