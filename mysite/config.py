from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. Values come from environment variables or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str
    environment: str = "local"  # local | staging | production

    database_url: str = "postgresql+asyncpg://orto:orto_dev@localhost:5433/orto"
    redis_url: str | None = None  # None -> in-memory rate limiter (single process only)

    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    password_reset_ttl_minutes: int = 30

    # Versions of the legal documents a user accepts at registration.
    terms_version: str = "2026-11"
    privacy_version: str = "2026-11"

    login_max_attempts: int = 5
    login_window_seconds: int = 15 * 60
    register_max_per_hour: int = 10
    password_reset_max_per_hour: int = 5

    # Number of answer options a question must have to be published (A/B/C/D).
    question_option_count: int = 4
    # Upper bound for time a client may report for one question.
    max_question_time_sec: int = 3600

    # AI tutor. Without a key the tutor answers with the verified solution only.
    anthropic_api_key: str | None = None
    tutor_model: str = "claude-opus-5-5"
    tutor_daily_limit: int = 30
    tutor_timeout_sec: float = 20.0

    # Telegram bot (python -m mysite.bot). Username is used to build the linking deep link.
    telegram_bot_token: str | None = None
    telegram_bot_username: str | None = None
    telegram_link_ttl_minutes: int = 10

    # Email over SMTP. Without SMTP_HOST, mail is only logged (and only in the local environment).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = True
    mail_from: str = "ORTO <no-reply@orto.kg>"
    # Public address of the website, used in links inside emails.
    frontend_url: str = "http://localhost:3000"

    # Uploaded images (question pictures). Local directory; swap for object storage later.
    media_dir: str = "media"
    media_max_bytes: int = 2 * 1024 * 1024

    # Background worker (python -m mysite.worker).
    worker_interval_sec: int = 60

    cors_origins: list[str] = ["http://localhost:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
