"""Reminders and weekly reports in Telegram, sent by the background worker.

Rules from the spec: nothing is sent until the student turns it on; never during quiet hours;
at most one daily reminder and one weekly report per local day (enforced by a unique row, so a
worker that runs twice, or two workers at once, cannot double-send); a student who blocked
the bot is switched off instead of being retried forever.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.db.models import (
    NotificationLog,
    NotificationPreference,
    TelegramAccount,
    User,
    UserStatus,
)
from mysite.services import stats
from mysite.services import telegram as tg

logger = logging.getLogger("mysite.notifications")
MAX_ATTEMPTS = 3


class BotBlocked(Exception):
    """The student blocked the bot or deleted the chat."""


# (telegram user id, text) -> None; raises BotBlocked or any other error.
Sender = Callable[[int, str], Awaitable[None]]


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # e.g. 22..8 crosses midnight


async def weekly_text(db: AsyncSession, user: User) -> str:
    report = await stats.weekly_report(db, user)
    ky = user.locale == "ky"
    lines = ["📊 Жуманын жыйынтыгы" if ky else "📊 Итоги недели"]
    lines.append(
        (f"Чечилди: {report.solved} (өткөн жумада {report.solved_previous})")
        if ky
        else (f"Решено: {report.solved} (на прошлой неделе {report.solved_previous})")
    )
    if report.accuracy is not None:
        lines.append(f"{'Тактык' if ky else 'Точность'}: {round(report.accuracy * 100)}%")
    lines.append(f"{'План' if ky else 'План'}: {report.plan_done}/{report.plan_total}")
    if report.score_now is not None:
        change = ""
        if report.score_week_ago is not None:
            delta = report.score_now - report.score_week_ago
            change = f" ({'+' if delta >= 0 else ''}{delta:g})"
        lines.append(f"{'Болжолдуу балл' if ky else 'Ориентировочный балл'}: {report.score_now:g}{change}")
    if report.next_milestone is not None:
        lines.append(f"{'Кийинки этап' if ky else 'Следующий этап'}: {report.next_milestone:g}")
    return "\n".join(lines)


async def daily_text(db: AsyncSession, user: User) -> str:
    greeting = "🔔 Бүгүнкү окуу" if user.locale == "ky" else "🔔 Учёба на сегодня"
    return f"{greeting}\n\n{await tg.today_text(db, user)}\n\n/today /review"


async def _claim(db: AsyncSession, user_id: uuid.UUID, kind: str, local_date) -> NotificationLog | None:
    """Reserves today's message of this kind. Returns None if it was already sent or gave up."""
    await db.execute(
        insert(NotificationLog)
        .values(id=uuid.uuid4(), user_id=user_id, kind=kind, local_date=local_date)
        .on_conflict_do_nothing(index_elements=["user_id", "kind", "local_date"])
    )
    row = await db.scalar(
        select(NotificationLog)
        .where(
            NotificationLog.user_id == user_id,
            NotificationLog.kind == kind,
            NotificationLog.local_date == local_date,
        )
        .with_for_update(skip_locked=True)  # another worker holds it: leave it to them
    )
    if row is None or row.status in ("sent", "blocked") or row.attempts >= MAX_ATTEMPTS:
        return None
    return row


async def run_once(db: AsyncSession, send: Sender, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    counts = {"sent": 0, "failed": 0, "blocked": 0}
    rows = (
        await db.execute(
            select(User, NotificationPreference, TelegramAccount.telegram_user_id)
            .join(NotificationPreference, NotificationPreference.user_id == User.id)
            .join(TelegramAccount, TelegramAccount.user_id == User.id)
            .where(NotificationPreference.telegram_enabled.is_(True), User.status == UserStatus.ACTIVE)
        )
    ).all()

    for user, prefs, chat_id in rows:
        local = now.astimezone(ZoneInfo(user.timezone))
        if in_quiet_hours(local.hour, prefs.quiet_start, prefs.quiet_end) or local.hour < prefs.reminder_hour:
            continue
        kinds = ["daily"]
        if prefs.weekly_report and local.weekday() == 6:  # Sunday
            kinds.append("weekly")
        for kind in kinds:
            log = await _claim(db, user.id, kind, local.date())
            if log is None:
                await db.commit()
                continue
            text = await (daily_text if kind == "daily" else weekly_text)(db, user)
            log.attempts += 1
            try:
                await send(chat_id, text)
                log.status = "sent"
                counts["sent"] += 1
            except BotBlocked:
                log.status = "blocked"
                await db.execute(
                    update(NotificationPreference)
                    .where(NotificationPreference.user_id == user.id)
                    .values(telegram_enabled=False)
                )
                counts["blocked"] += 1
            except Exception:  # noqa: BLE001 - one failing chat must not stop the others
                logger.exception("Sending %s reminder failed", kind)
                log.status = "failed"  # retried on the next run, up to MAX_ATTEMPTS
                counts["failed"] += 1
            await db.commit()
    return counts


async def get_preferences(db: AsyncSession, user: User) -> NotificationPreference:
    prefs = await db.get(NotificationPreference, user.id)
    if prefs is None:
        prefs = NotificationPreference(user_id=user.id)
        db.add(prefs)
        await db.commit()
        prefs = await db.get(NotificationPreference, user.id)
    return prefs


async def update_preferences(db: AsyncSession, user: User, changes: dict) -> NotificationPreference:
    prefs = await get_preferences(db, user)
    for field, value in changes.items():
        if value is not None:
            setattr(prefs, field, value)
    await db.commit()
    return prefs
