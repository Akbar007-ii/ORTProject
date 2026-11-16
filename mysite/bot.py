"""Telegram bot. Run with:  python -m mysite.bot   (needs TELEGRAM_BOT_TOKEN in .env)

This file is only the wiring to Telegram. What the bot says and does is in
mysite/services/telegram.py, on top of the same services the website uses.
Long polling is used here because it needs no public address; switch to a webhook
with a secret token when the API is deployed behind HTTPS.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from mysite.config import get_settings
from mysite.db.database import SessionLocal
from mysite.services import telegram as tg

logger = logging.getLogger("mysite.bot")
dp = Dispatcher()


def _keyboard(buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=data) for label, data in buttons]]
    )


@dp.message(CommandStart())
async def start(message: Message, command: CommandObject) -> None:
    async with SessionLocal() as db:
        if command.args:
            outcome = await tg.link(db, message.from_user.id, command.args.strip())
            user = await tg.user_for(db, message.from_user.id)
            await message.answer(tg.t(user, outcome))
        else:
            user = await tg.user_for(db, message.from_user.id)
            await message.answer(tg.t(user, "linked" if user else "start"))


async def _reply(message: Message, build) -> None:
    """Runs a command for a linked user, or explains how to link."""
    async with SessionLocal() as db:
        user = await tg.user_for(db, message.from_user.id)
        if user is None:
            await message.answer(tg.t(None, "not_linked"))
            return
        await message.answer(await build(db, user))


@dp.message(Command("today"))
async def today(message: Message) -> None:
    await _reply(message, tg.today_text)


@dp.message(Command("stats"))
async def stats(message: Message) -> None:
    await _reply(message, tg.stats_text)


@dp.message(Command("streak"))
async def streak(message: Message) -> None:
    await _reply(message, tg.streak_text)


@dp.message(Command("weak"))
async def weak(message: Message) -> None:
    await _reply(message, tg.weak_text)


@dp.message(Command("goal"))
async def goal(message: Message) -> None:
    await _reply(message, tg.goal_text)


@dp.message(Command("help"))
async def help_(message: Message) -> None:
    async with SessionLocal() as db:
        await message.answer(tg.t(await tg.user_for(db, message.from_user.id), "help"))


@dp.message(Command("language"))
async def language(message: Message) -> None:
    await message.answer(
        "Тил / Язык:", reply_markup=_keyboard([("Кыргызча", "lang:ky"), ("Русский", "lang:ru")])
    )


@dp.callback_query(F.data.in_({"lang:ky", "lang:ru"}))
async def language_chosen(callback: CallbackQuery) -> None:
    async with SessionLocal() as db:
        user = await tg.user_for(db, callback.from_user.id)
        text = await tg.set_language(db, user, callback.data[-2:]) if user else tg.t(None, "not_linked")
    await callback.answer()
    await callback.message.answer(text)


async def _send_card(message: Message, telegram_user_id: int) -> None:
    async with SessionLocal() as db:
        user = await tg.user_for(db, telegram_user_id)
        if user is None:
            await message.answer(tg.t(None, "not_linked"))
            return
        card = await tg.next_card(db, user)
    await message.answer(card.text, reply_markup=_keyboard(card.buttons))


@dp.message(Command("review"))
async def review(message: Message) -> None:
    await _send_card(message, message.from_user.id)


@dp.callback_query(F.data.startswith("r:"))
async def review_answer(callback: CallbackQuery) -> None:
    async with SessionLocal() as db:
        user = await tg.user_for(db, callback.from_user.id)
        text = await tg.answer_card(db, user, callback.data) if user else tg.t(None, "not_linked")
    await callback.answer()
    # Remove the buttons so the same card cannot be answered twice from the chat.
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(text)
    if user:
        await _send_card(callback.message, callback.from_user.id)


async def main() -> None:
    token = get_settings().telegram_bot_token
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (get it from @BotFather)")
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(Bot(token))


if __name__ == "__main__":
    asyncio.run(main())
