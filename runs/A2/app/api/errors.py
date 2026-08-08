"""Exception handlers producing a single, stable error envelope."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import (
    ConcurrentModificationError,
    ConflictError,
    ExitWorkflowError,
    RateLimitedError,
)
from app.core.logging import get_logger

log = get_logger("api.errors")


def _envelope(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload = {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": getattr(request.state, "request_id", None),
        }
    }
    return JSONResponse(status_code=status_code, content=payload, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ExitWorkflowError)
    async def _domain_error(request: Request, exc: ExitWorkflowError) -> JSONResponse:
        headers = None
        if isinstance(exc, RateLimitedError):
            headers = {"Retry-After": str(exc.retry_after_seconds)}
        if exc.status_code >= 500:
            log.error("domain.error", code=exc.code, message=exc.message)
        return _envelope(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _envelope(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="validation_failed",
            message="The request payload failed validation.",
            details={"errors": _clean_errors(exc.errors())},
        )

    @app.exception_handler(StaleDataError)
    async def _stale_data(request: Request, exc: StaleDataError) -> JSONResponse:
        # The optimistic version check lost a race; the client should re-read and retry.
        err = ConcurrentModificationError()
        return _envelope(
            request, status_code=err.status_code, code=err.code, message=err.message
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        log.warning("db.integrity_error", error=str(exc.orig)[:500])
        err = ConflictError(
            "The request conflicts with the current state of the resource.",
            code="constraint_violation",
        )
        return _envelope(
            request, status_code=err.status_code, code=err.code, message=err.message
        )

    @app.exception_handler(OperationalError)
    async def _operational_error(request: Request, exc: OperationalError) -> JSONResponse:
        log.error("db.operational_error", error=str(exc.orig)[:500])
        return _envelope(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="database_unavailable",
            message="The service is temporarily unable to reach its database.",
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {
            401: "unauthenticated",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            413: "payload_too_large",
            415: "unsupported_media_type",
        }
        return _envelope(
            request,
            status_code=exc.status_code,
            code=codes.get(exc.status_code, "http_error"),
            message=str(exc.detail),
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled.exception", path=request.url.path)
        # Never leak internals: the request id is the thread back to the log entry.
        return _envelope(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="An unexpected error occurred.",
        )


def _clean_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim pydantic errors to what a client can act on."""
    cleaned = []
    for error in errors[:20]:
        cleaned.append(
            {
                "field": ".".join(str(p) for p in error.get("loc", ())[1:]) or None,
                "message": error.get("msg"),
                "type": error.get("type"),
            }
        )
    return cleaned
