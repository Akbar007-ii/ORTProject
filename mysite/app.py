import uuid

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from mysite.admin import catalog as admin_catalog
from mysite.admin import content as admin_content
from mysite.admin import exams as admin_exams
from mysite.admin import questions as admin_questions
from mysite.admin import users as admin_users
from mysite.api import account, auth, catalog, exams, extras, learning, me
from mysite.config import get_settings
from mysite.core.deps import DB
from mysite.core.errors import register_error_handlers


def create_app() -> FastAPI:
    settings = get_settings()
    is_prod = settings.environment == "production"
    if is_prod and (len(settings.secret_key) < 32 or "localhost" in settings.frontend_url):
        # Refuse to start with a weak signing key or a development configuration.
        raise RuntimeError("Production needs SECRET_KEY of 32+ characters and a real FRONTEND_URL")
    app = FastAPI(
        title="ORTO API",
        version="0.1.0",
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
    )

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        # Every response and error body carries the id, so a user report can be matched to logs.
        incoming = request.headers.get("x-request-id", "")
        request.state.request_id = incoming[:64] if incoming.isascii() and incoming else str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        # A JSON API never needs to be framed, sniffed or to run scripts.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith(("/docs", "/openapi")):
            response.headers.setdefault(
                "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
            )
        if is_prod:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    register_error_handlers(app)

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(auth.router)
    v1.include_router(me.router)
    v1.include_router(catalog.router)
    v1.include_router(exams.router)
    v1.include_router(learning.router)
    v1.include_router(extras.router)
    v1.include_router(account.router)

    admin = APIRouter(prefix="/admin")
    admin.include_router(admin_catalog.router)
    admin.include_router(admin_users.router)
    admin.include_router(admin_questions.router)
    admin.include_router(admin_questions.reports_router)
    admin.include_router(admin_exams.router)
    admin.include_router(admin_content.router)
    v1.include_router(admin)
    app.include_router(v1)

    @app.get("/health", tags=["ops"])
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"])
    async def ready(db: DB) -> dict:
        await db.execute(text("SELECT 1"))
        return {"status": "ready"}

    return app


app = create_app()
