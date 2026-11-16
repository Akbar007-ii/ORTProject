"""Outgoing email over SMTP (any provider: Gmail Workspace, Mailgun, SES SMTP, Yandex...).

Without SMTP_HOST nothing is sent. Locally the message is written to the log so the flows can
be tried by hand; in other environments it is dropped with a warning and never logged, because
it may contain a one-time token.
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from mysite.config import get_settings

logger = logging.getLogger("mysite.mailer")

TEXTS = {
    "reset": {
        "ky": (
            "ORTO: сырсөздү калыбына келтирүү",
            "Сырсөздү алмаштыруу үчүн шилтемени ачыңыз (30 мүнөт жарактуу):\n{link}\n\n"
            "Эгер сиз сураган эмес болсоңуз, бул катты этибарга албаңыз.",
        ),
        "ru": (
            "ORTO: восстановление пароля",
            "Чтобы сменить пароль, откройте ссылку (действует 30 минут):\n{link}\n\n"
            "Если вы не запрашивали смену пароля, просто проигнорируйте это письмо.",
        ),
    },
    "deleted": {
        "ky": ("ORTO: аккаунт өчүрүлдү", "Сиздин ORTO аккаунтуңуз жана жеке маалыматтарыңыз өчүрүлдү."),
        "ru": ("ORTO: аккаунт удалён", "Ваш аккаунт ORTO и личные данные удалены."),
    },
}


def _send_sync(message: EmailMessage) -> None:
    settings = get_settings()
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password or "")
        smtp.send_message(message)


async def send(to: str, subject: str, body: str) -> bool:
    """Returns True when the message was handed to the SMTP server. Never raises:
    a mail outage must not break registration or password reset."""
    settings = get_settings()
    if not settings.smtp_host:
        if settings.environment == "local":
            logger.info("Email (not sent, no SMTP_HOST) to %s: %s\n%s", to, subject, body)
        else:
            logger.warning("SMTP is not configured: email '%s' was not sent", subject)
        return False
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        await asyncio.to_thread(_send_sync, message)
    except (OSError, smtplib.SMTPException):
        logger.exception("Sending email '%s' failed", subject)
        return False
    return True


def _text(kind: str, locale: str) -> tuple[str, str]:
    return TEXTS[kind].get(locale, TEXTS[kind]["ru"])


async def send_password_reset(email: str, token: str, locale: str) -> None:
    subject, body = _text("reset", locale)
    link = f"{get_settings().frontend_url.rstrip('/')}/reset-password?token={token}"
    await send(email, subject, body.format(link=link))


async def send_account_deleted(email: str, locale: str) -> None:
    subject, body = _text("deleted", locale)
    await send(email, subject, body)
