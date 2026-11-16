"""Background worker. Run with:  python -m mysite.worker

Every WORKER_INTERVAL_SEC seconds it:
- finalizes test sessions whose time ran out (students who simply closed the tab);
- sends due Telegram reminders and weekly reports (if TELEGRAM_BOT_TOKEN is set).

Every step is idempotent, so a restart, a slow run or a second worker cannot double anything.
"""

import asyncio
import logging
import signal

from mysite.config import get_settings
from mysite.db.database import SessionLocal, engine
from mysite.services import notifications
from mysite.services.sessions import expire_overdue

logger = logging.getLogger("mysite.worker")


def telegram_sender(token: str) -> notifications.Sender:
    from aiogram import Bot
    from aiogram.exceptions import TelegramForbiddenError

    bot = Bot(token)

    async def send(chat_id: int, text: str) -> None:
        try:
            await bot.send_message(chat_id, text)
        except TelegramForbiddenError as exc:
            raise notifications.BotBlocked() from exc

    return send


async def tick(send: notifications.Sender | None) -> None:
    async with SessionLocal() as db:
        expired = await expire_overdue(db)
        if expired:
            logger.info("Finalized %d expired sessions", expired)
    if send is not None:
        async with SessionLocal() as db:
            counts = await notifications.run_once(db, send)
            if any(counts.values()):
                logger.info("Notifications: %s", counts)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    send = telegram_sender(settings.telegram_bot_token) if settings.telegram_bot_token else None
    if send is None:
        logger.warning("TELEGRAM_BOT_TOKEN is not set: reminders are off, only session expiry runs")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C still raises KeyboardInterrupt
            pass

    logger.info("Worker started, interval %ss", settings.worker_interval_sec)
    while not stop.is_set():
        try:
            await tick(send)
        except Exception:  # noqa: BLE001 - log and keep the worker alive
            logger.exception("Worker tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.worker_interval_sec)
        except TimeoutError:
            pass
    await engine.dispose()
    logger.info("Worker stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
