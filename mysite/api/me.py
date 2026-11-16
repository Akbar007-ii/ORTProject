from fastapi import APIRouter

from mysite.core.deps import DB, CurrentUser
from mysite.db.schema.auth import MeUpdateIn, UserOut
from mysite.services import auth as auth_service

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=UserOut)
async def get_me(user: CurrentUser) -> UserOut:
    return UserOut.from_user(user)


@router.patch("", response_model=UserOut)
async def update_me(data: MeUpdateIn, user: CurrentUser, db: DB) -> UserOut:
    # Only fields the client actually sent; roles/status/email are not in the schema at all.
    for field, value in data.model_dump(exclude_unset=True).items():
        if value is None and field in ("locale", "timezone"):
            continue  # required columns: an explicit null means "leave as is"
        setattr(user, field, value)
    await db.commit()
    return UserOut.from_user(await auth_service.get_user(db, user.id))
