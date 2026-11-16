# One image for the API, the worker and the Telegram bot; the command picks the role.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY mysite ./mysite

# Never run as root inside the container.
RUN useradd --create-home --uid 10001 orto && mkdir -p /app/media && chown orto /app/media
USER orto

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# --proxy-headers: behind the reverse proxy the real client IP is used for rate limits.
CMD ["uvicorn", "mysite.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers"]
