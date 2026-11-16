"""Telegram side of the platform, without any Telegram library in it.

Linking, and the text of every bot reply, live here so they can be tested directly. The bot
(mysite/bot.py) only passes messages in and out. It uses the same services as the website,
so there is one history of answers.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.config import get_settings
from mysite.core import security
from mysite.core.errors import AppError
from mysite.db.models import TelegramAccount, TelegramLinkToken, User, UserStatus
from mysite.db.schema.learning import LinkTokenOut
from mysite.services import goals, learning, mastery, stats
from mysite.services import review as review_service

T = {
    "ky": {
        "start": "Салам! ORTO аккаунтуңузду байлоо үчүн сайттагы Жөндөөлөр → Telegram бөлүмүнөн шилтеме алыңыз.",
        "linked": "Аккаунт байланды ✅ Буйруктар: /today /review /stats /streak /weak /goal /language /help",
        "bad_token": "Шилтеме жараксыз же мөөнөтү өтүп кеткен. Сайттан жаңысын алыңыз.",
        "taken": "Бул Telegram башка аккаунтка байланган.",
        "not_linked": "Адегенде аккаунтту байлаңыз: сайт → Жөндөөлөр → Telegram.",
        "help": "/today — бүгүнкү план\n/review — каталарды кайталоо\n/stats — статистика\n/streak — окуу сериясы\n/weak — алсыз темалар\n/goal — максат\n/language — тил",
        "no_goal": "Максат коюла элек. Сайттан пробный тапшырып, максат коюңуз.",
        "today": "Бүгүнкү план ({done}/{total}):",
        "practice": "Практика · {title} · {minutes} мүн",
        "review": "Кайталоо · {minutes} мүн",
        "mock": "Пробный тест · {minutes} мүн",
        "due": "Кайталоого карточкалар: {n}",
        "stats": "Акыркы 30 күн:\nЧечилди: {solved}\nТактык: {accuracy}\nСерия: {streak} күн",
        "no_data": "азырынча маалымат жок",
        "streak": "Окуу сериясы: {streak} күн (эң узуну: {longest})",
        "weak": "Алсыз темалар:",
        "weak_none": "Азырынча маалымат жетишсиз. Бир аз практика кылыңыз.",
        "goal": "Максат: {target:g} · азыр ≈ {current} · {days} күн калды\nКийинки этап: {next}",
        "no_cards": "Бүгүн кайталай турган карточка жок 🎉",
        "correct": "✅ Туура! Кийинки кайталоо {days} күндөн кийин.",
        "mastered": "✅ Туура! Бул карточка өздөштүрүлдү 🎉",
        "wrong": "❌ Туура эмес. Туура жооп: {option}. Эртең кайра кайталайбыз.",
        "more": "Толук түшүндүрмө сайтта.",
        "stale": "Бул карточка азыр кайталоодо эмес.",
        "language": "Тил өзгөртүлдү: кыргызча",
    },
    "ru": {
        "start": "Привет! Чтобы привязать аккаунт ORTO, возьмите ссылку на сайте: Настройки → Telegram.",
        "linked": "Аккаунт привязан ✅ Команды: /today /review /stats /streak /weak /goal /language /help",
        "bad_token": "Ссылка недействительна или устарела. Получите новую на сайте.",
        "taken": "Этот Telegram уже привязан к другому аккаунту.",
        "not_linked": "Сначала привяжите аккаунт: сайт → Настройки → Telegram.",
        "help": "/today — план на сегодня\n/review — повторение ошибок\n/stats — статистика\n/streak — учебная серия\n/weak — слабые темы\n/goal — цель\n/language — язык",
        "no_goal": "Цель ещё не поставлена. Пройдите пробный тест на сайте и поставьте цель.",
        "today": "План на сегодня ({done}/{total}):",
        "practice": "Практика · {title} · {minutes} мин",
        "review": "Повторение · {minutes} мин",
        "mock": "Пробный тест · {minutes} мин",
        "due": "Карточек к повторению: {n}",
        "stats": "Последние 30 дней:\nРешено: {solved}\nТочность: {accuracy}\nСерия: {streak} дн.",
        "no_data": "пока нет данных",
        "streak": "Учебная серия: {streak} дн. (самая длинная: {longest})",
        "weak": "Слабые темы:",
        "weak_none": "Пока недостаточно данных. Немного попрактикуйтесь.",
        "goal": "Цель: {target:g} · сейчас ≈ {current} · осталось {days} дн.\nСледующий этап: {next}",
        "no_cards": "На сегодня карточек для повторения нет 🎉",
        "correct": "✅ Верно! Следующее повторение через {days} дн.",
        "mastered": "✅ Верно! Эта карточка освоена 🎉",
        "wrong": "❌ Неверно. Правильный ответ: {option}. Повторим завтра.",
        "more": "Полное объяснение — на сайте.",
        "stale": "Эта карточка сейчас не на повторении.",
        "language": "Язык изменён: русский",
    },
}


def t(user: User | None, key: str, **values) -> str:
    return T[user.locale if user else "ky"][key].format(**values)


# ---------- linking ----------


async def create_link_token(db: AsyncSession, user: User) -> LinkTokenOut:
    settings = get_settings()
    token = security.new_opaque_token()[:32]
    ttl = timedelta(minutes=settings.telegram_link_ttl_minutes)
    # Only the newest link works: asking for a new one cancels the old ones.
    await db.execute(delete(TelegramLinkToken).where(TelegramLinkToken.user_id == user.id))
    db.add(
        TelegramLinkToken(
            user_id=user.id, token_hash=security.hash_token(token), expires_at=datetime.now(UTC) + ttl
        )
    )
    await db.commit()
    username = settings.telegram_bot_username
    return LinkTokenOut(
        token=token,
        expires_in=int(ttl.total_seconds()),
        deep_link=f"https://t.me/{username}?start={token}" if username else None,
    )


async def link(db: AsyncSession, telegram_user_id: int, token: str) -> str:
    """Consumes a one-time token. Returns the reply text key: linked | bad_token | taken."""
    now = datetime.now(UTC)
    user_id = await db.scalar(
        update(TelegramLinkToken)
        .where(
            TelegramLinkToken.token_hash == security.hash_token(token),
            TelegramLinkToken.used_at.is_(None),
            TelegramLinkToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(TelegramLinkToken.user_id)
    )
    if user_id is None:
        await db.rollback()
        return "bad_token"
    owner = await db.scalar(
        select(TelegramAccount.user_id).where(TelegramAccount.telegram_user_id == telegram_user_id)
    )
    if owner is not None and owner != user_id:
        # The token stays spent: a guessed or stolen link cannot be retried from another chat.
        await db.commit()
        return "taken"
    await db.execute(delete(TelegramAccount).where(TelegramAccount.user_id == user_id))
    db.add(TelegramAccount(user_id=user_id, telegram_user_id=telegram_user_id))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return "taken"
    return "linked"


async def unlink(db: AsyncSession, user: User) -> None:
    await db.execute(delete(TelegramAccount).where(TelegramAccount.user_id == user.id))
    await db.commit()


async def user_for(db: AsyncSession, telegram_user_id: int) -> User | None:
    """The platform user behind a Telegram account, or None. Blocked users get nothing."""
    return await db.scalar(
        select(User)
        .join(TelegramAccount, TelegramAccount.user_id == User.id)
        .where(TelegramAccount.telegram_user_id == telegram_user_id, User.status == UserStatus.ACTIVE)
    )


# ---------- command replies ----------


def _title(obj, user: User, prefix: str = "title") -> str:
    return getattr(obj, f"{prefix}_{user.locale}") or ""


async def today_text(db: AsyncSession, user: User) -> str:
    try:
        plan = await goals.today(db, user)
    except AppError:
        due = await review_service.due_count(db, user.id)
        return t(user, "no_goal") + "\n" + t(user, "due", n=due)
    lines = [t(user, "today", done=plan.done, total=plan.total)]
    for item in plan.items:
        mark = {"done": "✅", "skipped": "➖"}.get(item.status, "▫️")
        title = item.skill_title_ky if user.locale == "ky" else item.skill_title_ru
        lines.append(f"{mark} " + t(user, item.kind, title=title, minutes=item.minutes))
    lines.append(t(user, "due", n=plan.due_cards))
    return "\n".join(lines)


async def stats_text(db: AsyncSession, user: User) -> str:
    data = await stats.overview(db, user, 30)
    accuracy = f"{round(data.accuracy * 100)}%" if data.accuracy is not None else t(user, "no_data")
    return t(user, "stats", solved=data.solved, accuracy=accuracy, streak=data.streak)


async def streak_text(db: AsyncSession, user: User) -> str:
    days = await learning.active_days(db, user)
    return t(
        user,
        "streak",
        streak=mastery.current_streak(days, learning.local_today(user)),
        longest=mastery.longest_streak(days),
    )


async def weak_text(db: AsyncSession, user: User) -> str:
    weakest = [s for s in await stats.skills(db, user) if not s.mastered][:3]
    if not weakest:
        return t(user, "weak_none")
    lines = [t(user, "weak")]
    lines += [f"• {_title(s, user)} — {round(s.accuracy * 100)}% ({s.answers})" for s in weakest]
    return "\n".join(lines)


async def goal_text(db: AsyncSession, user: User) -> str:
    try:
        progress = await goals.progress(db, user)
    except AppError:
        return t(user, "no_goal")
    current = f"{progress.current_score:g}" if progress.current_score is not None else "—"
    next_step = f"{progress.next_milestone:g}" if progress.next_milestone is not None else "✅"
    return t(
        user,
        "goal",
        target=progress.goal.target_score,
        current=current,
        days=progress.goal.days_left,
        next=next_step,
    )


async def set_language(db: AsyncSession, user: User, locale: str) -> str:
    user.locale = locale
    await db.commit()
    return t(user, "language")


# ---------- review in the chat ----------


@dataclass
class CardMessage:
    text: str
    buttons: list[tuple[str, str]]  # (label, callback data)


async def next_card(db: AsyncSession, user: User) -> CardMessage:
    queue = await review_service.queue(db, user, limit=1)
    if not queue.items:
        return CardMessage(t(user, "no_cards"), [])
    card = queue.items[0]
    options = "\n".join(f"{o.key}) {o.text}" for o in card.options)
    return CardMessage(
        f"{card.stem}\n\n{options}",
        # "r:<card id>:<option key>" - 37 bytes, within Telegram's 64-byte limit.
        [(o.key, f"r:{card.card_id.hex}:{o.key}") for o in card.options],
    )


async def answer_card(db: AsyncSession, user: User, callback_data: str) -> str:
    """Handles a pressed answer button. The card must belong to this user and be due now,
    so an old button or someone else's callback changes nothing."""
    try:
        _, card_hex, key = callback_data.split(":")
        card_id = uuid.UUID(hex=card_hex)
    except ValueError:
        return t(user, "stale")
    queue = await review_service.queue(db, user)
    card = next((c for c in queue.items if c.card_id == card_id), None)
    option = next((o for o in card.options if o.key == key), None) if card else None
    if option is None:
        return t(user, "stale")
    try:
        result = await review_service.answer(db, user, card_id, option.id, 0, None)
    except AppError:
        return t(user, "stale")
    if result.is_correct:
        if result.next_due_at is None:
            return t(user, "mastered")
        return t(user, "correct", days=result.interval_days)
    correct_key = next(o.key for o in card.options if o.id == result.correct_option_id)
    return t(user, "wrong", option=correct_key) + "\n" + t(user, "more")
