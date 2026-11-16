"""Single error format for the whole API: {"error": {code, message, details, request_id}}."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("mysite")


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


def unauthorized(code: str = "unauthorized", message: str = "Authentication required") -> AppError:
    return AppError(401, code, message, headers={"WWW-Authenticate": "Bearer"})


def forbidden(message: str = "Not enough permissions") -> AppError:
    return AppError(403, "forbidden", message)


def not_found(message: str = "Not found") -> AppError:
    return AppError(404, "not_found", message)


def conflict(code: str, message: str) -> AppError:
    return AppError(409, code, message)


def unprocessable(code: str, message: str, details: Any = None) -> AppError:
    return AppError(422, code, message, details=details)


def too_many_requests(retry_after: int) -> AppError:
    return AppError(
        429,
        "rate_limited",
        "Too many attempts, try again later",
        details={"retry_after": retry_after},
        headers={"Retry-After": str(retry_after)},
    )


def _body(request: Request, code: str, message: str, details: Any = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": getattr(request.state, "request_id", None),
        }
    }


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            _body(request, exc.code, exc.message, exc.details),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Only field location and message: never echo submitted values (may hold passwords).
        details = [
            {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(_body(request, "validation_error", "Invalid request", details), status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}
        return JSONResponse(
            _body(request, codes.get(exc.status_code, "http_error"), str(exc.detail)),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error", extra={"request_id": getattr(request.state, "request_id", None)})
        return JSONResponse(_body(request, "internal_error", "Internal server error"), status_code=500)
