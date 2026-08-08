"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from exit_workflow import __version__
from exit_workflow.api.v1 import api_router
from exit_workflow.container import build_container
from exit_workflow.core import db as db_module
from exit_workflow.core.config import Settings, get_settings
from exit_workflow.core.errors import AppError, ConflictError
from exit_workflow.core.logging import configure_logging, get_logger, request_id_ctx
from exit_workflow.worker import BackgroundWorker

log = get_logger(__name__)

PROBLEM_JSON = "application/problem+json"


def _problem_response(problem: dict[str, Any], headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        problem, status_code=problem["status"], media_type=PROBLEM_JSON, headers=headers
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    if not hasattr(app.state, "container"):
        app.state.container = build_container(settings)
    if not hasattr(app.state, "session_factory"):
        db_module.configure(db_module.create_engine(settings))
        app.state.session_factory = db_module.get_session_factory()

    worker: BackgroundWorker | None = None
    if settings.background_worker_enabled:
        worker = BackgroundWorker(
            settings,
            app.state.session_factory,
            publisher=app.state.container.publisher,
            email_sender=app.state.container.email_sender,
        )
        await worker.start()
    app.state.worker = worker
    log.info(
        "application_started",
        environment=settings.environment,
        version=__version__,
        worker=bool(worker),
        kafka=settings.kafka_enabled,
    )
    try:
        yield
    finally:
        if worker is not None:
            await worker.stop()
        if getattr(app.state, "owns_engine", True):
            await db_module.dispose_engine()
        log.info("application_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(debug=settings.debug)

    app = FastAPI(
        title="Meridian — Tenant Exit Workflow",
        version=__version__,
        summary="Exit initiation, third-party inspection, deposit settlement and Exit NOC.",
        description=(
            "Implements SRS v1.2 T13 (10-step tenant exit), O15 (inspection workflow), "
            "O16 (deposit settlement and NOC issuance) and BR-1 (exit workflow contract "
            "lock)."
        ),
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )
    app.state.settings = settings

    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID", "Idempotent-Replay", "Location"],
        )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        log.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        return response

    # -- error handling ----------------------------------------------------
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            log.error("app_error", code=exc.code, detail=exc.detail, path=request.url.path)
        return _problem_response(exc.to_problem(str(request.url.path)), exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _problem_response(
            {
                "type": "https://errors.meridian.ae/exit-workflow/validation_failed",
                "title": "Request failed validation",
                "status": 422,
                "code": "validation_failed",
                "detail": "One or more fields are invalid.",
                "instance": str(request.url.path),
                "errors": [
                    {
                        "location": list(err.get("loc", [])),
                        "message": err.get("msg"),
                        "type": err.get("type"),
                    }
                    for err in exc.errors()
                ],
            }
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return _problem_response(
            {
                "type": "https://errors.meridian.ae/exit-workflow/http_error",
                "title": "Request could not be completed",
                "status": exc.status_code,
                "code": "http_error",
                "detail": str(exc.detail),
                "instance": str(request.url.path),
            },
            dict(exc.headers or {}),
        )

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        # Never leak SQL: a constraint that reaches here is a race the service
        # layer did not name explicitly.
        log.warning("integrity_error", path=request.url.path, error=str(exc.orig))
        problem = ConflictError(
            "The request conflicts with the current state of the resource."
        ).to_problem(str(request.url.path))
        return _problem_response(problem)

    @app.exception_handler(OperationalError)
    async def handle_operational_error(request: Request, exc: OperationalError) -> JSONResponse:
        log.error("database_unavailable", path=request.url.path, error=str(exc.orig))
        return _problem_response(
            {
                "type": "https://errors.meridian.ae/exit-workflow/database_unavailable",
                "title": "Service temporarily unavailable",
                "status": 503,
                "code": "database_unavailable",
                "detail": "The service is temporarily unable to process this request.",
                "instance": str(request.url.path),
            },
            {"Retry-After": "5"},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception", path=request.url.path, error=str(exc), exc_info=True)
        return _problem_response(
            {
                "type": "https://errors.meridian.ae/exit-workflow/internal_error",
                "title": "Internal server error",
                "status": 500,
                "code": "internal_error",
                "detail": "An unexpected error occurred.",
                "instance": str(request.url.path),
            }
        )

    # -- health ------------------------------------------------------------
    @app.get("/health", tags=["ops"], summary="Liveness")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/health/ready", tags=["ops"], summary="Readiness (checks the database)")
    async def ready(request: Request) -> JSONResponse:
        try:
            async with request.app.state.session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - readiness must not raise
            log.warning("readiness_failed", error=str(exc))
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready", "version": __version__})

    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
